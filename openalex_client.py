"""
Client for the OpenAlex API.

OpenAlex is a free, open catalog of the world's scholarly works, authors,
institutions, and topics.  Documentation: https://docs.openalex.org

Provides methods to:
- Search for academic papers by keyword or topic
- Fetch individual works with full metadata
- Retrieve author information
- Normalize all data into Lakebase-ready records

Authentication:
    OpenAlex uses a "polite pool" — requests that include a mailto address
    in the User-Agent header get faster, more reliable responses.  No API
    key is required, but an email stored in the Databricks secret scope
    will be used if available.
"""

import logging
import os
import base64
from datetime import datetime, timezone
from typing import Any, Optional

import requests

try:
    from databricks.sdk import WorkspaceClient
    _w = WorkspaceClient()
except Exception:
    _w = None

logger = logging.getLogger("openalex-client")

_DEFAULT_TIMEOUT = 30
_BASE_URL = "https://api.openalex.org"


def _get_secret(key_env: str, default_key: str) -> str | None:
    """Fetch a secret from the Databricks secret scope, or return None."""
    if _w is None:
        return None
    scope = os.environ.get("RESEARCH_SECRET_SCOPE", "research_copilot")
    key = os.environ.get(key_env, default_key)
    try:
        secret = _w.secrets.get_secret(scope=scope, key=key)
        return base64.b64decode(secret.value).decode("utf-8")
    except Exception:
        return None


def _get_api_key() -> str | None:
    """Fetch the OpenAlex API key from Databricks secrets."""
    return _get_secret("OPENALEX_API_KEY_SECRET", "openalex-api-key")


def _get_polite_email() -> str | None:
    """Fetch the polite-pool email from Databricks secrets."""
    return _get_secret("OPENALEX_EMAIL_SECRET", "openalex-email")


