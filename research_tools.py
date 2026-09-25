"""
Research Tools — shared tool implementations for the AI Research Copilot.

Provides all 7 agent capabilities (matching the MCP server tools) plus a
general RAG fallback, and an LLM-based tool dispatcher that the Flask
frontend calls from /api/agent/chat.

Uses lakebase.py for database access and openalex_client.py for external
API calls — no duplication of connection logic.
"""

import json
import logging
import os

import lakebase
from openalex_client import OpenAlexClient

logger = logging.getLogger("research-tools")

DEFAULT_USER_ID = 1
EMBEDDING_MODEL_NAME = os.environ.get(
    "EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2"
)
LLM_MODEL = os.environ.get("LLM_MODEL", "databricks-meta-llama-3-3-70b-instruct")

_embedding_model = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer

        _embedding_model = SentenceTransformer(
            EMBEDDING_MODEL_NAME, cache_folder="/tmp/.cache/huggingface"
        )
    return _embedding_model


def vector_search(query: str, top_k: int = 10, source_type: str | None = None):
    """Cosine-similarity search over pgvector embeddings."""
    model = _get_embedding_model()
    query_vec = model.encode([query])[0].tolist()
    embedding_str = "[" + ",".join(str(float(x)) for x in query_vec) + "]"

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

    return lakebase.run_query(sql, tuple(params))


def call_llm(prompt: str, max_tokens: int = 800) -> str:
    """Call a Databricks Foundation Model for text generation."""
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    response = w.api_client.do(
        "POST",
        f"/serving-endpoints/{LLM_MODEL}/invocations",
        body={
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        },
    )
    return response["choices"][0]["message"]["content"]


def upsert_paper(paper: dict):
    """Upsert a normalized OpenAlex paper (with authors) into Lakebase."""
    lakebase.run_write(
        """
        INSERT INTO papers (paper_id, title, abstract, publication_date, doi,
                           cited_by_count, source_name, pdf_url, openalex_url, concepts)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (paper_id) DO UPDATE SET cited_by_count = EXCLUDED.cited_by_count
        """,
        (
            paper["paper_id"], paper["title"], paper["abstract"],
            paper["publication_date"], paper["doi"], paper["cited_by_count"],
            paper["source_name"], paper["pdf_url"], paper["openalex_url"],
            json.dumps(paper["concepts"]),
        ),
    )
    for auth in paper.get("authorships", []):
        if not auth["author_id"]:
            continue
        lakebase.run_write(
            "INSERT INTO authors (author_id, display_name, institution, orcid, openalex_url) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (auth["author_id"], auth["display_name"], auth["institution"],
             auth["orcid"], auth["openalex_url"]),
        )
        lakebase.run_write(
            "INSERT INTO paper_authors (paper_id, author_id, position) "
            "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (paper["paper_id"], auth["author_id"], auth["position"]),
        )


# ---------------------------------------------------------------------------
# Tool 1: search_papers
# ---------------------------------------------------------------------------

def search_papers(query: str, mode: str = "semantic", limit: int = 10) -> dict:
    """Find papers by semantic search (indexed DB) or OpenAlex (live)."""
    if mode == "openalex":
        client = OpenAlexClient()
        results = client.search_papers_for_goal(query, limit=limit)
        for paper in results:
            upsert_paper(paper)
        papers = [
            {"paper_id": p["paper_id"], "title": p["title"],
             "abstract": (p.get("abstract") or "")[:300],
             "cited_by_count": p.get("cited_by_count", 0)}
            for p in results[:limit]
        ]
    else:
        hits = vector_search(query, top_k=limit, source_type="abstract")
        paper_ids = list({h["source_id"] for h in hits})
        if not paper_ids:
            return {"tool": "search_papers", "answer": "No papers found for that query.", "citations": []}
        ph = ",".join(["%s"] * len(paper_ids))
        rows = lakebase.run_query(
            f"SELECT paper_id, title, abstract, cited_by_count "
            f"FROM papers WHERE paper_id IN ({ph}) ORDER BY cited_by_count DESC",
            tuple(paper_ids),
        )
        papers = [
            {"paper_id": p["paper_id"], "title": p["title"],
             "abstract": (p.get("abstract") or "")[:300],
             "cited_by_count": p.get("cited_by_count", 0)}
            for p in rows
        ]

    if not papers:
        return {"tool": "search_papers", "answer": "No papers found.", "citations": []}

    lines = [f"Found {len(papers)} papers ({mode} search):\n"]
    for i, p in enumerate(papers, 1):
        lines.append(f"{i}. **{p['title']}** (cited: {p['cited_by_count']})")
        if p.get("abstract"):
            lines.append(f"   {p['abstract'][:150]}...")
    return {
        "tool": "search_papers",
        "answer": "\n".join(lines),
        "citations": [{"paper_id": p["paper_id"], "title": p["title"]} for p in papers],
    }


