from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "user_store.db"

_lock = threading.Lock()


def _get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100000,
    ).hex()


def init_user_store() -> None:
    with _lock:
        conn = _get_connection()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

            try:
                conn.execute("ALTER TABLE users ADD COLUMN email TEXT;")
                conn.commit()
            except sqlite3.OperationalError:
                pass

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_active TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    status TEXT NOT NULL,
                    route_decision TEXT DEFAULT '',
                    model_used TEXT DEFAULT '',
                    generated_sql TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    result_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_user_time ON user_history(user_id, created_at DESC);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_session ON user_history(session_id);"
            )
            conn.commit()

            cursor = conn.execute("SELECT COUNT(*) FROM users;")
            count = cursor.fetchone()[0]
            if count == 0:
                admin_salt = secrets.token_hex(16)
                admin_hash = _hash_password("admin123", admin_salt)
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT INTO users (username, email, password_hash, salt, display_name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    ("admin", "admin@enterprise.ai", admin_hash, admin_salt, "Admin Analyst", now),
                )
                demo_salt = secrets.token_hex(16)
                demo_hash = _hash_password("demo123", demo_salt)
                conn.execute(
                    """
                    INSERT INTO users (username, email, password_hash, salt, display_name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    ("demo", "demo@enterprise.ai", demo_hash, demo_salt, "Demo User", now),
                )
                conn.commit()
                logger.info("Default accounts created: admin@enterprise.ai/admin123, demo@enterprise.ai/demo123")
            else:
                conn.execute("UPDATE users SET email = 'admin@enterprise.ai' WHERE username = 'admin' AND (email IS NULL OR email = '');")
                conn.execute("UPDATE users SET email = 'demo@enterprise.ai' WHERE username = 'demo' AND (email IS NULL OR email = '');")
                conn.commit()

            logger.info("User store initialized at %s", DB_PATH)
        except Exception as e:
            logger.error("Failed initializing user store: %s", e)
        finally:
            conn.close()


def register_user(
    name: str,
    email: str,
    password: str,
    username: Optional[str] = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name or len(name) < 2:
        raise ValueError("Please provide a valid Full Name (at least 2 characters).")

    email = (email or "").strip().lower()
    if not email or "@" not in email or "." not in email:
        raise ValueError("Please provide a valid Email address.")

    if not password or len(password) < 4:
        raise ValueError("Password must be at least 4 characters long.")

    user_handle = (username or email.split("@")[0]).strip().lower()
    import re
    user_handle = re.sub(r"[^a-z0-9_.-]", "", user_handle) or f"user_{secrets.token_hex(3)}"

    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)
    now = datetime.now(timezone.utc).isoformat()

    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO users (username, email, password_hash, salt, display_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (user_handle, email, pw_hash, salt, name, now),
            )
            conn.commit()
            user_id = cursor.lastrowid
            token = secrets.token_urlsafe(32)
            conn.execute(
                """
                INSERT INTO sessions (token, user_id, created_at, last_active)
                VALUES (?, ?, ?, ?);
                """,
                (token, user_id, now, now),
            )
            conn.commit()
            return {
                "token": token,
                "user": {
                    "id": user_id,
                    "username": user_handle,
                    "email": email,
                    "display_name": name,
                },
            }
        except sqlite3.IntegrityError as e:
            err_msg = str(e).lower()
            if "email" in err_msg:
                raise ValueError(f"An account with email '{email}' already exists.")
            raise ValueError(f"Username or email '{email}' is already taken.")
        finally:
            conn.close()


def authenticate_user(login_identifier: str, password: str) -> dict[str, Any]:
    login_id = (login_identifier or "").strip().lower()
    if not login_id:
        raise ValueError("Please provide your Email or Username.")

    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, username, email, password_hash, salt, display_name
                FROM users
                WHERE LOWER(username) = ? OR LOWER(email) = ?;
                """,
                (login_id, login_id),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError("Invalid credentials. Please check your email/username and password.")

            expected_hash = row["password_hash"]
            calculated_hash = _hash_password(password, row["salt"])
            if not secrets.compare_digest(expected_hash, calculated_hash):
                raise ValueError("Invalid credentials. Please check your email/username and password.")

            token = secrets.token_urlsafe(32)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO sessions (token, user_id, created_at, last_active)
                VALUES (?, ?, ?, ?);
                """,
                (token, row["id"], now, now),
            )
            conn.commit()
            return {
                "token": token,
                "user": {
                    "id": row["id"],
                    "username": row["username"],
                    "email": row["email"],
                    "display_name": row["display_name"],
                },
            }
        finally:
            conn.close()


def get_user_by_token(token: str) -> Optional[dict[str, Any]]:
    if not token:
        return None
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT u.id, u.username, u.email, u.display_name, s.token, s.last_active
                FROM sessions s
                JOIN users u ON s.user_id = u.id
                WHERE s.token = ?;
                """,
                (token,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE sessions SET last_active = ? WHERE token = ?;",
                (now, token),
            )
            conn.commit()
            return {
                "id": row["id"],
                "username": row["username"],
                "email": row["email"],
                "display_name": row["display_name"],
            }
        except Exception:
            return None
        finally:
            conn.close()


def logout_user(token: str) -> bool:
    if not token:
        return False
    with _lock:
        conn = _get_connection()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?;", (token,))
            conn.commit()
            return True
        finally:
            conn.close()


def save_user_history(
    *,
    user_id: int,
    session_id: str,
    query: str,
    status: str,
    route_decision: str = "",
    model_used: str = "",
    generated_sql: str = "",
    summary: str = "",
    result_json: str = "{}",
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO user_history (
                    user_id, session_id, query, status, route_decision,
                    model_used, generated_sql, summary, result_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    user_id,
                    session_id,
                    query,
                    status,
                    route_decision,
                    model_used,
                    generated_sql,
                    summary,
                    result_json,
                    now,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def get_user_history(
    user_id: int,
    session_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with _lock:
        conn = _get_connection()
        try:
            if session_id:
                cursor = conn.execute(
                    """
                    SELECT id, session_id, query, status, route_decision,
                           model_used, generated_sql, summary, result_json, created_at
                    FROM user_history
                    WHERE user_id = ? AND session_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?;
                    """,
                    (user_id, session_id, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT id, session_id, query, status, route_decision,
                           model_used, generated_sql, summary, result_json, created_at
                    FROM user_history
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?;
                    """,
                    (user_id, limit),
                )
            rows = cursor.fetchall()
            history = []
            for r in rows:
                item = dict(r)
                try:
                    item["result"] = json.loads(item.pop("result_json") or "{}")
                except Exception:
                    item["result"] = {}
                history.append(item)
            return history
        finally:
            conn.close()


def clear_user_history(user_id: int) -> bool:
    with _lock:
        conn = _get_connection()
        try:
            conn.execute("DELETE FROM user_history WHERE user_id = ?;", (user_id,))
            conn.commit()
            return True
        finally:
            conn.close()
