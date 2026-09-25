"""
Self-contained broker for the MCP server deployment.

Includes all database operations, vector search, LLM calls, and OpenAlex
API interactions in a single file so the mcp_server/ folder can be deployed
as a standalone Databricks App without importing from parent-level modules.

All public functions return dicts with a consistent structure:
    Success: {"status": "success", "data": {...}, "message": "..."}
    Error:   {"status": "error",   "message": "...", "details": "..."}
"""

import base64
import logging
import os
from contextlib import contextmanager

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

try:
    from databricks.sdk import WorkspaceClient
    _w = WorkspaceClient()
except Exception:
    _w = None

logger = logging.getLogger("research-broker")

_SCOPE = os.environ.get("RESEARCH_SECRET_SCOPE", "research_copilot")
_LAKEBASE_KEY = os.environ.get("LAKEBASE_SECRET_URL", "lakebase-url")
EMBEDDING_MODEL_NAME = os.environ.get(
    "EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2"
)
LLM_MODEL = os.environ.get(
    "LLM_MODEL", "databricks-meta-llama-3-3-70b-instruct"
)

_embedding_model = None


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _lakebase_url() -> str:
    if _w is None:
        raise RuntimeError("Databricks SDK not available")
    secret = _w.secrets.get_secret(scope=_SCOPE, key=_LAKEBASE_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    conn = psycopg2.connect(_lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def run_query(sql: str, params=None) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def run_query_one(sql: str, params=None) -> dict | None:
    rows = run_query(sql, params)
    return rows[0] if rows else None


def run_write(sql: str, params=None) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount


# ---------------------------------------------------------------------------
# Embedding + vector search
# ---------------------------------------------------------------------------

def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer(
            EMBEDDING_MODEL_NAME, cache_folder="/tmp/.cache/huggingface"
        )
    return _embedding_model


def vector_search(
    query: str, top_k: int = 10, source_type: str | None = None
) -> list[dict]:
    """Cosine-similarity search over pgvector embeddings."""
    model = _get_embedding_model()
    vec = model.encode([query])[0].tolist()
    embedding_str = "[" + ",".join(str(float(x)) for x in vec) + "]"

    sql = (
        "SELECT e.source_id, e.source_type, e.chunk_text, "
        "1 - (e.embedding <=> %s::vector) AS similarity "
        "FROM embeddings e"
    )
    params: list = [embedding_str]
    if source_type:
        sql += " WHERE e.source_type = %s"
        params.append(source_type)
    sql += " ORDER BY e.embedding <=> %s::vector LIMIT %s"
    params.extend([embedding_str, top_k])

    return run_query(sql, tuple(params))


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def call_llm(prompt: str, max_tokens: int = 800) -> str:
    w = WorkspaceClient()
    resp = w.api_client.do(
        "POST",
        f"/serving-endpoints/{LLM_MODEL}/invocations",
        body={
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        },
    )
    return resp["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# OpenAlex search
# ---------------------------------------------------------------------------

def _get_openalex_secret(key_env: str, default_key: str) -> str | None:
    try:
        if _w:
            s = _w.secrets.get_secret(
                scope=_SCOPE,
                key=os.environ.get(key_env, default_key),
            )
            return base64.b64decode(s.value).decode("utf-8")
    except Exception:
        pass
    return None


def openalex_search(query: str, limit: int = 15) -> list[dict]:
    """Search OpenAlex for papers and return normalized results."""
    api_key = _get_openalex_secret("OPENALEX_API_KEY_SECRET", "openalex-api-key")
    email = _get_openalex_secret("OPENALEX_EMAIL_SECRET", "openalex-email")

    params = {
        "search": query,
        "per_page": min(limit, 200),
        "filter": "has_abstract:true,type:article|review|preprint",
        "sort": "relevance_score:desc",
    }
    if api_key:
        params["api_key"] = api_key
    if email:
        params["mailto"] = email

    resp = requests.get(
        "https://api.openalex.org/works", params=params, timeout=30
    )
    resp.raise_for_status()

    results = []
    for work in resp.json().get("results", []):
        oid = work.get("id", "")
        pid = oid.split("/")[-1] if "/" in oid else oid
        # Reconstruct abstract from inverted index
        inv = work.get("abstract_inverted_index") or {}
        words: list[tuple[int, str]] = []
        for word, positions in inv.items():
            for pos in positions:
                words.append((pos, word))
        words.sort()
        abstract = " ".join(w for _, w in words)

        authors = [
            a.get("author", {}).get("display_name", "Unknown")
            for a in work.get("authorships", [])[:5]
        ]
        results.append({
            "paper_id": pid,
            "title": work.get("title", "Untitled"),
            "abstract": abstract[:500],
            "authors": authors,
            "publication_date": work.get("publication_date"),
            "cited_by_count": work.get("cited_by_count", 0),
            "doi": work.get("doi", ""),
        })
    return results


# ---------------------------------------------------------------------------
# Paper / collection / progress DB operations
# ---------------------------------------------------------------------------

def get_paper(paper_id: str) -> dict | None:
    return run_query_one(
        "SELECT * FROM papers WHERE paper_id = %s", (paper_id,)
    )


def search_papers_in_db(query: str, limit: int = 10) -> list[dict]:
    """Semantic search over indexed papers."""
    hits = vector_search(query, top_k=limit, source_type="abstract")
    pids = list({h["source_id"] for h in hits})
    if not pids:
        return []
    ph = ",".join(["%s"] * len(pids))
    return run_query(
        f"SELECT paper_id, title, abstract, cited_by_count, "
        f"publication_date, source_name "
        f"FROM papers WHERE paper_id IN ({ph}) "
        f"ORDER BY cited_by_count DESC",
        tuple(pids),
    )


def add_paper_to_collection(collection_id: int, paper_id: str) -> dict:
    run_write(
        "INSERT INTO collection_papers (collection_id, paper_id) "
        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (collection_id, paper_id),
    )
    return {
        "status": "success",
        "message": f"Paper {paper_id} added to collection {collection_id}.",
    }


def update_reading_progress(
    user_id: int, paper_id: str, status: str
) -> dict:
    run_write(
        """
        INSERT INTO reading_progress (user_id, paper_id, status,
                started_at, completed_at)
        VALUES (%s, %s, %s,
                CASE WHEN %s IN ('reading','completed') THEN now()
                     ELSE NULL END,
                CASE WHEN %s = 'completed' THEN now() ELSE NULL END)
        ON CONFLICT (user_id, paper_id) DO UPDATE
            SET status       = EXCLUDED.status,
                started_at   = COALESCE(
                    reading_progress.started_at, EXCLUDED.started_at),
                completed_at = EXCLUDED.completed_at
        """,
        (user_id, paper_id, status, status, status),
    )
    return {
        "status": "success",
        "message": f"Reading progress for {paper_id} set to '{status}'.",
    }


def get_reading_progress(user_id: int) -> list[dict]:
    return run_query(
        "SELECT rp.*, p.title FROM reading_progress rp "
        "JOIN papers p ON rp.paper_id = p.paper_id "
        "WHERE rp.user_id = %s",
        (user_id,),
    )
