"""Tests for web_search and fetch tools."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nally.tools import build_default_registry
from nally.tools.base import ToolRegistry
from nally.tools.fetch import Fetch, _strip_html
from nally.tools.websearch import WebSearch


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
        # Empty still passes schema (string type ok), but execute returns error
        # Validate should pass (we check emptiness in execute)
        assert ok
        _result, _ok2 = (
            ToolRegistry().execute.__wrapped__
            if hasattr(ToolRegistry.execute, "__wrapped__")
            else (None, None)
        )
        # Direct execute test
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
        # max_chars validation: integer type
        ok, _err = f.validate({"url": "https://example.com", "max_chars": "bad"})
        assert not ok

    def test_url_too_long(self):
        f = Fetch()
        result = f.execute(url="https://example.com/" + "a" * 2000)
        assert "too long" in result


# ---------------------------------------------------------------------------
# Fetch live (mocked)
# ---------------------------------------------------------------------------
class TestFetchExecute:
    def test_fetch_success_mocked(self):
        f = Fetch()
        fake_html = b"<html><body><p>Hello world</p></body></html>"
        mock_resp = MagicMock()
        mock_resp.headers.get.return_value = "text/html"
        mock_resp.read.return_value = fake_html
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False

        with patch("nally.tools.fetch.urllib.request.urlopen", return_value=mock_resp):
            result = f.execute(url="https://example.com")
            assert "Hello world" in result

    def test_fetch_http_error(self):
        import urllib.error

        f = Fetch()
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
        mock_resp.__enter__.return_value = mock_resp

        with patch("nally.tools.fetch.urllib.request.urlopen", return_value=mock_resp):
            result = f.execute(url="https://example.com", max_chars=100)
            assert len(result) < 500
            assert "truncated" in result

    def test_fetch_binary_content_type(self):
        f = Fetch()
        mock_resp = MagicMock()
        mock_resp.headers.get.return_value = "image/png"
        mock_resp.__enter__.return_value = mock_resp
        with patch("nally.tools.fetch.urllib.request.urlopen", return_value=mock_resp):
            result = f.execute(url="https://example.com/img.png")
            assert "unsupported content type" in result


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
            {"title": "Python", "href": "https://python.org", "body": "Python is great"},
            {
                "title": "Async",
                "href": "https://docs.python.org/3/library/asyncio.html",
                "body": "asyncio",
            },
        ]
        mock_ddgs = MagicMock()
        mock_ddgs.text.return_value = fake_results
        mock_ddgs.__enter__.return_value = mock_ddgs
        mock_ddgs.__exit__.return_value = False

        with patch("ddgs.DDGS", return_value=mock_ddgs):
            result = w.execute(query="python", num_results=2)
            assert "Python" in result
            assert "https://python.org" in result

    def test_search_no_results(self):
        w = WebSearch()
        mock_ddgs = MagicMock()
        mock_ddgs.text.return_value = []
        mock_ddgs.__enter__.return_value = mock_ddgs

        with patch("ddgs.DDGS", return_value=mock_ddgs):
            result = w.execute(query="asdkfjalsdkfjalsdkfj")
            assert "No results" in result

    def test_search_fallback_on_import_error(self):
        w = WebSearch()
        # Simulate missing library — should go to fallback scrape (both ddgs and legacy)
        with patch.dict("sys.modules", {"ddgs": None, "duckduckgo_search": None}):
            original_import = __import__

            def fake_import(name, *args, **kwargs):
                if name in ("ddgs", "duckduckgo_search"):
                    raise ImportError("No module")
                return original_import(name, *args, **kwargs)

            with (
                patch("builtins.__import__", side_effect=fake_import),
                patch(
                    "nally.tools.websearch._fallback_scrape",
                    return_value="1. Fallback\nhttps://example.com",
                ),
            ):
                result = w.execute(query="test")
                assert "Fallback" in result


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------
class TestWebRegistry:
    def test_build_default_registry_has_web_tools(self):
        reg = build_default_registry()
        assert "web_search" in reg
        assert "fetch" in reg
        # Total should be 7: read_file, write_file, list_dir, run_command, web_search, fetch, think
        assert len(reg) == 7

    def test_web_tools_via_registry(self):
        reg = build_default_registry()
        # Validate through registry (unknown param should fail)
        result, ok = reg.execute("web_search", {"query": "hi", "bad": 1})
        assert not ok
        assert "unknown parameter" in result

        result, ok = reg.execute("fetch", {"url": "ftp://bad"})
        assert not ok or "must start with http" in result