class OpenAlexClient:
    """Client for the OpenAlex API with polite-pool support."""

    def __init__(self, timeout: int = _DEFAULT_TIMEOUT):
        self.timeout = timeout
        self._session = requests.Session()

        email = _get_polite_email()
        api_key = _get_api_key()

        user_agent = "AIResearchCopilot/1.0"
        if email:
            user_agent += f" (mailto:{email})"

        # Persistent query params: API key takes priority, email for polite pool
        default_params: dict[str, str] = {}
        if api_key:
            default_params["api_key"] = api_key
            logger.info("OpenAlex API key loaded from secrets.")
        if email:
            default_params["mailto"] = email
        if default_params:
            self._session.params = default_params  # type: ignore[assignment]

        self._session.headers.update(
            {"User-Agent": user_agent, "Accept": "application/json"}
        )

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Make a GET request to the OpenAlex API."""
        url = f"{_BASE_URL}{path}"
        resp = self._session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Abstract reconstruction
    # ------------------------------------------------------------------

    @staticmethod
    def reconstruct_abstract(inverted_index: dict | None) -> str:
        """Rebuild plain-text abstract from OpenAlex inverted-index format.

        OpenAlex stores abstracts as ``{word: [pos1, pos2, ...], ...}``.
        This reconstructs the original text by placing each word at its
        recorded positions.
        """
        if not inverted_index:
            return ""
        word_positions: list[tuple[int, str]] = []
        for word, positions in inverted_index.items():
            for pos in positions:
                word_positions.append((pos, word))
        word_positions.sort(key=lambda x: x[0])
        return " ".join(word for _, word in word_positions)

    # ------------------------------------------------------------------
    # Search works
    # ------------------------------------------------------------------

    def search_works(
        self,
        query: str,
        per_page: int = 25,
        page: int = 1,
        sort: str = "relevance_score:desc",
        filter_str: str | None = None,
    ) -> dict:
        """Search for academic works (papers) matching a query.

        Args:
            query:      Free-text search string.
            per_page:   Results per page (max 200).
            page:       Page number (1-indexed). Which page of results to retrieve.
            sort:       Sort order (e.g. 'cited_by_count:desc').
            filter_str: Optional OpenAlex filter (e.g. 'publication_year:2023').

        Returns:
            dict with ``meta`` (pagination) and ``results`` (list of works).
        """
        params: dict[str, Any] = {
            "search": query,
            "per_page": min(per_page, 200),
            "page": page,
            "sort": sort,
        }
        if filter_str:
            params["filter"] = filter_str

        data = self._get("/works", params=params)
        return {
            "meta": data.get("meta", {}),
            "results": [self.normalize_work(w) for w in data.get("results", [])],
        }

    # ------------------------------------------------------------------
    # Get single work
    # ------------------------------------------------------------------

    def get_work(self, openalex_id: str) -> dict | None:
        """Fetch a single work by its OpenAlex ID.

        Args:
            openalex_id: Full URL (``https://openalex.org/W...``) or short
                         form (``W...``).
        """
        short_id = openalex_id.split("/")[-1] if "/" in openalex_id else openalex_id
        try:
            data = self._get(f"/works/{short_id}")
            return self.normalize_work(data)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise

    # ------------------------------------------------------------------
    # Get author
    # ------------------------------------------------------------------

    def get_author(self, openalex_id: str) -> dict | None:
        """Fetch a single author by OpenAlex ID."""
        short_id = openalex_id.split("/")[-1] if "/" in openalex_id else openalex_id
        try:
            data = self._get(f"/authors/{short_id}")
            return self.normalize_author(data)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise

    # ------------------------------------------------------------------
    # Normalization helpers
    # ------------------------------------------------------------------

    def normalize_work(self, work: dict) -> dict:
        """Normalize an OpenAlex work into a Lakebase-ready record.

        Returns a dict with keys matching the ``papers`` and ``paper_authors``
        tables in setup_database.sql.
        """
        openalex_id = work.get("id") or ""
        paper_id = openalex_id.split("/")[-1] if "/" in openalex_id else openalex_id

        # Reconstruct abstract
        abstract = self.reconstruct_abstract(
            work.get("abstract_inverted_index")
        )

        # Primary location info
        primary_loc = work.get("primary_location") or {}
        source = primary_loc.get("source") or {}
        source_name = source.get("display_name", "")

        # Open access PDF
        oa = work.get("open_access") or {}
        pdf_url = oa.get("oa_url", "")

        # Topics (replaced deprecated "concepts" — richer 4-level hierarchy)
        concepts = [
            {
                "id": t.get("id", ""),
                "display_name": t.get("display_name", ""),
                "score": t.get("score", 0),
                "subfield": (t.get("subfield") or {}).get("display_name", ""),
                "field": (t.get("field") or {}).get("display_name", ""),
                "domain": (t.get("domain") or {}).get("display_name", ""),
            }
            for t in work.get("topics", [])
            if t.get("score", 0) >= 0.3  # only keep relevant topics
        ]

        # Authors
        authorships = []
        for authorship in work.get("authorships", []):
            author_info = authorship.get("author", {})
            author_id_raw = author_info.get("id") or ""
            author_id = (
                author_id_raw.split("/")[-1] if "/" in author_id_raw else author_id_raw
            )

            institutions = authorship.get("institutions", [])
            institution_name = institutions[0].get("display_name", "") if institutions else ""

            position = authorship.get("author_position", "middle")

            authorships.append(
                {
                    "author_id": author_id,
                    "display_name": author_info.get("display_name", "Unknown"),
                    "institution": institution_name,
                    "orcid": author_info.get("orcid", ""),
                    "openalex_url": author_id_raw,
                    "position": position,
                }
            )

        return {
            "paper_id": paper_id,
            "title": work.get("title", "Untitled"),
            "abstract": abstract,
            "publication_date": work.get("publication_date"),
            "doi": work.get("doi", ""),
            "cited_by_count": work.get("cited_by_count", 0),
            "relevance_score": work.get("relevance_score"),
            "source_name": source_name,
            "pdf_url": pdf_url or "",
            "openalex_url": openalex_id,
            "concepts": concepts,
            "authorships": authorships,
            "referenced_works": [
                r.split("/")[-1]
                for r in work.get("referenced_works", [])
            ],
        }

    @staticmethod
    def normalize_author(author: dict) -> dict:
        """Normalize an OpenAlex author into a Lakebase-ready record."""
        openalex_id = author.get("id") or ""
        author_id = openalex_id.split("/")[-1] if "/" in openalex_id else openalex_id

        affil = author.get("last_known_institutions") or author.get("last_known_institution")
        if isinstance(affil, list):
            institution = affil[0].get("display_name", "") if affil else ""
        elif isinstance(affil, dict):
            institution = affil.get("display_name", "")
        else:
            institution = ""

        return {
            "author_id": author_id,
            "display_name": author.get("display_name", "Unknown"),
            "institution": institution,
            "orcid": author.get("orcid", ""),
            "openalex_url": openalex_id,
        }

    # ------------------------------------------------------------------
    # Convenience: search and return papers for a learning goal
    # ------------------------------------------------------------------

    def search_papers_for_goal(
        self,
        goal_text: str,
        limit: int = 20,
        year_from: int | None = None,
    ) -> list[dict]:
        """Search for papers relevant to a learning-goal description.

        Args:
            goal_text:  Natural-language description of the learning goal.
            limit:      Max papers to return.
            year_from:  Optional earliest publication year filter.

        Returns:
            List of normalized work dicts.
        """
        filter_parts = ["has_abstract:true", "type:article|review|preprint"]
        if year_from:
            filter_parts.append(f"publication_year:>{year_from - 1}")
        filter_str = ",".join(filter_parts)

        result = self.search_works(
            query=goal_text,
            per_page=min(limit, 200),
            filter_str=filter_str,
            sort="relevance_score:desc",
        )
        return result["results"]
