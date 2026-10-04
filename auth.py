import os
import time
import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException

from db import get_conn, dict_cursor

JWT_SECRET = os.environ.get("MPSC_JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("MPSC_JWT_SECRET is not set")
JWT_ALGO = "HS256"
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days — personal/closed tool, long-lived session is fine


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_token(user: dict) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user["id"]),
        "username": user["username"],
        "role": user["role"],
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_current_user(authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization[len("Bearer "):]
    payload = decode_token(token)

    conn = get_conn()
    try:
        cur = dict_cursor(conn)
        cur.execute("SELECT id, username, role, display_name FROM users WHERE id = %s", (int(payload["sub"]),))
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=401, detail="User no longer exists")
        return dict(user)
    finally:
        conn.close()


# Rank-additive role model: every role can do everything a lower rank
# can. 'guest' (unauthenticated) is rank 0 and computed only — never a
# stored role, since signup stays invite-only (admin/owner seeds
# accounts directly, no public register endpoint).
RANK = {"learner": 1, "moderator": 2, "reviewer": 3, "editor": 4, "admin": 5, "owner": 6}

CAPS = {
    "question.read": 0,
    "attempt.write": 1, "comment.create": 1, "comment.edit_own": 1,
    "comment.delete_own": 1, "report.create": 1, "note.write": 1,
    "report.reject": 2, "comment.moderate": 2,
    "report.accept": 3, "verification.review": 3,
    "correction.write": 4, "static_set.write": 4, "import.run": 4, "test.publish": 4, "paper.edit": 4,
    "audit.read_question": 4,
    "flag.write": 5, "admin.stats": 5, "audit.read": 5, "user.read": 5, "user.reset_password": 5,
    "user.role.assign": 6,
}


def rank_of(role: str) -> int:
    return RANK.get(role, 0)


def has_cap(role: str, cap: str) -> bool:
    return rank_of(role) >= CAPS[cap]


def capabilities_for(role: str) -> list:
    return sorted(c for c, r in CAPS.items() if rank_of(role) >= r)


def require_cap(cap: str):
    def _dep(user: dict = Depends(get_current_user)) -> dict:
        if not has_cap(user["role"], cap):
            raise HTTPException(status_code=403, detail=f"missing capability: {cap}")
        return user
    return _dep


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if rank_of(user["role"]) < RANK["admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
