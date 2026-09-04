# Simply NALLY

> The smallest reliable agent we can completely understand.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A minimalist AI agent framework implementing a ReAct (Reason + Act) loop with built-in tools, multi-provider LLM support, and optional integrations. Ground-up rewrite of `N.A.L.L.Y.` — not a fork.

## Philosophy

- Build the smallest reliable agent first. Every subsystem must earn its place.
- Prefer explicit code over abstractions. Prefer stdlib over dependencies.
- `BUILD → TEST → UNDERSTAND → HARDEN → SIMPLIFY → REPEAT`

## What's New in v1.0

- **Telegram Bot** — Run as a Telegram bot with `/link` device flow authentication
- **MCP Integrations** — GitHub, Gmail, and Notion via Model Context Protocol
- **Memory System** — Per-user facts and preferences with `remember`/`recall`/`forget`
- **OAuth Flows** — Google, GitHub, and Notion authentication (Desktop + Device flows)
- **NEON Persistence** — Cloud-hosted session history and user data
- **Render Deployment** — One-click deployment with `render.yaml`

## Features

- **ReAct Agent Loop** — Reason + Act pattern with tool validation and safety checks
- **Multi-Provider LLM** — OpenAI, Groq, NVIDIA NIM, Opencode (Zen) with provider defaults
- **Built-in Tools** — Filesystem, shell, web search, URL fetch, memory (6+ tools)
- **MCP Support** — Model Context Protocol for GitHub, Gmail, Notion integrations
- **Authentication** — Google OAuth (Desktop + Device), GitHub Device Flow, Notion PKCE
- **Session Persistence** — NEON Postgres with automatic schema management
- **Telegram Bot** — Polling + webhook modes with `/link` device flow
- **Memory System** — Per-user facts, preferences, and command handling
- **CLI Interface** — Interactive mode, single-shot commands, and subcommand system
- **Testing** — pytest + pytest-asyncio with comprehensive test suite

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt
# or with dev tools:
pip install -e ".[dev]"

# Configure environment
cp .env.example .env
# Edit .env with your API key and settings

# Run interactive mode
python main.py

# Or single-shot command
python main.py "List files in the current directory"

# Check CLI help
python main.py --help
```

## CLI Reference

### Chat Mode (Default)
```bash
python main.py [prompt] [--model MODEL] [--max-iterations N] [--no-persist]
```

### Subcommands
```bash
# Authentication
python main.py auth login        # Google OAuth (opens browser)
python main.py auth logout       # Clear local session
python main.py auth status       # Show login/session status
python main.py auth init-db      # Initialize NEON Postgres schema

# Session History
python main.py history [--json] [--limit N]
python main.py clear             # Clear persisted history

# Telegram Bot
python main.py telegram [--token TOKEN] [--drop-pending]

# MCP Integrations
python main.py mcp status        # Show MCP + GitHub/Gmail auth status
python main.py mcp login         # GitHub device flow (permanent)
python main.py mcp logout        # Clear cached GitHub token
python main.py mcp gmail-login   # Gmail OAuth (device flow)
python main.py mcp gmail-logout  # Clear cached Gmail token
python main.py mcp gmail-status  # Gmail MCP status only
```

### Interactive Mode Commands
- `exit` / `quit` — Exit interactive mode
- `clear` / `/clear` — Clear conversation history
- `/history` — Show message history
- `/status` / `/auth` — Show auth and persistence status

## Project Layout

```
simply-nally/
├── main.py                          # CLI entry point (675 lines)
├── pyproject.toml                   # Project metadata, deps, ruff/pytest config
├── requirements.txt                 # Flat dependency list
├── render.yaml                      # Render.com deployment config
├── .env.example                     # Full env reference (106 lines)
├── nally/
│   ├── __init__.py                  # __version__ = "1.0.0"
│   ├── config.py                    # Env-based settings, provider defaults, system prompt
│   ├── llm.py                       # Thin OpenAI-compatible LLM client wrapper
│   ├── agent.py                     # ReAct agent loop (core logic)
│   ├── conversation.py              # Message history + optional persistence bridge
│   ├── session.py                   # SessionStore — bridges Agent ↔ NEON
│   ├── db.py                        # Postgres schema, CRUD (users, sessions, messages, facts)
│   ├── auth.py                      # Google OAuth (Desktop + Device flows)
│   ├── github_oauth.py              # GitHub OAuth device flow
│   ├── notion_oauth.py              # Notion OAuth (PKCE)
│   ├── runtime.py                   # Runtime utilities
│   ├── tools/
│   │   ├── base.py                  # Tool base class + ToolRegistry + validation
│   │   ├── filesystem.py            # read_file, write_file, list_dir
│   │   ├── shell.py                 # run_command
│   │   ├── websearch.py             # web_search (DuckDuckGo)
│   │   ├── fetch.py                 # fetch (URL reader)
│   │   ├── memory.py                # Memory tools (remember, recall, forget)
│   │   └── mcp/                     # MCP tool loading
│   ├── integrations/
│   │   ├── base.py                  # Integration base class
│   │   ├── manager.py               # IntegrationManager
│   │   ├── token_store.py           # File-based token cache (~/.config/simply-nally/tokens/)
│   │   ├── github.py                # GitHub MCP integration
│   │   ├── gmail.py                 # Gmail MCP integration
│   │   └── notion.py                # Notion MCP integration
│   ├── mcp/
│   │   ├── adapter.py               # MCP tool discovery + normalization
│   │   ├── auth.py                  # MCP auth injection
│   │   └── client.py                # MCP client
│   ├── memory/
│   │   ├── models.py                # Memory data models
│   │   ├── manager.py               # MemoryManager (retrieve, store, handle commands)
│   │   └── store.py                 # Memory store
│   └── telegram/
│       ├── bot.py                   # Bot entry point (polling + webhook)
│       ├── formatting.py            # Message formatting
│       ├── mcp_ui.py                # MCP status UI for Telegram
│       └── ux/                      # UX helpers
└── tests/
    ├── conftest.py
    ├── test_agent.py
    ├── test_config.py
    ├── test_memory.py
    ├── test_persist.py
    ├── test_telegram.py
    ├── test_tools.py
    └── test_web.py
