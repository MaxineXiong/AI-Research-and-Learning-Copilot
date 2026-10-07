"""
AI Research & Learning Copilot — Flask Application

Serves the frontend UI for managing learning goals, discovering papers,
building collections, tracking reading progress, and chatting with the
AI research agent.
"""

import hashlib
import json
import logging
import os

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session

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
        "SELECT * FROM learning_goals WHERE user_id = %s ORDER BY updated_at DESC LIMIT 3",
        (DEFAULT_USER_ID,),
    )
    collections = lakebase.run_query(
        "SELECT c.*, (SELECT COUNT(*) FROM collection_papers cp WHERE cp.collection_id = c.collection_id) AS paper_count "
        "FROM collections c WHERE c.user_id = %s ORDER BY paper_count DESC LIMIT 4",
        (DEFAULT_USER_ID,),
    )
    stats = lakebase.run_query_one("""
        SELECT
            (SELECT COUNT(*) FROM learning_goals  WHERE user_id = %s)                       AS learning_goals,
            (SELECT COUNT(*) FROM collections     WHERE user_id = %s)                       AS collections,
            (SELECT COUNT(*) FROM reading_progress WHERE user_id = %s AND status = 'completed') AS papers_completed,
            (SELECT COUNT(*) FROM reading_progress WHERE user_id = %s AND status = 'reading')   AS currently_reading,
            (SELECT COUNT(*) FROM reading_progress WHERE user_id = %s AND status = 'not_started') AS papers_remaining
    """, (DEFAULT_USER_ID,) * 5)
    return render_template("index.html", goals=goals, collections=collections, stats=stats or {})


