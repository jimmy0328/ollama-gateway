"""Durable conversation turns and summaries; original turns are never compacted."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class SessionNotFound(Exception):
    pass


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path.resolve()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create with private permissions before SQLite opens the database.
        self.path.touch(mode=0o600, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    system_prompt TEXT NOT NULL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id, updated_at);
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    user_content TEXT NOT NULL, assistant_content TEXT NOT NULL,
                    model TEXT NOT NULL, finish_reason TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, id);
                CREATE TABLE IF NOT EXISTS summaries (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    content TEXT NOT NULL, model TEXT NOT NULL, instructions TEXT NOT NULL,
                    source_turns INTEGER NOT NULL, source_last_turn_id INTEGER NOT NULL,
                    model_calls INTEGER NOT NULL, finish_reason TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS summaries_session ON summaries(session_id, created_at);
            """)

    @staticmethod
    def require_session(db, user_id, session_id):
        row = db.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id)).fetchone()
        if row is None:
            raise SessionNotFound
        return dict(row)

    def create_session(self, user_id, system_prompt):
        session_id, now = uuid.uuid4().hex, time.time()
        with self.connection() as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (session_id, user_id, system_prompt, now, now))
        return self.get_session(user_id, session_id)

    def get_session(self, user_id, session_id):
        with self.connection() as db:
            session = self.require_session(db, user_id, session_id)
            session["turn_count"] = db.execute("SELECT count(*) FROM turns WHERE session_id=?", (session_id,)).fetchone()[0]
            return session

    def list_sessions(self, user_id, limit, offset):
        with self.connection() as db:
            rows = db.execute("""SELECT s.*, (SELECT count(*) FROM turns t WHERE t.session_id=s.id) AS turn_count
                                 FROM sessions s WHERE user_id=? ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                              (user_id, limit, offset)).fetchall()
            return [dict(row) for row in rows]

    def get_turns(self, user_id, session_id, limit=None, offset=0, recent=False):
        with self.connection() as db:
            self.require_session(db, user_id, session_id)
            query = "SELECT * FROM turns WHERE session_id=? ORDER BY id " + ("DESC" if recent else "ASC")
            params = [session_id]
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])
            rows = [dict(row) for row in db.execute(query, params)]
            return list(reversed(rows)) if recent else rows

    def append_turn(self, user_id, session_id, user_content, assistant_content, model, finish_reason):
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require_session(db, user_id, session_id)
            db.execute("""INSERT INTO turns(session_id,user_content,assistant_content,model,finish_reason,created_at)
                          VALUES (?,?,?,?,?,?)""", (session_id, user_content, assistant_content, model, finish_reason, now))
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))

    def delete_session(self, user_id, session_id):
        with self.connection() as db:
            self.require_session(db, user_id, session_id)
            db.execute("DELETE FROM sessions WHERE id=? AND user_id=?", (session_id, user_id))

    def save_summary(self, user_id, session_id, content, model, instructions, turns, model_calls, finish_reason):
        record = {"id": uuid.uuid4().hex, "session_id": session_id, "content": content, "model": model,
                  "instructions": instructions, "source_turns": len(turns), "source_last_turn_id": turns[-1]["id"],
                  "model_calls": model_calls, "finish_reason": finish_reason, "created_at": time.time()}
        with self.connection() as db:
            self.require_session(db, user_id, session_id)
            db.execute("INSERT INTO summaries VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(record.values()))
        return record

    def latest_summary(self, user_id, session_id):
        with self.connection() as db:
            self.require_session(db, user_id, session_id)
            row = db.execute("SELECT * FROM summaries WHERE session_id=? ORDER BY created_at DESC LIMIT 1", (session_id,)).fetchone()
            return dict(row) if row else None


def build_context(system_prompt, turns, current, budget):
    """Select contiguous recent complete turns; leave the stored transcript intact."""
    messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
    used = len(system_prompt) + len(current)
    if used > budget:
        raise ValueError("System prompt and current message exceed MAX_PROMPT_CHARS")
    selected = []
    for turn in reversed(turns):
        size = len(turn["user_content"]) + len(turn["assistant_content"])
        if used + size > budget:
            break
        selected.append(turn)
        used += size
    for turn in reversed(selected):
        messages.extend([{"role": "user", "content": turn["user_content"]},
                         {"role": "assistant", "content": turn["assistant_content"]}])
    return messages + [{"role": "user", "content": current}]


def transcript(turns):
    for index, turn in enumerate(turns, 1):
        yield json.dumps({"turn": index, "user": turn["user_content"], "assistant": turn["assistant_content"]}, ensure_ascii=False)
