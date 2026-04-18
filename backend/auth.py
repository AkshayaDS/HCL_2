"""
Authentication service.

Keeps a JSON-backed user directory (data/users.json) with PBKDF2-hashed
passwords and a parallel session store (data/sessions.json) so tokens
survive a Flask restart.

Public API:
    register(username, password, name, role, email=None) -> (user, token)
    login(username, password, expected_role=None)        -> (user, token)
    logout(token)                                        -> bool
    user_from_token(token)                               -> user | None
    get_user(username)                                   -> user | None
    list_users()                                         -> list[user]

User records never contain raw passwords — callers see only the safe
projection (`_public`). Sessions expire after `SESSION_TTL_HOURS`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import os
import secrets
import threading
from typing import Any

from backend.storage import load, save

USERS_FILE = "users.json"
SESSIONS_FILE = "sessions.json"
ROLES = ("operator", "supervisor")
SESSION_TTL_HOURS = 12

_lock = threading.Lock()


# ─── helpers ────────────────────────────────────────────────────────────────
def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return salt.hex(), digest.hex()


def _verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        _, got = _hash_password(password, salt)
        return hmac.compare_digest(got, digest_hex)
    except Exception:
        return False


def _public(user: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive fields before returning a user to the caller."""
    if not user:
        return {}
    return {
        "username": user.get("username"),
        "name": user.get("name"),
        "email": user.get("email"),
        "role": user.get("role"),
        "created_at": user.get("created_at"),
        "last_login": user.get("last_login"),
    }


def _load_users() -> list[dict[str, Any]]:
    try:
        data = load(USERS_FILE)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []


def _save_users(users: list[dict[str, Any]]) -> None:
    save(USERS_FILE, users)


def _load_sessions() -> dict[str, dict[str, Any]]:
    try:
        data = load(SESSIONS_FILE)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _save_sessions(sessions: dict[str, dict[str, Any]]) -> None:
    save(SESSIONS_FILE, sessions)


def _prune_sessions(sessions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Drop expired tokens. Returns the pruned dict (caller saves if dirty)."""
    now = _utcnow()
    alive: dict[str, dict[str, Any]] = {}
    for tok, meta in sessions.items():
        exp = meta.get("expires_at")
        if not exp:
            continue
        try:
            exp_dt = _dt.datetime.fromisoformat(exp)
        except Exception:
            continue
        if exp_dt > now:
            alive[tok] = meta
    return alive


def _new_token(username: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        sessions = _prune_sessions(_load_sessions())
        sessions[token] = {
            "username": username,
            "role": role,
            "issued_at": _now(),
            "expires_at": (_utcnow() + _dt.timedelta(hours=SESSION_TTL_HOURS)).isoformat(),
        }
        _save_sessions(sessions)
    return token


# ─── user directory ─────────────────────────────────────────────────────────
def _find(users: list[dict[str, Any]], username: str) -> dict[str, Any] | None:
    u = (username or "").strip().lower()
    for rec in users:
        if (rec.get("username") or "").strip().lower() == u:
            return rec
    return None


def get_user(username: str) -> dict[str, Any] | None:
    rec = _find(_load_users(), username)
    return _public(rec) if rec else None


def list_users() -> list[dict[str, Any]]:
    return [_public(u) for u in _load_users()]


# ─── register / login / logout ──────────────────────────────────────────────
class AuthError(Exception):
    """Raised with an HTTP-friendly message when auth fails."""
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def register(
    username: str,
    password: str,
    name: str,
    role: str,
    email: str | None = None,
) -> tuple[dict[str, Any], str]:
    username = (username or "").strip()
    name = (name or "").strip() or username
    role = (role or "").strip().lower()
    email = (email or "").strip() or None

    if not username or not password:
        raise AuthError("missing_fields", "Username and password are required.", 400)
    if role not in ROLES:
        raise AuthError("bad_role", f"Role must be one of {', '.join(ROLES)}.", 400)
    if len(password) < 4:
        raise AuthError("weak_password", "Password must be at least 4 characters.", 400)

    with _lock:
        users = _load_users()
        if _find(users, username):
            raise AuthError("exists", "An account with that username already exists.", 409)
        salt, digest = _hash_password(password)
        record = {
            "username": username,
            "name": name,
            "email": email,
            "role": role,
            "salt": salt,
            "password_hash": digest,
            "created_at": _now(),
            "last_login": None,
        }
        users.append(record)
        _save_users(users)

    token = _new_token(username, role)
    return _public(record), token


def login(
    username: str,
    password: str,
    expected_role: str | None = None,
) -> tuple[dict[str, Any], str]:
    username = (username or "").strip()
    if not username or not password:
        raise AuthError("missing_fields", "Enter your username and password.", 400)

    with _lock:
        users = _load_users()
        rec = _find(users, username)
        if not rec:
            raise AuthError("not_found", "No account found with that username.", 404)
        if not _verify_password(password, rec.get("salt", ""), rec.get("password_hash", "")):
            raise AuthError("bad_password", "Incorrect password — access denied.", 401)
        if expected_role and (rec.get("role") or "").lower() != expected_role.lower():
            raise AuthError(
                "wrong_role",
                f"This account is a {rec.get('role','?').upper()}, not a {expected_role.upper()}.",
                403,
            )
        rec["last_login"] = _now()
        _save_users(users)

    token = _new_token(rec["username"], rec["role"])
    return _public(rec), token


def logout(token: str) -> bool:
    if not token:
        return False
    with _lock:
        sessions = _prune_sessions(_load_sessions())
        if token in sessions:
            del sessions[token]
            _save_sessions(sessions)
            return True
    return False


def user_from_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with _lock:
        sessions = _prune_sessions(_load_sessions())
        _save_sessions(sessions)  # persist prune
        meta = sessions.get(token)
        if not meta:
            return None
        users = _load_users()
        rec = _find(users, meta.get("username", ""))
        return _public(rec) if rec else None
