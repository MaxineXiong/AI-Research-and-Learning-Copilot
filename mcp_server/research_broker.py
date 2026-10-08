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
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from functools import wraps

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

try:
    from databricks.sdk import WorkspaceClient
    _w = WorkspaceClient()
except Exception:
    _w = None

logger = logging.getLogger("research-broker")


# ---------------------------------------------------------------------------
# Structured logging helper
# ---------------------------------------------------------------------------

def _log_broker_call(func_name: str):
    """Decorator that logs broker function entry/exit with duration."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            logger.info("broker_call start func=%s", func_name)
            t0 = time.monotonic()
            result = func(*args, **kwargs)
            duration_ms = int((time.monotonic() - t0) * 1000)
            extra = ""
            if isinstance(result, list):
                extra = f" results={len(result)}"
            elif isinstance(result, dict) and "status" in result:
                extra = f" status={result['status']}"
            logger.info("broker_call end func=%s duration_ms=%d%s",
                        func_name, duration_ms, extra)
            return result
        return wrapper
    return decorator

_SCOPE = os.environ.get("RESEARCH_SECRET_SCOPE", "research_copilot")
_LAKEBASE_KEY = os.environ.get("LAKEBASE_SECRET_URL", "lakebase-url")
EMBEDDING_MODEL_NAME = os.environ.get(
    "EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2"
)
LLM_MODEL = os.environ.get(
    "LLM_MODEL", "databricks-meta-llama-3-3-70b-instruct"
)

_embedding_model = None

# OpenAlex paper IDs: W followed by 5+ digits (e.g., W2741809807)
_OPENALEX_ID_RE = re.compile(r'^W\d{5,}$')


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


def run_write_returning(sql: str, params=None) -> dict | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None


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


def _openalex_get(url: str, params: dict, timeout: int = 30) -> requests.Response:
    """GET from OpenAlex with retry, exponential backoff, and Retry-After support.

    This function mirrors OpenAlexClient._get() in openalex_client.py to
    ensure identical retry behavior (3 retries, exponential backoff, Retry-After
    header support) across all code paths.  The broker is self-contained by
    design (deployed as a standalone Databricks App) and cannot import
    OpenAlexClient, so the retry logic is intentionally duplicated here.

    Returns the raw Response.  The caller is responsible for checking
    status codes (e.g. 404) and calling raise_for_status / json().
    """
    import time

    max_retries = 3
    base_delay = 1.0  # seconds

    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
        except requests.Timeout:
            if attempt == max_retries:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "OpenAlex request timed out (attempt %d/%d), retrying in %.1fs",
                attempt + 1, max_retries, delay,
            )
            time.sleep(delay)
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_retries:
                resp.raise_for_status()
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = min(float(retry_after), 60.0)
            else:
                delay = base_delay * (2 ** attempt)
            logger.warning(
                "OpenAlex returned %d (attempt %d/%d), retrying in %.1fs",
                resp.status_code, attempt + 1, max_retries, delay,
            )
            time.sleep(delay)
            continue

        return resp  # caller handles status codes

    resp.raise_for_status()
    return resp


@_log_broker_call("openalex_search")
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

    resp = _openalex_get("https://api.openalex.org/works", params)
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
        # Extract source and OpenAlex metadata
        primary_loc = work.get("primary_location") or {}
        results.append({
            "paper_id": pid,
            "title": work.get("title", "Untitled"),
            "abstract": abstract[:500],
            "authors": authors,
            "publication_date": work.get("publication_date"),
            "cited_by_count": work.get("cited_by_count", 0),
            "doi": work.get("doi", ""),
            "relevance_score": work.get("relevance_score", 0),
            "source_name": (primary_loc.get("source") or {}).get("display_name"),
            "pdf_url": primary_loc.get("pdf_url"),
            "openalex_url": work.get("id", ""),
            "concepts": [c.get("display_name", "") for c in work.get("concepts", [])[:10]],
        })
    return results


def openalex_fetch_by_id(paper_id: str) -> dict | None:
    """Fetch a single paper from OpenAlex by its paper_id (e.g., 'W2741809807').

    Returns a normalized dict matching the shape of openalex_search results,
    or None if the paper is not found on OpenAlex.
    """
    api_key = _get_openalex_secret("OPENALEX_API_KEY_SECRET", "openalex-api-key")
    email = _get_openalex_secret("OPENALEX_EMAIL_SECRET", "openalex-email")

    url = f"https://api.openalex.org/works/{paper_id}"
    params = {}
    if api_key:
        params["api_key"] = api_key
    if email:
        params["mailto"] = email

    resp = _openalex_get(url, params)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    work = resp.json()
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
    primary_loc = work.get("primary_location") or {}
    return {
        "paper_id": pid,
        "title": work.get("title", "Untitled"),
        "abstract": abstract[:500],
        "authors": authors,
        "publication_date": work.get("publication_date"),
        "cited_by_count": work.get("cited_by_count", 0),
        "doi": work.get("doi", ""),
        "relevance_score": work.get("relevance_score", 0),
        "source_name": (primary_loc.get("source") or {}).get("display_name"),
        "pdf_url": primary_loc.get("pdf_url"),
        "openalex_url": work.get("id", ""),
        "concepts": [c.get("display_name", "") for c in work.get("concepts", [])[:10]],
    }


# ---------------------------------------------------------------------------
# Paper / collection / progress DB operations
# ---------------------------------------------------------------------------

def get_paper(paper_id: str) -> dict | None:
    return run_query_one(
        "SELECT * FROM papers WHERE paper_id = %s", (paper_id,)
    )


@_log_broker_call("upsert_paper")
def upsert_paper(paper: dict) -> None:
    """Upsert a paper into the papers table (insert or update on conflict).

    Matches the pipeline's ON CONFLICT behavior: on update, only refreshes
    title, abstract, cited_by_count, pdf_url, and concepts. Immutable fields
    (publication_date, doi, source_name, openalex_url) are set on insert only.
    """
    run_write(
        """
        INSERT INTO papers (paper_id, title, abstract, publication_date,
                           doi, cited_by_count, source_name, pdf_url,
                           openalex_url, concepts)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (paper_id) DO UPDATE
            SET title          = EXCLUDED.title,
                abstract       = EXCLUDED.abstract,
                cited_by_count = EXCLUDED.cited_by_count,
                pdf_url        = EXCLUDED.pdf_url,
                concepts       = EXCLUDED.concepts
        """,
        (paper["paper_id"], paper["title"], paper.get("abstract", ""),
         paper.get("publication_date"), paper.get("doi"),
         paper.get("cited_by_count", 0), paper.get("source_name", "OpenAlex"),
         paper.get("pdf_url"), paper.get("openalex_url", f"https://openalex.org/{paper['paper_id']}"),
         json.dumps(paper.get("concepts", []))),
    )


def search_papers_in_db(query: str, limit: int = 10, user_id: int = None) -> list[dict]:
    """Semantic search over indexed papers, ordered by relevance.

    If user_id is provided and the query matches an existing learning goal
    title, uses the goal's stored embedding vector from the embeddings table
    instead of re-encoding the query string.
    """
    # Check if the query matches an existing learning goal (use stored embedding)
    goal_id = None
    if user_id is not None:
        goal = run_query_one(
            "SELECT goal_id FROM learning_goals WHERE title = %s AND user_id = %s",
            (query, user_id),
        )
        if goal:
            goal_id = str(goal["goal_id"])

    if goal_id is not None:
        # Use the goal's pre-computed embedding for similarity search (in-DB)
        hits = run_query(
            "SELECT e.source_id, e.source_type, e.chunk_text, "
            "1 - (e.embedding <=> goal.embedding) AS similarity "
            "FROM embeddings e "
            "CROSS JOIN LATERAL ("
            "    SELECT embedding FROM embeddings "
            "    WHERE source_type = 'goal' AND source_id = %s LIMIT 1"
            ") goal "
            "WHERE e.source_type = 'abstract' "
            "ORDER BY e.embedding <=> goal.embedding LIMIT %s",
            (goal_id, limit),
        )
    else:
        hits = vector_search(query, top_k=limit, source_type="abstract")

    pids = list({h["source_id"] for h in hits})
    if not pids:
        return []
    # Preserve similarity scores for sorting (vector_search already ranks
    # by cosine similarity, but the DB query loses that ordering)
    sim_map = {h["source_id"]: h["similarity"] for h in hits}
    ph = ",".join(["%s"] * len(pids))
    papers = run_query(
        f"SELECT paper_id, title, abstract, cited_by_count, "
        f"publication_date, source_name "
        f"FROM papers WHERE paper_id IN ({ph})",
        tuple(pids),
    )
    for p in papers:
        p["similarity"] = sim_map.get(p["paper_id"], 0)
    papers.sort(key=lambda p: p["similarity"], reverse=True)
    return papers



def find_or_fetch_paper(
    paper_input: str, user_id: int = 1, similarity_threshold: float = 0.7
) -> dict | None:
    """Resolve a paper input (OpenAlex ID or title/topic) to a paper dict.

    Resolution logic:
    - If the input matches an OpenAlex paper ID pattern (e.g., "W2741809807"),
      looks it up directly in the knowledge base.  If not found locally, fetches
      from the OpenAlex API and upserts it.
    - If the input is a title or topic, performs a semantic search.  Uses the
      top match if its similarity meets the threshold; otherwise discovers
      new papers from OpenAlex.

    Args:
        paper_input: An OpenAlex paper ID or a paper title/topic string.
        user_id:     The user's numeric ID (for semantic search resolution).
        similarity_threshold: Minimum cosine similarity to accept a semantic
                              match from the knowledge base (default 0.7).

    Returns:
        A paper dict if resolved successfully, or None.
    """
    if _OPENALEX_ID_RE.match(paper_input):
        p = get_paper(paper_input)
        if p:
            return p
        # Paper ID not in DB -- fetch from OpenAlex and upsert
        logger.info(f"Paper ID '{paper_input}' not in DB, fetching from OpenAlex...")
        oa_paper = openalex_fetch_by_id(paper_input)
        if oa_paper:
            upsert_paper(oa_paper)
            p = get_paper(paper_input)
            if p:
                logger.info(f"Fetched and upserted '{p['title']}' from OpenAlex")
                return p
            logger.warning(f"Upsert succeeded but paper '{paper_input}' still not found in DB")
        else:
            logger.warning(f"Paper ID '{paper_input}' not found on OpenAlex either")
        return None
    else:
        # Input is a topic or title -- search for the most relevant paper
        logger.info(f"Input '{paper_input}' is not a paper ID, searching for matching paper...")
        results = search_papers_in_db(paper_input, limit=1, user_id=user_id)
        if results and results[0].get("similarity", 0) >= similarity_threshold:
            logger.info(
                f"Resolved '{paper_input}' to '{results[0]['title']}' "
                f"(similarity: {results[0]['similarity']:.3f})"
            )
            return results[0]
        # No good match in DB -- discover from OpenAlex
        sim = results[0].get("similarity", 0) if results else 0
        logger.info(f"Low similarity ({sim:.3f}) for '{paper_input}', discovering from OpenAlex...")
        oa_results = openalex_search(paper_input, limit=1)
        if oa_results:
            upsert_paper(oa_results[0])
            p = get_paper(oa_results[0]["paper_id"])
            if p:
                logger.info(f"Discovered and upserted '{p['title']}' from OpenAlex for '{paper_input}'")
                return p
            logger.warning(f"OpenAlex discovery succeeded but paper still not in DB")
        else:
            logger.warning(f"No paper found on OpenAlex for '{paper_input}'")
        return None


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


def create_collection(name: str, description: str = "", user_id: int = 1) -> dict:
    """Create a new collection for a user."""
    result = run_write_returning(
        "INSERT INTO collections (user_id, name, description) "
        "VALUES (%s, %s, %s) RETURNING collection_id, name",
        (user_id, name, description),
    )
    if not result:
        return {"status": "error", "message": "Failed to create collection."}
    return {
        "status": "success",
        "message": f"Collection '{name}' created.",
        "data": {"collection_id": result["collection_id"], "name": name},
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


def seed_reading_progress(user_id: int, paper_id: str) -> None:
    """Insert a 'not_started' reading-progress record if none exists yet.

    Uses ON CONFLICT DO NOTHING so an existing record (e.g., 'reading' or
    'completed') is never downgraded.
    """
    run_write(
        "INSERT INTO reading_progress (user_id, paper_id, status) "
        "VALUES (%s, %s, 'not_started') "
        "ON CONFLICT (user_id, paper_id) DO NOTHING",
        (user_id, paper_id),
    )


def get_reading_progress(user_id: int) -> list[dict]:
    return run_query(
        "SELECT rp.*, p.title FROM reading_progress rp "
        "JOIN papers p ON rp.paper_id = p.paper_id "
        "WHERE rp.user_id = %s",
        (user_id,),
    )


# ---------------------------------------------------------------------------
# User verification + learning goals
# ---------------------------------------------------------------------------

def get_user(user_id: int = None, username: str = None) -> dict | None:
    """Check if a user exists by user_id or username (email/display_name)."""
    if user_id is not None:
        return run_query_one(
            "SELECT user_id, email, display_name FROM users WHERE user_id = %s",
            (user_id,),
        )
    if username is not None:
        return run_query_one(
            "SELECT user_id, email, display_name FROM users "
            "WHERE email ILIKE %s OR display_name ILIKE %s",
            (username, username),
        )
    return None


def create_learning_goal(title: str, description: str = "", user_id: int = 1) -> dict:
    """Insert a new learning goal and embed it for semantic search."""
    import hashlib

    goal = run_write_returning(
        "INSERT INTO learning_goals (user_id, title, description) "
        "VALUES (%s, %s, %s) RETURNING goal_id, title",
        (user_id, title, description),
    )
    if not goal:
        return {"status": "error", "message": "Failed to insert learning goal."}

    goal_id = str(goal["goal_id"])

    # Embed the goal title + description (matching pipeline format)
    model = _get_embedding_model()
    text = f"{title}\n\n{description}" if description else title
    vec = model.encode([text])[0].tolist()
    embedding_str = "[" + ",".join(str(float(x)) for x in vec) + "]"

    # Generate unique embedding ID (MD5 hash of source_type:source_id)
    emb_id = hashlib.md5(f"goal:{goal_id}".encode()).hexdigest()

    run_write(
        "INSERT INTO embeddings (id, source_type, source_id, chunk_index, "
        "chunk_text, embedding, model_name) "
        "VALUES (%s, %s, %s, %s, %s, %s::vector, %s)",
        (emb_id, "goal", goal_id, 0, text, embedding_str, EMBEDDING_MODEL_NAME),
    )

    return {
        "status": "success",
        "message": f"Learning goal '{title}' added and embedded.",
        "data": {"goal_id": goal["goal_id"], "title": title},
    }


def get_learning_goals(user_id: int) -> list[dict]:
    """Retrieve all learning goals for a user."""
    return run_query(
        "SELECT goal_id, title, description, status, created_at "
        "FROM learning_goals WHERE user_id = %s ORDER BY created_at DESC",
        (user_id,),
    )


def get_collections(user_id: int) -> list[dict]:
    """Retrieve all collections for a user."""
    return run_query(
        "SELECT collection_id, name, description, created_at "
        "FROM collections WHERE user_id = %s ORDER BY created_at DESC",
        (user_id,),
    )


def get_collection_by_name(name: str, user_id: int) -> dict | None:
    """Look up a collection by name for a specific user."""
    return run_query_one(
        "SELECT collection_id, name, description FROM collections "
        "WHERE name = %s AND user_id = %s",
        (name, user_id),
    )


def get_collection_papers(collection_id: int) -> list[dict]:
    """Retrieve all papers in a collection."""
    return run_query(
        "SELECT cp.paper_id, p.title, p.abstract, p.cited_by_count, "
        "p.publication_date, cp.added_at "
        "FROM collection_papers cp "
        "JOIN papers p ON cp.paper_id = p.paper_id "
        "WHERE cp.collection_id = %s ORDER BY cp.added_at DESC",
        (collection_id,),
    )


def get_collections_for_paper(paper_id: str, user_id: int) -> list[dict]:
    """Find all collections containing a given paper for a specific user.

    Ordered by most recently added (cp.added_at DESC) so the caller can
    use collections[0] as the most recent collection.
    """
    return run_query(
        """SELECT c.collection_id, c.name, c.description, cp.added_at
        FROM collection_papers cp
        JOIN collections c ON cp.collection_id = c.collection_id
        WHERE cp.paper_id = %s AND c.user_id = %s
        ORDER BY cp.added_at DESC""",
        (paper_id, user_id),
    )
