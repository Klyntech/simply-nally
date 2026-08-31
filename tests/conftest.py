"""Isolate tests from real NEON + Google auth.

Without this, every Agent() creation would hit the real NEON DB when
DATABASE_URL and ~/.config/simply-nally/auth.json are present (as on this
machine), making tests slow/flaky and polluting the user's session.

We auto-mock get_session_store for all tests. Tests that need real
persistence behavior use explicit FakeStore or mocked db/auth patches.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _disable_real_persistence():
    # Prevent Agent.__init__ from discovering a real SessionStore
    with patch("nally.session.get_session_store", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _disable_mcp_by_default():
    """Prevent Agent() from hitting real MCP servers during tests.

    Without this, NALLY_MCP_ENABLED=true in .env would make every
    Agent() try to connect to GitHub/Notion MCP (30s timeout, network).
    Tests that need MCP should explicitly patch get_mcp_servers_config
    or pass load_mcp=False / mcp_config={}.
    """
    with patch("nally.config.MCP_ENABLED", False):
        with patch("nally.mcp.adapter.load_mcp_tools_sync", return_value=0):
            with patch("nally.tools.mcp.adapter.load_mcp_tools_sync", return_value=0):
                yield
