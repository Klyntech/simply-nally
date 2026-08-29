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
