# -*- coding: utf-8 -*-
"""Админ-API: управление пользователями, правами и ролями (только для is_admin).
Пароли хешируются PBKDF2, наружу password_hash не отдаётся."""
from fastapi import APIRouter, Depends, HTTPException, Request

from core.auth import _require_admin, hash_password
from core.users import _load_users, _save_users

router = APIRouter(tags=["admin"])

VALID_ROLES = ["ba", "sa", "dev", "test", "qa", "po", "pm", "lead"]
ROLE_LABELS = {
    "ba": "Бизнес-аналитик", "sa": "Системный аналитик", "dev": "Разработчик",
    "test": "Тестировщик", "qa": "QA-инженер", "po": "Product Owner",
    "pm": "Product Manager", "lead": "Лид",
}


def _clean_roles(raw):
    if not isinstance(raw, list):
        return []
    got = {str(x).strip().lower() for x in raw if isinstance(x, str) and str(x).strip()}
    return [r for r in VALID_ROLES if r in got]


def _public(u):
    """Пользователь без password_hash (наружу секреты не отдаём)."""
    return {"username": u.get("username"), "is_admin": bool(u.get("is_admin")),
            "planner_access": bool(u.get("planner_access")), "roles": _clean_roles(u.get("roles"))}


@router.get("/api/admin/roles")
def roles_ref(_=Depends(_require_admin)):
    """Справочник ролей для UI: [{id, label}]."""
    return {"roles": [{"id": r, "label": ROLE_LABELS[r]} for r in VALID_ROLES]}


@router.get("/api/admin/users")
def list_users(_=Depends(_require_admin)):
    users = _load_users().get("users", [])
    return {"users": [_public(u) for u in sorted(users, key=lambda x: (x.get("username") or ""))]}


@router.post("/api/admin/users")
async def upsert_user(request: Request, actor: str = Depends(_require_admin)):
    """Создать/обновить пользователя. Поля: username, password?, is_admin, planner_access, roles[].
    password обязателен только для НОВОГО пользователя."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")
    username = (body.get("username") or "").strip().lower()
    if not username:
        raise HTTPException(status_code=400, detail="username обязателен")

    data = _load_users()
    users = data.setdefault("users", [])
    rec = next((u for u in users if (u.get("username") or "").lower() == username), None)
    is_new = rec is None
    if is_new:
        rec = {"username": username}
        users.append(rec)

    pwd = body.get("password") or ""
    if pwd:
        rec["password_hash"] = hash_password(pwd)
    if is_new and not rec.get("password_hash"):
        # откатываем добавление пустого пользователя
        users.remove(rec)
        raise HTTPException(status_code=400, detail="для нового пользователя нужен пароль")

    rec["is_admin"] = bool(body.get("is_admin"))
    rec["planner_access"] = bool(body.get("planner_access")) or rec["is_admin"]
    rec["roles"] = _clean_roles(body.get("roles"))

    # защита: нельзя снять с себя админ-права (чтобы не потерять доступ к админке)
    if username == actor.lower() and not rec["is_admin"]:
        raise HTTPException(status_code=400, detail="нельзя снять администратора с самого себя")

    _save_users(data)
    return {"ok": True, "created": is_new, "user": _public(rec)}


@router.delete("/api/admin/users/{username}")
def delete_user(username: str, actor: str = Depends(_require_admin)):
    uname = (username or "").strip().lower()
    if uname == actor.lower():
        raise HTTPException(status_code=400, detail="нельзя удалить самого себя")
    data = _load_users()
    before = len(data.get("users", []))
    data["users"] = [u for u in data.get("users", []) if (u.get("username") or "").lower() != uname]
    if len(data["users"]) == before:
        raise HTTPException(status_code=404, detail="пользователь не найден")
    _save_users(data)
    return {"ok": True}
