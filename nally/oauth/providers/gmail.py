"""Gmail provider alias — re-exports GoogleProvider for backward compat."""

from .google import GmailProvider, GoogleProvider

__all__ = ["GmailProvider", "GoogleProvider"]