# ---------------------------------------------------------------------------
# Tool 2: summarize_papers
# ---------------------------------------------------------------------------

def summarize_papers(paper_ids: list[str]) -> dict:
    """Summarize one or more papers using LLM."""
    if not paper_ids:
        return {"tool": "summarize_papers", "answer": "No paper IDs provided.", "citations": []}

    ph = ",".join(["%s"] * len(paper_ids))
    papers = lakebase.run_query(
        f"SELECT paper_id, title, abstract FROM papers WHERE paper_id IN ({ph})",
        tuple(paper_ids),
    )
    if not papers:
        return {"tool": "summarize_papers", "answer": "No matching papers found.", "citations": []}

    context = "\n\n".join(
        f"[{p['title']}]\n{(p.get('abstract') or 'No abstract')[:600]}"
        for p in papers
    )
    prompt = (
        "Summarize the following academic papers. For each, provide a 2-3 sentence "
        "summary of its key contribution. Then briefly synthesize common themes.\n\n"
        f"{context}\n\nSummary:"
    )
    summary = call_llm(prompt, max_tokens=600)
    return {
        "tool": "summarize_papers",
        "answer": summary,
        "citations": [{"paper_id": p["paper_id"], "title": p["title"]} for p in papers],
    }


# ---------------------------------------------------------------------------
# Tool 3: compare_papers
# ---------------------------------------------------------------------------

def compare_papers(paper_id_1: str, paper_id_2: str) -> dict:
    """Side-by-side comparison of two papers."""
    p1 = lakebase.run_query_one("SELECT * FROM papers WHERE paper_id = %s", (paper_id_1,))
    p2 = lakebase.run_query_one("SELECT * FROM papers WHERE paper_id = %s", (paper_id_2,))

    if not p1 or not p2:
        missing = paper_id_1 if not p1 else paper_id_2
        return {"tool": "compare_papers", "answer": f"Paper {missing} not found.", "citations": []}

    prompt = (
        "Compare these two academic papers. Discuss their approaches, "
        "methodologies, contributions, and how they relate.\n\n"
        f"Paper 1: {p1['title']}\n{(p1.get('abstract') or '')[:600]}\n\n"
        f"Paper 2: {p2['title']}\n{(p2.get('abstract') or '')[:600]}\n\n"
        "Comparison:"
    )
    comparison = call_llm(prompt, max_tokens=600)
    return {
        "tool": "compare_papers",
        "answer": comparison,
        "citations": [
            {"paper_id": p1["paper_id"], "title": p1["title"]},
            {"paper_id": p2["paper_id"], "title": p2["title"]},
        ],
    }


# ---------------------------------------------------------------------------
# Tool 4: generate_study_plan
# ---------------------------------------------------------------------------

def generate_study_plan(topic: str, num_papers: int = 8) -> dict:
    """Create a sequenced reading plan for a research topic."""
    hits = vector_search(topic, top_k=num_papers * 2, source_type="abstract")
    paper_ids = list({h["source_id"] for h in hits})[:num_papers]

    if not paper_ids:
        return {"tool": "generate_study_plan", "answer": "No papers found for this topic.", "citations": []}

    ph = ",".join(["%s"] * len(paper_ids))
    papers = lakebase.run_query(
        f"SELECT paper_id, title, abstract, cited_by_count, publication_date "
        f"FROM papers WHERE paper_id IN ({ph}) ORDER BY cited_by_count DESC",
        tuple(paper_ids),
    )
    paper_list = "\n".join(
        f"- [{p['title']}] (cited: {p.get('cited_by_count', 0)}, "
        f"date: {p.get('publication_date', 'unknown')}): "
        f"{(p.get('abstract') or '')[:200]}"
        for p in papers
    )
    prompt = (
        f"Create a study plan for learning about '{topic}'. "
        "Sequence these papers from foundational to advanced. "
        "For each, explain why it should be read at that point.\n\n"
        f"Available papers:\n{paper_list}\n\n"
        "Study plan (numbered, with reasoning):"
    )
    plan = call_llm(prompt, max_tokens=800)
    return {
        "tool": "generate_study_plan",
        "answer": plan,
        "citations": [{"paper_id": p["paper_id"], "title": p["title"]} for p in papers],
    }


