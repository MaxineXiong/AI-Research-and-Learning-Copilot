"""
Research Copilot MCP Server.

Exposes research tools over MCP (Model Context Protocol) so a Databricks
Agent Bricks agent can help users with:
    - search_papers          — find papers matching a query or learning goal
    - summarize_papers       — summarize one or more papers with citations
    - compare_papers         — compare two papers side by side
    - generate_study_plan    — create a sequenced reading plan
    - add_to_collection      — add a paper to a user's collection (write action)
    - update_reading_progress— mark a paper as reading/completed (write action)
    - recommend_next_paper   — suggest the next paper to read

Deploy as a Databricks App (see app.yaml).
"""

import logging
from typing import Optional

from fastmcp import FastMCP

import research_broker as broker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("research-mcp-server")

mcp = FastMCP("research-copilot")

DEFAULT_USER_ID = 1  # demo user


@mcp.tool
def search_papers(query: str, mode: str = "semantic", limit: int = 10) -> dict:
    """
    Search for academic papers matching a query or learning goal.

    Uses semantic vector search over indexed papers or live OpenAlex API.

    Args:
        query: Natural-language search query (e.g., "attention mechanisms in transformers")
        mode:  "semantic" (search indexed embeddings) or "openalex" (discover new papers)
        limit: Max papers to return (1-50, default 10)

    Returns:
        dict with status and list of matching papers with titles, abstracts, citations.
    """
    logger.info(f"search_papers: query='{query}', mode={mode}, limit={limit}")
    limit = max(1, min(50, limit))

    try:
        if mode == "openalex":
            papers = broker.openalex_search(query, limit=limit)
        else:
            papers = broker.search_papers_in_db(query, limit=limit)

        return {
            "status": "success",
            "message": f"Found {len(papers)} papers for '{query}' ({mode} search).",
            "data": {"papers": papers, "count": len(papers)},
        }
    except Exception as e:
        logger.exception("search_papers failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def summarize_papers(paper_ids: list[str]) -> dict:
    """
    Summarize one or more papers using their abstracts and indexed content.

    Retrieves paper content from the knowledge base and generates an LLM summary
    with citations.

    Args:
        paper_ids: List of paper IDs to summarize (e.g., ["W2741809807", "W3177318507"])

    Returns:
        dict with an LLM-generated summary referencing each paper.
    """
    logger.info(f"summarize_papers: {paper_ids}")

    try:
        papers = []
        for pid in paper_ids:
            p = broker.get_paper(pid)
            if p:
                papers.append(p)

        if not papers:
            return {"status": "error", "message": "No papers found for the given IDs."}

        context = "\n\n".join(
            f"[{p['title']}]\nAbstract: {(p.get('abstract') or 'N/A')[:600]}"
            for p in papers
        )

        prompt = (
            "Summarize the following academic papers. For each paper, highlight the "
            "key contributions, methods, and findings. Cite papers by their titles "
            "in square brackets.\n\n"
            f"Papers:\n{context}\n\nSummary:"
        )

        summary = broker.call_llm(prompt)

        return {
            "status": "success",
            "message": f"Summarized {len(papers)} paper(s).",
            "data": {
                "summary": summary,
                "papers_summarized": [{"paper_id": p["paper_id"], "title": p["title"]} for p in papers],
            },
        }
    except Exception as e:
        logger.exception("summarize_papers failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def compare_papers(paper_id_1: str, paper_id_2: str) -> dict:
    """
    Compare two papers side by side, highlighting similarities and differences.

    Args:
        paper_id_1: First paper's OpenAlex ID (e.g., "W2741809807")
        paper_id_2: Second paper's OpenAlex ID

    Returns:
        dict with an LLM comparison of methodology, findings, and contributions.
    """
    logger.info(f"compare_papers: {paper_id_1} vs {paper_id_2}")

    try:
        p1 = broker.get_paper(paper_id_1)
        p2 = broker.get_paper(paper_id_2)

        if not p1 or not p2:
            missing = [pid for pid, p in [(paper_id_1, p1), (paper_id_2, p2)] if not p]
            return {"status": "error", "message": f"Paper(s) not found: {missing}"}

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

        comparison = broker.call_llm(prompt, max_tokens=1000)

        return {
            "status": "success",
            "message": f"Compared '{p1['title'][:50]}...' with '{p2['title'][:50]}...'",
            "data": {
                "comparison": comparison,
                "paper_1": {"paper_id": p1["paper_id"], "title": p1["title"]},
                "paper_2": {"paper_id": p2["paper_id"], "title": p2["title"]},
            },
        }
    except Exception as e:
        logger.exception("compare_papers failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def generate_study_plan(topic: str, num_papers: int = 8) -> dict:
    """
    Generate a sequenced reading plan for a research topic.

    Finds relevant papers via semantic search and uses an LLM to order them
    from foundational to advanced, creating a structured learning path.

    Args:
        topic: The research topic or learning goal (e.g., "understanding attention mechanisms")
        num_papers: Number of papers to include in the plan (3-20, default 8)

    Returns:
        dict with an ordered reading plan with rationale for each paper.
    """
    logger.info(f"generate_study_plan: topic='{topic}', num_papers={num_papers}")
    num_papers = max(3, min(20, num_papers))

    try:
        papers = broker.search_papers_in_db(topic, limit=num_papers)

        if not papers:
            return {"status": "error", "message": f"No indexed papers found for '{topic}'. Try running an OpenAlex search first."}

        paper_list = "\n".join(
            f"{i+1}. [{p['title']}] (Cited by: {p.get('cited_by_count', 0)}, "
            f"Date: {p.get('publication_date', 'N/A')})\n"
            f"   Abstract: {(p.get('abstract') or 'N/A')[:300]}"
            for i, p in enumerate(papers)
        )

        prompt = (
            f"Create a structured study plan for learning about: \"{topic}\"\n\n"
            f"Available papers:\n{paper_list}\n\n"
            "Create a sequenced reading plan that:\n"
            "1. Starts with foundational/survey papers\n"
            "2. Progresses to specialized research\n"
            "3. Ends with cutting-edge or advanced work\n"
            "4. Explains WHY each paper should be read in that position\n"
            "5. Estimates reading time (short/medium/long)\n\n"
            "Format each entry as: Order #. [Paper Title] - Rationale\n"
            "Study Plan:"
        )

        plan = broker.call_llm(prompt, max_tokens=1200)

        return {
            "status": "success",
            "message": f"Study plan created for '{topic}' with {len(papers)} papers.",
            "data": {
                "study_plan": plan,
                "topic": topic,
                "papers_included": [{"paper_id": p["paper_id"], "title": p["title"]} for p in papers],
            },
        }
    except Exception as e:
        logger.exception("generate_study_plan failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def add_to_collection(collection_id: int, paper_id: str) -> dict:
    """
    Add a paper to a user's collection. This is a WRITE action.

    Args:
        collection_id: The collection's numeric ID
        paper_id: The paper's OpenAlex ID (e.g., "W2741809807")

    Returns:
        dict confirming the paper was added.
    """
    logger.info(f"add_to_collection: collection={collection_id}, paper={paper_id}")
    try:
        result = broker.add_paper_to_collection(collection_id, paper_id)
        return result
    except Exception as e:
        logger.exception("add_to_collection failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def update_reading_progress(paper_id: str, status: str = "reading") -> dict:
    """
    Update reading progress for a paper. This is a WRITE action.

    Args:
        paper_id: The paper's OpenAlex ID
        status: One of 'not_started', 'reading', or 'completed'

    Returns:
        dict confirming the progress update.
    """
    logger.info(f"update_reading_progress: paper={paper_id}, status={status}")
    if status not in ("not_started", "reading", "completed"):
        return {"status": "error", "message": f"Invalid status '{status}'. Use: not_started, reading, completed."}
    try:
        result = broker.update_reading_progress(DEFAULT_USER_ID, paper_id, status)
        return result
    except Exception as e:
        logger.exception("update_reading_progress failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def recommend_next_paper(topic: Optional[str] = None) -> dict:
    """
    Recommend the next paper to read based on reading history and interests.

    Looks at what the user has already read and suggests unread papers
    that build on their knowledge.

    Args:
        topic: Optional topic to focus recommendations on

    Returns:
        dict with recommended paper(s) and reasoning.
    """
    logger.info(f"recommend_next_paper: topic='{topic}'")

    try:
        # Get reading history
        progress = broker.get_reading_progress(DEFAULT_USER_ID)
        completed = [p for p in progress if p["status"] == "completed"]
        reading = [p for p in progress if p["status"] == "reading"]
        read_ids = {p["paper_id"] for p in completed + reading}

        # Search for candidate papers
        search_query = topic or "recent advances in research"
        if completed:
            # Use titles of completed papers as context
            search_query = " ".join(p["title"][:50] for p in completed[:3])

        candidates = broker.search_papers_in_db(search_query, limit=15)
        unread = [p for p in candidates if p["paper_id"] not in read_ids]

        if not unread:
            return {
                "status": "success",
                "message": "No unread papers found. Try searching for new papers first.",
                "data": {"recommendation": None},
            }

        # Use LLM to pick the best next paper
        read_titles = [p["title"] for p in completed[:5]]
        candidate_list = "\n".join(
            f"- [{p['title']}] (Citations: {p.get('cited_by_count', 0)})"
            for p in unread[:8]
        )

        prompt = (
            "Based on the papers the student has already read, recommend the SINGLE "
            "best next paper to read and explain why.\n\n"
            f"Papers already read: {read_titles if read_titles else 'None yet'}\n\n"
            f"Candidate papers (unread):\n{candidate_list}\n\n"
            "Recommend ONE paper and explain why it's the best next read:"
        )

        recommendation = broker.call_llm(prompt, max_tokens=400)

        return {
            "status": "success",
            "message": "Recommendation generated.",
            "data": {
                "recommendation": recommendation,
                "candidates": [{"paper_id": p["paper_id"], "title": p["title"]} for p in unread[:5]],
                "already_read": len(completed),
                "currently_reading": len(reading),
            },
        }
    except Exception as e:
        logger.exception("recommend_next_paper failed")
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
