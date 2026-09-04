"""Simply NALLY agent — the smallest reliable loop.

Flow:
  user message -> retrieve memories (once) -> append -> LLM (with tools) -> tool_calls? -> validate -> execute -> append results -> loop
  No LangGraph, no guardrails. Just a while loop that works.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

from .config import MAX_ITERATIONS, MAX_TOOL_CALLS, get_system_prompt
from .conversation import Conversation
from .llm import LLMClient, LLMError, default_client
from .tools import build_default_registry
from .tools.base import ToolRegistry

logger = logging.getLogger(__name__)


class Agent:
    """ReAct agent loop."""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        registry: ToolRegistry | None = None,
        conversation: Conversation | None = None,
        system_prompt: str | None = None,
        max_iterations: int | None = None,
        max_tool_calls: int | None = None,
        on_tool_start: Any | None = None,
        telegram_user_id: str | None = None,
    ) -> None:
        self.llm: LLMClient = llm_client or default_client
        self.max_iterations = max_iterations if max_iterations is not None else MAX_ITERATIONS
        self.max_tool_calls = max_tool_calls if max_tool_calls is not None else MAX_TOOL_CALLS
        self.on_tool_start = on_tool_start
        self._telegram_user_id = telegram_user_id

        if conversation is not None:
            self.conversation = conversation
        else:
            prompt = system_prompt if system_prompt is not None else get_system_prompt()
            self.conversation = Conversation(
                system_prompt=prompt,
                default_model=getattr(self.llm, "model", None),
            )

        # Build registry with user_id if available (enables memory tools)
        user_id = self._get_user_id()
        if registry is not None:
            self.registry = registry
        else:
            # Prefer internal UUID for MCP vault lookup (credentials keyed by directory id)
            mcp_id = telegram_user_id
            if telegram_user_id:
                try:
                    from .directory import get_directory

                    u = get_directory().get_or_create_for_telegram(telegram_id=str(telegram_user_id))
                    if u and u.get("id"):
                        mcp_id = u["id"]
                except Exception:
                    pass
            self.registry = build_default_registry(
                user_id=user_id or mcp_id,
                mcp_user_id=mcp_id,
            )

        # Memory manager for dynamic retrieval (lazy init)
        self._memory_manager: Any | None = None
        if user_id:
            try:
                from .memory.manager import MemoryManager

                self._memory_manager = MemoryManager(user_id)
            except Exception as exc:
                logger.debug("MemoryManager init skipped: %s", exc)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.conversation.get_messages()

    def _get_user_id(self) -> str | None:
        return self.conversation.user_id

    def _build_messages_with_context(self, memory_context: str) -> list[dict[str, Any]]:
        """Build message list with pre-computed memory context injected into system prompt."""
        messages = self.conversation.get_messages()
        if not memory_context:
            return messages

        if messages and messages[0].get("role") == "system":
            enriched = dict(messages[0])
            enriched["content"] = enriched["content"] + "\n\n" + memory_context
            return [enriched, *messages[1:]]

        return [{"role": "system", "content": memory_context}, *messages]


    def _ensure_mcp_tools(self) -> str:
        """Refresh MCP/fallback tools and return a short status line for the model.

        Cached agents keep a stale empty registry after OAuth connect or deploys.
        Re-registering is cheap when tools already exist; critical when they don't.
        """
        try:
            from nally.config import MCP_ENABLED
            if not MCP_ENABLED:
                return "MCP is disabled (NALLY_MCP_ENABLED is not true)."
        except Exception:
            return ""

        lookup = None
        tid = getattr(self, "_telegram_user_id", None)
        if tid:
            try:
                from nally.directory import get_directory
                u = get_directory().get_or_create_for_telegram(telegram_id=str(tid))
                if u and u.get("id"):
                    lookup = u["id"]
            except Exception:
                lookup = str(tid)
        if not lookup:
            lookup = self._get_user_id()

        mcp_count = sum(1 for n in self.registry.names() if n.startswith("mcp_") or n.startswith("mcp__"))
        if mcp_count == 0 and lookup:
            try:
                from nally.tools import build_default_registry
                # Merge freshly built MCP tools into existing registry
                fresh = build_default_registry(
                    user_id=lookup,
                    mcp_user_id=lookup,
                    load_mcp=True,
                )
                for name in fresh.names():
                    if name.startswith("mcp_") or name.startswith("mcp__"):
                        if name not in self.registry:
                            try:
                                self.registry.register(fresh.get(name))  # type: ignore[arg-type]
                            except Exception:
                                pass
            except Exception as exc:
                logger.warning("MCP refresh failed: %s", exc)
            # Explicit GitHub fallback
            try:
                from nally.mcp.github_fallback import register_github_fallback_tools
                register_github_fallback_tools(self.registry, lookup)
            except Exception as exc:
                logger.warning("GitHub fallback refresh failed: %s", exc)

        # Status line for the model
        lines = []
        try:
            from nally.vault import get_vault
            vault = get_vault()
            for p in ("github", "gmail", "notion"):
                cred = vault.get_valid(lookup, p) if lookup else None
                if cred:
                    acct = (cred.provider_metadata or {}).get("account") or cred.subject or "connected"
                    lines.append(f"{p}: connected as {acct}")
                else:
                    lines.append(f"{p}: not connected")
        except Exception:
            pass
        tools = [n for n in self.registry.names() if n.startswith("mcp_") or n.startswith("mcp__")]
        if tools:
            sample = ", ".join(sorted(set(tools))[:15])
            lines.append(f"Available MCP tools (use these exact names): {sample}")
        else:
            lines.append("No MCP tools loaded. If a provider shows connected, reconnect via /mcp.")
        return "MCP status:\n" + "\n".join(lines) if lines else ""

    # ------------------------------------------------------------------ public
    def run(self, user_input: str) -> str:
        """Process a user message and return the final assistant response."""
        if not user_input or not user_input.strip():
            return "Please provide a message."

        # Refresh MCP tools + connection context (fixes stale cached agents)
        mcp_context = ""
        try:
            mcp_context = self._ensure_mcp_tools()
        except Exception as exc:
            logger.debug("ensure mcp tools: %s", exc)

        # Check for explicit memory commands first
        if self._memory_manager is not None:
            cmd_response = self._memory_manager.handle_memory_command(user_input)
            if cmd_response is not None:
                return cmd_response

        user_msg: dict[str, Any] = {"role": "user", "content": user_input}
        self.conversation.append(user_msg)

        # Retrieve memory context ONCE for this turn
        memory_context = ""
        if self._memory_manager is not None:
            memory_context = self._memory_manager.build_context_block(user_input)
        if mcp_context:
            memory_context = (memory_context + "\n\n" + mcp_context).strip() if memory_context else mcp_context

        total_tool_calls = 0

        for _iteration in range(1, self.max_iterations + 1):
            # Guard: too many tool calls overall
            if total_tool_calls >= self.max_tool_calls:
                return self._stop_message(
                    f"Stopped: reached max tool calls ({self.max_tool_calls}). "
                    f"Partial progress saved in history."
                )

            # Build messages with pre-computed memory context
            messages = self._build_messages_with_context(memory_context)

            # Call LLM
            try:
                response = self.llm.chat(
                    messages=messages,
                    tools=self.registry.all_schemas() or None,
                )
            except LLMError as exc:
                return f"LLM error: {exc}"
            except Exception as exc:
                return f"Unexpected LLM error: {type(exc).__name__}: {exc}"

            choice = response.choices[0]
            msg = choice.message
            tool_calls = getattr(msg, "tool_calls", None)

            # No tools requested -> final response
            if not tool_calls:
                content = self._extract_content(msg)

                # Handle empty response — retry once before giving up
                if not content.strip():
                    self.conversation.append(
                        {"role": "user", "content": "(You must respond to the user.)"}
                    )
                    retry_messages = self._build_messages_with_context(memory_context)
                    try:
                        response = self.llm.chat(
                            messages=retry_messages,
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
                            self.conversation.append(final_msg, response=response)
                            return content
                        # Retry returned tool_calls — fall through to tool processing below
                    except Exception:
                        content = "I'm having trouble generating a response. Try again with a shorter request."
                        final_msg = {"role": "assistant", "content": content}
                        self.conversation.append(final_msg)
                        return content

                final_msg = {"role": "assistant", "content": content}
                self.conversation.append(final_msg, response=response)
                return content

            # Process tool calls
            assistant_msg = self._build_assistant_tool_msg(tool_calls)
            self.conversation.append(assistant_msg, response=response)

            total_tool_calls = self._process_tool_calls(tool_calls, total_tool_calls)

            # Check if we hit limit during this batch
            if total_tool_calls >= self.max_tool_calls:
                return self._summarize_limit()

            # Otherwise continue loop — LLM will see tool results next iteration

        # If we exit loop without returning, we hit max iterations
        # Surface last tool error so the user sees the real failure
        last_err = ""
        for m in reversed(self.conversation.get_messages()):
            if m.get("role") == "tool" and isinstance(m.get("content"), str):
                c = m["content"]
                if c.startswith("Error:") or "unknown tool" in c.lower() or "AUTH_REQUIRED" in c:
                    last_err = c[:500]
                    break
        if last_err:
            return self._stop_message(
                f"Stopped: reached max iterations ({self.max_iterations}). Last tool error:\n{last_err}"
            )
        return self._stop_message(
            f"Stopped: reached max iterations ({self.max_iterations}) without final response."
        )

    def clear_history(self) -> None:
        """Reset to just the system prompt."""
        self.conversation.clear()

    def get_history(self) -> list[dict[str, Any]]:
        return self.conversation.get_messages()

    # ----------------------------------------------------------------- private

    def _extract_content(self, msg: Any) -> str:
        """Extract text content, never leaking internal reasoning.

        Reasoning models (Nemotron, Ling) put private CoT in
        ``reasoning_content``/``reasoning``. We must NOT surface that
        as the user-facing answer — it pollutes history and caused the
        16:16 leak (\"Here's a thinking process...\"). Only return
        ``content``; empty content triggers retry.
        """
        content = getattr(msg, "content", None) or ""
        if not content.strip():
            return ""
        # Detect leaked thinking masquerading as content — treat as empty
        low = content.lower()
        if (
            "here's a thinking process" in low
            or "analyze user input" in low
            or "identify available tools" in low
        ) and len(content) > 300:
            # Looks like internal reasoning dumped into content — retry
            return ""
        return content

    def _build_assistant_tool_msg(self, tool_calls: list[Any]) -> dict[str, Any]:
        """Format the assistant message with tool_calls for OpenAI history format."""
        return {
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

    def _process_tool_calls(self, tool_calls: list[Any], current_count: int) -> int:
        """Execute tool calls sequentially. Returns updated count."""
        for tc in tool_calls:
            if current_count >= self.max_tool_calls:
                break
            current_count += 1

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
                self.conversation.append(tool_err)
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
            self.conversation.append(tool_msg)

        return current_count

    def _summarize_limit(self) -> str:
        """Ask the LLM to summarize progress after hitting tool limit."""
        try:
            final = self.llm.chat(
                messages=[
                    *self.conversation.get_messages(),
                    {
                        "role": "system",
                        "content": "You have reached the tool call limit. Summarize what you found so far.",
                    },
                ],
                tools=None,
            )
            summary = (
                final.choices[0].message.content or "Tool limit reached. Partial results available."
            )
        except Exception as exc:
            logger.warning("Summary call failed: %s", exc)
            summary = "Tool limit reached. Partial results available."

        summary_msg: dict[str, Any] = {"role": "assistant", "content": summary}
        self.conversation.append(summary_msg)
        return summary

    def _stop_message(self, text: str) -> str:
        """Append a stop message and return it."""
        stop_msg: dict[str, Any] = {"role": "assistant", "content": text}
        self.conversation.append(stop_msg)
        return text
