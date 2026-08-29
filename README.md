# Simply NALLY

> The smallest reliable agent we can completely understand.

Rebuilt from scratch. Not a fork of `N.A.L.L.Y.` — that project is kept as a read-only reference on the Desktop.

## Philosophy

- Build the smallest reliable agent first. Every subsystem must earn its place.
- Prefer explicit code over abstractions. Prefer stdlib over dependencies.
- `BUILD → TEST → UNDERSTAND → HARDEN → SIMPLIFY → REPEAT`

## What it does (v0.1)

```
User → Agent Loop → LLM → Tool Decision → Registry → Validation → Execution → LLM → Final Response
```

- Receives a user message
- Sends context to an LLM (OpenAI-compatible)
- Decides when a tool is needed, selects it, validates args, executes safely
- Returns results to the LLM and produces a final response

## Quick start

```bash
pip install -r requirements.txt  # or pip install -e ".[dev]"
cp .env.example .env             # fill in your API key
python main.py --help
python main.py "List files in the current directory"
```

## Project layout

```
simply-nally/
├── main.py            # CLI entry point
├── nally/
│   ├── config.py      # env-based settings, no side effects
│   ├── llm.py         # OpenAI-compatible client
│   ├── agent.py       # ReAct loop
│   └── tools/
│       ├── base.py        # Tool + ToolRegistry
│       ├── filesystem.py  # read_file, write_file, list_dir
│       └── shell.py       # run_command
└── tests/
```

## Relationship to N.A.L.L.Y.

```
Desktop/N.A.L.L.Y.  (READ ONLY)  ──►  Desktop/simply-nally  (this repo → Klyntech/simply-nally)
```

Dependency is one-way. Simply NALLY never imports from N.A.L.L.Y.

## License

MIT
