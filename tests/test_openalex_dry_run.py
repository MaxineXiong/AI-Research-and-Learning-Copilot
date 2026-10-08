"""
Dry-run tests for OpenAlex API integration.

Verifies that the OpenAlex API is reachable and that the normalization
logic produces records with all expected fields.  Uses a small per_page
limit to keep the test fast and avoid hammering the API.

Usage:
    python tests/test_openalex_dry_run.py

Exits with code 0 if all checks pass, 1 otherwise.
"""

import sys
import os

# Ensure the project root is on the path so we can import openalex_client
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
# Expected normalization fields
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = [
    "paper_id",
    "title",
    "abstract",
    "publication_date",
    "cited_by_count",
    "doi",
    "source_name",
    "pdf_url",
    "openalex_url",
    "concepts",
    "authorships",
]

REQUIRED_AUTHORSHIP_FIELDS = [
    "author_id",
    "display_name",
    "institution",
    "orcid",
    "openalex_url",
    "position",
]

TEST_QUERY = "attention mechanisms in transformers"

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_api_reachable():
    """Verify that the OpenAlex API responds to a minimal search request."""
    print("\n1. Checking OpenAlex API reachability...")
    try:
        from openalex_client import OpenAlexClient
        client = OpenAlexClient()
        result = client.search_works(TEST_QUERY, per_page=1)
        check("API returned a response", "meta" in result, "missing 'meta' key")
        check("Response has results list", "results" in result, "missing 'results' key")
        check("At least 1 result returned", len(result["results"]) >= 1, "0 results")
        return result["results"] if result.get("results") else []
    except Exception as e:
        check("OpenAlex API reachable", False, str(e))
        return []


def test_normalization_fields(papers: list):
    """Verify that each normalized paper has all required fields."""
    print("\n2. Checking normalization fields...")
    if not papers:
        check("Papers to test", False, "no papers returned from API")
        return

    for i, paper in enumerate(papers):
        label = f"Paper[{i}]"
        for field in REQUIRED_FIELDS:
            check(
                f"{label} has '{field}'",
                field in paper,
                f"missing key: {field}",
            )

        # paper_id should be non-empty and start with 'W'
        check(
            f"{label}.paper_id is non-empty",
            bool(paper.get("paper_id")),
            "paper_id is empty",
        )
        check(
            f"{label}.paper_id starts with 'W'",
            paper.get("paper_id", "").startswith("W"),
            f"got: {paper.get('paper_id')}",
        )

        # title should be non-empty
        check(
            f"{label}.title is non-empty",
            bool(paper.get("title")),
            "title is empty",
        )

        # cited_by_count should be an integer
        check(
            f"{label}.cited_by_count is int",
            isinstance(paper.get("cited_by_count"), int),
            f"got type: {type(paper.get('cited_by_count'))}",
        )

        # concepts should be a list
        check(
            f"{label}.concepts is list",
            isinstance(paper.get("concepts"), list),
            f"got type: {type(paper.get('concepts'))}",
        )


def test_authorship_normalization(papers: list):
    """Verify that authorship records have all required fields."""
    print("\n3. Checking authorship normalization...")
    if not papers:
        check("Papers to test", False, "no papers returned")
        return

    # Test the first paper that has authorships
    for paper in papers:
        authorships = paper.get("authorships", [])
        if not authorships:
            continue

        check("At least one paper has authorships", True)

        for j, auth in enumerate(authorships[:3]):  # check up to 3 authors
            label = f"Author[{j}]"
            for field in REQUIRED_AUTHORSHIP_FIELDS:
                check(
                    f"{label} has '{field}'",
                    field in auth,
                    f"missing key: {field}",
                )

            # display_name should be non-empty
            check(
                f"{label}.display_name is non-empty",
                bool(auth.get("display_name")),
                "display_name is empty",
            )

            # author_id should be non-empty (if present, starts with 'A')
            if auth.get("author_id"):
                check(
                    f"{label}.author_id starts with 'A'",
                    auth["author_id"].startswith("A"),
                    f"got: {auth['author_id']}",
                )
        break
    else:
        check("At least one paper has authorships", False, "no authorships found in any paper")


def test_abstract_reconstruction(papers: list):
    """Verify that abstract reconstruction produces readable text."""
    print("\n4. Checking abstract reconstruction...")
    if not papers:
        check("Papers to test", False, "no papers returned")
        return

    has_abstract = sum(1 for p in papers if p.get("abstract"))
    check(
        "At least one paper has a non-empty abstract",
        has_abstract > 0,
        f"{has_abstract}/{len(papers)} papers have abstracts",
    )

    # Check that abstract is a string (not None or dict)
    for i, paper in enumerate(papers[:3]):
        abstract = paper.get("abstract", "")
        check(
            f"Paper[{i}].abstract is string",
            isinstance(abstract, str),
            f"got type: {type(abstract)}",
        )


def test_search_papers_for_goal():
    """Verify the convenience method returns normalized papers."""
    print("\n5. Checking search_papers_for_goal convenience method...")
    try:
        from openalex_client import OpenAlexClient
        client = OpenAlexClient()
        results = client.search_papers_for_goal(TEST_QUERY, limit=3)
        check(
            "search_papers_for_goal returns list",
            isinstance(results, list),
            f"got type: {type(results)}",
        )
        check(
            "Returns <= 3 results",
            len(results) <= 3,
            f"got {len(results)} results",
        )
        if results:
            check(
                "First result has paper_id",
                "paper_id" in results[0],
                "missing paper_id",
            )
    except Exception as e:
        check("search_papers_for_goal", False, str(e))


def test_get_single_work():
    """Verify fetching a single work by ID returns a normalized record."""
    print("\n6. Checking get_work (single paper fetch)...")
    try:
        from openalex_client import OpenAlexClient
        client = OpenAlexClient()

        # Use a well-known paper ID (Attention Is All You Need)
        work = client.get_work("W2963403868")
        check("get_work returns a dict", work is not None and isinstance(work, dict),
              "returned None")
        if work:
            check("get_work result has paper_id", "paper_id" in work, "missing paper_id")
            check("get_work result has title", bool(work.get("title")), "title is empty")
            check("get_work result has authorships", "authorships" in work, "missing authorships")
    except Exception as e:
        check("get_work", False, str(e))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  AI Research & Learning Copilot — OpenAlex Dry-Run Tests")
    print("=" * 60)

    papers = test_api_reachable()
    test_normalization_fields(papers)
    test_authorship_normalization(papers)
    test_abstract_reconstruction(papers)
    test_search_papers_for_goal()
    test_get_single_work()

    print("\n" + "=" * 60)
    print(f"  Results: {_passed} passed, {_failed} failed")
    print("=" * 60)

    if _failed > 0:
        print("\n  \u2717 OPENALEX TESTS FAILED")
        sys.exit(1)
    else:
        print("\n  \u2713 ALL OPENALEX TESTS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
