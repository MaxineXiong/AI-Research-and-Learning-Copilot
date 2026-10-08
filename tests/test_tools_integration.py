"""
Integration tests for key research tools.

These tests exercise the tool layer (research_tools.py) against the live
Lakebase database to verify end-to-end behavior:
  1. add_to_collection creates a reading-progress seed record
  2. recommend_next_paper respects the user's reading history
  3. update_reading_progress chains a recommendation when status=completed

Prerequisites:
  - Lakebase Postgres is running and setup_database.sql has been executed
  - At least one user exists in the users table (DEFAULT_USER_ID=1)
  - At least one collection exists for the user
  - At least one paper exists in the papers table

Usage:
    python tests/test_tools_integration.py

Exits with code 0 if all checks pass, 1 otherwise.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_passed = 0
_failed = 0

def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        print(f"  \u2713 {name}")
        _passed += 1
    else:
        print(f"  \u2717 {name}: {detail}")
        _failed += 1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_add_to_collection_creates_progress_seed():
    """Verify that add_to_collection seeds a 'not_started' reading-progress record."""
    print("\n1. Testing add_to_collection creates progress seed...")
    try:
        import lakebase
        import research_tools

        # Find an existing collection and paper for the test
        collection = lakebase.run_query_one(
            "SELECT collection_id, name FROM collections WHERE user_id = 1 LIMIT 1"
        )
        paper = lakebase.run_query_one(
            "SELECT paper_id, title FROM papers LIMIT 1"
        )
        if not collection or not paper:
            check("Preconditions (collection + paper exist)", False,
                  "Need at least one collection and one paper")
            return

        # Call add_to_collection via the tool function
        result = research_tools.add_to_collection(
            collection_name=collection["name"],
            paper_input=paper["paper_id"],
            user_id=1,
        )

        check("add_to_collection returns success",
              "added" in result.get("answer", "").lower() or "already" in result.get("answer", "").lower(),
              f"answer: {result.get('answer')}")

        # Verify reading_progress seed was created
        progress = lakebase.run_query_one(
            "SELECT status FROM reading_progress WHERE user_id = 1 AND paper_id = %s",
            (paper["paper_id"],),
        )
        check("Reading-progress seed created", progress is not None,
              "no reading_progress row found")
        if progress:
            check("Seed status is 'not_started' (or higher)",
                  progress["status"] in ("not_started", "reading", "completed"),
                  f"got: {progress['status']}")

        # Clean up: remove the paper from the collection and delete progress
        lakebase.run_write(
            "DELETE FROM collection_papers WHERE collection_id = %s AND paper_id = %s",
            (collection["collection_id"], paper["paper_id"]),
        )
        # Only delete if it was 'not_started' (don't delete real progress)
        if progress and progress["status"] == "not_started":
            lakebase.run_write(
                "DELETE FROM reading_progress WHERE user_id = 1 AND paper_id = %s AND status = 'not_started'",
                (paper["paper_id"],),
            )

    except Exception as e:
        check("add_to_collection test", False, str(e))


def test_recommend_next_paper_respects_history():
    """Verify recommend_next_paper considers the user's reading history."""
    print("\n2. Testing recommend_next_paper respects reading history...")
    try:
        import lakebase
        import research_tools

        # Get the user's reading history
        history = lakebase.run_query(
            "SELECT paper_id, status FROM reading_progress WHERE user_id = 1"
        )
        completed_ids = {r["paper_id"] for r in history if r["status"] == "completed"}
        reading_ids = {r["paper_id"] for r in history if r["status"] == "reading"}
        read_ids = completed_ids | reading_ids

        # Find a topic from the first completed paper (or any paper)
        topic = None
        if completed_ids:
            paper = lakebase.run_query_one(
                "SELECT title FROM papers WHERE paper_id = %s",
                (list(completed_ids)[0],),
            )
            if paper:
                topic = paper["title"]

        if not topic:
            # Fall back to a learning goal title as topic
            goal = lakebase.run_query_one(
                "SELECT title FROM learning_goals WHERE user_id = 1 LIMIT 1"
            )
            topic = goal["title"] if goal else "machine learning"

        result = research_tools.recommend_next_paper(topic=topic, user_id=1)

        check("recommend_next_paper returns a result", "answer" in result,
              "no 'answer' key")
        check("recommend_next_paper has citations", "citations" in result,
              "no 'citations' key")

        # If the result has citations, verify they don't overlap with completed papers
        # (unless all papers are read)
        citations = result.get("citations", [])
        if citations and read_ids:
            recommended_ids = {c["paper_id"] for c in citations}
            overlap = recommended_ids & completed_ids
            # Some overlap is OK if the plan includes already-read papers for context,
            # but the PRIMARY recommendation should be unread
            check("Recommendation includes unread papers",
                  len(recommended_ids - completed_ids) > 0 or len(read_ids) == 0,
                  "all recommended papers are already completed")

    except Exception as e:
        check("recommend_next_paper test", False, str(e))


