"""
Research Tools — shared tool implementations for the AI Research Copilot.

Provides all 15 agent capabilities (matching the MCP server tools) including
a general RAG fallback, and an LLM-based tool dispatcher that the Flask
frontend calls from /api/agent/chat.

Uses lakebase.py for database access and openalex_client.py for external
API calls — no duplication of connection logic.
"""

import json
import logging
import os
import re

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


# OpenAlex paper IDs: W followed by 5+ digits (e.g., W2741809807)
_OPENALEX_ID_RE = re.compile(r'^W\d{5,}$')


def _openalex_get_with_retry(url: str, timeout: int = 30):
    """GET from OpenAlex with retry, exponential backoff, and Retry-After support."""
    import time
    import requests as _req

    max_retries = 3
    base_delay = 1.0

    for attempt in range(max_retries + 1):
        try:
            resp = _req.get(url, timeout=timeout)
        except _req.Timeout:
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

        return resp

    resp.raise_for_status()
    return resp


def _openalex_fetch_by_id(paper_id: str) -> dict | None:
    """Fetch a single paper from OpenAlex by its paper_id (e.g., 'W2741809807')."""
    resp = _openalex_get_with_retry(f"https://api.openalex.org/works/{paper_id}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    work = resp.json()
    oid = work.get("id", "")
    pid = oid.split("/")[-1] if "/" in oid else oid
    inv = work.get("abstract_inverted_index") or {}
    words = [(pos, word) for word, positions in inv.items() for pos in positions]
    words.sort()
    abstract = " ".join(w for _, w in words)
    primary_loc = work.get("primary_location") or {}
    return {
        "paper_id": pid,
        "title": work.get("title", "Untitled"),
        "abstract": abstract[:500],
        "publication_date": work.get("publication_date"),
        "cited_by_count": work.get("cited_by_count", 0),
        "doi": work.get("doi", ""),
        "source_name": (primary_loc.get("source") or {}).get("display_name"),
        "pdf_url": primary_loc.get("pdf_url"),
        "openalex_url": work.get("id", ""),
        "concepts": [c.get("display_name", "") for c in work.get("concepts", [])[:10]],
    }


def _search_papers_in_db(
    query: str, limit: int = 10, user_id: int | None = None
) -> list[dict]:
    """Semantic search with goal-aware embedding reuse and similarity scores.

    If user_id is provided and the query matches an existing learning goal
    title, uses the goal's stored embedding vector instead of re-encoding.
    """
    goal_id = None
    if user_id is not None:
        goal = lakebase.run_query_one(
            "SELECT goal_id FROM learning_goals WHERE title = %s AND user_id = %s",
            (query, user_id),
        )
        if goal:
            goal_id = str(goal["goal_id"])

    if goal_id is not None:
        hits = lakebase.run_query(
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
    sim_map = {h["source_id"]: h["similarity"] for h in hits}
    ph = ",".join(["%s"] * len(pids))
    papers = lakebase.run_query(
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
    paper_input: str,
    user_id: int = DEFAULT_USER_ID,
    similarity_threshold: float = 0.7,
) -> dict | None:
    """Resolve a paper input (OpenAlex ID or title/topic) to a paper dict.

    - If input matches an OpenAlex ID pattern, looks up directly. If not in
      DB, fetches from OpenAlex and upserts.
    - If input is a title/topic, does semantic search. Uses the top match
      if similarity >= threshold; otherwise discovers from OpenAlex.
    """
    if _OPENALEX_ID_RE.match(paper_input):
        p = lakebase.run_query_one(
            "SELECT * FROM papers WHERE paper_id = %s", (paper_input,)
        )
        if p:
            return p
        logger.info("Paper ID '%s' not in DB, fetching from OpenAlex...", paper_input)
        oa_paper = _openalex_fetch_by_id(paper_input)
        if oa_paper:
            upsert_paper(oa_paper)
            p = lakebase.run_query_one(
                "SELECT * FROM papers WHERE paper_id = %s", (paper_input,)
            )
            if p:
                return p
        return None
    else:
        results = _search_papers_in_db(paper_input, limit=1, user_id=user_id)
        if results and results[0].get("similarity", 0) >= similarity_threshold:
            return results[0]
        client = OpenAlexClient()
        oa_results = client.search_papers_for_goal(paper_input, limit=1)
        if oa_results:
            upsert_paper(oa_results[0])
            p = lakebase.run_query_one(
                "SELECT * FROM papers WHERE paper_id = %s",
                (oa_results[0]["paper_id"],),
            )
            if p:
                return p
        return None


def _seed_reading_progress(user_id: int, paper_id: str) -> None:
    """Insert a 'not_started' reading-progress record if none exists yet."""
    lakebase.run_write(
        "INSERT INTO reading_progress (user_id, paper_id, status) "
        "VALUES (%s, %s, 'not_started') "
        "ON CONFLICT (user_id, paper_id) DO NOTHING",
        (user_id, paper_id),
    )


# ---------------------------------------------------------------------------
# Tool 1: search_papers
# ---------------------------------------------------------------------------

def search_papers(query: str, mode: str = "semantic", limit: int = 10, user_id: int = DEFAULT_USER_ID) -> dict:
    """Find papers by semantic or OpenAlex search, with auto-fallback.

    Searches the indexed knowledge base first (goal-aware). Falls back to
    OpenAlex when: no results, top similarity < 0.6, or mode == 'openalex'.
    """
    limit = max(1, min(50, limit))

    # Search the indexed knowledge base (goal-aware)
    papers = _search_papers_in_db(query, limit=limit, user_id=user_id)

    # Fall back to OpenAlex if DB has no good match or caller requests it
    needs_openalex = (
        (not papers)
        or (papers[0].get("similarity", 0) < 0.6)
        or (mode == "openalex")
    )
    if needs_openalex:
        client = OpenAlexClient()
        results = client.search_papers_for_goal(query, limit=limit)
        for paper in results:
            upsert_paper(paper)
        papers = results

    if not papers:
        return {"tool": "search_papers", "answer": "No papers found.", "citations": []}

    source = "OpenAlex" if needs_openalex else "semantic"
    lines = [f"Found {len(papers)} papers ({source} search):\n"]
    for i, p in enumerate(papers[:limit], 1):
        lines.append(f"{i}. **{p['title']}** (cited: {p.get('cited_by_count', 0)})")
        abstract = (p.get("abstract") or "")[:150]
        if abstract:
            lines.append(f"   {abstract}...")
    return {
        "tool": "search_papers",
        "answer": "\n".join(lines),
        "citations": [{"paper_id": p["paper_id"], "title": p["title"]} for p in papers[:limit]],
    }


# ---------------------------------------------------------------------------
# Tool 2: summarize_papers
# ---------------------------------------------------------------------------

def summarize_papers(paper_inputs: list[str], user_id: int = DEFAULT_USER_ID) -> dict:
    """Summarize one or more papers using LLM.

    Accepts either OpenAlex paper IDs (e.g., 'W2741809807') or paper
    titles/topics. Uses find_or_fetch_paper() to resolve each input.
    """
    if not paper_inputs:
        return {"tool": "summarize_papers", "answer": "No paper inputs provided.", "citations": []}

    papers = []
    for item in paper_inputs:
        p = find_or_fetch_paper(item, user_id=user_id)
        if p:
            papers.append(p)
        else:
            logger.warning("Could not resolve paper input: '%s'", item)

    if not papers:
        return {"tool": "summarize_papers", "answer": "No matching papers found.", "citations": []}

    context = "\n\n".join(
        f"[{p['title']}]\nAbstract: {(p.get('abstract') or 'N/A')[:600]}"
        for p in papers
    )
    prompt = (
        "Summarize the following academic papers. For each, highlight the "
        "key contributions, methods, and findings. Cite papers by their titles "
        "in square brackets.\n\n"
        f"Papers:\n{context}\n\nSummary:"
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

def compare_papers(paper_input_1: str, paper_input_2: str, user_id: int = DEFAULT_USER_ID) -> dict:
    """Side-by-side comparison of two papers.

    Accepts either OpenAlex paper IDs or paper titles/topics.
    """
    p1 = find_or_fetch_paper(paper_input_1, user_id=user_id)
    p2 = find_or_fetch_paper(paper_input_2, user_id=user_id)

    if not p1 or not p2:
        missing = [inp for inp, p in [(paper_input_1, p1), (paper_input_2, p2)] if not p]
        return {"tool": "compare_papers", "answer": f"Could not resolve paper(s): {missing}", "citations": []}

    prompt = (
        "Compare the following two academic papers. Discuss their:\n"
        "1. Research objectives and scope\n"
        "2. Methodology and approach\n"
        "3. Key findings and contributions\n"
        "4. Similarities and differences\n\n"
        f"Paper 1: [{p1['title']}]\nAbstract: {(p1.get('abstract') or 'N/A')[:600]}\n\n"
        f"Paper 2: [{p2['title']}]\nAbstract: {(p2.get('abstract') or 'N/A')[:600]}\n\n"
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

def generate_study_plan(topic: str, num_papers: int = 5, user_id: int = DEFAULT_USER_ID) -> dict:
    """Create a sequenced reading plan for a research topic.

    Uses goal-aware search, filters by similarity >= 0.7, and discovers
    additional papers from OpenAlex when the knowledge base is insufficient.
    """
    num_papers = max(3, min(20, num_papers))

    # Search knowledge base with goal-aware search
    papers = _search_papers_in_db(topic, limit=num_papers * 2, user_id=user_id)

    # Filter to papers with similarity >= 0.7
    papers = [p for p in papers if p.get("similarity", 0) >= 0.7]

    # If insufficient papers, discover from OpenAlex
    if (not papers) or (len(papers) < num_papers):
        needed = num_papers - len(papers)
        client = OpenAlexClient()
        oa_results = client.search_papers_for_goal(topic, limit=needed * 3)
        if not oa_results and not papers:
            return {"tool": "generate_study_plan", "answer": f"No papers found for '{topic}'.", "citations": []}
        existing_ids = {p["paper_id"] for p in papers}
        for p in oa_results:
            if p["paper_id"] not in existing_ids:
                upsert_paper(p)
                papers.append(p)
                existing_ids.add(p["paper_id"])
            if len(papers) >= num_papers:
                break

    if not papers:
        return {"tool": "generate_study_plan", "answer": "No papers found for this topic.", "citations": []}

    paper_list = "\n".join(
        f"{i+1}. [{p['title']}] (Cited by: {p.get('cited_by_count', 0)}, "
        f"Date: {p.get('publication_date', 'N/A')})\n"
        f"Abstract: {(p.get('abstract') or 'N/A')[:200]}"
        for i, p in enumerate(papers)
    )
    prompt = (
        f"Create a structured study plan for learning about: \"{topic}\"\n\n"
        f"Available papers:\n{paper_list}\n\n"
        "Create a sequenced reading plan that:\n"
        "1. Starts with foundational/survey papers\n"
        "2. Progresses to specialized research\n"
        "3. Ends with cutting-edge or advanced work\n"
        "4. Explains WHY each paper should be read at that position\n"
        "5. Estimates reading time (short/medium/long)\n\n"
        "Format each entry as: Order #. [Paper Title] - Rationale\n"
        "Study Plan:"
    )
    plan = call_llm(prompt, max_tokens=1200)
    return {
        "tool": "generate_study_plan",
        "answer": plan,
        "citations": [{"paper_id": p["paper_id"], "title": p["title"]} for p in papers],
    }


# ---------------------------------------------------------------------------
# Tool 5: add_to_collection
# ---------------------------------------------------------------------------

def add_to_collection(collection_name: str, paper_input: str, user_id: int = DEFAULT_USER_ID) -> dict:
    """Add a paper to a user's collection by name (write action).

    Resolves the collection by name and the paper by ID or title.
    Seeds a 'not_started' reading-progress record so the paper
    appears in the user's reading pipeline.
    """
    collection = lakebase.run_query_one(
        "SELECT collection_id, name FROM collections "
        "WHERE name ILIKE %s AND user_id = %s",
        (collection_name, user_id),
    )
    if not collection:
        return {"tool": "add_to_collection", "answer": f"No collection named '{collection_name}' found.", "citations": []}

    paper = find_or_fetch_paper(paper_input, user_id=user_id)
    if not paper:
        return {"tool": "add_to_collection", "answer": f"Could not resolve paper: '{paper_input}'", "citations": []}

    lakebase.run_write(
        "INSERT INTO collection_papers (collection_id, paper_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (collection["collection_id"], paper["paper_id"]),
    )
    _seed_reading_progress(user_id, paper["paper_id"])
    return {
        "tool": "add_to_collection",
        "answer": f"Added '{paper['title']}' to collection '{collection['name']}'.",
        "citations": [{"paper_id": paper["paper_id"], "title": paper["title"]}],
    }


# ---------------------------------------------------------------------------
# Tool 6: update_reading_progress
# ---------------------------------------------------------------------------

def update_reading_progress(paper_input: str, status: str = "reading", user_id: int = DEFAULT_USER_ID) -> dict:
    """Update reading progress for a paper (write action).

    Accepts either an OpenAlex paper ID or a paper title/topic.
    Validates status before updating.
    """
    if status not in ("not_started", "reading", "completed"):
        return {
            "tool": "update_reading_progress",
            "answer": f"Invalid status '{status}'. Use: not_started, reading, completed.",
            "citations": [],
        }

    paper = find_or_fetch_paper(paper_input, user_id=user_id)
    if not paper:
        return {"tool": "update_reading_progress", "answer": f"Could not resolve paper: '{paper_input}'", "citations": []}

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
        (user_id, paper["paper_id"], status, status, status),
    )
    return {
        "tool": "update_reading_progress",
        "answer": f"Progress for '{paper['title']}' set to '{status}'.",
        "citations": [{"paper_id": paper["paper_id"], "title": paper["title"]}],
    }


# ---------------------------------------------------------------------------
# Tool 7: recommend_next_paper
# ---------------------------------------------------------------------------

def recommend_next_paper(topic: str | None = None, user_id: int = DEFAULT_USER_ID) -> dict:
    """Recommend the next paper based on a study plan and reading history.

    Generates a study plan for the topic, checks reading history, and uses
    the LLM to pick the next unread paper in the sequence. Falls back to
    OpenAlex discovery if all plan papers have been read.
    """
    if not topic:
        # Without a topic, derive one from reading history
        read = lakebase.run_query(
            "SELECT paper_id FROM reading_progress WHERE user_id = %s "
            "AND status IN ('reading', 'completed')",
            (user_id,),
        )
        read_ids = {r["paper_id"] for r in read}
        if not read_ids:
            papers = lakebase.run_query(
                "SELECT paper_id, title, cited_by_count FROM papers "
                "ORDER BY cited_by_count DESC LIMIT 5"
            )
            lines = ["No reading history yet — here are the most-cited papers:\n"]
            for i, p in enumerate(papers, 1):
                lines.append(f"{i}. **{p['title']}** (cited: {p['cited_by_count']})")
            return {
                "tool": "recommend_next_paper",
                "answer": "\n".join(lines),
                "citations": [{"paper_id": p["paper_id"], "title": p["title"]} for p in papers],
            }
        ph = ",".join(["%s"] * len(read_ids))
        read_papers = lakebase.run_query(
            f"SELECT title FROM papers WHERE paper_id IN ({ph}) LIMIT 3",
            tuple(read_ids),
        )
        topic = " ".join(p.get("title", "") for p in read_papers)

    # Get reading history
    progress = lakebase.run_query(
        "SELECT paper_id, status FROM reading_progress WHERE user_id = %s",
        (user_id,),
    )
    completed_ids = {p["paper_id"] for p in progress if p["status"] == "completed"}
    reading_ids = {p["paper_id"] for p in progress if p["status"] == "reading"}
    read_ids = completed_ids | reading_ids

    # Generate a study plan to get a structured learning sequence
    plan_result = generate_study_plan(topic, num_papers=10, user_id=user_id)
    if plan_result.get("answer", "").startswith("No papers found"):
        return plan_result

    study_plan = plan_result["answer"]
    plan_papers = plan_result.get("citations", [])

    already_read = [p for p in plan_papers if p["paper_id"] in read_ids]
    unread = [p for p in plan_papers if p["paper_id"] not in read_ids]

    if unread:
        read_titles = [p["title"] for p in already_read]
        prompt = (
            f'The student is studying: "{topic}"\n\n'
            f"Here is their study plan (ordered from foundational to advanced):\n"
            f"{study_plan}\n\n"
            f"Papers already read: {read_titles if read_titles else 'None yet'}\n\n"
            "Based on the study plan ordering, recommend the SINGLE best next "
            "unread paper to read. Explain:\n"
            "1. Where this paper fits in the study plan progression\n"
            "2. Why it's the logical next step given what's already been read\n"
            "3. What the student will gain from reading it\n\n"
            "Recommendation:"
        )
        recommendation = call_llm(prompt, max_tokens=800)
        return {
            "tool": "recommend_next_paper",
            "answer": recommendation,
            "citations": unread,
        }

    # All plan papers already read — discover more from OpenAlex
    client = OpenAlexClient()
    oa_results = client.search_papers_for_goal(topic, limit=10)
    new_candidates = [p for p in oa_results if p["paper_id"] not in read_ids]

    if not new_candidates:
        return {
            "tool": "recommend_next_paper",
            "answer": "All available papers have been read. Great progress!",
            "citations": [],
        }

    for p in new_candidates:
        upsert_paper(p)

    candidate_list = "\n".join(
        f"- [{p['title']}] (Citations: {p.get('cited_by_count', 0)})\n"
        f"  Abstract: {(p.get('abstract', 'N/A') or 'N/A')[:200]}"
        for p in new_candidates[:8]
    )
    prompt = (
        f'The student has completed all papers in their study plan for "{topic}".\n\n'
        f"Study plan completed:\n{study_plan}\n\n"
        f"Here are newly discovered papers to continue learning:\n{candidate_list}\n\n"
        "Recommend the SINGLE best next paper that builds on the completed "
        "study plan. Explain what new ground it covers.\n\n"
        "Recommendation:"
    )
    recommendation = call_llm(prompt, max_tokens=800)
    return {
        "tool": "recommend_next_paper",
        "answer": recommendation,
        "citations": [{"paper_id": p["paper_id"], "title": p["title"]} for p in new_candidates[:8]],
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
# Tool 9: verify_user
# ---------------------------------------------------------------------------

def verify_user(user_id: int | None = None, username: str | None = None) -> dict:
    """Verify that a user exists before performing user-scoped actions."""
    if user_id is not None:
        user = lakebase.run_query_one(
            "SELECT user_id, email, display_name FROM users WHERE user_id = %s",
            (user_id,),
        )
    elif username is not None:
        user = lakebase.run_query_one(
            "SELECT user_id, email, display_name FROM users "
            "WHERE email ILIKE %s OR display_name ILIKE %s",
            (username, username),
        )
    else:
        return {"tool": "verify_user", "answer": "Please provide a user ID or username.", "citations": []}

    if not user:
        return {"tool": "verify_user", "answer": "User not found. Please check your user ID or username.", "citations": []}
    return {
        "tool": "verify_user",
        "answer": f"User verified: **{user['display_name']}** (ID: {user['user_id']}, email: {user.get('email', 'N/A')}).",
        "citations": [],
    }


# ---------------------------------------------------------------------------
# Tool 10: create_collection_tool
# ---------------------------------------------------------------------------

def create_collection_tool(name: str, description: str = "", user_id: int = DEFAULT_USER_ID) -> dict:
    """Create a new paper collection for the current user."""
    if not name:
        return {"tool": "create_collection", "answer": "Collection name is required.", "citations": []}
    result = lakebase.run_returning(
        "INSERT INTO collections (user_id, name, description) "
        "VALUES (%s, %s, %s) RETURNING collection_id, name",
        (user_id, name, description),
    )
    if not result:
        return {"tool": "create_collection", "answer": "Failed to create collection.", "citations": []}
    return {
        "tool": "create_collection",
        "answer": f"Collection **'{result['name']}'** created (ID: {result['collection_id']}).",
        "citations": [],
    }


# ---------------------------------------------------------------------------
# Tool 11: get_reading_progress_tool
# ---------------------------------------------------------------------------

def get_reading_progress_tool(user_id: int = DEFAULT_USER_ID) -> dict:
    """Retrieve reading progress for the current user."""
    rows = lakebase.run_query(
        "SELECT rp.status, p.paper_id, p.title FROM reading_progress rp "
        "JOIN papers p ON rp.paper_id = p.paper_id "
        "WHERE rp.user_id = %s ORDER BY rp.started_at DESC",
        (user_id,),
    )
    if not rows:
        return {"tool": "get_reading_progress", "answer": "No reading progress found.", "citations": []}

    grouped = {"completed": [], "reading": [], "not_started": []}
    for r in rows:
        grouped.get(r["status"], grouped["not_started"]).append(r)

    lines = [f"Reading progress ({len(rows)} papers tracked):\n"]
    for status, label in [("completed", "Completed"), ("reading", "Currently Reading"), ("not_started", "Not Started")]:
        if grouped[status]:
            lines.append(f"\n**{label}** ({len(grouped[status])}):")
            for p in grouped[status]:
                lines.append(f"  - {p['title']}")
    return {
        "tool": "get_reading_progress",
        "answer": "\n".join(lines),
        "citations": [{"paper_id": r["paper_id"], "title": r["title"]} for r in rows],
    }


# ---------------------------------------------------------------------------
# Tool 12: create_learning_goal_tool
# ---------------------------------------------------------------------------

def create_learning_goal_tool(title: str, description: str = "", user_id: int = DEFAULT_USER_ID) -> dict:
    """Create a new learning goal and embed it for semantic search."""
    import hashlib

    if not title:
        return {"tool": "create_learning_goal", "answer": "Learning goal title is required.", "citations": []}

    result = lakebase.run_returning(
        "INSERT INTO learning_goals (user_id, title, description) "
        "VALUES (%s, %s, %s) RETURNING goal_id, title",
        (user_id, title, description),
    )
    if not result:
        return {"tool": "create_learning_goal", "answer": "Failed to create learning goal.", "citations": []}

    goal_id = str(result["goal_id"])

    # Embed the goal (matching pipeline format)
    model = _get_embedding_model()
    text = f"{title}\n\n{description}" if description else title
    vec = model.encode([text])[0].tolist()
    embedding_str = "[" + ",".join(str(float(x)) for x in vec) + "]"
    emb_id = hashlib.md5(f"goal:{goal_id}".encode()).hexdigest()

    lakebase.run_write(
        "INSERT INTO embeddings (id, source_type, source_id, chunk_index, "
        "chunk_text, embedding, model_name) "
        "VALUES (%s, %s, %s, %s, %s, %s::vector, %s)",
        (emb_id, "goal", goal_id, 0, text, embedding_str, EMBEDDING_MODEL_NAME),
    )

    return {
        "tool": "create_learning_goal",
        "answer": f"Learning goal **'{title}'** created and embedded (ID: {result['goal_id']}).",
        "citations": [],
    }


# ---------------------------------------------------------------------------
# Tool 13: get_learning_goals_tool
# ---------------------------------------------------------------------------

def get_learning_goals_tool(user_id: int = DEFAULT_USER_ID) -> dict:
    """Retrieve all learning goals for the current user."""
    goals = lakebase.run_query(
        "SELECT goal_id, title, description, status, created_at "
        "FROM learning_goals WHERE user_id = %s ORDER BY created_at DESC",
        (user_id,),
    )
    if not goals:
        return {"tool": "get_learning_goals", "answer": "No learning goals found.", "citations": []}

    lines = [f"Found {len(goals)} learning goal(s):\n"]
    for i, g in enumerate(goals, 1):
        status_label = {"active": "Active", "completed": "Completed", "paused": "Paused"}.get(g.get("status", "active"), "Active")
        lines.append(f"{i}. [{status_label}] **{g['title']}** (ID: {g['goal_id']})")
        if g.get("description"):
            lines.append(f"   {g['description'][:150]}")
    return {
        "tool": "get_learning_goals",
        "answer": "\n".join(lines),
        "citations": [],
    }


# ---------------------------------------------------------------------------
# Tool 14: get_collections_tool
# ---------------------------------------------------------------------------

def get_collections_tool(user_id: int = DEFAULT_USER_ID) -> dict:
    """Retrieve all collections for the current user."""
    collections = lakebase.run_query(
        "SELECT c.collection_id, c.name, c.description, c.created_at, "
        "(SELECT COUNT(*) FROM collection_papers cp WHERE cp.collection_id = c.collection_id) AS paper_count "
        "FROM collections c WHERE c.user_id = %s ORDER BY c.created_at DESC",
        (user_id,),
    )
    if not collections:
        return {"tool": "get_collections", "answer": "No collections found.", "citations": []}

    lines = [f"Found {len(collections)} collection(s):\n"]
    for i, c in enumerate(collections, 1):
        lines.append(f"{i}. **{c['name']}** \u2014 {c.get('paper_count', 0)} papers (ID: {c['collection_id']})")
        if c.get("description"):
            lines.append(f"   {c['description'][:150]}")
    return {
        "tool": "get_collections",
        "answer": "\n".join(lines),
        "citations": [],
    }


# ---------------------------------------------------------------------------
# Tool 15: get_collection_papers_tool
# ---------------------------------------------------------------------------

def get_collection_papers_tool(collection_input: str, user_id: int = DEFAULT_USER_ID) -> dict:
    """Retrieve all papers in a collection (by name or numeric ID)."""
    if not collection_input:
        return {"tool": "get_collection_papers", "answer": "Please provide a collection name or ID.", "citations": []}

    if collection_input.isdigit():
        collection_id = int(collection_input)
        collection_name = collection_input
    else:
        coll = lakebase.run_query_one(
            "SELECT collection_id, name FROM collections "
            "WHERE name ILIKE %s AND user_id = %s",
            (collection_input, user_id),
        )
        if not coll:
            return {
                "tool": "get_collection_papers",
                "answer": f"No collection named '{collection_input}' found.",
                "citations": [],
            }
        collection_id = coll["collection_id"]
        collection_name = coll["name"]

    papers = lakebase.run_query(
        "SELECT cp.paper_id, p.title, p.abstract, p.cited_by_count, cp.added_at "
        "FROM collection_papers cp "
        "JOIN papers p ON cp.paper_id = p.paper_id "
        "WHERE cp.collection_id = %s ORDER BY cp.added_at DESC",
        (collection_id,),
    )
    if not papers:
        return {
            "tool": "get_collection_papers",
            "answer": f"Collection '{collection_name}' is empty.",
            "citations": [],
        }

    lines = [f"Collection '{collection_name}' has {len(papers)} paper(s):\n"]
    for i, p in enumerate(papers, 1):
        lines.append(f"{i}. **{p['title']}** (cited: {p.get('cited_by_count', 0)})")
        if p.get("abstract"):
            lines.append(f"   {(p['abstract'] or '')[:150]}...")
    return {
        "tool": "get_collection_papers",
        "answer": "\n".join(lines),
        "citations": [{"paper_id": p["paper_id"], "title": p["title"]} for p in papers],
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
2. summarize_papers(paper_inputs) — Summarize papers. paper_inputs: list of paper IDs or titles like ["W1234", "attention mechanisms"].
3. compare_papers(paper_input_1, paper_input_2) — Compare two papers. Accepts IDs or titles.
4. generate_study_plan(topic, num_papers) — Create a sequenced reading plan. num_papers: integer, default 5.
5. add_to_collection(collection_name, paper_input) — Add a paper to a collection by name. collection_name: string, paper_input: paper ID or title.
6. update_reading_progress(paper_input, status) — Mark paper status. paper_input: paper ID or title. status: "not_started", "reading", or "completed".
7. recommend_next_paper(topic) — Suggest next paper to read. topic: optional string.
8. general_rag(query) — Answer a general question using RAG search (DEFAULT for most questions).
9. verify_user(user_id, username) — Check if a user exists. user_id: integer OR username: string (email or display name). Provide one.
10. create_collection(name, description) — Create a new paper collection. name: string (required), description: optional string.
11. get_reading_progress() — Retrieve the user's reading progress across all tracked papers. No parameters.
12. create_learning_goal(title, description) — Add a new learning goal and embed it for search. title: string (required), description: optional string.
13. get_learning_goals() — Retrieve all learning goals. No parameters.
14. get_collections() — Retrieve all paper collections. No parameters.
15. get_collection_papers(collection_input) — Get all papers in a collection. collection_input: collection name or numeric ID string.

Routing rules:
- Use general_rag for open-ended questions, explanations, "what is...", "how does...", or "explain..." queries.
- Use search_papers when the user wants to FIND or LOOK UP papers on a topic.
- Use summarize_papers when the user wants to summarize specific papers (by ID or title).
- Use compare_papers when comparing exactly two papers (by ID or title).
- Use generate_study_plan when the user asks for a study plan, reading plan, or learning path.
- Use recommend_next_paper when the user asks "what should I read next?" or wants recommendations.
- Use add_to_collection, update_reading_progress, create_collection, or create_learning_goal only for explicit write requests.
- Use verify_user when the user wants to check if a user account exists or verify their identity.
- Use create_collection when the user wants to create a new paper collection.
- Use get_reading_progress when the user asks about their reading history, progress, or what they've read.
- Use create_learning_goal when the user wants to add a new learning goal or research objective.
- Use get_learning_goals when the user asks to see or list their learning goals.
- Use get_collections when the user asks to see or list their collections.
- Use get_collection_papers when the user asks what papers are in a specific collection.

Respond with ONLY valid JSON (no markdown fences, no explanation):
{{"tool": "<name>", "params": {{...}}}}

User message: \"{user_message}\"\n"""

def dispatch(user_message: str, user_id: int | None = None) -> dict:
    """Route a user message to the appropriate tool and return a unified response.

    If *user_id* is provided (from session verification), all tool calls
    will run in that user's context instead of DEFAULT_USER_ID.
    The global DEFAULT_USER_ID is never mutated, so concurrent sessions
    are safe from cross-user leakage.

    Returns dict with keys: tool, answer, citations.
    """
    _uid = user_id if user_id is not None else DEFAULT_USER_ID

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
                user_id=_uid,
            )
        elif tool_name == "summarize_papers":
            return summarize_papers(
                paper_inputs=params.get("paper_inputs", params.get("paper_ids", [])),
                user_id=_uid,
            )
        elif tool_name == "compare_papers":
            return compare_papers(
                paper_input_1=params.get("paper_input_1", params.get("paper_id_1", "")),
                paper_input_2=params.get("paper_input_2", params.get("paper_id_2", "")),
                user_id=_uid,
            )
        elif tool_name == "generate_study_plan":
            return generate_study_plan(
                topic=params.get("topic", user_message),
                num_papers=int(params.get("num_papers", 5)),
                user_id=_uid,
            )
        elif tool_name == "add_to_collection":
            return add_to_collection(
                collection_name=params.get("collection_name", params.get("name", "")),
                paper_input=params.get("paper_input", params.get("paper_id", "")),
                user_id=_uid,
            )
        elif tool_name == "update_reading_progress":
            return update_reading_progress(
                paper_input=params.get("paper_input", params.get("paper_id", "")),
                status=params.get("status", "reading"),
                user_id=_uid,
            )
        elif tool_name == "recommend_next_paper":
            return recommend_next_paper(topic=params.get("topic"), user_id=_uid)
        elif tool_name == "verify_user":
            return verify_user(
                user_id=params.get("user_id"),
                username=params.get("username"),
            )
        elif tool_name == "create_collection":
            return create_collection_tool(
                name=params.get("name", ""),
                description=params.get("description", ""),
                user_id=_uid,
            )
        elif tool_name == "get_reading_progress":
            return get_reading_progress_tool(user_id=_uid)
        elif tool_name == "create_learning_goal":
            return create_learning_goal_tool(
                title=params.get("title", ""),
                description=params.get("description", ""),
                user_id=_uid,
            )
        elif tool_name == "get_learning_goals":
            return get_learning_goals_tool(user_id=_uid)
        elif tool_name == "get_collections":
            return get_collections_tool(user_id=_uid)
        elif tool_name == "get_collection_papers":
            return get_collection_papers_tool(
                collection_input=params.get("collection_input", params.get("collection_name", "")),
                user_id=_uid,
            )
        else:
            return general_rag(query=params.get("query", user_message))
    except Exception as exc:
        logger.exception("Tool %s failed", tool_name)
        return {"tool": tool_name, "answer": f"Tool error: {exc}", "citations": []}
