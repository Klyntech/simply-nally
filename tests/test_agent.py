"""Tests for the agent loop — with a fake LLM so no API call is made."""

from __future__ import annotations

import json

from nally.agent import Agent
from nally.llm import LLMError
from nally.tools import build_default_registry


# ---------------------------------------------------------------------------
# Fake OpenAI response helpers
# ---------------------------------------------------------------------------
class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id: str, name: str, args: dict):
        self.id = id
        self.function = _FakeFunction(name, json.dumps(args))


class _FakeMessage:
    def __init__(self, content: str | None = None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, content=None, tool_calls=None):
        self.choices = [_FakeChoice(_FakeMessage(content, tool_calls))]


class FakeLLM:
    """Queue of responses to return."""

    def __init__(self, responses: list[_FakeResponse] | Exception):
        if isinstance(responses, Exception):
            self._responses = responses
            self._is_error = True
        else:
            self._responses = list(responses)
            self._is_error = False
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, model=None, temperature=0.7, max_tokens=4096):
        self.calls.append({"messages": list(messages), "tools": tools})
        if self._is_error:
            raise self._responses  # type: ignore
        if not self._responses:
            return _FakeResponse(content="done")
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestAgentBasic:
    def test_no_tool_needed(self):
        llm = FakeLLM([_FakeResponse(content="Hello there!")])
        agent = Agent(llm_client=llm, registry=build_default_registry())
        result = agent.run("hi")
        assert result == "Hello there!"
        assert len(llm.calls) == 1

    def test_empty_input(self):
        llm = FakeLLM([_FakeResponse(content="hi")])
        agent = Agent(llm_client=llm)
        assert "Please provide" in agent.run("")
        assert "Please provide" in agent.run("   ")

    def test_llm_error_reported(self):
        llm = FakeLLM(LLMError("auth failed"))
        agent = Agent(llm_client=llm)
        result = agent.run("hi")
        assert "LLM error" in result

    def test_single_tool_then_answer(self, tmp_path):
        # LLM first asks to list_dir, then answers
        llm = FakeLLM(
            [
                _FakeResponse(
                    content="",
                    tool_calls=[_FakeToolCall("call_1", "list_dir", {"path": str(tmp_path)})],
                ),
                _FakeResponse(content="Found 0 files."),
            ]
        )
        # Create a file so list_dir has something
        (tmp_path / "a.txt").write_text("x")
        agent = Agent(llm_client=llm, registry=build_default_registry())
        result = agent.run("list files")
        assert result == "Found 0 files."
        assert len(llm.calls) == 2
        # Second call should have tool result in history
        second_messages = llm.calls[1]["messages"]
        assert any(m.get("role") == "tool" for m in second_messages)

    def test_write_file_via_agent(self, tmp_path):
        p = tmp_path / "out.txt"
        llm = FakeLLM(
            [
                _FakeResponse(
                    tool_calls=[
                        _FakeToolCall("call_1", "write_file", {"path": str(p), "content": "hello"})
                    ]
                ),
                _FakeResponse(content="Done, wrote file."),
            ]
        )
        agent = Agent(llm_client=llm, registry=build_default_registry(workspace=tmp_path))
        result = agent.run("write hello to out.txt")
        assert result == "Done, wrote file."
        assert p.read_text() == "hello"

    def test_invalid_json_args(self):
        # Simulate LLM returning malformed JSON (we craft response manually)
        class BadJsonLLM:
            def __init__(self):
                self.n = 0
                self.calls = []

            def chat(self, messages, tools=None, **kwargs):
                self.calls.append(messages)
                if self.n == 0:
                    self.n += 1
                    # Return a tool call with bad JSON string
                    msg = _FakeMessage(content="", tool_calls=None)
                    # Patch raw arguments to be invalid JSON
                    tc = _FakeToolCall("call_1", "read_file", {"path": "x"})
                    tc.function.arguments = "{bad json"
                    msg.tool_calls = [tc]
                    return _FakeResponse(content=None, tool_calls=[tc])
                return _FakeResponse(content="Recovered.")

        llm = BadJsonLLM()
        agent = Agent(llm_client=llm)
        result = agent.run("read x")
        assert result == "Recovered."
        # Should have fed an error back to LLM
        assert any(
            m.get("role") == "tool" and "invalid JSON" in m.get("content", "") for m in llm.calls[1]
        )

    def test_unknown_tool(self):
        llm = FakeLLM(
            [
                _FakeResponse(tool_calls=[_FakeToolCall("call_1", "no_such_tool", {})]),
                _FakeResponse(content="I see the tool does not exist."),
            ]
        )
        agent = Agent(llm_client=llm)
        result = agent.run("do magic")
        assert "does not exist" in result

    def test_max_iterations_guard(self):
        # LLM keeps requesting tools forever
        responses = [
            _FakeResponse(tool_calls=[_FakeToolCall(f"call_{i}", "list_dir", {"path": "."})])
            for i in range(10)
        ]
        llm = FakeLLM(responses)
        agent = Agent(llm_client=llm, max_iterations=3, max_tool_calls=100)
        result = agent.run("loop forever")
        assert "max iterations" in result.lower()
        assert len(llm.calls) == 3

    def test_max_tool_calls_guard(self):
        # Each turn has 2 tool calls -> hit limit quickly
        responses = [
            _FakeResponse(
                tool_calls=[
                    _FakeToolCall("c1", "list_dir", {"path": "."}),
                    _FakeToolCall("c2", "list_dir", {"path": "."}),
                ]
            ),
            _FakeResponse(content="Summary after limit."),
        ]
        llm = FakeLLM(responses)
        agent = Agent(llm_client=llm, max_iterations=10, max_tool_calls=2)
        result = agent.run("many tools")
        # After 2 calls we hit limit and ask for summary
        assert "Summary after limit" in result

    def test_history_tracking(self):
        llm = FakeLLM([_FakeResponse(content="hi")])
        agent = Agent(llm_client=llm)
        assert len(agent.get_history()) == 1  # system
        agent.run("hello")
        assert len(agent.get_history()) == 3  # system, user, assistant
        agent.clear_history()
        assert len(agent.get_history()) == 1

    def test_custom_system_prompt(self):
        llm = FakeLLM([_FakeResponse(content="ok")])
        agent = Agent(llm_client=llm, system_prompt="You are a test bot.")
        assert agent.messages[0]["content"] == "You are a test bot."

    def test_multiple_tool_calls_in_one_turn(self, tmp_path):
        (tmp_path / "f1.txt").write_text("one")
        (tmp_path / "f2.txt").write_text("two")
        llm = FakeLLM(
            [
                _FakeResponse(
                    tool_calls=[
                        _FakeToolCall("c1", "read_file", {"path": str(tmp_path / "f1.txt")}),
                        _FakeToolCall("c2", "read_file", {"path": str(tmp_path / "f2.txt")}),
                    ]
                ),
                _FakeResponse(content="Got both."),
            ]
        )
        agent = Agent(llm_client=llm, registry=build_default_registry(workspace=tmp_path))
        result = agent.run("read both")
        assert result == "Got both."
        # Both tool results should be in history
        hist = agent.get_history()
        tool_msgs = [m for m in hist if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        assert any("one" in m["content"] for m in tool_msgs)
        assert any("two" in m["content"] for m in tool_msgs)
