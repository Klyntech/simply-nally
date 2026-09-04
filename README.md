# Simply NALLY

> The smallest reliable agent we can completely understand.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A minimalist AI agent framework implementing a ReAct (Reason + Act) loop with built-in tools, multi-provider LLM support, and optional integrations. Ground-up rewrite of `N.A.L.L.Y.` — not a fork.

## Philosophy

- Build the smallest reliable agent first. Every subsystem must earn its place.
- Prefer explicit code over abstractions. Prefer stdlib over dependencies.
- `BUILD → TEST → UNDERSTAND → HARDEN → SIMPLIFY → REPEAT`

## What's New in v2 (MCP Login & Credential Isolation)

- **Browser-only OAuth** — One link, choose account in browser, done. No device codes, no polling, no `enter code` screens.
- **Credential Vault** — Encrypted-at-rest per-user tokens with envelope encryption (AAD binding). Raw tokens never reach Telegram, LLM prompts, or logs.
- **AuthBroker** — Single login session lifecycle with atomic one-time callback consumption, state + PKCE verification, and deterministic failure pages.
- **MCP Broker** — Per-user tool cache with invalidation on connect/disconnect. MCP transport injects ephemeral headers/env at call time.
- **UserDirectory** — Canonical internal UUID linking Telegram, CLI, and provider identities.
- **MCP Integrations** — GitHub, Gmail, and Notion via browser OAuth + PKCE + discovery.
- **NEON Persistence** — Cloud session history + `login_sessions`/`credentials`/`external_identities` tables.
- **Render Deployment** — Single HTTPS callback `https://your-domain/oauth/callback/{provider}`.

## Features

- **ReAct Agent Loop** — Reason + Act pattern with tool validation and safety checks
- **Multi-Provider LLM** — OpenAI, Groq, NVIDIA NIM, Opencode (Zen) with provider defaults
- **Built-in Tools** — Filesystem, shell, web search, URL fetch, memory (6+ tools)
- **MCP Support** — Model Context Protocol for GitHub, Gmail, Notion (broker + vault)
- **Authentication** — Browser-only OAuth (Auth Code + PKCE, RFC 8252/9700), no device flow
- **Session Persistence** — NEON Postgres with encrypted credentials + login sessions
- **Telegram Bot** — Polling + webhook modes with browser link (`/mcp` → one HTTPS link)
- **Memory System** — Per-user facts, preferences, and command handling
- **CLI Interface** — Browser login, `mcp connect/disconnect/status`, loopback callback
- **Testing** — pytest + pytest-asyncio with vault/credential isolation tests

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
# Authentication (browser-only)
python main.py login [--provider google|github|gmail|notion]  # Opens browser, choose account
python main.py logout [--provider google|github|gmail|notion]
python main.py status                # Vault + session status
python main.py auth login            # Alias for `login` (Google)
python main.py auth logout
python main.py auth status
python main.py auth init-db          # Initialize NEON Postgres schema

# Session History
python main.py history [--json] [--limit N]
python main.py clear                 # Clear persisted history

# Telegram Bot
python main.py telegram [--token TOKEN] [--drop-pending]

# MCP — browser-only (v2)
python main.py mcp status            # Vault status for all providers
python main.py mcp connect <provider>   # github|gmail|notion — opens browser or prints URL
python main.py mcp disconnect <provider>
python main.py mcp status            # Same as `status`
```

### Interactive Mode Commands
- `exit` / `quit` — Exit interactive mode
- `clear` / `/clear` — Clear conversation history
- `/history` — Show message history
- `/status` / `/auth` — Show auth and persistence status

## Project Layout

```
simply-nally/
├── main.py
├── pyproject.toml
├── .env.example                     # Vault + browser OAuth env
├── nally/
│   ├── config.py                    # Env, providers, vault master key
│   ├── db.py                        # Postgres schema (users, sessions, messages, facts, login_sessions, credentials, external_identities)
│   ├── directory.py                 # UserDirectory — canonical UUID linking
│   ├── vault/
│   │   ├── __init__.py              # CredentialVault (encrypted, DB + file fallback)
│   │   └── crypto.py                # Envelope encryption (AES-GCM + AAD binding)
│   ├── auth_broker/
│   │   ├── __init__.py              # AuthBroker — single browser login lifecycle
│   │   ├── models.py                # LoginSession, ProviderIdentity
│   │   └── loopback.py              # CLI loopback server (127.0.0.1 ephemeral port)
│   ├── oauth/
│   │   ├── providers/               # github, google (gmail), notion (+ PKCE/discovery)
│   │   └── ...
│   ├── mcp/
│   │   ├── broker.py                # MCPConnectionBroker + per-user tool cache
│   │   ├── adapter.py               # MCP tool discovery (vault-based auth injection)
│   │   └── client.py                # MCP client
│   ├── tools/                       # registry + filesystem/shell/web/memory
│   ├── memory/                      # MemoryManager + store
│   ├── telegram/
│   │   ├── bot.py                   # Webhook + polling, central /oauth/callback
│   │   ├── mcp_ui.py                # Browser-only MCP UI (one HTTPS link, vault polling)
│   │   └── ux/
│   └── agent.py / conversation.py / session.py / llm.py
└── tests/
    ├── conftest.py
    └── test_*.py
```

> Legacy `nally/integrations/`, `github_oauth.py`, `notion_oauth.py`, and `mcp/auth.py` device-flow code remain for
> migration grace period but are not used when `NALLY_AUTH_V2=true` (default). They will be removed.

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

### Google OAuth (Browser Flow, RFC 8252)
1. Create OAuth client (Web Application) at [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Add redirect: `https://your-domain/oauth/callback/gmail` (prod) or rely on loopback for CLI
3. Set `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` + `OAUTH_BASE_URL` (prod) in `.env`
4. Run `python main.py login` — opens browser, choose account, done. No device code.

### Telegram Bot
1. Create bot via [@BotFather](https://t.me/BotFather)
2. Set `TELEGRAM_BOT_TOKEN` + `OAUTH_BASE_URL=https://your-domain` in `.env`
3. Run `python main.py telegram` (polling) or deploy via `render.yaml` (webhook)
4. In Telegram: `/mcp` → tap `Connect GitHub/Gmail/Notion` → one HTTPS link → browser → choose account → success message.

### MCP Integrations (Browser-only)
Enable with `NALLY_MCP_ENABLED=true` and set provider client IDs + `OAUTH_BASE_URL`:

```bash
python main.py mcp status                    # Vault status
python main.py mcp connect github            # Browser → choose GitHub account → cached
python main.py mcp connect gmail             # Browser → choose Google account
python main.py mcp connect notion            # PKCE + discovery
python main.py mcp disconnect github
```

Tokens are encrypted per `(user_id, provider, subject)` and injected only at MCP transport time. No env fallback in multi-user mode unless `NALLY_ALLOW_ENV_FALLBACK=true`.

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
