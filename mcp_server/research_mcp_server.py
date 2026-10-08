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
    - get_collection_papers  — retrieve all papers inside a collection (by name or ID)

Deploy as a Databricks App (see app.yaml).
"""

import logging

from fastmcp import FastMCP

import research_broker as broker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("research-mcp-server")

mcp = FastMCP("research-copilot")

DEFAULT_USER_ID = 1  # demo user


def _verify_user_guard(user_id: int) -> dict | None:
    """Verify that *user_id* exists before any user-scoped action.

    Returns None if the user exists, or an error dict suitable for direct
    return from an @mcp.tool function.  This defensive guard ensures that
    every tool validates user_id independently — not just search_papers —
    even though the AGENT_SYSTEM_PROMPT instructs the agent to call
    verify_user first.
    """
    user = broker.get_user(user_id=user_id)
    if not user:
        return {
            "status": "error",
            "message": f"User {user_id} does not exist. Call verify_user first to confirm the user ID.",
        }
    return None


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
        err = _verify_user_guard(user_id)
        if err:
            return err

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
    - If an input is a topic/title and the top semantic match has similarity >= 0.7,
      uses that match.
    - If the top match has similarity < 0.7 (or no match at all), discovers new
      papers from OpenAlex, upserts the most relevant one, then summarizes it.

    Args:
        paper_inputs: List of paper IDs or paper titles/topics to summarize
        user_id: The user's numeric ID (used for topic-based search resolution)

    Returns:
        dict with an LLM-generated summary referencing each paper.
    """
    logger.info(f"summarize_papers: {paper_inputs}, user_id={user_id}")

    try:
        err = _verify_user_guard(user_id)
        if err:
            return err

        papers = []
        for item in paper_inputs:
            p = broker.find_or_fetch_paper(item, user_id=user_id)
            if p:
                papers.append(p)
            else:
                logger.warning(f"Could not resolve paper input: '{item}'")

        if not papers:
            return {"status": "error", "message": "No papers found for the given inputs."}

        context = "\n\n".join(
            f"[{p['title']}]\nAbstract: {(p.get('abstract', 'N/A') or 'N/A')}"
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
def compare_papers(paper_input_1: str, paper_input_2: str, user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Compare two papers side by side, highlighting similarities and differences.

    Accepts either OpenAlex paper IDs (e.g., "W2741809807") or paper titles/topics.

    Resolution logic:
    - If an input is a paper ID and exists in the knowledge base, uses it directly.
    - If an input is a paper ID but NOT in the knowledge base, fetches it from
      OpenAlex and upserts it into the papers table before comparing.
    - If an input is a topic/title and the top semantic match has similarity >= 0.7,
      uses that match.
    - If the top match has similarity < 0.7 (or no match at all), discovers new
      papers from OpenAlex, upserts the most relevant one, then uses it.

    Args:
        paper_input_1: First paper's OpenAlex ID or title/topic
        paper_input_2: Second paper's OpenAlex ID or title/topic
        user_id: The user's numeric ID (used for topic-based search resolution)

    Returns:
        dict with an LLM comparison of methodology, findings, and contributions.
    """
    logger.info(f"compare_papers: {paper_input_1} vs {paper_input_2}, user_id={user_id}")

    try:
        err = _verify_user_guard(user_id)
        if err:
            return err

        p1 = broker.find_or_fetch_paper(paper_input_1, user_id=user_id)
        p2 = broker.find_or_fetch_paper(paper_input_2, user_id=user_id)

        if not p1 or not p2:
            missing = [inp for inp, p in [(paper_input_1, p1), (paper_input_2, p2)] if not p]
            return {"status": "error", "message": f"Could not resolve paper(s): {missing}"}

        prompt = (
            "Compare the following two academic papers. Discuss their:\n"
            "1. Research objectives and scope\n"
            "2. Methodology and approach\n"
            "3. Key findings and contributions\n"
            "4. Similarities and differences\n\n"
            f"Paper 1: [{p1['title']}]\nAbstract: {(p1.get('abstract', 'N/A') or 'N/A')}\n\n"
            f"Paper 2: [{p2['title']}]\nAbstract: {(p2.get('abstract', 'N/A') or 'N/A')}\n\n"
            "Comparison:"
        )

        comparison = broker.call_llm(prompt, max_tokens=1200)

        return {
            "status": "success",
            "message": f"Compared '{p1['title']}' with '{p2['title']}'",
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
def generate_study_plan(topic: str, num_papers: int = 5, user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Generate a sequenced reading plan for a research topic.

    Finds relevant papers via semantic search and uses an LLM to order them
    from foundational to advanced, creating a structured learning path.

    Resolution logic:
    - Searches the knowledge base via semantic search and keeps only papers
      with similarity >= 0.7.
    - If no papers meet the threshold (topic not well-represented in the index),
      discovers new papers from OpenAlex, upserts them into the papers table,
      and uses those for the study plan.

    Args:
        topic: The research topic or learning goal (e.g., "understanding attention mechanisms")
        num_papers: The least number of papers to include in the plan (3-20, default 5)
        user_id: The user's numeric ID (for semantic search with learning goal matching)

    Returns:
        dict with an ordered reading plan with rationale for each paper.
    """
    logger.info(f"generate_study_plan: topic='{topic}', num_papers={num_papers}, user_id={user_id}")
    num_papers = max(3, min(20, num_papers))

    try:
        err = _verify_user_guard(user_id)
        if err:
            return err

        # Search the knowledge base for relevant papers
        papers = broker.search_papers_in_db(topic, limit=num_papers * 2, user_id=user_id)

        # Filter to only papers with similarity >= 0.7
        papers = [p for p in papers if p.get("similarity", 0) >= 0.7]

        # If no papers meet the threshold, discover from OpenAlex
        if (not papers) or (len(papers) < num_papers):
            logger.info(
                f"Only {len(papers)} paper(s) with similarity >= 0.7 for '{topic}', "
                "discovering more from OpenAlex..."
            )
            needed = num_papers - len(papers)
            oa_results = broker.openalex_search(topic, limit=needed * 3)
            if not oa_results and not papers:
                return {"status": "error", "message": f"No papers found for '{topic}' in the knowledge base or on OpenAlex."}
            # Upsert discovered relevant papers into the knowledge base
            if papers:
                existing_ids = {p['paper_id'] for p in papers}
            else:
                existing_ids = set()

            upsert_count = 0
            for p in oa_results:
                if p['paper_id'] not in existing_ids:
                    broker.upsert_paper(p)
                    papers.append(p)
                    existing_ids.add(p['paper_id'])
                    upsert_count += 1
                if len(papers) >= num_papers:
                    break

            logger.info(f"Discovered and upserted {upsert_count} papers from OpenAlex for '{topic}'")

        if not papers:
            return {"status": "error", "message": f"No relevant papers found for '{topic}' in the knowledge base or on OpenAlex."}

        paper_list = "\n".join(
            f"{i+1}. [{p['title']}] (Cited by: {p.get('cited_by_count', 0)}, "
            f"Date: {p.get('publication_date', 'N/A')}, Relevance: {p.get('similarity', 0)})\n"
            f"Abstract: {(p.get('abstract', 'N/A') or 'N/A')}\n"
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
def add_to_collection(collection_name: str, paper_input: str, user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Add a paper to a user's collection. This is a WRITE action.

    Accepts either an OpenAlex paper ID (e.g., "W2741809807") or a paper
    title/topic.  Uses the same resolution logic as summarize_papers and
    compare_papers to find or fetch the paper before adding it.

    Looks up the collection by name for the given user. If the collection
    does not exist, returns an error and does not proceed.

    Args:
        collection_name: The name of the collection (e.g., "AI Applications")
        paper_input: The paper's OpenAlex ID or title/topic
        user_id: The user's numeric ID (default 1). Ask the user for their ID if unclear.

    Returns:
        dict confirming the paper was added.
    """
    logger.info(f"add_to_collection: collection_name='{collection_name}', paper_input='{paper_input}', user_id={user_id}")
    try:
        err = _verify_user_guard(user_id)
        if err:
            return err

        collection = broker.get_collection_by_name(collection_name, user_id)
        if not collection:
            return {"status": "error", "message": f"No collection named '{collection_name}' found."}

        paper = broker.find_or_fetch_paper(paper_input, user_id=user_id)
        if not paper:
            return {"status": "error", "message": f"Could not resolve paper: '{paper_input}'"}

        result = broker.add_paper_to_collection(collection["collection_id"], paper["paper_id"])

        # Seed a "not_started" reading-progress record so the paper appears
        # in the user's reading pipeline.  ON CONFLICT DO NOTHING ensures we
        # never downgrade an existing 'reading' or 'completed' status.
        broker.seed_reading_progress(user_id, paper["paper_id"])

        result["data"] = {"paper_id": paper["paper_id"], "title": paper["title"]}
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
        err = _verify_user_guard(user_id)
        if err:
            return err

        result = broker.create_collection(name, description, user_id)
        return result
    except Exception as e:
        logger.exception("create_collection failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def update_reading_progress(paper_input: str, status: str = "reading", user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Update reading progress for a paper. This is a WRITE action.

    Accepts either an OpenAlex paper ID (e.g., "W2741809807") or a paper
    title/topic.  Uses the same resolution logic as other tools to find or
    fetch the paper before updating progress.

    Args:
        paper_input: The paper's OpenAlex ID or title/topic
        status: One of 'not_started', 'reading', or 'completed'
        user_id: The user's numeric ID (default 1). Ask the user for their ID if unclear.

    Returns:
        dict confirming the progress update.
    """
    logger.info(f"update_reading_progress: paper_input='{paper_input}', status={status}, user_id={user_id}")
    if status not in ("not_started", "reading", "completed"):
        return {"status": "error", "message": f"Invalid status '{status}'. Use: not_started, reading, completed."}
    try:
        err = _verify_user_guard(user_id)
        if err:
            return err

        paper = broker.find_or_fetch_paper(paper_input, user_id=user_id)
        if not paper:
            return {"status": "error", "message": f"Could not resolve paper: '{paper_input}'"}

        result = broker.update_reading_progress(user_id, paper["paper_id"], status)
        result["data"] = {"paper_id": paper["paper_id"], "title": paper["title"]}

        # Proactive follow-up: when a paper is marked completed, recommend
        # the next paper to read so the user gets an immediate suggestion.
        if status == "completed":
            try:
                rec = recommend_next_paper(topic=None, user_id=user_id)
                if rec.get("status") == "success" and rec.get("data", {}).get("recommendation"):
                    result["data"]["next_recommendation"] = rec["data"]["recommendation"]
                    result["data"]["recommendation_candidates"] = rec["data"].get("candidates", [])
                    result["message"] += " See data.next_recommendation for your suggested next read."
            except Exception as rec_exc:
                logger.warning("Chained recommend_next_paper failed: %s", rec_exc)

        return result
    except Exception as e:
        logger.exception("update_reading_progress failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def get_reading_progress(user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Retrieve a user's reading progress history.

    Returns all papers the user has tracked, grouped by status
    (not_started, reading, completed).

    Args:
        user_id: The user's numeric ID (default 1). Ask the user for their ID if unclear.

    Returns:
        dict with reading progress records including paper titles and statuses.
    """
    logger.info(f"get_reading_progress: user_id={user_id}")
    try:
        err = _verify_user_guard(user_id)
        if err:
            return err

        progress = broker.get_reading_progress(user_id)
        return {
            "status": "success",
            "message": f"Found {len(progress)} progress record(s) for user {user_id}.",
            "data": {"progress": progress, "count": len(progress)},
        }
    except Exception as e:
        logger.exception("get_reading_progress failed")
        return {"status": "error", "message": str(e)}


@mcp.tool
def recommend_next_paper(topic: str, user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Recommend the next paper to read based on a generated study plan.

    Generates a study plan for the topic (foundational → advanced), checks
    the user's reading history, and recommends the next unread paper in the
    learning sequence.  If all papers in the plan have already been read,
    discovers additional papers from OpenAlex as a fallback.

    Args:
        topic: The research topic to focus recommendations on
        user_id: The user's numeric ID (default 1). Ask the user for their ID if unclear.

    Returns:
        dict with recommended paper and reasoning based on the study plan.
    """
    logger.info(f"recommend_next_paper: topic='{topic}', user_id={user_id}")

    try:
        err = _verify_user_guard(user_id)
        if err:
            return err

        # Get reading history to know which papers are already read
        progress = broker.get_reading_progress(user_id)
        completed_ids = {p["paper_id"] for p in progress if p["status"] == "completed"}
        reading_ids = {p["paper_id"] for p in progress if p["status"] == "reading"}
        read_ids = completed_ids | reading_ids

        # Generate a study plan to get a structured learning sequence
        plan_result = generate_study_plan(topic, num_papers=10, user_id=user_id)

        if plan_result["status"] != "success":
            return plan_result  # propagate error (e.g., no papers found)

        study_plan = plan_result["data"]["study_plan"]
        plan_papers = plan_result["data"]["papers_included"]

        # Split plan papers into read and unread
        already_read = [p for p in plan_papers if p["paper_id"] in read_ids]
        unread = [p for p in plan_papers if p["paper_id"] not in read_ids]

        if unread:
            # Ask LLM to pick the next paper based on the study plan sequence
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

            recommendation = broker.call_llm(prompt, max_tokens=800)

            return {
                "status": "success",
                "message": "Recommendation generated from study plan.",
                "data": {
                    "recommendation": recommendation,
                    "study_plan": study_plan,
                    "candidates": unread,
                    "already_read": [
                        p for p in already_read if p["paper_id"] in completed_ids
                    ],
                    "currently_reading": [
                        p for p in already_read if p["paper_id"] in reading_ids
                    ],
                },
            }

        # -----------------------------------------------------------
        # All plan papers already read — fall back to OpenAlex for
        # new papers that build on the completed study plan.
        # -----------------------------------------------------------
        logger.info(
            f"All {len(plan_papers)} plan papers already read for '{topic}', "
            "discovering new papers from OpenAlex..."
        )
        oa_results = broker.openalex_search(topic, limit=10)
        new_candidates = [p for p in oa_results if p["paper_id"] not in read_ids]

        if not new_candidates:
            return {
                "status": "success",
                "message": "All study plan papers and available papers have been read.",
                "data": {"recommendation": None, "study_plan": study_plan},
            }

        for p in new_candidates:
            broker.upsert_paper(p)

        candidate_list = "\n".join(
            f"- [{p['title']}] (Citations: {p.get('cited_by_count', 0)})\n"
            f"  Abstract: {(p.get('abstract', 'N/A') or 'N/A')}\n"
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

        recommendation = broker.call_llm(prompt, max_tokens=800)

        return {
            "status": "success",
            "message": "Recommendation generated (study plan completed, new papers discovered).",
            "data": {
                "recommendation": recommendation,
                "study_plan": study_plan,
                "candidates": [
                    {"paper_id": p["paper_id"], "title": p["title"]}
                    for p in new_candidates[:8]
                ],
                "plan_completed": True,
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
        err = _verify_user_guard(user_id)
        if err:
            return err

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
        err = _verify_user_guard(user_id)
        if err:
            return err

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
        err = _verify_user_guard(user_id)
        if err:
            return err

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
def get_collection_papers(collection_input: str, user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Retrieve all papers in a specific collection.

    Accepts either a collection name (e.g., "Machine Learning") or a numeric
    collection ID.  When a name is provided, looks it up for the given user.

    Args:
        collection_input: The collection's name or numeric ID
        user_id: The user's numeric ID (default 1). Required when looking up by name.

    Returns:
        dict with list of papers in the collection.
    """
    logger.info(f"get_collection_papers: collection_input='{collection_input}', user_id={user_id}")
    try:
        err = _verify_user_guard(user_id)
        if err:
            return err

        # Resolve as numeric ID or look up by name
        if collection_input.isdigit():
            collection_id = int(collection_input)
        else:
            collection = broker.get_collection_by_name(collection_input, user_id)
            if not collection:
                return {"status": "error", "message": f"No collection named '{collection_input}' found for user {user_id}."}
            collection_id = collection["collection_id"]

        papers = broker.get_collection_papers(collection_id)
        return {
            "status": "success",
            "message": f"Found {len(papers)} paper(s) in collection '{collection_input}'.",
            "data": {"papers": papers, "count": len(papers)},
        }
    except Exception as e:
        logger.exception("get_collection_papers failed")
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
