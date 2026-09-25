"""
AI Research & Learning Copilot — Flask Application

Serves the frontend UI for managing learning goals, discovering papers,
building collections, tracking reading progress, and chatting with the
AI research agent.
"""

import json
import logging
import os

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

import lakebase
from openalex_client import OpenAlexClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("research-copilot")

app = Flask(__name__)
app.secret_key = os.urandom(24)

DEFAULT_USER_ID = 1  # demo user

# ---------------------------------------------------------------------------
# Shared helpers — delegated to research_tools module
# ---------------------------------------------------------------------------
import research_tools

_vector_search = research_tools.vector_search


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Dashboard: goals overview, recent papers, collections."""
    goals = lakebase.run_query(
        "SELECT * FROM learning_goals WHERE user_id = %s ORDER BY created_at DESC LIMIT 5",
        (DEFAULT_USER_ID,),
    )
    collections = lakebase.run_query(
        "SELECT c.*, (SELECT COUNT(*) FROM collection_papers cp WHERE cp.collection_id = c.collection_id) AS paper_count "
        "FROM collections c WHERE c.user_id = %s ORDER BY c.created_at DESC LIMIT 5",
        (DEFAULT_USER_ID,),
    )
    stats = lakebase.run_query_one("""
        SELECT
            (SELECT COUNT(*) FROM papers) AS total_papers,
            (SELECT COUNT(*) FROM authors) AS total_authors,
            (SELECT COUNT(*) FROM embeddings) AS total_embeddings,
            (SELECT COUNT(*) FROM reading_progress WHERE user_id = %s AND status = 'completed') AS papers_read
    """, (DEFAULT_USER_ID,))
    return render_template("index.html", goals=goals, collections=collections, stats=stats or {})


# ---------------------------------------------------------------------------
# Routes — Learning Goals
# ---------------------------------------------------------------------------
@app.route("/goals")
def goals_page():
    goals = lakebase.run_query(
        "SELECT * FROM learning_goals WHERE user_id = %s ORDER BY created_at DESC",
        (DEFAULT_USER_ID,),
    )
    return render_template("goals.html", goals=goals)


@app.route("/goals/create", methods=["POST"])
def create_goal():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    if not title:
        flash("Title is required.", "error")
        return redirect(url_for("goals_page"))
    lakebase.run_returning(
        "INSERT INTO learning_goals (user_id, title, description) VALUES (%s, %s, %s) RETURNING goal_id",
        (DEFAULT_USER_ID, title, description),
    )
    flash(f"Goal '{title}' created!", "success")
    return redirect(url_for("goals_page"))


@app.route("/goals/<int:goal_id>/delete", methods=["POST"])
def delete_goal(goal_id):
    lakebase.run_write("DELETE FROM learning_goals WHERE goal_id = %s AND user_id = %s", (goal_id, DEFAULT_USER_ID))
    flash("Goal deleted.", "success")
    return redirect(url_for("goals_page"))


# ---------------------------------------------------------------------------
# Routes — Paper Search
# ---------------------------------------------------------------------------
@app.route("/search")
def search_page():
    return render_template("search.html", results=None, query="")


@app.route("/search/results")
def search_results():
    query = request.args.get("q", "").strip()
    mode = request.args.get("mode", "semantic")  # 'semantic' or 'openalex'
    if not query:
        flash("Please enter a search query.", "error")
        return redirect(url_for("search_page"))

    results = []
    if mode == "semantic":
        # Vector search over stored embeddings
        hits = _vector_search(query, top_k=15, source_type="abstract")
        paper_ids = list({h["source_id"] for h in hits})
        if paper_ids:
            placeholders = ",".join(["%s"] * len(paper_ids))
            results = lakebase.run_query(
                f"SELECT * FROM papers WHERE paper_id IN ({placeholders}) ORDER BY cited_by_count DESC",
                tuple(paper_ids),
            )
    else:
        # Live OpenAlex search
        client = OpenAlexClient()
        openalex_results = client.search_papers_for_goal(query, limit=20)
        # Upsert discovered papers into Lakebase for future use
        for paper in openalex_results:
            _upsert_paper_from_openalex(paper)
        paper_ids = [p["paper_id"] for p in openalex_results]
        if paper_ids:
            placeholders = ",".join(["%s"] * len(paper_ids))
            results = lakebase.run_query(
                f"SELECT * FROM papers WHERE paper_id IN ({placeholders}) ORDER BY cited_by_count DESC",
                tuple(paper_ids),
            )

    return render_template("search.html", results=results, query=query, mode=mode)


_upsert_paper_from_openalex = research_tools.upsert_paper


# ---------------------------------------------------------------------------
# Routes — Collections
# ---------------------------------------------------------------------------
@app.route("/collections")
def collections_page():
    collections = lakebase.run_query(
        "SELECT c.*, (SELECT COUNT(*) FROM collection_papers cp WHERE cp.collection_id = c.collection_id) AS paper_count "
        "FROM collections c WHERE c.user_id = %s ORDER BY c.created_at DESC",
        (DEFAULT_USER_ID,),
    )
    return render_template("collections.html", collections=collections)


@app.route("/collections/create", methods=["POST"])
def create_collection():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name:
        flash("Collection name is required.", "error")
        return redirect(url_for("collections_page"))
    lakebase.run_returning(
        "INSERT INTO collections (user_id, name, description) VALUES (%s, %s, %s) RETURNING collection_id",
        (DEFAULT_USER_ID, name, description),
    )
    flash(f"Collection '{name}' created!", "success")
    return redirect(url_for("collections_page"))


@app.route("/collections/<int:collection_id>")
def collection_detail(collection_id):
    collection = lakebase.run_query_one(
        "SELECT * FROM collections WHERE collection_id = %s AND user_id = %s",
        (collection_id, DEFAULT_USER_ID),
    )
    if not collection:
        flash("Collection not found.", "error")
        return redirect(url_for("collections_page"))
    papers = lakebase.run_query(
        "SELECT p.*, cp.added_at, cp.notes AS collection_note "
        "FROM collection_papers cp JOIN papers p ON cp.paper_id = p.paper_id "
        "WHERE cp.collection_id = %s ORDER BY cp.added_at DESC",
        (collection_id,),
    )
    return render_template("collection.html", collection=collection, papers=papers)


@app.route("/collections/<int:collection_id>/add_paper", methods=["POST"])
def add_paper_to_collection(collection_id):
    paper_id = request.form.get("paper_id", "").strip()
    if not paper_id:
        flash("Paper ID required.", "error")
        return redirect(url_for("collection_detail", collection_id=collection_id))
    lakebase.run_write(
        "INSERT INTO collection_papers (collection_id, paper_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (collection_id, paper_id),
    )
    flash("Paper added to collection.", "success")
    return redirect(request.referrer or url_for("collection_detail", collection_id=collection_id))


@app.route("/collections/<int:collection_id>/remove_paper", methods=["POST"])
def remove_paper_from_collection(collection_id):
    paper_id = request.form.get("paper_id", "").strip()
    lakebase.run_write(
        "DELETE FROM collection_papers WHERE collection_id = %s AND paper_id = %s",
        (collection_id, paper_id),
    )
    flash("Paper removed from collection.", "success")
    return redirect(url_for("collection_detail", collection_id=collection_id))


# ---------------------------------------------------------------------------
# Routes — Paper Detail
# ---------------------------------------------------------------------------
@app.route("/paper/<paper_id>")
def paper_detail(paper_id):
    paper = lakebase.run_query_one("SELECT * FROM papers WHERE paper_id = %s", (paper_id,))
    if not paper:
        flash("Paper not found.", "error")
        return redirect(url_for("index"))
    authors = lakebase.run_query(
        "SELECT a.* FROM authors a JOIN paper_authors pa ON a.author_id = pa.author_id "
        "WHERE pa.paper_id = %s ORDER BY pa.position", (paper_id,)
    )
    notes = lakebase.run_query(
        "SELECT * FROM notes WHERE paper_id = %s AND user_id = %s ORDER BY created_at DESC",
        (paper_id, DEFAULT_USER_ID),
    )
    progress = lakebase.run_query_one(
        "SELECT * FROM reading_progress WHERE paper_id = %s AND user_id = %s",
        (paper_id, DEFAULT_USER_ID),
    )
    collections = lakebase.run_query(
        "SELECT * FROM collections WHERE user_id = %s ORDER BY name", (DEFAULT_USER_ID,)
    )
    return render_template(
        "paper.html", paper=paper, authors=authors, notes=notes,
        progress=progress, collections=collections,
    )


@app.route("/paper/<paper_id>/note", methods=["POST"])
def add_note(paper_id):
    content = request.form.get("content", "").strip()
    if not content:
        flash("Note content is required.", "error")
        return redirect(url_for("paper_detail", paper_id=paper_id))
    lakebase.run_write(
        "INSERT INTO notes (user_id, paper_id, content) VALUES (%s, %s, %s)",
        (DEFAULT_USER_ID, paper_id, content),
    )
    flash("Note added!", "success")
    return redirect(url_for("paper_detail", paper_id=paper_id))


@app.route("/paper/<paper_id>/progress", methods=["POST"])
def update_progress(paper_id):
    status = request.form.get("status", "not_started")
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
    flash(f"Progress updated to: {status}", "success")
    return redirect(url_for("paper_detail", paper_id=paper_id))


# ---------------------------------------------------------------------------
# Routes — Agent Chat
# ---------------------------------------------------------------------------
@app.route("/agent")
def agent_page():
    return render_template("agent.html")


@app.route("/api/agent/chat", methods=["POST"])
def agent_chat():
    """Agent chat with LLM-based tool dispatch.

    Routes user messages to the appropriate research tool (search, summarize,
    compare, study plan, recommend, etc.) and returns a structured response
    with keys: tool, answer, citations.
    """
    body = request.json or {}
    user_msg = body.get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "Message required."}), 400

    try:
        result = research_tools.dispatch(user_msg)
        return jsonify(result)
    except Exception as e:
        logger.exception("Agent chat error")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------
@app.errorhandler(Exception)
def handle_exception(err):
    logger.exception("Unhandled exception")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