# ---------------------------------------------------------------------------
# Tool 5: add_to_collection
# ---------------------------------------------------------------------------

def add_to_collection(collection_id: int, paper_id: str) -> dict:
    """Add a paper to a user's collection (write action)."""
    collection = lakebase.run_query_one(
        "SELECT name FROM collections WHERE collection_id = %s", (collection_id,)
    )
    paper = lakebase.run_query_one(
        "SELECT title FROM papers WHERE paper_id = %s", (paper_id,)
    )
    if not collection:
        return {"tool": "add_to_collection", "answer": f"Collection {collection_id} not found.", "citations": []}
    if not paper:
        return {"tool": "add_to_collection", "answer": f"Paper {paper_id} not found.", "citations": []}

    lakebase.run_write(
        "INSERT INTO collection_papers (collection_id, paper_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (collection_id, paper_id),
    )
    return {
        "tool": "add_to_collection",
        "answer": f"Added '{paper['title']}' to collection '{collection['name']}'.",
        "citations": [{"paper_id": paper_id, "title": paper["title"]}],
    }


# ---------------------------------------------------------------------------
# Tool 6: update_reading_progress
# ---------------------------------------------------------------------------

def update_reading_progress(paper_id: str, status: str = "reading") -> dict:
    """Update reading progress for a paper (write action)."""
    paper = lakebase.run_query_one(
        "SELECT title FROM papers WHERE paper_id = %s", (paper_id,)
    )
    if not paper:
        return {"tool": "update_reading_progress", "answer": f"Paper {paper_id} not found.", "citations": []}

    lakebase.run_write(
        """
        INSERT INTO reading_progress (user_id, paper_id, status, started_at, completed_at)
        VALUES (%s, %s, %s,
                CASE WHEN %s IN ('reading','completed') THEN now() ELSE NULL END,
                CASE WHEN %s = 'completed' THEN now() ELSE NULL END)
        ON CONFLICT (user_id, paper_id) DO UPDATE
            SET status = EXCLUDED.status,
                started_at = COALESCE(reading_progress.started_at, EXCLUDED.started_at),
                completed_at = EXCLUDED.completed_at
        """,
        (DEFAULT_USER_ID, paper_id, status, status, status),
    )
    return {
        "tool": "update_reading_progress",
        "answer": f"Progress for '{paper['title']}' set to '{status}'.",
        "citations": [{"paper_id": paper_id, "title": paper["title"]}],
    }


# ---------------------------------------------------------------------------
# Tool 7: recommend_next_paper
# ---------------------------------------------------------------------------

def recommend_next_paper(topic: str | None = None) -> dict:
    """Suggest the next paper to read based on reading history."""
    read = lakebase.run_query(
        "SELECT paper_id FROM reading_progress WHERE user_id = %s AND status IN ('reading', 'completed')",
        (DEFAULT_USER_ID,),
    )
    read_ids = {r["paper_id"] for r in read}

    if topic:
        hits = vector_search(topic, top_k=20, source_type="abstract")
        candidate_ids = [h["source_id"] for h in hits if h["source_id"] not in read_ids][:5]
    elif read_ids:
        ph = ",".join(["%s"] * len(read_ids))
        read_papers = lakebase.run_query(
            f"SELECT title FROM papers WHERE paper_id IN ({ph}) LIMIT 3",
            tuple(read_ids),
        )
        combined = " ".join(p.get("title", "") for p in read_papers)
        hits = vector_search(combined, top_k=20, source_type="abstract")
        candidate_ids = [h["source_id"] for h in hits if h["source_id"] not in read_ids][:5]
    else:
        papers = lakebase.run_query(
            "SELECT paper_id, title, cited_by_count FROM papers ORDER BY cited_by_count DESC LIMIT 5"
        )
        lines = ["No reading history yet — here are the most-cited papers:\n"]
        for i, p in enumerate(papers, 1):
            lines.append(f"{i}. **{p['title']}** (cited: {p['cited_by_count']})")
        return {
            "tool": "recommend_next_paper",
            "answer": "\n".join(lines),
            "citations": [{"paper_id": p["paper_id"], "title": p["title"]} for p in papers],
        }

    if not candidate_ids:
        return {"tool": "recommend_next_paper", "answer": "No unread papers to recommend.", "citations": []}

    ph = ",".join(["%s"] * len(candidate_ids))
    papers = lakebase.run_query(
        f"SELECT paper_id, title, cited_by_count FROM papers "
        f"WHERE paper_id IN ({ph}) ORDER BY cited_by_count DESC",
        tuple(candidate_ids),
    )
    basis = "topic" if topic else "reading history"
    lines = [f"Based on your {basis}, here are my recommendations:\n"]
    for i, p in enumerate(papers, 1):
        lines.append(f"{i}. **{p['title']}** (cited: {p['cited_by_count']})")
    return {
        "tool": "recommend_next_paper",
        "answer": "\n".join(lines),
        "citations": [{"paper_id": p["paper_id"], "title": p["title"]} for p in papers],
    }


