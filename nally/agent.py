"""Simply NALLY agent — the smallest reliable loop.

Flow:
  user message -> append -> LLM (with tools) -> tool_calls? -> validate -> execute -> append results -> loop
  No LangGraph, no memory, no guardrails. Just a while loop that works.
"""

from __future__ import annotations

import json
from typing import Any

from .config import MAX_ITERATIONS, MAX_TOOL_CALLS, get_system_prompt
from .llm import LLMClient, LLMError, default_client
from .tools.base import ToolRegistry
from .tools.fetch import register_fetch_tools
from .tools.filesystem import register_filesystem_tools
from .tools.shell import register_shell_tools
from .tools.websearch import register_web_tools


def build_default_registry(max_output: int = 8000) -> ToolRegistry:
    """Create a registry with all v0.1 tools."""
    registry = ToolRegistry(max_output=max_output)
    register_filesystem_tools(registry)
    register_shell_tools(registry)
    register_web_tools(registry)
    register_fetch_tools(registry)
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
    ) -> None:
        self.llm: LLMClient = llm_client or default_client
        self.registry: ToolRegistry = registry or build_default_registry()
        self.max_iterations = max_iterations if max_iterations is not None else MAX_ITERATIONS
        self.max_tool_calls = max_tool_calls if max_tool_calls is not None else MAX_TOOL_CALLS

        system_content = system_prompt if system_prompt is not None else get_system_prompt()
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]

    # ------------------------------------------------------------------ public
    def run(self, user_input: str) -> str:
        """Process a user message and return the final assistant response."""
        if not user_input or not user_input.strip():
            return "Please provide a message."

        self.messages.append({"role": "user", "content": user_input})

        total_tool_calls = 0

        for _iteration in range(1, self.max_iterations + 1):
            # Guard: too many tool calls overall
            if total_tool_calls >= self.max_tool_calls:
                msg = (
                    f"Stopped: reached max tool calls ({self.max_tool_calls}). "
                    f"Partial progress saved in history."
                )
                self.messages.append({"role": "assistant", "content": msg})
                return msg

            # Call LLM
            try:
                response = self.llm.chat(
                    messages=self.messages,
                    tools=self.registry.all_schemas() or None,
                )
            except LLMError as exc:
                err_msg = f"LLM error: {exc}"
                self.messages.append({"role": "assistant", "content": err_msg})
                return err_msg
            except Exception as exc:
                err_msg = f"Unexpected LLM error: {type(exc).__name__}: {exc}"
                self.messages.append({"role": "assistant", "content": err_msg})
                return err_msg

            choice = response.choices[0]
            msg = choice.message
            tool_calls = getattr(msg, "tool_calls", None)

            # No tools requested -> final response
            if not tool_calls:
                content = msg.content or ""
                # Handle empty response
                if not content.strip():
                    content = "(no response from model)"
                self.messages.append({"role": "assistant", "content": content})
                return content

            # Process tool calls
            # First, append the assistant message with tool_calls (OpenAI history format)
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
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
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_result,
                        }
                    )
                    continue

                # Registry handles validation + execution + truncation
                result_text, _success = self.registry.execute(name, args)

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    }
                )

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
                except Exception:
                    summary = "Tool limit reached. Partial results available."
                self.messages.append({"role": "assistant", "content": summary})
                return summary

            # Otherwise continue loop — LLM will see tool results next iteration

        # If we exit loop without returning, we hit max iterations
        msg = f"Stopped: reached max iterations ({self.max_iterations}) without final response."
        self.messages.append({"role": "assistant", "content": msg})
        return msg

    def clear_history(self) -> None:
        """Reset to just the system prompt."""
        system_msg = (
            self.messages[0] if self.messages and self.messages[0].get("role") == "system" else None
        )
        if system_msg:
            self.messages = [system_msg]
        else:
            self.messages = [{"role": "system", "content": get_system_prompt()}]

    def get_history(self) -> list[dict[str, Any]]:
        return list(self.messages)
