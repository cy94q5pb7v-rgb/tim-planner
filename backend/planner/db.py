# -*- coding: utf-8 -*-
"""ТИМ Планер — SQLite слой. Совместная доска: сервер — источник правды.
Хранит доску одной строкой key='board' = {rev, tasks, epics}. Клиенты шлют
операции (upsert/delete по сущностям) → apply_ops сливает и поднимает rev;
клиенты опрашивают get_board и подхватывают чужие изменения. Мутации
сериализуются процессным Lock (uvicorn — один процесс). Обратная совместимость:
старый снимок key='main' {state,epics} мигрирует в board. Pure stdlib."""
import os, json, sqlite3, time, threading
import config

_DB_DEFAULT = config.DB_PATH  # путь к БД из конфигурации (env PLANNER_DB_PATH)
_SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
_LOCK = threading.Lock()


def _connect(db_path):
    con = sqlite3.connect(db_path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=8000")
    return con


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_schema(db_path=_DB_DEFAULT):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with open(_SCHEMA, "r", encoding="utf-8") as f:
        ddl = f.read()
    con = _connect(db_path)
    try:
        con.executescript(ddl)
        con.execute("CREATE TABLE IF NOT EXISTS planner_state_history ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, data TEXT, saved_at TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS planner_chat ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT, text TEXT, ts TEXT, kind TEXT)")
        con.commit()
    finally:
        con.close()


def _chat_ensure(con):
    con.execute("CREATE TABLE IF NOT EXISTS planner_chat ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT, text TEXT, ts TEXT, kind TEXT)")


def chat_add(author, text, kind="user", db_path=_DB_DEFAULT):
    text = (text or "").strip()
    if not text:
        raise ValueError("empty message")
    text = text[:4000]
    with _LOCK:
        con = _connect(db_path)
        try:
            _chat_ensure(con)
            ts = _now()
            cur = con.execute("INSERT INTO planner_chat(author,text,ts,kind) VALUES(?,?,?,?)",
                              (author or "", text, ts, kind or "user"))
            con.commit()
            return {"id": cur.lastrowid, "author": author or "", "text": text, "ts": ts, "kind": kind or "user"}
        finally:
            con.close()


def chat_since(since_id=0, limit=300, db_path=_DB_DEFAULT):
    with _LOCK:
        con = _connect(db_path)
        try:
            _chat_ensure(con)
            rows = con.execute(
                "SELECT id,author,text,ts,kind FROM planner_chat WHERE id>? ORDER BY id ASC LIMIT ?",
                (int(since_id or 0), int(limit))).fetchall()
            msgs = [{"id": r["id"], "author": r["author"], "text": r["text"], "ts": r["ts"], "kind": r["kind"]}
                    for r in rows]
            last = con.execute("SELECT COALESCE(MAX(id),0) m FROM planner_chat").fetchone()["m"]
            return {"messages": msgs, "lastId": last}
        finally:
            con.close()


def _migrate(con):
    """Create key='board' from legacy key='main' snapshot if board is absent."""
    b = con.execute("SELECT 1 FROM planner_state WHERE key='board'").fetchone()
    if b:
        return
    tasks, epics = [], []
    old = con.execute("SELECT data FROM planner_state WHERE key='main'").fetchone()
    if old:
        try:
            o = json.loads(old["data"])
            st = o.get("state") or {}
            tasks = st.get("tasks") or []
            epics = o.get("epics") or []
        except Exception:
            pass
    board = {"rev": 1, "tasks": tasks, "epics": epics}
    con.execute("INSERT OR IGNORE INTO planner_state(key,data,updated_at) VALUES('board',?,?)",
                (json.dumps(board, ensure_ascii=False), _now()))


def _read_board(con):
    r = con.execute("SELECT data FROM planner_state WHERE key='board'").fetchone()
    if not r:
        return {"rev": 0, "tasks": [], "epics": []}, None
    try:
        b = json.loads(r["data"])
    except Exception:
        return {"rev": 0, "tasks": [], "epics": []}, r["data"]
    b.setdefault("rev", 0)
    b.setdefault("tasks", [])
    b.setdefault("epics", [])
    return b, r["data"]


def get_board(db_path=_DB_DEFAULT):
    with _LOCK:
        con = _connect(db_path)
        try:
            _migrate(con)
            con.commit()
            board, _ = _read_board(con)
            return board
        finally:
            con.close()


def apply_ops(ops, db_path=_DB_DEFAULT, keep_history=30):
    """ops: list of {type, entity|id}. Types: task.upsert/task.delete/epic.upsert/epic.delete.
    Returns the new authoritative board {rev, tasks, epics}."""
    if not isinstance(ops, list):
        raise ValueError("ops must be a list")
    if len(ops) > 5000:
        raise ValueError("too many ops")
    with _LOCK:
        con = _connect(db_path)
        try:
            _migrate(con)
            board, raw = _read_board(con)
            tasks = {}
            t_order = []
            for t in board.get("tasks", []):
                if isinstance(t, dict) and t.get("id"):
                    if t["id"] not in tasks:
                        t_order.append(t["id"])
                    tasks[t["id"]] = t
            epics = {}
            e_order = []
            for e in board.get("epics", []):
                if isinstance(e, dict) and e.get("id"):
                    if e["id"] not in epics:
                        e_order.append(e["id"])
                    epics[e["id"]] = e
            for op in ops:
                if not isinstance(op, dict):
                    continue
                ty = op.get("type")
                if ty == "task.upsert":
                    ent = op.get("entity")
                    if isinstance(ent, dict) and ent.get("id"):
                        if ent["id"] not in tasks:
                            t_order.append(ent["id"])
                        tasks[ent["id"]] = ent
                elif ty == "task.delete":
                    i = op.get("id")
                    if tasks.pop(i, None) is not None and i in t_order:
                        t_order.remove(i)
                elif ty == "epic.upsert":
                    ent = op.get("entity")
                    if isinstance(ent, dict) and ent.get("id"):
                        if ent["id"] not in epics:
                            e_order.append(ent["id"])
                        epics[ent["id"]] = ent
                elif ty == "epic.delete":
                    i = op.get("id")
                    if epics.pop(i, None) is not None and i in e_order:
                        e_order.remove(i)
            new_board = {
                "rev": int(board.get("rev", 0)) + 1,
                "tasks": [tasks[i] for i in t_order if i in tasks],
                "epics": [epics[i] for i in e_order if i in epics],
            }
            if raw is not None:
                con.execute("INSERT INTO planner_state_history(key,data,saved_at) VALUES('board',?,?)",
                            (raw, _now()))
                con.execute("DELETE FROM planner_state_history WHERE key='board' AND id NOT IN "
                            "(SELECT id FROM planner_state_history WHERE key='board' ORDER BY id DESC LIMIT ?)",
                            (keep_history,))
            con.execute(
                "INSERT INTO planner_state(key,data,updated_at) VALUES('board',?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                (json.dumps(new_board, ensure_ascii=False), _now()))
            con.commit()
            return new_board
        finally:
            con.close()


# ---- legacy snapshot API (kept for safety/back-compat; UI uses board) ----
def get_state(db_path=_DB_DEFAULT):
    con = _connect(db_path)
    try:
        r = con.execute("SELECT data FROM planner_state WHERE key='main'").fetchone()
        if not r:
            return None
        try:
            return json.loads(r["data"])
        except Exception:
            return None
    finally:
        con.close()


def save_state(obj, db_path=_DB_DEFAULT, keep_history=20):
    if not isinstance(obj, dict) or not isinstance(obj.get("state"), dict):
        raise ValueError("bad planner snapshot (need {state:{...}})")
    data = json.dumps(obj, ensure_ascii=False)
    con = _connect(db_path)
    try:
        prev = con.execute("SELECT data FROM planner_state WHERE key='main'").fetchone()
        if prev:
            con.execute("INSERT INTO planner_state_history(key,data,saved_at) VALUES('main',?,?)",
                        (prev["data"], _now()))
            con.execute("DELETE FROM planner_state_history WHERE key='main' AND id NOT IN "
                        "(SELECT id FROM planner_state_history WHERE key='main' ORDER BY id DESC LIMIT ?)",
                        (keep_history,))
        con.execute(
            "INSERT INTO planner_state(key,data,updated_at) VALUES('main',?,?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
            (data, _now()))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "bytes": len(data), "updated_at": _now()}


def health(db_path=_DB_DEFAULT):
    try:
        con = _connect(db_path)
        try:
            b = con.execute("SELECT updated_at,length(data) n FROM planner_state WHERE key='board'").fetchone()
            h = con.execute("SELECT COUNT(*) c FROM planner_state_history").fetchone()
            board, _ = _read_board(con)
        finally:
            con.close()
        return {"ok": True, "db": "up", "has_board": bool(b),
                "rev": board.get("rev", 0),
                "tasks": len(board.get("tasks", [])), "epics": len(board.get("epics", [])),
                "updated_at": (b["updated_at"] if b else None),
                "bytes": (b["n"] if b else 0), "history": (h["c"] if h else 0)}
    except Exception as e:
        return {"ok": False, "db": "down", "error": str(e)[:200]}


def presence_touch(user, db_path=_DB_DEFAULT):
    """Обновить время последнего захода пользователя (upsert)."""
    if not user:
        return
    with _LOCK:
        con = _connect(db_path)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS planner_presence(user TEXT PRIMARY KEY, last_seen TEXT)")
            con.execute("INSERT INTO planner_presence(user,last_seen) VALUES(?,?) "
                        "ON CONFLICT(user) DO UPDATE SET last_seen=excluded.last_seen", (user, _now()))
            con.commit()
        finally:
            con.close()


def presence_all(db_path=_DB_DEFAULT):
    """{user: last_seen_iso} по всем известным пользователям."""
    with _LOCK:
        con = _connect(db_path)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS planner_presence(user TEXT PRIMARY KEY, last_seen TEXT)")
            rows = con.execute("SELECT user,last_seen FROM planner_presence").fetchall()
            return {r["user"]: r["last_seen"] for r in rows}
        finally:
            con.close()
