"""Tests for web_search and fetch tools."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nally.tools import build_default_registry
from nally.tools.base import ToolRegistry
from nally.tools.fetch import Fetch, _strip_html
from nally.tools.websearch import WebSearch, search, SearchResult


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------
class TestStripHtml:
    def test_strips_tags(self):
        html = "<html><body><h1>Hello</h1><p>World</p></body></html>"
        text = _strip_html(html)
        assert "Hello" in text
        assert "World" in text
        assert "<h1>" not in text

    def test_removes_script(self):
        html = "<script>alert('x')</script><p>Keep</p>"
        text = _strip_html(html)
        assert "alert" not in text
        assert "Keep" in text

    def test_decodes_entities(self):
        html = "<p>a &amp; b &lt; c</p>"
        text = _strip_html(html)
        assert "a & b < c" in text


# ---------------------------------------------------------------------------
# Fetch validation
# ---------------------------------------------------------------------------
class TestFetchValidation:
    def test_empty_url(self):
        f = Fetch()
        ok, _err = f.validate({"url": ""})
        assert ok
        result = f.execute(url="")
        assert "must not be empty" in result

    def test_invalid_scheme(self):
        f = Fetch()
        result = f.execute(url="ftp://example.com")
        assert "must start with http" in result

    def test_tool_schema(self):
        f = Fetch()
        schema = f.to_openai_schema()
        assert schema["function"]["name"] == "fetch"
        assert "url" in schema["function"]["parameters"]["required"]

    def test_max_chars_clamped(self):
        f = Fetch()
        ok, _err = f.validate({"url": "https://example.com", "max_chars": "bad"})
        assert not ok

    def test_url_too_long(self):
        f = Fetch()
        result = f.execute(url="https://example.com/" + "a" * 2000)
        assert "too long" in result


# ---------------------------------------------------------------------------
# Fetch live (mocked) — bypass SSRF for testing
# ---------------------------------------------------------------------------
class TestFetchExecute:
    def test_fetch_success_mocked(self):
        f = Fetch()
        fake_html = b"<html><body><p>Hello world</p></body></html>"
        mock_resp = MagicMock()
        mock_resp.headers.get.return_value = "text/html"
        mock_resp.read.return_value = fake_html
        mock_resp.url = "https://example.com"
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False

        with patch("nally.tools.fetch._check_url_safety", return_value=None):
            with patch("nally.tools.fetch.urllib.request.urlopen", return_value=mock_resp):
                result = f.execute(url="https://example.com")
                assert "Hello world" in result

    def test_fetch_http_error(self):
        import urllib.error

        f = Fetch()
        with patch("nally.tools.fetch._check_url_safety", return_value=None):
            with patch(
                "nally.tools.fetch.urllib.request.urlopen",
                side_effect=urllib.error.HTTPError("https://example.com", 404, "Not Found", {}, None),
            ):
                result = f.execute(url="https://example.com/missing")
                assert "404" in result

    def test_fetch_truncation(self):
        f = Fetch()
        big = b"<p>" + b"x" * 20000 + b"</p>"
        mock_resp = MagicMock()
        mock_resp.headers.get.return_value = "text/html"
        mock_resp.read.return_value = big
        mock_resp.url = "https://example.com"
        mock_resp.__enter__.return_value = mock_resp

        with patch("nally.tools.fetch._check_url_safety", return_value=None):
            with patch("nally.tools.fetch.urllib.request.urlopen", return_value=mock_resp):
                result = f.execute(url="https://example.com", max_chars=100)
                assert len(result) < 500
                assert "truncated" in result

    def test_fetch_binary_content_type(self):
        f = Fetch()
        mock_resp = MagicMock()
        mock_resp.headers.get.return_value = "image/png"
        mock_resp.url = "https://example.com/img.png"
        mock_resp.__enter__.return_value = mock_resp
        with patch("nally.tools.fetch._check_url_safety", return_value=None):
            with patch("nally.tools.fetch.urllib.request.urlopen", return_value=mock_resp):
                result = f.execute(url="https://example.com/img.png")
                assert "unsupported content type" in result

    def test_fetch_ssrf_blocked(self):
        """SSRF check blocks private IPs."""
        f = Fetch()
        result = f.execute(url="http://127.0.0.1/admin")
        assert "SSRF" in result or "blocked" in result.lower()

    def test_fetch_ssrf_redirect_blocked(self):
        """Redirect to private IP is blocked."""
        f = Fetch()
        mock_resp = MagicMock()
        mock_resp.headers.get.return_value = "text/html"
        mock_resp.read.return_value = b"<p>ok</p>"
        mock_resp.url = "http://169.254.169.254/metadata"
        mock_resp.__enter__.return_value = mock_resp

        with patch("nally.tools.fetch._check_url_safety", side_effect=[None, "IP is in blocked range"]):
            with patch("nally.tools.fetch.urllib.request.urlopen", return_value=mock_resp):
                result = f.execute(url="https://redirect.example.com")
                assert "blocked" in result.lower()


# ---------------------------------------------------------------------------
# WebSearch validation + mocked
# ---------------------------------------------------------------------------
class TestWebSearchValidation:
    def test_empty_query(self):
        w = WebSearch()
        result = w.execute(query="")
        assert "must not be empty" in result

    def test_query_too_long(self):
        w = WebSearch()
        result = w.execute(query="a" * 501)
        assert "too long" in result

    def test_schema(self):
        w = WebSearch()
        schema = w.to_openai_schema()
        assert schema["function"]["name"] == "web_search"
        assert "query" in schema["function"]["parameters"]["required"]

    def test_num_results_validation(self):
        w = WebSearch()
        ok, _ = w.validate({"query": "hi", "num_results": 3})
        assert ok
        ok, _err = w.validate({"query": "hi", "num_results": "bad"})
        assert not ok


class TestWebSearchMocked:
    def test_search_success(self):
        w = WebSearch()
        fake_results = [
            SearchResult(title="Python", url="https://python.org", snippet="Python is great"),
            SearchResult(title="Async", url="https://docs.python.org/3/library/asyncio.html", snippet="asyncio"),
        ]

        with patch("nally.tools.websearch.search", return_value=fake_results):
            result = w.execute(query="python", num_results=2)
            assert "Python" in result
            assert "https://python.org" in result

    def test_search_no_results(self):
        w = WebSearch()

        with patch("nally.tools.websearch.search", return_value=[]):
            result = w.execute(query="asdkfjalsdkfjalsdkfj")
            assert "No results" in result

    def test_search_provider_separation(self):
        """Verify search() returns SearchResult objects."""
        results = search("python", num_results=1)
        assert isinstance(results, list)
        if results:
            assert isinstance(results[0], SearchResult)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------
class TestWebRegistry:
    def test_build_default_registry_has_web_tools(self):
        reg = build_default_registry()
        assert "web_search" in reg
        assert "fetch" in reg
        # Total should be 6: read_file, write_file, list_dir, run_command, web_search, fetch
        assert len(reg) == 6

    def test_web_tools_via_registry(self):
        reg = build_default_registry()
        result, ok = reg.execute("web_search", {"query": "hi", "bad": 1})
        assert not ok
        assert "unknown parameter" in result

        result, ok = reg.execute("fetch", {"url": "ftp://bad"})
        assert not ok or "must start with http" in result
