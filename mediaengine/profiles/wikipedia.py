"""Explicit Wikipedia search and sourced introductory biographies."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["WikipediaClient"]


_LANGUAGE_RE = re.compile(r"^[a-z][a-z0-9-]{0,11}$")
_TAG_RE = re.compile(r"<[^>]+>")


class WikipediaClient:
    """Small MediaWiki Action API client with an injectable test transport."""

    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        transport: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self._transport = transport

    @staticmethod
    def _endpoint(language: str) -> str:
        normalized = language.strip().lower()
        if not _LANGUAGE_RE.fullmatch(normalized):
            raise ValueError("Wikipedia language must be a short language code such as 'en'")
        return f"https://{normalized}.wikipedia.org/w/api.php"

    def _get(self, language: str, params: Mapping[str, object]) -> Mapping[str, Any]:
        query = urllib.parse.urlencode({**params, "format": "json", "formatversion": 2})
        url = f"{self._endpoint(language)}?{query}"
        if self._transport is not None:
            return self._transport(url)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "MediaEngine/0.1 local-media-indexer"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("Wikipedia returned an invalid response")
        return payload

    def search(self, query: str, *, language: str = "en", limit: int = 5) -> list[dict[str, Any]]:
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("Wikipedia search query may not be empty")
        limit = max(1, min(int(limit), 10))
        payload = self._get(
            language,
            {
                "action": "query",
                "list": "search",
                "srsearch": clean_query,
                "srnamespace": 0,
                "srlimit": limit,
                "srprop": "snippet|description|titlesnippet",
            },
        )
        query_payload = payload.get("query")
        rows = query_payload.get("search", []) if isinstance(query_payload, Mapping) else []
        results: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, Mapping):
                continue
            snippet = html.unescape(_TAG_RE.sub("", str(row.get("snippet") or "")))
            results.append(
                {
                    "page_id": int(row["pageid"]) if row.get("pageid") is not None else None,
                    "page_title": str(row.get("title") or ""),
                    "description": str(row.get("description") or "") or None,
                    "snippet": snippet,
                    "language": language,
                }
            )
        return results

    def biography(self, page_title: str, *, language: str = "en") -> dict[str, Any]:
        clean_title = page_title.strip()
        if not clean_title:
            raise ValueError("Wikipedia page_title may not be empty")
        payload = self._get(
            language,
            {
                "action": "query",
                "prop": "extracts|info|pageprops|revisions",
                "titles": clean_title,
                "redirects": 1,
                "exintro": 1,
                "explaintext": 1,
                "inprop": "url",
                "rvprop": "ids",
            },
        )
        query_payload = payload.get("query")
        pages = query_payload.get("pages", []) if isinstance(query_payload, Mapping) else []
        page = pages[0] if isinstance(pages, list) and pages else None
        if not isinstance(page, Mapping) or page.get("missing") is True:
            raise ValueError(f"Wikipedia page {clean_title!r} was not found")
        revisions = page.get("revisions")
        revision = revisions[0] if isinstance(revisions, list) and revisions else {}
        pageprops = page.get("pageprops")
        summary = str(page.get("extract") or "").strip()
        if not summary:
            raise ValueError(f"Wikipedia page {clean_title!r} has no introductory extract")
        title = str(page.get("title") or clean_title)
        source_url = str(page.get("fullurl") or "")
        if not source_url:
            source_url = f"https://{language}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
        return {
            "provider": "wikipedia",
            "language": language,
            "page_id": int(page["pageid"]) if page.get("pageid") is not None else None,
            "page_title": title,
            "source_url": source_url,
            "summary": summary,
            "description": str(page.get("description") or "") or None,
            "wikibase_item": pageprops.get("wikibase_item")
            if isinstance(pageprops, Mapping)
            else None,
            "source_revision": revision.get("revid") if isinstance(revision, Mapping) else None,
            "metadata": {
                "source": "MediaWiki Action API",
                "explicit_user_selection": True,
                "reuse_terms": "See the linked Wikipedia page for license and attribution terms",
            },
        }
