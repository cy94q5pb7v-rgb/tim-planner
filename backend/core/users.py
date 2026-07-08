# -*- coding: utf-8 -*-
"""Пользователи ТИМ Планера: хранятся в JSON-файле (PLANNER_USERS_FILE).
Формат: {"users": [{"username","password_hash","is_admin","planner_access","roles":[...]}]}."""
import json
import threading

import config

_LOCK = threading.Lock()


def _load_users():
    """Вернуть {"users": [...]} из файла. Если файла нет — пустой список."""
    try:
        with open(config.USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("users"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {"users": []}


def _save_users(data):
    with _LOCK:
        with open(config.USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _find_user(username):
    if not username:
        return None
    uname = str(username).strip().lower()
    for u in _load_users().get("users", []):
        if (u.get("username") or "").strip().lower() == uname:
            return u
    return None


def _update_user(username, patch):
    data = _load_users()
    uname = str(username).strip().lower()
    for u in data.get("users", []):
        if (u.get("username") or "").strip().lower() == uname:
            u.update(patch)
            _save_users(data)
            return u
    return None
