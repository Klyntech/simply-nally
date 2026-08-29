"""Think tool — internal reasoning, hidden from user."""

from __future__ import annotations

from .base import Tool


class Think(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="think",
            description=(
                "Internal reasoning. Use before acting on complex tasks to plan "
                "your approach. Think through the problem, consider alternatives, "
                "and decide on the best next step. Hidden from the user."
            ),
            parameters={
                "thought": {
                    "type": "string",
                    "description": "Your internal reasoning and plan",
                    "required": True,
                }
            },
        )

    def execute(self, thought: str = "", **kwargs) -> str:  # type: ignore[override]
        # No output — reasoning is captured via the tool call itself, hidden from user.
        # Return empty so it does not clutter tool results.
        return ""


def register_think_tools(registry) -> None:
    registry.register(Think())
