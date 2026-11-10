"""SQLite-хранилище диалогов: все сообщения без автоматического удаления."""

import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "chat.db"
TITLE_LIMIT = 60


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def init_db() -> None:
    with closing(connect()) as conn, conn:
        tables = _tables(conn)
        if "sessions" in tables and "dialogues" not in tables:
            conn.execute("ALTER TABLE sessions RENAME TO dialogues")
        if "turns" in tables and "messages" not in tables:
            conn.execute("ALTER TABLE turns RENAME TO messages")

        tables = _tables(conn)
        if "messages" in tables:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
            if "session_id" in columns and "dialogue_id" not in columns:
                conn.execute("ALTER TABLE messages RENAME COLUMN session_id TO dialogue_id")
            if "reasons" not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN reasons TEXT NOT NULL DEFAULT '[]'")

        conn.execute("DROP INDEX IF EXISTS idx_turns_session")
        conn.execute("DROP INDEX IF EXISTS idx_sessions_updated")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS dialogues (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dialogue_id TEXT NOT NULL REFERENCES dialogues(id) ON DELETE CASCADE,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                calls TEXT NOT NULL,
                verdict TEXT NOT NULL,
                reasons TEXT NOT NULL,
                final TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                steps INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_dialogue ON messages(dialogue_id, id);
            CREATE INDEX IF NOT EXISTS idx_dialogues_updated ON dialogues(updated_at DESC);
            """
        )


def _title(question: str) -> str:
    text = " ".join(question.split())
    return text[:TITLE_LIMIT] + ("…" if len(text) > TITLE_LIMIT else "")


def list_dialogues() -> list[dict[str, Any]]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM dialogues ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_dialogue(dialogue_id: str) -> dict[str, Any] | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT id, title, created_at, updated_at FROM dialogues WHERE id = ?", (dialogue_id,)
        ).fetchone()
    return dict(row) if row else None


def get_messages(dialogue_id: str) -> list[dict[str, Any]]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, dialogue_id, question, answer, calls, verdict, reasons, final, attempts, steps, created_at "
            "FROM messages WHERE dialogue_id = ? ORDER BY id",
            (dialogue_id,),
        ).fetchall()

    messages = []
    for row in rows:
        message = dict(row)
        message["message_id"] = message.pop("id")
        for key in ("answer", "calls", "verdict", "reasons"):
            message[key] = json.loads(message[key])
        messages.append(message)
    return messages


def save_message(dialogue_id: str | None, question: str, result: dict[str, Any]) -> str:
    """Сохранить сообщение; для нового диалога создать его по первому вопросу."""
    now = time.time()
    with closing(connect()) as conn, conn:
        exists = bool(dialogue_id and conn.execute("SELECT 1 FROM dialogues WHERE id = ?", (dialogue_id,)).fetchone())
        if not exists:
            dialogue_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO dialogues (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (dialogue_id, _title(question), now, now),
            )

        conn.execute(
            "INSERT INTO messages "
            "(dialogue_id, question, answer, calls, verdict, reasons, final, attempts, steps, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dialogue_id,
                question,
                json.dumps(result.get("answer"), ensure_ascii=False),
                json.dumps(result.get("calls", []), ensure_ascii=False, default=str),
                json.dumps(result.get("verdict"), ensure_ascii=False),
                json.dumps(result.get("reasons", []), ensure_ascii=False),
                str(result.get("final", "")),
                int(result.get("attempts", 0)),
                int(result.get("steps", 0)),
                now,
            ),
        )
        conn.execute("UPDATE dialogues SET updated_at = ? WHERE id = ?", (now, dialogue_id))

    return dialogue_id
