"""Web search tool — DuckDuckGo (free, no key) with provider/fallback separation.

Architecture::

    SearchProvider -> list[SearchResult] -> WebSearch tool formats for LLM

Two providers:
  1. DDGS library (duckduckgo_search / ddgs) — preferred
  2. Raw HTTP scrape of lite.duckduckgo.com — stdlib-only fallback
"""

from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .base import Tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SearchResult — normalized search result
# ---------------------------------------------------------------------------
@dataclass
class SearchResult:
    """Normalized search result from any provider."""

    title: str
    url: str
    snippet: str


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def _ddg_library_search(query: str, num_results: int) -> list[SearchResult] | None:
    """Try DDGS library (new or legacy package name)."""

    def _search(q: str, n: int) -> list[dict[str, Any]]:
        try:
            from ddgs import DDGS as _DDGS  # type: ignore

            with _DDGS() as ddgs:
                return list(ddgs.text(q, max_results=n))
        except ImportError:
            from duckduckgo_search import DDGS as _DDGS2  # type: ignore

            with _DDGS2() as ddgs:
                return list(ddgs.text(q, max_results=n))

    try:
        raw = _search(query, num_results)
        results = []
        for r in raw:
            title = (r.get("title") or "").strip()
            url = (r.get("href") or r.get("link") or "").strip()
            snippet = (r.get("body") or r.get("snippet") or "").strip()
            if title and url:
                results.append(SearchResult(title=title, url=url, snippet=snippet))
        return results if results else None
    except ImportError:
        return None  # library not installed
    except Exception as exc:
        logger.debug("DDGS library search failed: %s: %s", type(exc).__name__, exc)
        return None


def _fallback_scrape_search(query: str, num_results: int) -> list[SearchResult]:
    """Scrape lite.duckduckgo.com with stdlib only."""
    try:
        data = urllib.parse.urlencode({"q": query}).encode()
        req = urllib.request.Request(
            "https://lite.duckduckgo.com/lite/",
            data=data,
            headers={"User-Agent": "Mozilla/5.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        pattern = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', re.IGNORECASE)
        matches = pattern.findall(html)
        results: list[SearchResult] = []
        for href, title in matches:
            if "duckduckgo.com" in href or not href.startswith("http"):
                continue
            title = title.strip()
            if not title or len(title) < 3:
                continue
            title = (
                title.replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
            )
            href = href.replace("&amp;", "&")
            results.append(SearchResult(title=title, url=href, snippet=""))
            if len(results) >= num_results:
                break
        return results
    except Exception as exc:
        logger.debug("Fallback scrape failed: %s: %s", type(exc).__name__, exc)
        return []


def search(query: str, num_results: int = 3) -> list[SearchResult]:
    """Search with library-first, fallback-scrape-second strategy."""
    results = _ddg_library_search(query, num_results)
    if results is not None:
        return results
    return _fallback_scrape_search(query, num_results)


# ---------------------------------------------------------------------------
# Tool — formats SearchResult for LLM
# ---------------------------------------------------------------------------
class WebSearch(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="web_search",
            description="Search the web and return titles, URLs and snippets. Use for current information, news, docs, or anything not in training data.",
            parameters={
                "query": {
                    "type": "string",
                    "description": "Search query",
                    "required": True,
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (1-5, default 3)",
                    "required": False,
                },
            },
        )

    def execute(self, query: str = "", num_results: int = 3, **kwargs: Any) -> str:  # type: ignore[override]
        if not query or not query.strip():
            return "Error: query must not be empty"
        if len(query) > 500:
            return "Error: query too long (max 500 chars)"

        try:
            n = int(num_results) if num_results is not None else 3
        except (ValueError, TypeError):
            n = 3
        n = max(1, min(5, n))

        results = search(query.strip(), n)

        if not results:
            return "No results found."

        lines: list[str] = []
        for i, r in enumerate(results, 1):
            snippet = r.snippet.replace("\n", " ").strip()
            if len(snippet) > 300:
                snippet = snippet[:300] + "..."
            lines.append(f"{i}. {r.title}\n{r.url}\n{snippet}")

        return "\n\n".join(lines)


def register_web_tools(registry) -> None:
    registry.register(WebSearch())
