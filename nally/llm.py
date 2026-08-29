"""Minimal OpenAI-compatible LLM client.

No multi-key rotation, no model fallback chains, no Responses API.
Just a thin wrapper around openai.OpenAI that knows how to read nally.config.
"""

from __future__ import annotations

from typing import Any

from .config import API_KEY, BASE_URL, MODEL


class LLMError(Exception):
    """Raised when the LLM call fails in a way the agent should report."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMClient:
    """Thin wrapper around openai.OpenAI."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = (api_key or API_KEY or "").strip()
        self.base_url = (base_url or BASE_URL or "").strip()
        self.model = (model or MODEL or "").strip()
        self._client = None  # lazy

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise LLMError(
                "Missing API key. Set OPENAI_API_KEY (or provider key) in .env",
                retryable=False,
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError("openai package not installed. Run: pip install openai") from exc

        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url if self.base_url else None,
            timeout=60.0,
            max_retries=2,
        )
        return self._client

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        """Call chat.completions.create. Returns OpenAI response object."""
        client = self._ensure_client()
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            msg = str(exc).lower()
            # Map common errors to clearer messages
            if "401" in msg or "auth" in msg or "api key" in msg:
                raise LLMError(f"Authentication failed: {exc}", retryable=False) from exc
            if "429" in msg or "rate" in msg:
                raise LLMError(f"Rate limited: {exc}", retryable=True) from exc
            if "model" in msg and ("not found" in msg or "does not exist" in msg):
                raise LLMError(f"Model not found: {exc}", retryable=False) from exc
            raise LLMError(f"LLM call failed: {exc}", retryable=False) from exc

    def simple_chat(self, user_message: str, system_prompt: str | None = None) -> str:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})
        resp = self.chat(messages)
        return resp.choices[0].message.content or ""


# Default singleton (lazy — safe to import even without API key)
default_client = LLMClient()
