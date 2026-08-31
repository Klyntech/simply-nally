"""NEON Postgres persistence — single source of truth.

One session per user (v1). Every message + tool result is persisted.
Memory layer stores explicit user facts/preferences.

Graceful fallback: if DATABASE_URL is not set or psycopg is missing,
all functions return None/[] and the agent runs in-memory only.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_pool: Any = None


def get_database_url() -> str:
    """Resolve DATABASE_URL (NEON). Also checks NALLY_DATABASE_URL."""
    return (
        os.getenv("DATABASE_URL", "").strip()
        or os.getenv("NALLY_DATABASE_URL", "").strip()
        or os.getenv("NEON_DATABASE_URL", "").strip()
    )


def is_configured() -> bool:
    return bool(get_database_url())


def _get_pool():
    """Return the application-level connection pool, creating it lazily."""
    global _pool
    if _pool is not None:
        return _pool
    url = get_database_url()
    if not url:
        return None
    try:
        from psycopg_pool import ConnectionPool  # type: ignore
    except ImportError as exc:
        logger.warning("psycopg_pool not installed, falling back to raw connections: %s", exc)
        return None
    _pool = ConnectionPool(url, min_size=1, max_size=5, check=ConnectionPool.check_connection)
    return _pool


def connect():
    """Open a psycopg (v3) connection. Raises if not configured or driver missing.

    Prefer pooled_connect() for new code.
    """
    url = get_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL not set — persistence disabled")
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise RuntimeError('psycopg not installed. Run: pip install "psycopg[binary]"') from exc
    return psycopg.connect(url)


def pooled_connect():
    """Get a connection from the pool. Falls back to raw connect() if pool unavailable."""
    pool = _get_pool()
    if pool is not None:
        return pool.connection()
    return connect()


def close_pool() -> None:
    """Shutdown the connection pool. Safe to call multiple times."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = r"""
-- Enable pgcrypto for gen_random_uuid() if available (NEON usually has it)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    google_id TEXT UNIQUE,
    telegram_id TEXT UNIQUE,
    email TEXT UNIQUE NOT NULL,
    name TEXT,
    picture TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model TEXT,
    total_tokens INT NOT NULL DEFAULT 0,
    message_count INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
    content TEXT,
    tool_calls JSONB,
    tool_call_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model TEXT,
    prompt_tokens INT,
    completion_tokens INT,
    total_tokens INT,
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);

CREATE TABLE IF NOT EXISTS facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('preference','fact','instruction','profile','project')),
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, key)
);

