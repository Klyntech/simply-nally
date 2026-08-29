#!/usr/bin/env python3
"""Simply NALLY — CLI entry point."""

from __future__ import annotations

import argparse
import sys

from nally.agent import Agent
from nally.config import validate_config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Simply NALLY — the smallest reliable agent we can completely understand.",
    )
    p.add_argument(
        "prompt",
        nargs="*",
        help="User message. If omitted, enters interactive mode.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Override model (default from NALLY_MODEL / provider default)",
    )
    p.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Override max iterations per turn",
    )
    return p


def run_once(agent: Agent, prompt: str) -> int:
    print(f"\n> {prompt}\n")
    try:
        reply = agent.run(prompt)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    print(reply)
    return 0


def interactive_loop(agent: Agent) -> int:
    print("Simply NALLY — interactive mode. Type 'exit' or 'quit' to leave.\n")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0
        if not user:
            continue
        if user.lower() in ("exit", "quit", "/exit", "/quit"):
            print("Bye.")
            return 0
        if user.lower() in ("clear", "/clear"):
            agent.clear_history()
            print("(history cleared)")
            continue
        reply = agent.run(user)
        print(f"\nnally> {reply}\n")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    errors = validate_config(require_api_key=True)
    if errors:
        for e in errors:
            print(f"Config error: {e}", file=sys.stderr)
        print("Hint: copy .env.example to .env and set your API key.", file=sys.stderr)
        return 2

    # Build agent with optional overrides
    kwargs: dict = {}
    if args.model:
        from nally.llm import LLMClient

        # Override model only; keep api key / base_url from env
        kwargs["llm_client"] = LLMClient(model=args.model)
    if args.max_iterations:
        kwargs["max_iterations"] = args.max_iterations

    agent = Agent(**kwargs)

    if args.prompt:
        prompt = " ".join(args.prompt)
        return run_once(agent, prompt)
    else:
        return interactive_loop(agent)


if __name__ == "__main__":
    raise SystemExit(main())
