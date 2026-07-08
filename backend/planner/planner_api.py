# -*- coding: utf-8 -*-
"""ТИМ Планер — JSON API. Совместная доска: GET снимка (rev+tasks+epics),
POST операций (mutate). Доступ по planner_access (или is_admin).

Эндпоинты: /api/planner/{state,mutate,people,me,chat,presence,health,
github-sync,upload,file/{id}}. Интеграции (ИИ-агент чата, GitHub-синхронизация,
почтовые уведомления) — опциональны и включаются через .env."""
import os, sys, threading, time, datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from web_app import _require_auth, _find_user

try:
    from planner import db as db
except Exception:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "planner"))
    import db as db  # type: ignore

_agent_lock = threading.Lock()


def _run_planner_agent(question: str, context: str = ""):
    """Фоново: спросить подключённого ИИ-агента (core.agent.run_agent) и
    опубликовать ответ в чат. context — компактный текстовый снапшот доски.
    Режим агента (API/CLI/выкл) настраивается через .env — см. core/openclaw.py."""
    try:
        from core.agent import run_agent
        with _agent_lock:
            text = run_agent(question, context)
    except Exception as e:  # noqa: BLE001
        text = "Ошибка агента: " + str(e)[:180]
    try:
        db.chat_add("Агент", (text or "(пустой ответ)")[:4000], kind="agent")
    except Exception:
        pass

try:
    from core.users import _load_users as _load_users_planner
except Exception:  # pragma: no cover
    def _load_users_planner():
        return {"users": []}

# ---- присутствие (онлайн): в памяти процесса, TTL 60с. Условие: uvicorn в 1 воркер ----
_PRESENCE = {}
_PRESENCE_LOCK = threading.Lock()
_PRESENCE_TTL = 60.0

_STAGES = {"ba": "БА", "sa": "СА", "dev": "Разработка", "test": "Тест", "prod": "Пром"}
_STATUS = {"todo": "Не начато", "doing": "В работе", "done": "Готово"}


