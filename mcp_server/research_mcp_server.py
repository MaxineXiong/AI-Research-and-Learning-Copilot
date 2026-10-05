"""
Research Copilot MCP Server.

Exposes research tools over MCP (Model Context Protocol) so a Databricks
Agent Bricks agent can help users with:
    - search_papers          — find papers matching a query or learning goal
    - summarize_papers       — summarize one or more papers with citations
    - compare_papers         — compare two papers side by side
    - generate_study_plan    — create a sequenced reading plan
    - add_to_collection      — add a paper to a user's collection (write action)
    - create_collection      — create a new paper collection (write action)
    - update_reading_progress— mark a paper as reading/completed (write action)
    - get_reading_progress   — retrieve a user's reading progress history
    - recommend_next_paper   — suggest the next paper to read
    - verify_user            — check that a user_id exists before user-scoped actions
    - create_learning_goal  — add a new learning goal and embed it (write action)
    - get_learning_goals     — retrieve all learning goals for a user
    - get_collections        — retrieve all collections for a user
    - get_collection_papers  — retrieve all papers inside a collection

Deploy as a Databricks App (see app.yaml).
"""

import logging
import re
from typing import Optional

from fastmcp import FastMCP

import research_broker as broker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("research-mcp-server")

mcp = FastMCP("research-copilot")

DEFAULT_USER_ID = 1  # demo user

# OpenAlex paper IDs: W followed by 5+ digits (e.g., W2741809807)
_OPENALEX_ID_RE = re.compile(r'^W\d{5,}$')