CREATE INDEX IF NOT EXISTS idx_facts_user_id ON facts(user_id);
CREATE INDEX IF NOT EXISTS idx_facts_user_type ON facts(user_id, type);
CREATE INDEX IF NOT EXISTS idx_facts_user_key ON facts(user_id, key);
"""


def _migrate_add_telegram(conn) -> None:
    """Idempotent migration: add telegram_id column and make google_id nullable."""
    with conn.cursor() as cur:
        # Add telegram_id column if missing
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name='users' AND column_name='telegram_id'
            """
        )
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE users ADD COLUMN telegram_id TEXT")
            conn.commit()
        # Make google_id nullable if still NOT NULL
        cur.execute(
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name='users' AND column_name='google_id'
            """
        )
        row = cur.fetchone()
        if row is not None and row[0] == "NO":
            cur.execute("ALTER TABLE users ALTER COLUMN google_id DROP NOT NULL")
            conn.commit()
        # Ensure partial unique index for telegram_id
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id) WHERE telegram_id IS NOT NULL"
        )
        conn.commit()


def _migrate_add_facts(conn) -> None:
    """Idempotent migration: create facts table for memory layer."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'facts'
            """
        )
        if cur.fetchone() is None:
            cur.execute(
                """
                CREATE TABLE facts (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    type TEXT NOT NULL CHECK (type IN ('preference','fact','instruction','profile','project')),
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'user',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (user_id, key)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_facts_user_id ON facts(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_facts_user_type ON facts(user_id, type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_facts_user_key ON facts(user_id, key)")
            conn.commit()


def init_schema(conn=None) -> None:
    """Create tables if they do not exist. Safe to call multiple times."""
    close = False
    if conn is None:
        try:
            conn = pooled_connect()
        except Exception:
            conn = connect()
        close = True
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
        _migrate_add_telegram(conn)
        _migrate_add_facts(conn)
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def upsert_user(
    conn,
    *,
    google_id: str,
    email: str,
    name: str | None = None,
    picture: str | None = None,
) -> dict[str, Any]:
    """Insert or update a user by google_id. Returns the user row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (google_id, email, name, picture, last_login)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (google_id) DO UPDATE SET
                email = EXCLUDED.email,
                name = EXCLUDED.name,
                picture = EXCLUDED.picture,
                last_login = NOW()
            RETURNING id::text, google_id, telegram_id, email, name, picture, created_at, last_login
            """,
            (google_id, email, name, picture),
        )
        row = cur.fetchone()
        conn.commit()
    # psycopg returns tuple; map to dict
    cols = [
        "id",
        "google_id",
        "telegram_id",
        "email",
        "name",
        "picture",
        "created_at",
        "last_login",
    ]
    return dict(zip(cols, row, strict=False))


def get_user_by_google_id(conn, google_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, google_id, telegram_id, email, name, picture, created_at, last_login FROM users WHERE google_id = %s",
            (google_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [
            "id",
            "google_id",
            "telegram_id",
            "email",
            "name",
            "picture",
            "created_at",
            "last_login",
        ]
        return dict(zip(cols, row, strict=False))


def get_user_by_id(conn, user_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, google_id, telegram_id, email, name, picture, created_at, last_login FROM users WHERE id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [
            "id",
            "google_id",
            "telegram_id",
            "email",
            "name",
            "picture",
            "created_at",
            "last_login",
        ]
        return dict(zip(cols, row, strict=False))


def get_user_by_telegram_id(conn, telegram_id: str) -> dict[str, Any] | None:
    """Lookup user by Telegram ID (string form of the integer)."""
    tid = str(telegram_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, google_id, telegram_id, email, name, picture, created_at, last_login FROM users WHERE telegram_id = %s",
            (tid,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [
            "id",
            "google_id",
            "telegram_id",
            "email",
            "name",
            "picture",
            "created_at",
            "last_login",
        ]
        return dict(zip(cols, row, strict=False))


def link_telegram_to_user(conn, user_id: str, telegram_id: str) -> dict[str, Any]:
    """Set telegram_id on an existing user. Returns updated user row."""
    tid = str(telegram_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE users SET telegram_id = %s, last_login = NOW()
            WHERE id = %s
            RETURNING id::text, google_id, telegram_id, email, name, picture, created_at, last_login
            """,
            (tid, user_id),
        )
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            raise ValueError(f"User {user_id} not found")
        conn.commit()
    cols = [
        "id",
        "google_id",
        "telegram_id",
        "email",
        "name",
        "picture",
        "created_at",
        "last_login",
    ]
    return dict(zip(cols, row, strict=False))


def unlink_telegram(conn, telegram_id: str) -> bool:
    """Clear telegram_id from whichever user has it. Returns True if found."""
    tid = str(telegram_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET telegram_id = NULL WHERE telegram_id = %s", (tid,))
        found = cur.rowcount > 0
        conn.commit()
    return bool(found)


def create_user_by_telegram(
    conn,
    *,
    telegram_id: str,
    username: str | None = None,
    first_name: str | None = None,
) -> dict[str, Any]:
    """Create a user keyed only by telegram_id (no Google account)."""
    tid = str(telegram_id)
    # Use telegram handle as email placeholder if no Google email
    email = f"tg_{tid}@telegram.local"
    name = first_name or username or f"Telegram {tid}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (telegram_id, email, name, last_login)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (telegram_id) WHERE telegram_id IS NOT NULL DO UPDATE SET
                name = EXCLUDED.name,
                last_login = NOW()
            RETURNING id::text, google_id, telegram_id, email, name, picture, created_at, last_login
            """,
            (tid, email, name),
        )
        row = cur.fetchone()
        conn.commit()
    cols = [
        "id",
        "google_id",
        "telegram_id",
        "email",
        "name",
        "picture",
        "created_at",
        "last_login",
    ]
    return dict(zip(cols, row, strict=False))


# ---------------------------------------------------------------------------
# Sessions (one per user)
# ---------------------------------------------------------------------------


def get_or_create_session(conn, user_id: str) -> dict[str, Any]:
    """Return the single session for this user, creating it if needed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sessions (user_id)
            VALUES (%s)
            ON CONFLICT (user_id) DO UPDATE SET updated_at = NOW()
            RETURNING id::text, user_id::text, created_at, updated_at, model, total_tokens, message_count
            """,
            (user_id,),
        )
        row = cur.fetchone()
        conn.commit()
    cols = ["id", "user_id", "created_at", "updated_at", "model", "total_tokens", "message_count"]
    return dict(zip(cols, row, strict=False))


def get_session(conn, session_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, user_id::text, created_at, updated_at, model, total_tokens, message_count FROM sessions WHERE id = %s",
            (session_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [
            "id",
            "user_id",
            "created_at",
            "updated_at",
            "model",
            "total_tokens",
            "message_count",
        ]
        return dict(zip(cols, row, strict=False))


def touch_session(conn, session_id: str, *, model: str | None = None, tokens: int = 0) -> None:
    """Update session updated_at, message_count, total_tokens, model."""
    with conn.cursor() as cur:
        if model:
            cur.execute(
                """
                UPDATE sessions
                SET updated_at = NOW(),
                    message_count = message_count + 1,
                    total_tokens = total_tokens + %s,
                    model = %s
                WHERE id = %s
                """,
                (tokens, model, session_id),
            )
        else:
            cur.execute(
                """
                UPDATE sessions
                SET updated_at = NOW(),
                    message_count = message_count + 1,
                    total_tokens = total_tokens + %s
                WHERE id = %s
                """,
                (tokens, session_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Messages — ALL persisted (user, assistant, tool, system)
# ---------------------------------------------------------------------------


def _next_seq(conn, session_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_id = %s FOR UPDATE",
            (session_id,),
        )
        (seq,) = cur.fetchone()
        return int(seq)


def append_message(
    conn,
    session_id: str,
    message: dict[str, Any],
    *,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
) -> dict[str, Any]:
    """Persist a single message. Returns the inserted row."""
    seq = _next_seq(conn, session_id)
    role = message.get("role", "")
    content = message.get("content", "")
    tool_calls = message.get("tool_calls")
    tool_call_id = message.get("tool_call_id")

    # Normalize tool_calls to JSON string for JSONB column
    tool_calls_json = None
    if tool_calls is not None:
        tool_calls_json = json.dumps(tool_calls)

    # Only assistant messages carry model/tokens; but allow override
    msg_model = model or message.get("model")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages (session_id, seq, role, content, tool_calls, tool_call_id, model, prompt_tokens, completion_tokens, total_tokens)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            RETURNING id::text, session_id::text, seq, role, content, tool_calls, tool_call_id, created_at, model, prompt_tokens, completion_tokens, total_tokens
            """,
            (
                session_id,
                seq,
                role,
                content,
                tool_calls_json,
                tool_call_id,
                msg_model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
            ),
        )
        row = cur.fetchone()
        # Update session stats
        tok = total_tokens or 0
        cur.execute(
            """
            UPDATE sessions
            SET updated_at = NOW(),
                message_count = message_count + 1,
                total_tokens = total_tokens + %s,
                model = COALESCE(%s, model)
            WHERE id = %s
            """,
            (tok, msg_model, session_id),
        )
        conn.commit()

    cols = [
        "id",
        "session_id",
        "seq",
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "created_at",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    ]
    # tool_calls comes back as Python object if JSONB; normalize
    result = dict(zip(cols, row, strict=False))
    return result


def load_messages(conn, session_id: str) -> list[dict[str, Any]]:
    """Load all messages for a session in seq order, as OpenAI-compatible dicts."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT role, content, tool_calls, tool_call_id
            FROM messages
            WHERE session_id = %s
            ORDER BY seq ASC
            """,
            (session_id,),
        )
        rows = cur.fetchall()

    messages: list[dict[str, Any]] = []
    for role, content, tool_calls, tool_call_id in rows:
        msg: dict[str, Any] = {"role": role}
        if content is not None:
            msg["content"] = content
        if tool_calls is not None:
            # psycopg returns dict/list for JSONB; keep as is
            if isinstance(tool_calls, str):
                try:
                    msg["tool_calls"] = json.loads(tool_calls)
                except json.JSONDecodeError:
                    msg["tool_calls"] = []
            else:
                msg["tool_calls"] = tool_calls
        if tool_call_id is not None:
            msg["tool_call_id"] = tool_call_id
        # OpenAI tool messages require content
        if role == "tool" and "content" not in msg:
            msg["content"] = ""
        # Assistant messages should have content even if empty (for round-trip)
        if role == "assistant" and "content" not in msg:
            msg["content"] = ""
        messages.append(msg)
    return messages


def load_messages_with_meta(conn, session_id: str) -> list[dict[str, Any]]:
    """Load messages with all metadata (timestamps, tokens, etc.)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, seq, role, content, tool_calls, tool_call_id, created_at, model, prompt_tokens, completion_tokens, total_tokens
            FROM messages
            WHERE session_id = %s
            ORDER BY seq ASC
            """,
            (session_id,),
        )
        rows = cur.fetchall()
    cols = [
        "id",
        "seq",
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "created_at",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    ]
    return [dict(zip(cols, r, strict=False)) for r in rows]


def clear_messages(conn, session_id: str) -> int:
    """Delete all messages for a session. Returns count deleted."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM messages WHERE session_id = %s", (session_id,))
        count = cur.rowcount
        cur.execute(
            "UPDATE sessions SET message_count = 0, total_tokens = 0, updated_at = NOW() WHERE id = %s",
            (session_id,),
        )
        conn.commit()
    return int(count)


def delete_session(conn, session_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Facts / Memory — explicit user knowledge
# ---------------------------------------------------------------------------

_FACT_COLS = ["id", "user_id", "type", "key", "value", "source", "confidence", "created_at", "updated_at"]


def _row_to_fact(row: Any) -> dict[str, Any]:
    return dict(zip(_FACT_COLS, row, strict=False))


def upsert_fact(
    conn,
    user_id: str,
    type: str,
    key: str,
    value: str,
    *,
    source: str = "user",
    confidence: float = 1.0,
) -> dict[str, Any]:
    """Insert or update a fact. Returns the record."""
    normalized_key = _normalize_key(key)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO facts (user_id, type, key, value, source, confidence)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, key) DO UPDATE SET
                type = EXCLUDED.type,
                value = EXCLUDED.value,
                source = EXCLUDED.source,
                confidence = EXCLUDED.confidence,
                updated_at = NOW()
            RETURNING id::text, user_id::text, type, key, value, source, confidence, created_at, updated_at
            """,
            (user_id, type, normalized_key, value, source, confidence),
        )
        row = cur.fetchone()
        conn.commit()
    return _row_to_fact(row)


def get_fact(conn, user_id: str, key: str) -> dict[str, Any] | None:
    """Get a single fact by key."""
    normalized_key = _normalize_key(key)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id::text, user_id::text, type, key, value, source, confidence, created_at, updated_at
               FROM facts WHERE user_id = %s AND key = %s""",
            (user_id, normalized_key),
        )
        row = cur.fetchone()
    return _row_to_fact(row) if row else None


def search_facts(
    conn,
    user_id: str,
    query: str,
    *,
    type: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search facts by ILIKE on key and value."""
    with conn.cursor() as cur:
        if type:
            cur.execute(
                """SELECT id::text, user_id::text, type, key, value, source, confidence, created_at, updated_at
                   FROM facts
                   WHERE user_id = %s AND type = %s
                     AND (key ILIKE %s OR value ILIKE %s)
                   ORDER BY updated_at DESC
                   LIMIT %s""",
                (user_id, type, f"%{query}%", f"%{query}%", limit),
            )
        else:
            cur.execute(
                """SELECT id::text, user_id::text, type, key, value, source, confidence, created_at, updated_at
                   FROM facts
                   WHERE user_id = %s
                     AND (key ILIKE %s OR value ILIKE %s)
                   ORDER BY updated_at DESC
                   LIMIT %s""",
                (user_id, f"%{query}%", f"%{query}%", limit),
            )
        rows = cur.fetchall()
    return [_row_to_fact(r) for r in rows]


def delete_fact(conn, user_id: str, key: str) -> bool:
    """Delete a fact by key. Returns True if deleted."""
    normalized_key = _normalize_key(key)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM facts WHERE user_id = %s AND key = %s", (user_id, normalized_key))
        deleted = cur.rowcount > 0
        conn.commit()
    return deleted


def list_facts(conn, user_id: str, *, type: str | None = None) -> list[dict[str, Any]]:
    """List all facts for a user, optionally filtered by type."""
    with conn.cursor() as cur:
        if type:
            cur.execute(
                """SELECT id::text, user_id::text, type, key, value, source, confidence, created_at, updated_at
                   FROM facts WHERE user_id = %s AND type = %s ORDER BY updated_at DESC""",
                (user_id, type),
            )
        else:
            cur.execute(
                """SELECT id::text, user_id::text, type, key, value, source, confidence, created_at, updated_at
                   FROM facts WHERE user_id = %s ORDER BY updated_at DESC""",
                (user_id,),
            )
        rows = cur.fetchall()
    return [_row_to_fact(r) for r in rows]


# ---------------------------------------------------------------------------
# Key normalization
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"[^a-z0-9_]")


def _normalize_key(key: str) -> str:
    """Normalize a memory key to canonical snake_case format.

    - lowercase
    - spaces/ hyphens -> underscores
    - strip non-alphanumeric except underscores
    - collapse consecutive underscores
    - strip leading/trailing underscores
    - max 64 chars
    """
    k = key.strip().lower()
    k = k.replace(" ", "_").replace("-", "_")
    k = _KEY_RE.sub("", k)
    k = re.sub(r"_+", "_", k)
    k = k.strip("_")
    return k[:64]