def _sprint_today():
    """Мирроринг клиента: Спринт 0 до чт 02.07.2026, Спринт 1 с пт 03.07.2026, Пт–Чт."""
    s1 = datetime.date(2026, 7, 3)
    diff = (datetime.date.today() - s1).days
    k = (diff // 7) + 1
    return 0 if k < 1 else k


def _est(t):
    e = t.get("est") or {}
    try:
        return sum(int(e.get(k, 0) or 0) for k in ("ba", "sa", "dev", "test"))
    except Exception:
        return 0


def _board_snapshot():
    """Компактный текстовый снапшот доски для агента (агрегаты + эпики + задачи)."""
    board = db.get_board()
    tasks = board.get("tasks", []) or []
    epics = board.get("epics", []) or []
    today = datetime.date.today()
    L = ["Сегодня: %s. Текущий спринт: %d (спринт = пятница–четверг)." % (today.isoformat(), _sprint_today())]
    by_status, by_stage, total_sp, overdue = {}, {}, 0, 0
    for t in tasks:
        st = t.get("status") or "todo"; by_status[st] = by_status.get(st, 0) + 1
        sg = t.get("stage") or "ba"; by_stage[sg] = by_stage.get(sg, 0) + 1
        total_sp += _est(t)
        dl = t.get("deadline")
        if dl and st != "done":
            try:
                if datetime.date.fromisoformat(dl) < today:
                    overdue += 1
            except Exception:
                pass
    L.append("Задач всего: %d. По статусам: %s. По этапам: %s. Сумма SP: %d. Просрочено: %d." % (
        len(tasks),
        ", ".join("%s=%d" % (_STATUS.get(k, k), v) for k, v in by_status.items()) or "—",
        ", ".join("%s=%d" % (_STAGES.get(k, k), v) for k, v in by_stage.items()) or "—",
        total_sp, overdue))
    emap = {e.get("id"): e.get("key", "?") for e in epics}
    L.append("ЭПИКИ (%d):" % len(epics))
    for e in epics:
        ec = sum(1 for t in tasks if t.get("epicId") == e.get("id"))
        ed = sum(1 for t in tasks if t.get("epicId") == e.get("id") and t.get("status") == "done")
        L.append("- %s | %s | %s | %s–%s | %s | задач %d (готово %d)" % (
            e.get("key", "?"), (e.get("name") or "")[:70], _STATUS.get(e.get("status", "todo"), "?"),
            e.get("start") or "—", e.get("due") or "—",
            "в календаре" if e.get("planned") else "черновик", ec, ed))
    if len(tasks) <= 160:
        L.append("ЗАДАЧИ (%d):" % len(tasks))
        for t in tasks:
            spr = t.get("sprint")
            L.append("- %s | эпик %s | %s | %s | исп. %s | %dSP | старт %s+%sд | дедлайн %s | спринт %s" % (
                (t.get("title") or "")[:70], emap.get(t.get("epicId"), "—"),
                _STAGES.get(t.get("stage"), "?"), _STATUS.get(t.get("status", "todo"), "?"),
                t.get("assigneeId") or "—", _est(t), t.get("start") or "—", t.get("dur") or "?",
                t.get("deadline") or "—", spr if spr is not None else "(по дате старта)"))
    else:
        L.append("(Список задач усечён — их %d; используйте агрегаты и эпики выше.)" % len(tasks))
    return "\n".join(L)


router = APIRouter(tags=["planner"])

_PLANNER_COLORS = ["#3D6BE5", "#12A594", "#E0900A", "#D93A4A", "#8E4EC6",
                   "#0091D2", "#E5484D", "#0EA5E9", "#6E56CF", "#30A46C"]

# --- ROLES PATCH: справочник ролей и порядок (для human-readable метки первой роли) ---
ALLOWED_ROLES = {"ba", "sa", "dev", "test", "qa", "po", "pm", "lead"}
_ROLES_ORDER = ["ba", "sa", "dev", "test", "qa", "po", "pm", "lead"]
_ROLE_LABELS = {
    "ba": "Бизнес-аналитик", "sa": "Системный аналитик", "dev": "Разработчик",
    "test": "Тестировщик", "qa": "QA-инженер", "po": "Product Owner", "pm": "Product Manager", "lead": "Лид",
}


def _clean_roles(raw):
    """Пересечение присланного списка ролей с whitelist; порядок из справочника, без дублей."""
    if not isinstance(raw, list):
        return []
    got = {str(x).strip().lower() for x in raw if isinstance(x, str) and str(x).strip()}
    return [r for r in _ROLES_ORDER if r in got and r in ALLOWED_ROLES]
# --- /ROLES PATCH ---


def _require_planner(user: str = Depends(_require_auth)) -> str:
    u = _find_user(user)
    if not u or not (u.get("is_admin") or u.get("planner_access")):
        raise HTTPException(status_code=403, detail="planner access denied")
    return user


try:
    db.init_schema()
except Exception as _e:  # pragma: no cover
    import logging
    logging.getLogger("planner").warning("init_schema failed: %s", _e)


@router.get("/api/planner/state")
def get_state(_=Depends(_require_planner)):
    """Authoritative board snapshot: {rev, tasks, epics}."""
    return db.get_board()


@router.post("/api/planner/mutate")
async def mutate(request: Request, user: str = Depends(_require_planner)):
    """Apply a list of entity ops, return the new authoritative board.

    *** PATCH: почтовые уведомления о смене статуса/этапа задач. ***
    Логика доски НЕ изменена: перед apply снимаем before-снимок, после apply
    сравниваем статус/этап у изменённых задач и в ФОНЕ шлём письма. Любой сбой
    уведомлений полностью изолирован и не влияет на ответ API."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")
    ops = body.get("ops") if isinstance(body, dict) else None
    if not isinstance(ops, list):
        raise HTTPException(status_code=400, detail="ops must be a list")

    # PATCH ↓ снимок ДО применения (одно чтение), чтобы узнать старые статус/этап
    try:
        before = db.get_board()
        _before_tasks = {t.get("id"): t for t in (before.get("tasks") or [])
                         if isinstance(t, dict) and t.get("id")}
    except Exception:
        _before_tasks = {}
    # PATCH ↑

    # REVERSE-GH: не дать клиенту затереть связь задача↔issue (ghNumber и пр.)
    try:
        for _op in ops:
            if isinstance(_op, dict) and _op.get("type") == "task.upsert":
                _e = _op.get("entity")
                if isinstance(_e, dict):
                    _old = _before_tasks.get(_e.get("id"))
                    if _old:
                        for _k in ("ghNumber", "ghUrl", "ghState", "source"):
                            if _old.get(_k) is not None and _e.get(_k) is None:
                                _e[_k] = _old.get(_k)
    except Exception:
        pass

    try:
        board = db.apply_ops(ops)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # PATCH ↓ собрать изменения статуса/этапа и уведомить (изолировано от ответа)
    try:
        changes = []
        for op in ops:
            if not isinstance(op, dict) or op.get("type") != "task.upsert":
                continue
            new = op.get("entity")
            if not isinstance(new, dict) or not new.get("id"):
                continue
            old = _before_tasks.get(new.get("id"))
            if not old:
                continue  # новая задача (нет old) — пропускаем
            st_changed = old.get("status") != new.get("status")
            sg_changed = old.get("stage") != new.get("stage")
            if st_changed or sg_changed:
                changes.append({
                    "task": new,
                    "old_status": old.get("status") if st_changed else None,
                    "old_stage": old.get("stage") if sg_changed else None,
                })
        import config as _cfg
        if changes and _cfg.NOTIFY_ENABLED:
            from planner import notify
            people = [{"id": u.get("username"), "name": u.get("username")}
                      for u in _load_users_planner().get("users", []) if u.get("username")]
            epics = board.get("epics", [])
            notify.notify_task_changes(changes, user, people, epics)  # опционально (env PLANNER_NOTIFY_ENABLED=1)
    except Exception:
        import logging
        logging.getLogger("planner").warning("notify hook failed", exc_info=False)
    # PATCH ↑

    # REVERSE-GH: задача переведена на этап «Разработка» → завести issue в GitHub (в фоне)
    try:
        _dev_ids = []
        for _op in ops:
            if not isinstance(_op, dict) or _op.get("type") != "task.upsert":
                continue
            _new = _op.get("entity")
            if not isinstance(_new, dict) or not _new.get("id"):
                continue
            _old = _before_tasks.get(_new.get("id"))
            _old_stage = _old.get("stage") if _old else None
            if _new.get("stage") == "dev" and _old_stage != "dev" and not _new.get("ghNumber"):
                _dev_ids.append(_new.get("id"))
        if _dev_ids:
            threading.Thread(target=_github_create_for_tasks, args=(_dev_ids,), daemon=True).start()
    except Exception:
        import logging as _lg
        _lg.getLogger("planner").warning("reverse-gh hook failed", exc_info=False)

    return board


def _planner_initials(name: str) -> str:
    s = (name or "").strip()
    return s[:2].upper() if s else "??"


@router.get("/api/planner/people")
def get_people(_=Depends(_require_planner)):
    """Люди Планера = пользователи с доступом (planner_access или is_admin)."""
    users = _load_users_planner().get("users", [])
    people, idx = [], 0
    for u in sorted(users, key=lambda x: (x.get("username") or "")):
        if not (u.get("is_admin") or u.get("planner_access")):
            continue
        un = u.get("username") or ""
        # ROLES PATCH ↓ аддитивно: список ролей + human-readable метка ПЕРВОЙ роли
        roles = _clean_roles(u.get("roles"))
        role_label = _ROLE_LABELS[roles[0]] if roles else (
            "Администратор" if u.get("is_admin") else "Участник")
        # ROLES PATCH ↑
        people.append({
            "id": un,
            "initials": _planner_initials(un),
            "short": un,
            "name": un,
            "role": role_label,      # ROLES PATCH: метка первой роли, фолбэк на старую логику
            "roles": roles,          # ROLES PATCH: массив id ролей (аддитивное поле)
            "color": _PLANNER_COLORS[idx % len(_PLANNER_COLORS)],
            "cap": 10,
        })
        idx += 1
    return {"people": people}


@router.get("/api/planner/me")
def whoami(user: str = Depends(_require_planner)):
    return {"username": user}


@router.get("/api/planner/chat")
def chat_get(since: int = 0, _=Depends(_require_planner)):
    return db.chat_since(since)


@router.post("/api/planner/chat")
async def chat_post(request: Request, user: str = Depends(_require_planner)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")
    text = (body.get("text") if isinstance(body, dict) else "") or ""
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    try:
        msg = db.chat_add(user, text[:4000], kind="user")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # обращение к агенту: текст начинается с @agent — агент отвечает по данным доски
    if text[:6].lower() == "@agent":
        question = text[6:].strip() or "Ответь на вопрос пользователя."
        try:
            context = _board_snapshot()
        except Exception:
            context = ""
        try:
            threading.Thread(target=_run_planner_agent, args=(question, context), daemon=True).start()
        except Exception:
            pass
    return {"message": msg}


@router.post("/api/planner/presence")
def presence(user: str = Depends(_require_planner)):
    """Heartbeat присутствия: обновляет last-seen и возвращает список онлайн (< 60с)."""
    now = time.time()
    with _PRESENCE_LOCK:
        _PRESENCE[user] = now
        online = [u for u, ts in _PRESENCE.items() if now - ts < _PRESENCE_TTL]
        # чистка протухших
        for u in [u for u, ts in list(_PRESENCE.items()) if now - ts >= _PRESENCE_TTL * 3]:
            _PRESENCE.pop(u, None)
    try:
        db.presence_touch(user)
        seen = db.presence_all()
    except Exception:
        seen = {}
    return {"online": online, "seen": seen}


@router.get("/api/planner/health")
def planner_health():
    return db.health()


# ==== GITHUB SYNC PATCH (P19) ====
import urllib.error as _gh_urlerr

@router.post("/api/planner/github-sync")
def github_sync(_=Depends(_require_planner)):
    """Импорт открытых issues из GitHub в доску как «неопределённых» задач пула."""
    try:
        from planner import github as _ghsync
    except Exception:
        import github as _ghsync  # type: ignore
    if not _ghsync._token():
        raise HTTPException(status_code=503, detail="GitHub-токен не настроен на сервере")
    try:
        return _ghsync.sync(db)
    except _gh_urlerr.HTTPError as e:
        raise HTTPException(status_code=502, detail="GitHub ответил %s (проверьте токен/доступ к репозиторию)" % e.code)
    except Exception as e:
        raise HTTPException(status_code=502, detail="Ошибка синхронизации GitHub: " + str(e)[:200])


def _github_autosync_loop():
    import logging
    try:
        hours = float(os.environ.get("GITHUB_SYNC_HOURS", "3"))
    except Exception:
        hours = 3.0
    if hours <= 0:
        return
    time.sleep(90)
    while True:
        try:
            try:
                from planner import github as _ghsync
            except Exception:
                import github as _ghsync  # type: ignore
            if _ghsync._token():
                _ghsync.sync(db)
        except Exception:
            logging.getLogger("planner").warning("github autosync failed", exc_info=False)
        time.sleep(hours * 3600)


if os.environ.get("GITHUB_SYNC_ENABLED", "1") == "1":
    try:
        threading.Thread(target=_github_autosync_loop, daemon=True).start()
    except Exception:
        pass
# ==== /GITHUB SYNC PATCH (P19) ====


# ==== REVERSE-GH HELPERS (P22) ====
_gh_create_lock = threading.Lock()
_gh_creating = set()


def _github_create_for_tasks(task_ids):
    """Завести issue на GitHub для задач, переведённых на этап 'dev'. Идемпотентно:
    пропускает уже связанные (ghNumber) и те, что сейчас в процессе. Пишет ghNumber назад."""
    import logging
    try:
        from planner import github as _gh
    except Exception:
        import github as _gh  # type: ignore
    if not _gh._token():
        return
    with _gh_create_lock:
        try:
            board = db.get_board()
        except Exception:
            return
        tmap = {t.get("id"): t for t in board.get("tasks", []) if isinstance(t, dict) and t.get("id")}
        made = []
        for tid in task_ids:
            if tid in _gh_creating:
                continue
            t = tmap.get(tid)
            if not t or t.get("ghNumber") or t.get("stage") != "dev":
                continue
            _gh_creating.add(tid)
            try:
                _gh.ensure_label("team-planer")
                title = t.get("title") or "Задача из ТИМ Планера"
                body = (t.get("description") or "").strip()
                body = (body + "\n\n") if body else ""
                body += "— заведено автоматически из ТИМ Планера (этап «Разработка»)."
                try:
                    from planner import files as _pf2
                    _fm = t.get("files") or []
                    if _fm:
                        body += "\n\n---\n**Вложения из ТИМ Планера:**\n"
                        for _f in _fm:
                            _fid = _f.get("fileId"); _nm = _f.get("name") or "файл"
                            _txt = _pf2.read_text(_fid, 100000) if (_fid and _pf2.is_text(_nm)) else None
                            if _txt is not None:
                                body += "\n<details><summary>%s</summary>\n\n```\n%s\n```\n</details>\n" % (_nm, _txt)
                            else:
                                body += "\n- %s — файл во вложении задачи (ТИМ Планер)" % _nm
                except Exception:
                    pass
                iss = _gh.create_issue(title, body, ["team-planer"])
                t2 = dict(t)
                t2["ghNumber"] = iss.get("number")
                t2["ghUrl"] = iss.get("html_url")
                t2["ghState"] = iss.get("state") or "open"
                t2.setdefault("source", "planner")
                made.append({"type": "task.upsert", "entity": t2})
            except Exception:
                logging.getLogger("planner").warning("reverse-gh create failed for %s" % tid, exc_info=False)
            finally:
                _gh_creating.discard(tid)
        if made:
            try:
                db.apply_ops(made)
            except Exception:
                logging.getLogger("planner").warning("reverse-gh writeback failed", exc_info=False)
# ==== /REVERSE-GH HELPERS (P22) ====


# ==== FILES ENDPOINTS (P24) ====
@router.post("/api/planner/upload")
async def planner_upload(request: Request, user: str = Depends(_require_planner)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="bad body")
    name = (body.get("name") or "file")[:200]
    ftype = body.get("type") or ""
    data = body.get("data") or ""
    if not data:
        raise HTTPException(status_code=400, detail="no data")
    try:
        from planner import files as _pf
    except Exception:
        import files as _pf  # type: ignore
    try:
        return _pf.save(name, ftype, data)
    except ValueError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="save failed")


@router.get("/api/planner/file/{fid}")
def planner_file(fid: str, dl: int = 0, _=Depends(_require_planner)):
    try:
        from planner import files as _pf
    except Exception:
        import files as _pf  # type: ignore
    import urllib.parse as _up
    from fastapi import Response as _Resp
    m = _pf.meta(fid)
    b = _pf.read_bytes(fid)
    if m is None or b is None:
        raise HTTPException(status_code=404, detail="not found")
    disp = ("attachment" if dl else "inline") + "; filename*=UTF-8''" + _up.quote(m.get("name", "file"))
    return _Resp(content=b, media_type=(m.get("type") or "application/octet-stream"),
                 headers={"Content-Disposition": disp})
# ==== /FILES ENDPOINTS (P24) ====