# ---------------------------------------------------------------------------
# Tool 8: general_rag (default fallback)
# ---------------------------------------------------------------------------

def general_rag(query: str) -> dict:
    """Answer a general question using RAG over the paper database."""
    hits = vector_search(query, top_k=8)

    context_parts = []
    cited_papers = {}
    for h in hits:
        paper = lakebase.run_query_one(
            "SELECT paper_id, title, doi FROM papers WHERE paper_id = %s",
            (h["source_id"],),
        )
        if paper:
            cited_papers[paper["paper_id"]] = paper
            context_parts.append(
                f"[{paper['title']}] (similarity: {h['similarity']:.2f})\n"
                f"{h['chunk_text'][:600]}"
            )

    context = "\n\n---\n\n".join(context_parts) if context_parts else "No relevant papers found."

    prompt = (
        "You are an AI research assistant helping a student learn about academic topics. "
        "Based on the following retrieved paper excerpts, answer the question. "
        "Include citations by referencing paper titles in square brackets. "
        "If the excerpts don't contain enough information, say so honestly.\n\n"
        f"Question: \"{query}\"\n\n"
        f"Retrieved excerpts:\n{context}\n\n"
        "Answer (with citations):"
    )
    answer = call_llm(prompt, max_tokens=800)
    return {
        "tool": "general_rag",
        "answer": answer,
        "citations": [
            {"paper_id": p["paper_id"], "title": p["title"], "doi": p.get("doi", "")}
            for p in cited_papers.values()
        ],
    }


# ---------------------------------------------------------------------------
# Broker-compatible API (used by research_mcp_server.py)
# These match the original research_broker.py signatures so the MCP server
# can simply `import research_tools as broker`.
# ---------------------------------------------------------------------------

def openalex_search(query: str, limit: int = 15) -> list[dict]:
    """Search OpenAlex directly. Returns list of normalized paper dicts."""
    client = OpenAlexClient()
    return client.search_papers_for_goal(query, limit=limit)


def search_papers_in_db(query: str, limit: int = 10) -> list[dict]:
    """Semantic search returning raw paper rows from DB."""
    hits = vector_search(query, top_k=limit, source_type="abstract")
    pids = list({h["source_id"] for h in hits})
    if not pids:
        return []
    ph = ",".join(["%s"] * len(pids))
    return lakebase.run_query(
        f"SELECT paper_id, title, abstract, cited_by_count, publication_date, "
        f"source_name FROM papers WHERE paper_id IN ({ph}) "
        f"ORDER BY cited_by_count DESC",
        tuple(pids),
    )


def get_paper(paper_id: str) -> dict | None:
    """Fetch a single paper by ID."""
    return lakebase.run_query_one(
        "SELECT * FROM papers WHERE paper_id = %s", (paper_id,)
    )


def get_reading_progress(user_id: int) -> list[dict]:
    """Get reading progress rows for a user, including paper title."""
    return lakebase.run_query(
        "SELECT rp.*, p.title FROM reading_progress rp "
        "JOIN papers p ON rp.paper_id = p.paper_id "
        "WHERE rp.user_id = %s",
        (user_id,),
    )


def add_paper_to_collection(collection_id: int, paper_id: str) -> dict:
    """Add paper to collection. Returns {status, message}."""
    lakebase.run_write(
        "INSERT INTO collection_papers (collection_id, paper_id) "
        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (collection_id, paper_id),
    )
    return {
        "status": "success",
        "message": f"Paper {paper_id} added to collection {collection_id}.",
    }