def test_update_reading_progress_chains_recommendation():
    """Verify update_reading_progress chains a recommendation when status=completed."""
    print("\n3. Testing update_reading_progress chains recommendation on completed...")
    try:
        import lakebase
        import research_tools

        # Find a paper that exists in the DB but is not yet completed
        paper = lakebase.run_query_one(
            """
            SELECT p.paper_id, p.title
            FROM papers p
            LEFT JOIN reading_progress rp ON rp.paper_id = p.paper_id AND rp.user_id = 1
            WHERE rp.status IS NULL OR rp.status != 'completed'
            LIMIT 1
            """
        )
        if not paper:
            check("Found a paper not yet completed", False,
                  "all papers are already completed")
            return

        # Save original state for cleanup
        orig_progress = lakebase.run_query_one(
            "SELECT status FROM reading_progress WHERE user_id = 1 AND paper_id = %s",
            (paper["paper_id"],),
        )

        # Call update_reading_progress with status=completed
        result = research_tools.update_reading_progress(
            paper_input=paper["paper_id"],
            status="completed",
            user_id=1,
        )

        check("update_reading_progress returns answer", "answer" in result,
              "no 'answer' key")
        check("Answer mentions completion",
              "completed" in result.get("answer", "").lower(),
              f"answer: {result.get('answer', '')[:100]}")

        # The chained recommendation should be appended to the answer
        # (it will contain additional text beyond the progress update message)
        answer = result.get("answer", "")
        check("Answer includes chained recommendation (longer than just progress msg)",
              len(answer) > 100 or "No reading history" in answer or "most-cited" in answer,
              f"answer length: {len(answer)}")

        # Verify the DB was updated
        new_progress = lakebase.run_query_one(
            "SELECT status FROM reading_progress WHERE user_id = 1 AND paper_id = %s",
            (paper["paper_id"],),
        )
        check("DB status updated to 'completed'",
              new_progress and new_progress["status"] == "completed",
              f"got: {new_progress['status'] if new_progress else 'None'}")

        # Clean up: restore original state
        if orig_progress:
            lakebase.run_write(
                """
                UPDATE reading_progress SET status = %s,
                    started_at = CASE WHEN %s IN ('reading','completed') THEN started_at ELSE NULL END,
                    completed_at = CASE WHEN %s = 'completed' THEN completed_at ELSE NULL END
                WHERE user_id = 1 AND paper_id = %s
                """,
                (orig_progress["status"], orig_progress["status"],
                 orig_progress["status"], paper["paper_id"]),
            )
        else:
            lakebase.run_write(
                "DELETE FROM reading_progress WHERE user_id = 1 AND paper_id = %s",
                (paper["paper_id"],),
            )

    except Exception as e:
        check("update_reading_progress chain test", False, str(e))


def test_update_reading_progress_no_chain_on_reading():
    """Verify update_reading_progress does NOT chain when status != completed."""
    print("\n4. Testing update_reading_progress does NOT chain on 'reading'...")
    try:
        import lakebase
        import research_tools

        paper = lakebase.run_query_one(
            "SELECT paper_id, title FROM papers LIMIT 1"
        )
        if not paper:
            check("Found a paper", False, "no papers in DB")
            return

        result = research_tools.update_reading_progress(
            paper_input=paper["paper_id"],
            status="reading",
            user_id=1,
        )

        answer = result.get("answer", "")
        # When status is 'reading', the answer should be short (just the progress msg)
        # without a chained recommendation
        check("Answer is short (no chained recommendation)",
              len(answer) < 200,
              f"answer length: {len(answer)}, may have chained recommendation")
        check("Answer mentions 'reading'",
              "reading" in answer.lower(),
              f"answer: {answer[:100]}")

    except Exception as e:
        check("update_reading_progress no-chain test", False, str(e))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  AI Research & Learning Copilot — Tool Integration Tests")
    print("=" * 60)

    test_add_to_collection_creates_progress_seed()
    test_recommend_next_paper_respects_history()
    test_update_reading_progress_chains_recommendation()
    test_update_reading_progress_no_chain_on_reading()

    print("\n" + "=" * 60)
    print(f"  Results: {_passed} passed, {_failed} failed")
    print("=" * 60)

    if _failed > 0:
        print("\n  \u2717 INTEGRATION TESTS FAILED")
        sys.exit(1)
    else:
        print("\n  \u2713 ALL INTEGRATION TESTS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
