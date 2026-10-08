"""
Smoke tests for the AI Research & Learning Copilot.

Verifies that the infrastructure is correctly configured and operational:
  1. Databricks secrets are accessible (Lakebase URL, OpenAlex keys)
  2. Lakebase Postgres connectivity works
  3. All 10 required tables exist
  4. The HNSW vector index exists on the embeddings table
  5. A sample vector search returns results

Usage:
    python tests/smoke_test.py

Exits with code 0 if all checks pass, 1 otherwise.
"""

import base64
import sys
import os

# Ensure the project root is on the path so we can import lakebase
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

def test_secrets_accessible():
    """Verify that Databricks secrets can be read."""
    print("\n1. Checking Databricks secrets...")
    try:
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()
        scope = os.environ.get("RESEARCH_SECRET_SCOPE", "research_copilot")

        # Lakebase URL
        lakebase_key = os.environ.get("LAKEBASE_SECRET_URL", "lakebase-url")
        secret = w.secrets.get_secret(scope=scope, key=lakebase_key)
        url = base64.b64decode(secret.value).decode("utf-8")
        check("Lakebase URL secret readable", bool(url), "secret value is empty")
        check("Lakebase URL starts with postgres://", url.startswith("postgres://"), f"got: {url[:20]}...")

        # OpenAlex API key (optional)
        openalex_key = os.environ.get("OPENALEX_API_KEY_SECRET", "openalex-api-key")
        try:
            secret = w.secrets.get_secret(scope=scope, key=openalex_key)
            api_key = base64.b64decode(secret.value).decode("utf-8")
            check("OpenAlex API key secret readable", bool(api_key), "secret value is empty")
        except Exception:
            check("OpenAlex API key secret readable (optional, skipped)", True)

        # OpenAlex polite email (optional)
        email_key = os.environ.get("OPENALEX_EMAIL_SECRET", "openalex-email")
        try:
            secret = w.secrets.get_secret(scope=scope, key=email_key)
            email = base64.b64decode(secret.value).decode("utf-8")
            check("OpenAlex polite email secret readable", bool(email), "secret value is empty")
        except Exception:
            check("OpenAlex polite email secret readable (optional, skipped)", True)

    except Exception as e:
        check("Secrets accessible", False, str(e))


def test_db_connectivity():
    """Verify Lakebase Postgres connectivity and required tables."""
    print("\n2. Checking Lakebase Postgres connectivity...")
    try:
        import lakebase

        # Simple connectivity check
        rows = lakebase.run_query("SELECT 1 AS ok")
        check("DB connection works", rows and rows[0]["ok"] == 1, "SELECT 1 returned no rows")

        # Check all 10 required tables exist
        required_tables = [
            "users", "learning_goals", "papers", "authors", "paper_authors",
            "collections", "collection_papers", "reading_progress", "notes", "embeddings",
        ]
        table_rows = lakebase.run_query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (required_tables,),
        )
        found_tables = {r["table_name"] for r in table_rows}
        for t in required_tables:
            check(f"Table '{t}' exists", t in found_tables, f"table not found in information_schema")

    except Exception as e:
        check("DB connectivity", False, str(e))


def test_hnsw_index():
    """Verify the HNSW vector index exists on the embeddings table."""
    print("\n3. Checking HNSW index on embeddings table...")
    try:
        import lakebase

        # Check for HNSW index via pg_indexes
        indexes = lakebase.run_query(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE tablename = 'embeddings' AND schemaname = 'public'",
        )
        index_names = [r["indexname"] for r in indexes]
        check("At least one index on embeddings", len(indexes) > 0, "no indexes found")

        # Look for HNSW-specific index (created by pgvector)
        has_hnsw = any("hnsw" in r["indexdef"].lower() for r in indexes)
        check("HNSW index present", has_hnsw, f"no HNSW index in: {index_names}")

    except Exception as e:
        check("HNSW index", False, str(e))


def test_vector_search():
    """Verify that a sample vector search returns results."""
    print("\n4. Checking sample vector search...")
    try:
        import lakebase

        # Check if embeddings table has data
        count_row = lakebase.run_query_one("SELECT COUNT(*) AS cnt FROM embeddings")
        count = count_row["cnt"] if count_row else 0
        check("Embeddings table has data", count > 0, f"found {count} rows")

        if count > 0:
            # Try a simple cosine similarity search using a literal vector
            # (avoids loading sentence-transformers in the smoke test)
            # Create a simple 384-dim zero vector to test the query mechanism
            zero_vec = "[" + ",".join(["0"] * 384) + "]"
            results = lakebase.run_query(
                "SELECT source_id, source_type, "
                "1 - (embedding <=> %s::vector) AS similarity "
                "FROM embeddings ORDER BY embedding <=> %s::vector LIMIT 3",
                (zero_vec, zero_vec),
            )
            check("Vector search returns results", len(results) > 0, "no results returned")
            if results:
                check("Results have similarity scores", "similarity" in results[0],
                      "missing 'similarity' key")

    except Exception as e:
        check("Vector search", False, str(e))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  AI Research & Learning Copilot — Smoke Tests")
    print("=" * 60)

    test_secrets_accessible()
    test_db_connectivity()
    test_hnsw_index()
    test_vector_search()

    print("\n" + "=" * 60)
    print(f"  Results: {_passed} passed, {_failed} failed")
    print("=" * 60)

    if _failed > 0:
        print("\n  \u2717 SMOKE TESTS FAILED")
        sys.exit(1)
    else:
        print("\n  \u2713 ALL SMOKE TESTS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