def update_reading_progress_for_user(
    user_id: int, paper_id: str, status: str
) -> dict:
    """Update reading progress with explicit user_id. Returns {status, message}."""
    lakebase.run_write(
        """
        INSERT INTO reading_progress (user_id, paper_id, status, started_at, completed_at)
        VALUES (%s, %s, %s,
                CASE WHEN %s IN ('reading','completed') THEN now() ELSE NULL END,
                CASE WHEN %s = 'completed' THEN now() ELSE NULL END)
        ON CONFLICT (user_id, paper_id) DO UPDATE
            SET status = EXCLUDED.status,
                started_at = COALESCE(reading_progress.started_at, EXCLUDED.started_at),
                completed_at = EXCLUDED.completed_at
        """,
        (user_id, paper_id, status, status, status),
    )
    return {
        "status": "success",
        "message": f"Reading progress for {paper_id} set to '{status}'.",
    }


# ---------------------------------------------------------------------------
# LLM-based tool dispatcher
# ---------------------------------------------------------------------------

_DISPATCH_PROMPT = """You are a tool router for a research assistant. Given the user's message, determine which tool to call and extract the parameters.

Available tools:
1. search_papers(query, mode, limit) — Find papers. mode: "semantic" (default, search indexed DB) or "openalex" (discover new papers online). limit: integer, default 10.
2. summarize_papers(paper_ids) — Summarize specific papers. paper_ids: list of paper ID strings like ["W1234", "W5678"].
3. compare_papers(paper_id_1, paper_id_2) — Compare exactly two papers by their IDs.
4. generate_study_plan(topic, num_papers) — Create a sequenced reading plan. num_papers: integer, default 8.
5. add_to_collection(collection_id, paper_id) — Add a paper to a collection. collection_id: integer, paper_id: string.
6. update_reading_progress(paper_id, status) — Mark paper status. status: "not_started", "reading", or "completed".
7. recommend_next_paper(topic) — Suggest next paper to read. topic: optional string.
8. general_rag(query) — Answer a general question using RAG search (DEFAULT for most questions).

Routing rules:
- Use general_rag for open-ended questions, explanations, "what is...", "how does...", or "explain..." queries.
- Use search_papers when the user wants to FIND or LOOK UP papers on a topic.
- Use summarize_papers only when specific paper IDs are mentioned.
- Use compare_papers only when comparing exactly two papers with IDs.
- Use generate_study_plan when the user asks for a study plan, reading plan, or learning path.
- Use recommend_next_paper when the user asks "what should I read next?" or wants recommendations.
- Use add_to_collection or update_reading_progress only for explicit write requests.

Respond with ONLY valid JSON (no markdown fences, no explanation):
{{"tool": "<name>", "params": {{...}}}}

User message: "{user_message}""""


def dispatch(user_message: str) -> dict:
    """Route a user message to the appropriate tool and return a unified response.

    Returns dict with keys: tool, answer, citations.
    """
    prompt = _DISPATCH_PROMPT.format(user_message=user_message.replace('"', '\\"'))

    try:
        raw = call_llm(prompt, max_tokens=200)
        raw = raw.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        decision = json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("Tool dispatch parse failed (%s), falling back to general_rag", exc)
        decision = {"tool": "general_rag", "params": {"query": user_message}}

    tool_name = decision.get("tool", "general_rag")
    params = decision.get("params", {})
    logger.info("Dispatching to tool=%s params=%s", tool_name, params)

    try:
        if tool_name == "search_papers":
            return search_papers(
                query=params.get("query", user_message),
                mode=params.get("mode", "semantic"),
                limit=int(params.get("limit", 10)),
            )
        elif tool_name == "summarize_papers":
            return summarize_papers(paper_ids=params.get("paper_ids", []))
        elif tool_name == "compare_papers":
            return compare_papers(
                paper_id_1=params.get("paper_id_1", ""),
                paper_id_2=params.get("paper_id_2", ""),
            )
        elif tool_name == "generate_study_plan":
            return generate_study_plan(
                topic=params.get("topic", user_message),
                num_papers=int(params.get("num_papers", 8)),
            )
        elif tool_name == "add_to_collection":
            return add_to_collection(
                collection_id=int(params.get("collection_id", 0)),
                paper_id=params.get("paper_id", ""),
            )
        elif tool_name == "update_reading_progress":
            return update_reading_progress(
                paper_id=params.get("paper_id", ""),
                status=params.get("status", "reading"),
            )
        elif tool_name == "recommend_next_paper":
            return recommend_next_paper(topic=params.get("topic"))
        else:
            return general_rag(query=params.get("query", user_message))
    except Exception as exc:
        logger.exception("Tool %s failed", tool_name)
        return {"tool": tool_name, "answer": f"Tool error: {exc}", "citations": []}
