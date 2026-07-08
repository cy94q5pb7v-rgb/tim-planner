# -*- coding: utf-8 -*-
"""Авторизация: пароли — PBKDF2 (stdlib hashlib), сессия — JWT в httpOnly-cookie (PyJWT).
Никаких секретов в коде — секрет подписи берётся из config.SECRET (env PLANNER_SECRET)."""
import hashlib
import hmac
import os
import base64
import datetime

import jwt  # PyJWT
from fastapi import Request, HTTPException

import config

_PBKDF2_ITER = 200_000


# ---------- пароли ----------
def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256. Формат строки: pbkdf2$<iter>$<salt_b64>$<hash_b64>."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, _PBKDF2_ITER)
    return "pbkdf2$%d$%s$%s" % (_PBKDF2_ITER, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_b64, hash_b64 = (stored or "").split("$")
        if scheme != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ---------- токены ----------
def _make_token(username: str) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {"sub": username, "iat": now, "exp": now + datetime.timedelta(days=config.TOKEN_TTL_DAYS)}
    return jwt.encode(payload, config.SECRET, algorithm=config.JWT_ALG)


def _decode_token(token: str):
    try:
        return jwt.decode(token, config.SECRET, algorithms=[config.JWT_ALG]).get("sub")
    except jwt.PyJWTError:
        return None


# ---------- зависимость авторизации FastAPI ----------
def _require_auth(request: Request) -> str:
    """Вернуть username из cookie-сессии или 401. Браузерную HTML-навигацию
    обработчик в web_app превращает в редирект на /login."""
    token = request.cookies.get(config.COOKIE)
    user = _decode_token(token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="auth required")
    return user