# ---------------------------------------------------------------------------
# Routes — Learning Goals
# ---------------------------------------------------------------------------
@app.route("/goals")
def goals_page():
    goals = lakebase.run_query(
        "SELECT * FROM learning_goals WHERE user_id = %s ORDER BY updated_at DESC",
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
    query = request.args.get("q", "")
    mode = request.args.get("mode", "semantic")
    return render_template("search.html", results=None, query=query, mode=mode)


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
        sim_map = {h["source_id"]: h["similarity"] for h in hits}
        paper_ids = list(sim_map.keys())
        if paper_ids:
            placeholders = ",".join(["%s"] * len(paper_ids))
            results = lakebase.run_query(
                f"SELECT * FROM papers WHERE paper_id IN ({placeholders})",
                tuple(paper_ids),
            )
            for r in results:
                r["score"] = round(sim_map.get(r["paper_id"], 0), 4)
            results.sort(key=lambda r: r["score"], reverse=True)
    else:
        # Live OpenAlex search
        client = OpenAlexClient()
        openalex_results = client.search_papers_for_goal(query, limit=20)
        # Build relevance map (preserve API order as rank)
        relevance_map = {p["paper_id"]: p.get("relevance_score") for p in openalex_results}
        # Upsert discovered papers into Lakebase for future use
        for paper in openalex_results:
            _upsert_paper_from_openalex(paper)
        paper_ids = [p["paper_id"] for p in openalex_results]
        if paper_ids:
            placeholders = ",".join(["%s"] * len(paper_ids))
            rows = lakebase.run_query(
                f"SELECT * FROM papers WHERE paper_id IN ({placeholders})",
                tuple(paper_ids),
            )
            row_map = {r["paper_id"]: r for r in rows}
            # Preserve OpenAlex relevance order and attach scores
            results = []
            for pid in paper_ids:
                if pid in row_map:
                    r = row_map[pid]
                    r["score"] = relevance_map.get(pid)
                    results.append(r)

    return render_template("search.html", results=results, query=query, mode=mode)


_upsert_paper_from_openalex = research_tools.upsert_paper


# ---------------------------------------------------------------------------
# Routes — Sync Embeddings
# ---------------------------------------------------------------------------

def _chunk_text(text, chunk_size=800, chunk_overlap=200):
    """Split text into overlapping chunks."""
    result = []
    start = 0
    idx = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            result.append((idx, chunk.strip()))
            idx += 1
        start += chunk_size - chunk_overlap
    return result


@app.route("/search/sync", methods=["POST"])
def sync_embeddings():
    """Embed new abstracts, notes, and goals that are missing from pgvector."""
    try:
        model = research_tools._get_embedding_model()
        model_name = research_tools.EMBEDDING_MODEL_NAME

        # ── 1. Gather all text sources ──────────────────────────────
        papers = lakebase.run_query(
            "SELECT paper_id, title, abstract FROM papers "
            "WHERE abstract IS NOT NULL AND TRIM(abstract) != ''"
        )
        notes = lakebase.run_query(
            "SELECT note_id, content, paper_id FROM notes "
            "WHERE content IS NOT NULL AND TRIM(content) != ''"
        )
        goals = lakebase.run_query(
            "SELECT goal_id, title, description FROM learning_goals "
            "WHERE title IS NOT NULL AND TRIM(title) != ''"
        )

        # ── 2. Build chunks ─────────────────────────────────────────
        chunks = []  # (source_type, source_id, chunk_index, chunk_text)
        for row in papers:
            text = f"{row['title']}\n\n{row['abstract']}"
            for idx, chunk in _chunk_text(text):
                chunks.append(("abstract", row["paper_id"], idx, chunk))
        for row in notes:
            for idx, chunk in _chunk_text(row["content"]):
                chunks.append(("note", str(row["note_id"]), idx, chunk))
        for row in goals:
            desc = row["description"] or ""
            text = f"{row['title']}\n\n{desc}" if desc else row["title"]
            for idx, chunk in _chunk_text(text):
                chunks.append(("goal", str(row["goal_id"]), idx, chunk))

        # ── 3. Deterministic IDs & filter to missing ────────────────
        chunk_rows = []
        all_ids = []
        for src_type, src_id, chunk_idx, chunk_txt in chunks:
            row_id = hashlib.sha256(
                f"{src_type}:{src_id}:{chunk_idx}".encode()
            ).hexdigest()[:32]
            all_ids.append(row_id)
            chunk_rows.append((row_id, src_type, src_id, chunk_idx, chunk_txt))

        # Find which IDs already exist
        existing_ids = set()
        if all_ids:
            for batch_start in range(0, len(all_ids), 500):
                batch = all_ids[batch_start:batch_start + 500]
                placeholders = ",".join(["%s"] * len(batch))
                rows = lakebase.run_query(
                    f"SELECT id FROM embeddings WHERE id IN ({placeholders})",
                    tuple(batch),
                )
                existing_ids.update(r["id"] for r in rows)

        new_chunks = [c for c in chunk_rows if c[0] not in existing_ids]

        if not new_chunks:
            return jsonify({"status": "ok", "message": "All embeddings are up to date.", "inserted": 0})

        # ── 4. Encode & upsert only new chunks ─────────────────────
        texts = [c[4] for c in new_chunks]
        vectors = model.encode(texts, show_progress_bar=False, batch_size=64)

        inserted = 0
        with lakebase.get_connection() as conn:
            with conn.cursor() as cur:
                for i, (emb_id, src_type, src_id, chunk_idx, chunk_txt) in enumerate(new_chunks):
                    embedding_str = "[" + ",".join(str(float(x)) for x in vectors[i].tolist()) + "]"
                    cur.execute(
                        "INSERT INTO embeddings (id, source_type, source_id, chunk_index, "
                        "chunk_text, embedding, model_name) "
                        "VALUES (%s, %s, %s, %s, %s, %s::vector, %s) "
                        "ON CONFLICT (id) DO NOTHING",
                        (emb_id, src_type, src_id, chunk_idx, chunk_txt, embedding_str, model_name),
                    )
                    inserted += cur.rowcount
                conn.commit()

        return jsonify({"status": "ok", "message": f"Synced {inserted} new embeddings.", "inserted": inserted})

    except Exception as e:
        logger.exception("Sync embeddings failed")
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Routes — Collections
# ---------------------------------------------------------------------------
@app.route("/collections")
def collections_page():
    collections = lakebase.run_query(
        "SELECT c.*, (SELECT COUNT(*) FROM collection_papers cp WHERE cp.collection_id = c.collection_id) AS paper_count "
        "FROM collections c WHERE c.user_id = %s ORDER BY paper_count DESC",
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


@app.route("/collections/<int:collection_id>/delete", methods=["POST"])
def delete_collection(collection_id):
    lakebase.run_write(
        "DELETE FROM collection_papers WHERE collection_id = %s", (collection_id,)
    )
    lakebase.run_write(
        "DELETE FROM collections WHERE collection_id = %s AND user_id = %s",
        (collection_id, DEFAULT_USER_ID),
    )
    flash("Collection deleted.", "success")
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
    collection = lakebase.run_query_one(
        "SELECT name FROM collections WHERE collection_id = %s AND user_id = %s",
        (collection_id, DEFAULT_USER_ID),
    )
    paper = lakebase.run_query_one(
        "SELECT title FROM papers WHERE paper_id = %s", (paper_id,)
    )
    inserted = lakebase.run_write(
        "INSERT INTO collection_papers (collection_id, paper_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (collection_id, paper_id),
    )
    collection_name = collection["name"] if collection else f"collection {collection_id}"
    paper_title = paper["title"] if paper else paper_id
    if inserted:
        flash(f'"{paper_title}" added to the collection "{collection_name}"', "success")
    else:
        flash(f'"{paper_title}" is already in the collection "{collection_name}"', "success")
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
    assigned_collections = lakebase.run_query(
        "SELECT c.collection_id, c.name, cp.added_at "
        "FROM collection_papers cp JOIN collections c "
        "ON cp.collection_id = c.collection_id "
        "WHERE cp.paper_id = %s AND c.user_id = %s "
        "ORDER BY cp.added_at DESC",
        (paper_id, DEFAULT_USER_ID),
    )
    return render_template(
        "paper.html", paper=paper, authors=authors, notes=notes,
        progress=progress, collections=collections,
        assigned_collections=assigned_collections,
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

    At the start of every chat session the agent verifies the user's identity.
    Once verified, routes messages to the appropriate research tool and returns
    a structured response with keys: tool, answer, citations.
    """
    body = request.json or {}
    user_msg = body.get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "Message required."}), 400

    # ------------------------------------------------------------------
    # Session-level user verification gate
    # ------------------------------------------------------------------
    if not session.get("verified_user_id"):
        # Try to verify the input as a user ID (number) or username/email
        if user_msg.isdigit():
            user = lakebase.run_query_one(
                "SELECT user_id, email, display_name FROM users WHERE user_id = %s",
                (int(user_msg),),
            )
        else:
            user = lakebase.run_query_one(
                "SELECT user_id, email, display_name FROM users "
                "WHERE email ILIKE %s OR display_name ILIKE %s",
                (user_msg, user_msg),
            )

        if user:
            session["verified_user_id"] = user["user_id"]
            session["verified_display_name"] = user["display_name"]
            return jsonify({
                "tool": "verify_user",
                "answer": (
                    f"Welcome, **{user['display_name']}**! "
                    f"Your identity has been verified "
                    f"(ID: {user['user_id']}, email: {user.get('email', 'N/A')}).\n\n"
                    "How can I help with your research today?"
                ),
                "citations": [],
            })
        else:
            return jsonify({
                "tool": "verify_user",
                "answer": (
                    f"User '{user_msg}' not found. "
                    "Please provide a valid **user ID** (number) or **username / email**."
                ),
                "citations": [],
            })

    # ------------------------------------------------------------------
    # Normal tool dispatch (user verified)
    # ------------------------------------------------------------------
    try:
        result = research_tools.dispatch(
            user_msg, user_id=session["verified_user_id"]
        )
        return jsonify(result)
    except Exception as e:
        logger.exception("Agent chat error")
        return jsonify({"error": str(e)}), 500


@app.route("/api/agent/reset", methods=["POST"])
def agent_reset():
    """Reset the chat session so the agent asks for user verification again."""
    session.pop("verified_user_id", None)
    session.pop("verified_display_name", None)
    return jsonify({"status": "ok"})


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
