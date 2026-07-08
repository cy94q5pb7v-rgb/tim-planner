#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Создать/обновить пользователя ТИМ Планера (пароль хешируется PBKDF2).

Примеры:
  python scripts/make_user.py admin --password 'СильныйПароль' --admin
  python scripts/make_user.py ivanov --password 'pass' --planner --roles ba,dev
  python scripts/make_user.py ivanov --password 'new'         # обновить пароль

Файл пользователей: $PLANNER_USERS_FILE (по умолчанию data/users.json)."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
import config          # noqa: E402
from core.auth import hash_password  # noqa: E402

VALID_ROLES = ["ba", "sa", "dev", "test", "qa", "po", "pm", "lead"]


def main():
    ap = argparse.ArgumentParser(description="Создать/обновить пользователя ТИМ Планера")
    ap.add_argument("username")
    ap.add_argument("--password", required=True)
    ap.add_argument("--admin", action="store_true", help="is_admin (полный доступ)")
    ap.add_argument("--planner", action="store_true", help="planner_access (доступ к Планеру)")
    ap.add_argument("--roles", default="", help="роли через запятую: " + ",".join(VALID_ROLES))
    args = ap.parse_args()

    roles = [r.strip().lower() for r in args.roles.split(",") if r.strip()]
    roles = [r for r in VALID_ROLES if r in roles]  # валидация + порядок

    path = config.USERS_FILE
    data = {"users": []}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    data.setdefault("users", [])

    uname = args.username.strip().lower()
    rec = next((u for u in data["users"] if (u.get("username") or "").lower() == uname), None)
    if rec is None:
        rec = {"username": uname}
        data["users"].append(rec)
    rec["password_hash"] = hash_password(args.password)
    # admin/planner: выставляем только если флаг передан (иначе не трогаем существующее)
    if args.admin:
        rec["is_admin"] = True
    rec.setdefault("is_admin", False)
    if args.planner or args.admin:
        rec["planner_access"] = True
    rec.setdefault("planner_access", False)
    if args.roles != "":
        rec["roles"] = roles
    rec.setdefault("roles", [])

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("OK: пользователь '%s' сохранён (is_admin=%s, planner_access=%s, roles=%s)"
          % (uname, rec["is_admin"], rec["planner_access"], rec["roles"]))
    print("Файл:", path)


if __name__ == "__main__":
    main()