@mcp.tool
def search_papers(query: str, user_id: int, mode: str = "semantic", limit: int = 10) -> dict:
    """
    Search for academic papers matching a query or learning goal.

    Requires a valid user_id — the tool verifies the user exists before searching.
    If the user does not exist, returns an error and does not proceed.

    Always searches the indexed knowledge base first. Falls back to the live
    OpenAlex API (and upserts discovered papers) when:
    - The knowledge base has no matching papers, or
    - The top semantic result has similarity < 0.6, or
    - The caller explicitly requests mode="openalex".

    Args:
        query: Natural-language search query (e.g., "attention mechanisms in transformers")
        user_id: The user's numeric ID (required). Must exist in the users table.
        mode:  "semantic" (search indexed embeddings) or "openalex" (discover new papers)
        limit: Max papers to return (1-50, default 10)

    Returns:
        dict with status and list of matching papers with titles, abstracts, citations.
    """
    logger.info(f"search_papers: query='{query}', user_id={user_id}, mode={mode}, limit={limit}")
    limit = max(1, min(50, limit))

    try:
        # Verify user exists before proceeding
        user = broker.get_user(user_id=user_id)
        if not user:
            return {"status": "error", "message": f"User {user_id} does not exist. Cannot proceed with search."}

        # Search the indexed knowledge base
        papers = broker.search_papers_in_db(query, limit=limit, user_id=user_id)

        # Fall back to OpenAlex if DB has no good match or caller requests openalex mode
        needs_openalex = (not papers) or (papers[0]['similarity'] < 0.6) or (mode == "openalex")
        if needs_openalex:
            papers = broker.openalex_search(query, limit=limit)
            for paper in papers:
                broker.upsert_paper(paper)

        return {
            "status": "success",
            "message": f"Found {len(papers)} papers for '{query}' ({mode} search).",
            "data": {"papers": papers, "count": len(papers)},
        }
    except Exception as e:
        logger.exception("search_papers failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def summarize_papers(paper_inputs: list[str], user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Summarize one or more papers using their abstracts and indexed content.

    Accepts either OpenAlex paper IDs (e.g., "W2741809807") or paper titles/topics.

    Resolution logic:
    - If an input is a paper ID and exists in the knowledge base, uses it directly.
    - If an input is a paper ID but NOT in the knowledge base, fetches it from
      OpenAlex and upserts it into the papers table before summarizing.
    - If an input is a topic/title and the top semantic match has similarity >= 0.6,
      uses that match.
    - If the top match has similarity < 0.6 (or no match at all), discovers new
      papers from OpenAlex, upserts the most relevant one, then summarizes it.

    Args:
        paper_inputs: List of paper IDs or paper titles/topics to summarize
        user_id: The user's numeric ID (used for topic-based search resolution)

    Returns:
        dict with an LLM-generated summary referencing each paper.
    """
    logger.info(f"summarize_papers: {paper_inputs}, user_id={user_id}")

    try:
        papers = []
        for item in paper_inputs:
            if _OPENALEX_ID_RE.match(item):
                # Input is an OpenAlex paper ID -- look it up directly
                p = broker.get_paper(item)
                if p:
                    papers.append(p)
                else:
                    # Paper ID not in DB -- fetch from OpenAlex and upsert
                    logger.info(f"Paper ID '{item}' not in DB, fetching from OpenAlex...")
                    oa_paper = broker.openalex_fetch_by_id(item)
                    if oa_paper:
                        broker.upsert_paper(oa_paper)
                        p = broker.get_paper(item)
                        if p:
                            papers.append(p)
                            logger.info(f"Fetched and upserted '{p['title']}' from OpenAlex")
                        else:
                            logger.warning(f"Upsert succeeded but paper '{item}' still not found in DB")
                    else:
                        logger.warning(f"Paper ID '{item}' not found on OpenAlex either")
            else:
                # Input is a topic or title -- search for the most relevant paper
                logger.info(f"Input '{item}' is not a paper ID, searching for matching paper...")
                results = broker.search_papers_in_db(item, limit=1, user_id=user_id)
                if results and results[0].get("similarity", 0) >= 0.6:
                    papers.append(results[0])
                    logger.info(f"Resolved '{item}' to '{results[0]['title']}' (similarity: {results[0]['similarity']:.3f})")
                else:
                    # No good match in DB (similarity < 0.6 or no results)
                    # Discover from OpenAlex and upsert
                    sim = results[0].get("similarity", 0) if results else 0
                    logger.info(f"Low similarity ({sim:.3f}) or no results for '{item}', discovering from OpenAlex...")
                    oa_results = broker.openalex_search(item, limit=1)
                    if oa_results:
                        broker.upsert_paper(oa_results[0])
                        p = broker.get_paper(oa_results[0]["paper_id"])
                        if p:
                            papers.append(p)
                            logger.info(f"Discovered and upserted '{p['title']}' from OpenAlex for '{item}'")
                        else:
                            logger.warning(f"OpenAlex discovery succeeded but paper still not in DB")
                    else:
                        logger.warning(f"No paper found on OpenAlex for '{item}'")

        if not papers:
            return {"status": "error", "message": "No papers found for the given inputs."}

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
def generate_study_plan(topic: str, num_papers: int = 8, user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Generate a sequenced reading plan for a research topic.

    Finds relevant papers via semantic search and uses an LLM to order them
    from foundational to advanced, creating a structured learning path.

    Resolution logic:
    - Searches the knowledge base via semantic search and keeps only papers
      with similarity >= 0.6.
    - If no papers meet the threshold (topic not well-represented in the index),
      discovers new papers from OpenAlex, upserts them into the papers table,
      and uses those for the study plan.

    Args:
        topic: The research topic or learning goal (e.g., "understanding attention mechanisms")
        num_papers: Number of papers to include in the plan (3-20, default 8)
        user_id: The user's numeric ID (for semantic search with learning goal matching)

    Returns:
        dict with an ordered reading plan with rationale for each paper.
    """
    logger.info(f"generate_study_plan: topic='{topic}', num_papers={num_papers}, user_id={user_id}")
    num_papers = max(3, min(20, num_papers))

    try:
        # Search the knowledge base for relevant papers
        papers = broker.search_papers_in_db(topic, limit=num_papers, user_id=user_id)

        # Filter to only papers with similarity >= 0.6
        papers = [p for p in papers if p.get("similarity", 0) >= 0.6]

        # If no papers meet the threshold, discover from OpenAlex
        if not papers:
            logger.info(f"No papers with similarity >= 0.6 for '{topic}', discovering from OpenAlex...")
            oa_results = broker.openalex_search(topic, limit=num_papers)
            if not oa_results:
                return {"status": "error", "message": f"No papers found for '{topic}' in the knowledge base or on OpenAlex."}
            # Upsert discovered relevant papers into the knowledge base
            for p in oa_results:
                if p.get("similarity", 0) >= 0.6:
                    broker.upsert_paper(p)
                    papers.append(p)
            logger.info(f"Discovered and upserted {len(papers)} papers from OpenAlex for '{topic}'")

        if not papers:
            return {"status": "error", "message": f"No relevant papers found for '{topic}' in the knowledge base or on OpenAlex."}

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
def add_to_collection(collection_name: str, paper_id: str, user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Add a paper to a user's collection. This is a WRITE action.

    Looks up the collection by name for the given user. If the collection
    does not exist, returns an error and does not proceed.

    Args:
        collection_name: The name of the collection (e.g., "AI Applications")
        paper_id: The paper's OpenAlex ID (e.g., "W2741809807")
        user_id: The user's numeric ID (default 1). Ask the user for their ID if unclear.

    Returns:
        dict confirming the paper was added.
    """
    logger.info(f"add_to_collection: collection_name='{collection_name}', paper={paper_id}, user_id={user_id}")
    try:
        collection = broker.get_collection_by_name(collection_name, user_id)
        if not collection:
            return {"status": "error", "message": f"No collection named '{collection_name}' found."}

        result = broker.add_paper_to_collection(collection["collection_id"], paper_id)
        return result
    except Exception as e:
        logger.exception("add_to_collection failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def create_collection(name: str, description: str = "", user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Create a new paper collection for a user. This is a WRITE action.

    Args:
        name: The collection name (e.g., "AI Applications")
        description: Optional longer description of the collection
        user_id: The user's numeric ID (default 1). Ask the user for their ID if unclear.

    Returns:
        dict confirming the collection was created with its collection_id.
    """
    logger.info(f"create_collection: name='{name}', user_id={user_id}")
    try:
        result = broker.create_collection(name, description, user_id)
        return result
    except Exception as e:
        logger.exception("create_collection failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def update_reading_progress(paper_id: str, status: str = "reading", user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Update reading progress for a paper. This is a WRITE action.

    Args:
        paper_id: The paper's OpenAlex ID
        status: One of 'not_started', 'reading', or 'completed'
        user_id: The user's numeric ID (default 1). Ask the user for their ID if unclear.

    Returns:
        dict confirming the progress update.
    """
    logger.info(f"update_reading_progress: paper={paper_id}, status={status}, user_id={user_id}")
    if status not in ("not_started", "reading", "completed"):
        return {"status": "error", "message": f"Invalid status '{status}'. Use: not_started, reading, completed."}
    try:
        result = broker.update_reading_progress(user_id, paper_id, status)
        return result
    except Exception as e:
        logger.exception("update_reading_progress failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def recommend_next_paper(topic: Optional[str] = None, user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Recommend the next paper to read based on reading history and interests.

    Looks at what the user has already read and suggests unread papers
    that build on their knowledge.

    Args:
        topic: Optional topic to focus recommendations on
        user_id: The user's numeric ID (default 1). Ask the user for their ID if unclear.

    Returns:
        dict with recommended paper(s) and reasoning.
    """
    logger.info(f"recommend_next_paper: topic='{topic}', user_id={user_id}")

    try:
        # Get reading history
        progress = broker.get_reading_progress(user_id)
        completed = [p for p in progress if p["status"] == "completed"]
        reading = [p for p in progress if p["status"] == "reading"]
        read_ids = {p["paper_id"] for p in completed + reading}

        # Search for candidate papers
        search_query = topic or "recent advances in research"
        if completed:
            # Use titles of completed papers as context
            search_query = " ".join(p["title"][:50] for p in completed[:3])

        candidates = broker.search_papers_in_db(search_query, limit=15, user_id=user_id)
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


@mcp.tool
def verify_user(user_id: int = None, username: str = None) -> dict:
    """
    Verify that a user exists before performing user-scoped actions.

    Call this at the start of any conversation before using other tools.
    Pass user_id (if the user gives a number) or username (if they give a name or email).

    Args:
        user_id: The user's numeric ID (if provided as a number)
        username: The user's email or display name (if provided as a name)

    Returns:
        dict confirming the user exists with their display_name.
    """
    logger.info(f"verify_user: user_id={user_id}, username={username}")
    try:
        user = broker.get_user(user_id=user_id, username=username)
        if not user:
            return {"status": "error", "message": f"User not found. Please check your user ID or username."}
        return {
            "status": "success",
            "message": f"User '{user['display_name']}' verified.",
            "data": {"user_id": user["user_id"], "display_name": user["display_name"]},
        }
    except Exception as e:
        logger.exception("verify_user failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def create_learning_goal(title: str, description: str = "", user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Add a new learning goal for a user. This is a WRITE action.

    Inserts the goal into the learning_goals table and creates an embedding
    so it can be discovered via semantic search on future searches.

    Args:
        title: The learning goal title (e.g., "Understand transformer attention mechanisms")
        description: Optional longer description of the goal
        user_id: The user's numeric ID.

    Returns:
        dict confirming the goal was added with its goal_id.
    """
    logger.info(f"create_learning_goal: title='{title}', user_id={user_id}")
    try:
        result = broker.create_learning_goal(title, description, user_id)
        return result
    except Exception as e:
        logger.exception("create_learning_goal failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def get_learning_goals(user_id: int) -> dict:
    """
    Retrieve all learning goals for a user.

    Args:
        user_id: The user's numeric ID.

    Returns:
        dict with list of learning goals including title, description, and status.
    """
    logger.info(f"get_learning_goals: user_id={user_id}")
    try:
        goals = broker.get_learning_goals(user_id)
        return {
            "status": "success",
            "message": f"Found {len(goals)} learning goal(s) for user {user_id}.",
            "data": {"goals": goals, "count": len(goals)},
        }
    except Exception as e:
        logger.exception("get_learning_goals failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def get_collections(user_id: int) -> dict:
    """
    Retrieve all collections for a user.

    Args:
        user_id: The user's numeric ID.

    Returns:
        dict with list of collections including name, description, and created_at.
    """
    logger.info(f"get_collections: user_id={user_id}")
    try:
        collections = broker.get_collections(user_id)
        return {
            "status": "success",
            "message": f"Found {len(collections)} collection(s) for user {user_id}.",
            "data": {"collections": collections, "count": len(collections)},
        }
    except Exception as e:
        logger.exception("get_collections failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def get_collection_papers(collection_id: int) -> dict:
    """
    Retrieve all papers in a specific collection.

    Args:
        collection_id: The collection's numeric ID.

    Returns:
        dict with list of papers in the collection.
    """
    logger.info(f"get_collection_papers: collection_id={collection_id}")
    try:
        papers = broker.get_collection_papers(collection_id)
        return {
            "status": "success",
            "message": f"Found {len(papers)} paper(s) in collection {collection_id}.",
            "data": {"papers": papers, "count": len(papers)},
        }
    except Exception as e:
        logger.exception("get_collection_papers failed")
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
