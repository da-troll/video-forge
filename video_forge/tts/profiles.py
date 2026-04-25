"""SQLite profile CRUD for video-forge TTS profiles.

DB lives at ~/.local/share/video-forge/profiles.db (NOT inside the repo, so a
git pull from upstream stays clean and the DB survives reinstalls).

Schema mirrors voice-palette's profiles table, extended with `provider` and
`instructions` so we can store a complete (provider, voice_id, instructions)
triple under one profile_id — that's what the phase-2 voice picker references.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_DIR = Path(os.path.expanduser("~/.local/share/video-forge"))
DB_PATH = DB_DIR / "profiles.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _init_db() -> None:
    conn = _get_db()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                voice_id TEXT NOT NULL,
                instructions TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


# Initialise on import — voice-palette does the same.
_init_db()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "voice_id": row["voice_id"],
        "instructions": row["instructions"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_profiles() -> list[dict]:
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM profiles ORDER BY updated_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_profile(profile_id: str) -> dict | None:
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def create_profile(name: str, provider: str, voice_id: str, instructions: str | None = None) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("profile name required")
    pid = str(uuid.uuid4())
    now = _now_iso()
    conn = _get_db()
    try:
        try:
            conn.execute(
                "INSERT INTO profiles (id, name, provider, voice_id, instructions, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pid, name, provider, voice_id, instructions, now, now),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            raise ValueError(f"profile name '{name}' already exists") from e
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (pid,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def update_profile(profile_id: str, *, name: str | None = None, provider: str | None = None, voice_id: str | None = None, instructions: str | None = None) -> dict:
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if not row:
            raise ValueError(f"profile not found: {profile_id}")
        updates: dict = {}
        if name is not None:        updates["name"] = name.strip()
        if provider is not None:    updates["provider"] = provider
        if voice_id is not None:    updates["voice_id"] = voice_id
        if instructions is not None: updates["instructions"] = instructions
        if updates:
            updates["updated_at"] = _now_iso()
            set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
            params = list(updates.values()) + [profile_id]
            try:
                conn.execute(f"UPDATE profiles SET {set_clause} WHERE id = ?", params)
                conn.commit()
            except sqlite3.IntegrityError as e:
                raise ValueError(f"profile name conflict") from e
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def delete_profile(profile_id: str) -> bool:
    conn = _get_db()
    try:
        cur = conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
