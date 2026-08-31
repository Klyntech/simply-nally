"""Simply NALLY agent — the smallest reliable loop.

Flow:
  user message -> append -> LLM (with tools) -> tool_calls? -> validate -> execute -> append results -> loop
  No LangGraph, no memory, no guardrails. Just a while loop that works.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from .config import MAX_ITERATIONS, MAX_TOOL_CALLS, get_system_prompt
from .llm import LLMClient, LLMError, default_client
from .tools.base import ToolRegistry
from .tools.fetch import register_fetch_tools
from .tools.filesystem import register_filesystem_tools
from .tools.shell import register_shell_tools
from .tools.think import register_think_tools
from .tools.websearch import register_web_tools


def build_default_registry(
    max_output: int = 8000,
    mcp_config: dict | None = None,
    load_mcp: bool = True,
) -> ToolRegistry:
    """Create a registry with all v0.1 tools (+ MCP when enabled).

    MCP tools are discovered via ``nally.mcp`` and injected as normalized
    ``Tool`` objects. Agent never knows whether a tool came from
    ``filesystem.py`` or an MCP server.
    """
    import logging

    registry = ToolRegistry(max_output=max_output)
    register_filesystem_tools(registry)
    register_shell_tools(registry)
    register_web_tools(registry)
    register_fetch_tools(registry)
    register_think_tools(registry)
    if load_mcp:
        try:
            from .config import MCP_ENABLED, get_mcp_servers_config

            if MCP_ENABLED:
                cfg = mcp_config if mcp_config is not None else get_mcp_servers_config()
                if cfg:
                    try:
                        from nally.mcp.adapter import load_mcp_tools_sync

                        load_mcp_tools_sync(registry, config=cfg)
                    except Exception as exc:
                        logging.getLogger(__name__).warning(
                            "MCP tools not loaded: %s: %s", type(exc).__name__, exc
                        )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "MCP setup failed: %s: %s", type(exc).__name__, exc
            )
    return registry


class Agent:
    """ReAct agent loop."""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        registry: ToolRegistry | None = None,
        system_prompt: str | None = None,
        max_iterations: int | None = None,
        max_tool_calls: int | None = None,
        session_store: Any | None = None,
        auto_persist: bool = True,
        on_tool_start: Any | None = None,
    ) -> None:
        self.llm: LLMClient = llm_client or default_client
        self.registry: ToolRegistry = registry or build_default_registry()
        self.max_iterations = max_iterations if max_iterations is not None else MAX_ITERATIONS
        self.max_tool_calls = max_tool_calls if max_tool_calls is not None else MAX_TOOL_CALLS

        self.on_tool_start = on_tool_start

        system_content = system_prompt if system_prompt is not None else get_system_prompt()
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]

        # Persistence: optional SessionStore (NEON). None = in-memory only.
        self.session_store = session_store
        if self.session_store is None and auto_persist:
            try:
                from .session import get_session_store

                self.session_store = get_session_store()
            except Exception:
                self.session_store = None

        # If persisted session has history, load it (replaces the fresh system prompt)
        if self.session_store is not None:
            try:
                loaded = self.session_store.load()
                if loaded:
                    self.messages = loaded
                else:
                    # First run for this user — persist the system prompt so the DB isn't empty
                    self.session_store.append(self.messages[0])
            except Exception:
                pass  # never crash on persistence

    # ---------------------------------------------------------------- persist
    def _persist(self, message: dict[str, Any], response=None) -> None:
        """Best-effort persist a message + usage (never raises)."""
        if self.session_store is None:
            return
        try:
            model = None
            prompt_tokens = completion_tokens = total_tokens = None
            # Try to extract usage/model from the LLM response if provided
            if response is not None:
                try:
                    model = getattr(response, "model", None) or self.llm.model
                except Exception:
                    model = self.llm.model
                usage = getattr(response, "usage", None)
                if usage is not None:
                    prompt_tokens = getattr(usage, "prompt_tokens", None)
                    completion_tokens = getattr(usage, "completion_tokens", None)
                    total_tokens = getattr(usage, "total_tokens", None)
                    # OpenAI sometimes uses dict-like usage
                    if isinstance(usage, dict):
                        prompt_tokens = usage.get("prompt_tokens")
                        completion_tokens = usage.get("completion_tokens")
                        total_tokens = usage.get("total_tokens")
                elif isinstance(response, dict) and "usage" in response:
                    u = response["usage"]
                    if isinstance(u, dict):
                        prompt_tokens = u.get("prompt_tokens")
                        completion_tokens = u.get("completion_tokens")
                        total_tokens = u.get("total_tokens")
            # Assistant messages should carry model even without usage
            if message.get("role") == "assistant" and model is None:
                try:
                    model = self.llm.model
                except Exception:
                    model = None
            self.session_store.append(
                message,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ public
    def run(self, user_input: str) -> str:
        """Process a user message and return the final assistant response."""
        if not user_input or not user_input.strip():
            return "Please provide a message."

        user_msg: dict[str, Any] = {"role": "user", "content": user_input}
        self.messages.append(user_msg)
        self._persist(user_msg)

        total_tool_calls = 0

        for _iteration in range(1, self.max_iterations + 1):
            # Guard: too many tool calls overall
            if total_tool_calls >= self.max_tool_calls:
                msg_text = (
                    f"Stopped: reached max tool calls ({self.max_tool_calls}). "
                    f"Partial progress saved in history."
                )
                stop_msg: dict[str, Any] = {"role": "assistant", "content": msg_text}
                self.messages.append(stop_msg)
                self._persist(stop_msg)
                return msg_text

            # Call LLM
            try:
                response = self.llm.chat(
                    messages=self.messages,
                    tools=self.registry.all_schemas() or None,
                )
            except LLMError as exc:
                err_msg = f"LLM error: {exc}"
                err: dict[str, Any] = {"role": "assistant", "content": err_msg}
                self.messages.append(err)
                self._persist(err)
                return err_msg
            except Exception as exc:
                err_msg = f"Unexpected LLM error: {type(exc).__name__}: {exc}"
                err2: dict[str, Any] = {"role": "assistant", "content": err_msg}
                self.messages.append(err2)
                self._persist(err2)
                return err_msg

            choice = response.choices[0]
            msg = choice.message
            tool_calls = getattr(msg, "tool_calls", None)

            # No tools requested -> final response
            if not tool_calls:
                content = msg.content or ""
                # Handle empty response — retry once before giving up
                if not content.strip():
                    self.messages.append(
                        {"role": "user", "content": "(You must respond to the user.)"}
                    )
                    try:
                        response = self.llm.chat(
                            messages=self.messages,
                            tools=self.registry.all_schemas() or None,
                        )
                        choice = response.choices[0]
                        msg = choice.message
                        tool_calls = getattr(msg, "tool_calls", None)
                        if not tool_calls:
                            content = msg.content or ""
                            if not content.strip():
                                content = "I'm having trouble generating a response. Try again with a shorter request."
                            final_msg: dict[str, Any] = {"role": "assistant", "content": content}
                            self.messages.append(final_msg)
                            self._persist(final_msg, response=response)
                            return content
                        # Retry returned tool_calls — fall through to tool processing below
                    except Exception:
                        content = "I'm having trouble generating a response. Try again with a shorter request."
                        final_msg = {"role": "assistant", "content": content}
                        self.messages.append(final_msg)
                        self._persist(final_msg)
                        return content

                final_msg = {"role": "assistant", "content": content}
                self.messages.append(final_msg)
                self._persist(final_msg, response=response)
                return content

            # Process tool calls
            # First, append the assistant message with tool_calls (OpenAI history format)
            # Hide CoT reasoning when tool calls exist — internal, not user-visible
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
            self.messages.append(assistant_msg)
            self._persist(assistant_msg, response=response)

            # Execute each tool call sequentially (simple, deterministic)
            for tc in tool_calls:
                total_tool_calls += 1
                if total_tool_calls > self.max_tool_calls:
                    break

                name = tc.function.name or ""
                raw_args = tc.function.arguments or "{}"

                # Parse arguments
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError as exc:
                    tool_result = f"Error: invalid JSON arguments for '{name}': {exc}"
                    tool_err: dict[str, Any] = {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    }
                    self.messages.append(tool_err)
                    self._persist(tool_err)
                    continue

                # UX: notify before execution (best-effort, never crash)
                if self.on_tool_start is not None:
                    with contextlib.suppress(Exception):
                        self.on_tool_start(name, args)

                # Registry handles validation + execution + truncation
                result_text, _success = self.registry.execute(name, args)

                tool_msg: dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text,
                }
                self.messages.append(tool_msg)
                self._persist(tool_msg)

            # Check if we hit limit during this batch
            if total_tool_calls >= self.max_tool_calls:
                # Let the LLM summarize what it has so far (one final call without tools)
                try:
                    final = self.llm.chat(
                        messages=[
                            *self.messages,
                            {
                                "role": "system",
                                "content": "You have reached the tool call limit. Summarize what you found so far.",
                            },
                        ],
                        tools=None,
                    )
                    summary = (
                        final.choices[0].message.content
                        or "Tool limit reached. Partial results available."
                    )
                    # Try to persist the summary with its response
                    summary_msg: dict[str, Any] = {"role": "assistant", "content": summary}
                    self.messages.append(summary_msg)
                    self._persist(summary_msg, response=final)
                    return summary
                except Exception:
                    summary = "Tool limit reached. Partial results available."
                    summary_msg2: dict[str, Any] = {"role": "assistant", "content": summary}
                    self.messages.append(summary_msg2)
                    self._persist(summary_msg2)
                    return summary

            # Otherwise continue loop — LLM will see tool results next iteration

        # If we exit loop without returning, we hit max iterations
        msg_text2 = (
            f"Stopped: reached max iterations ({self.max_iterations}) without final response."
        )
        stop2: dict[str, Any] = {"role": "assistant", "content": msg_text2}
        self.messages.append(stop2)
        self._persist(stop2)
        return msg_text2

    def clear_history(self) -> None:
        """Reset to just the system prompt."""
        system_msg = (
            self.messages[0] if self.messages and self.messages[0].get("role") == "system" else None
        )
        if system_msg:
            self.messages = [system_msg]
        else:
            self.messages = [{"role": "system", "content": get_system_prompt()}]
        # Persist clear to DB (delete old messages, keep system prompt)
        if self.session_store is not None:
            import contextlib

            with contextlib.suppress(Exception):
                self.session_store.clear(keep_system_prompt=self.messages[0].get("content", ""))

    def get_history(self) -> list[dict[str, Any]]:
        return list(self.messages)