```

## Built-in Tools

| Tool | Description | Usage |
|------|-------------|-------|
| `read_file` | Read file contents | `read_file(path="file.txt")` |
| `write_file` | Create or overwrite files | `write_file(path="file.txt", content="...")` |
| `list_dir` | List directory contents | `list_dir(path=".")` |
| `run_command` | Execute shell commands | `run_command(command="ls -la")` |
| `web_search` | Search the internet (DuckDuckGo) | `web_search(query="python async")` |
| `fetch` | Read URL contents | `fetch(url="https://example.com")` |

### Memory Tools (Per-User)
| Tool | Description | Usage |
|------|-------------|-------|
| `remember` | Store a fact or preference | `remember(key="language", value="python")` |
| `recall` | Retrieve stored facts | `recall(query="language")` |
| `forget` | Remove stored facts | `forget(key="language")` |

## Integrations

### Google OAuth (Desktop Flow)
1. Create OAuth client at [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`
3. Run `python main.py auth login` — opens browser for authentication

### Telegram Bot
1. Create bot via [@BotFather](https://t.me/BotFather) on Telegram
2. Set `TELEGRAM_BOT_TOKEN` in `.env`
3. Run `python main.py telegram` (polling) or deploy with `render.yaml` (webhook)
4. Users authenticate via `/link` command using device flow

### MCP Integrations
Enable with `NALLY_MCP_ENABLED=true` in `.env`:

**GitHub MCP:**
```bash
python main.py mcp login     # Device flow (permanent token)
python main.py mcp status    # Check authentication
```

**Gmail MCP:**
```bash
python main.py mcp gmail-login   # Google OAuth device flow
python main.py mcp gmail-status  # Check Gmail auth
```

**Notion MCP:**
- Set `NOTION_TOKEN` (local stdio) or `NOTION_MCP_URL` (remote) in `.env`

### Memory System
- Per-user facts stored in NEON Postgres
- Automatic retrieval on conversation start
- Use natural language: "Remember that I prefer Python" or "What's my preferred language?"

## Deployment

### Render (Recommended)
```bash
# One-click deployment
render yaml render.yaml

# Or manual setup:
# 1. Set environment variables in Render dashboard
# 2. Deploy with start command: python main.py telegram
```

### Environment Variables
See `.env.example` for complete reference. Key variables:
- `NALLY_PROVIDER` — LLM provider (openai/groq/opencode/nvidia)
- `NVIDIA_API_KEY` / `OPENAI_API_KEY` / `GROQ_API_KEY` — Provider API keys
- `DATABASE_URL` — NEON Postgres connection string
- `TELEGRAM_BOT_TOKEN` — Telegram bot token
- `NALLY_MCP_ENABLED` — Enable MCP integrations

## Relationship to N.A.L.L.Y.

```
Desktop/N.A.L.L.Y.  (READ ONLY)  ──►  Desktop/simply-nally  (this repo → Klyntech/simply-nally)
```

Dependency is one-way. Simply NALLY never imports from N.A.L.L.Y.

## Testing

```bash
# Run tests
pytest

# Run with coverage
pytest --cov=nally

# Run specific test file
pytest tests/test_agent.py
```

## License

MIT
