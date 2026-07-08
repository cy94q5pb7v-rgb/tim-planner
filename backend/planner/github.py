# -*- coding: utf-8 -*-
"""ТИМ Планер — импорт issues из GitHub в доску как «неопределённые» задачи.

Тянет ТОЛЬКО открытые issues (Pull Request'ы пропускаются) и маппит их в задачи
пула: без эпика, без исполнителя, статус «Не начато» (todo), SP=0, без дат —
то есть «неопределённая» задача, которую пользователь потом разложит вручную.

Дедуп по id 'gh-<number>'. Повторный импорт ОБНОВЛЯЕТ заголовок/состояние/метки,
НО сохраняет ручную сортировку пользователя (эпик, исполнитель, даты, статус,
порядок), проставленную после импорта. Описание обновляется только если оно
всё ещё авто-сгенерированное (не редактировалось руками).

Токен и репозиторий — из окружения, НИКОГДА не логируются. Только stdlib."""
import os, re, json, time, urllib.request, urllib.error, urllib.parse

GH_API = "https://api.github.com"
_TOKEN_KEYS = ("PLANNER_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


def _repo():
    """Репозиторий вида owner/repo из окружения (env GITHUB_REPO). Пусто = интеграция выключена."""
    return (os.environ.get("GITHUB_REPO") or "").strip()


def _token():
    """GitHub-токен из окружения. Значение НИКОГДА не логируется. Пусто = интеграция выключена."""
    for k in _TOKEN_KEYS:
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    return None


def _auth(req, token):
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "tim-planner-sync")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    return req


def _api_get(path, token):
    req = _auth(urllib.request.Request(GH_API + path), token)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _api_post(path, obj, token):
    data = json.dumps(obj).encode("utf-8")
    req = _auth(urllib.request.Request(GH_API + path, data=data, method="POST"), token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---- обратное направление: задача Планера → issue на GitHub ----
def ensure_label(name, color="6E56CF", desc="Заведено из ТИМ Планера", repo=None, token=None):
    """Создать метку, если её ещё нет (идемпотентно). Ошибки не критичны."""
    repo = repo or _repo(); token = token or _token()
    if not token:
        return False
    try:
        _api_get("/repos/%s/labels/%s" % (repo, urllib.parse.quote(name)), token)
        return True                      # уже есть
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return False
    try:
        _api_post("/repos/%s/labels" % repo, {"name": name, "color": color, "description": desc}, token)
        return True
    except Exception:
        return False


def create_issue(title, body="", labels=None, repo=None, token=None):
    """Создать issue. Возвращает {number, html_url, state, ...}."""
    repo = repo or _repo(); token = token or _token()
    if not token:
        raise RuntimeError("no github token configured")
    payload = {"title": (title or "Задача из ТИМ Планера")[:250]}
    if body:
        payload["body"] = body[:60000]
    if labels:
        payload["labels"] = labels
    return _api_post("/repos/%s/issues" % repo, payload, token)


def fetch_issues(state="all", repo=None, token=None):
    """Список issues (без PR) с заданным состоянием: 'open' | 'closed' | 'all'.
    Пагинация до 40 страниц (4000 issues)."""
    repo = repo or _repo()
    token = token or _token()
    if not token:
        raise RuntimeError("no github token configured")
    if not repo:
        raise RuntimeError("GITHUB_REPO not configured (ожидается формат owner/repo)")
    issues, page = [], 1
    while page <= 40:
        data = _api_get("/repos/%s/issues?state=%s&per_page=100&page=%d" % (repo, state, page), token)
        if not isinstance(data, list) or not data:
            break
        for it in data:
            if isinstance(it, dict) and "pull_request" not in it:  # PR тоже приходят сюда — пропускаем
                issues.append(it)
        if len(data) < 100:
            break
        page += 1
    return issues


def fetch_open_issues(repo=None, token=None):  # back-compat
    return fetch_issues("open", repo, token)


def _labels(issue):
    return [l.get("name", "") for l in (issue.get("labels") or [])
            if isinstance(l, dict) and l.get("name")]


def _priority_from_labels(labels):
    low = [l.lower() for l in labels]
    if any(x in low for x in ("prio:p1", "priority:p1", "p1")):
        return "critical"
    if any(x in low for x in ("prio:p2", "priority:p2", "p2")):
        return "important"
    return "normal"


def issue_to_task(issue, order=0):
    num = issue.get("number")
    labels = _labels(issue)
    url = issue.get("html_url") or ""
    title = (issue.get("title") or ("issue #%s" % num)).strip()
    lab_line = ("\nМетки: " + ", ".join(labels)) if labels else ""
    desc = "GitHub #%s — %s%s" % (num, url, lab_line)
    body = (issue.get("body") or "").strip()
    if body:
        desc += "\n\n" + body[:1500]
    return {
        "id": "gh-%s" % num,
        "title": title,
        "epicId": None, "streamId": "", "stage": "ba", "status": "todo",
        "assigneeId": None, "est": {"ba": 0, "sa": 0, "dev": 0, "test": 0},
        "start": None, "dur": 5, "deadline": None,
        "description": desc,
        "priority": _priority_from_labels(labels),
        "createdBy": "GitHub", "createdAt": int(time.time() * 1000),
        "order": order,
        # маркеры источника: дедуп + обновление, не терять при ре-импорте
        "source": "github", "ghNumber": num, "ghUrl": url,
        "ghState": issue.get("state"), "ghLabels": labels,
    }


# поля, которые обновляем при ре-импорте; всё остальное у существующей gh-задачи сохраняется
_REFRESH = ("title", "ghState", "ghLabels", "ghUrl")


def build_ops(issues, board):
    """Строит список task.upsert.
    - открытый issue: новый → «неопределённая» задача; существующий → мердж (обновляем
      заголовок/метки, сохраняем ручную сортировку, статус НЕ трогаем);
    - закрытый issue: только если задача УЖЕ импортирована — переводим в «Готово» (done)
      и обновляем маркеры; закрытые issue, которых нет в доске, НЕ создаём (не тащим историю)."""
    tasks = [t for t in (board.get("tasks") or []) if isinstance(t, dict) and t.get("id")]
    # сопоставление по НОМЕРУ issue (ghNumber), а не по id — так реверс-задачи
    # (созданные из Планера, со своим id вроде 't123', но с ghNumber) не задваиваются
    by_gh = {}
    for t in tasks:
        n = t.get("ghNumber")
        if n is not None:
            by_gh[str(n)] = t
    max_order = 0
    for t in tasks:
        try:
            max_order = max(max_order, int(t.get("order") or 0))
        except Exception:
            pass
    ops, added, updated, closed = [], 0, 0, 0
    for i, issue in enumerate(issues):
        num = str(issue.get("number"))
        is_closed = (issue.get("state") == "closed")
        fresh = issue_to_task(issue, order=max_order + (i + 1) * 16)
        if num in by_gh:
            merged = dict(by_gh[num])              # сохраняем ручную сортировку и id задачи
            for k in _REFRESH:
                merged[k] = fresh[k]
            # стрим НЕ трогаем: импорт кладёт в «нераспределённые» (streamId=''), дальше — ручная сортировка
            if str(merged.get("description", "")).startswith("GitHub #"):
                merged["description"] = fresh["description"]  # описание не редактировали — освежаем
            if is_closed:
                if merged.get("status") != "done":
                    merged["status"] = "done"      # issue закрыт в GitHub → статус «Готово»
                    closed += 1
                if merged.get("stage") != "prod":
                    merged["stage"] = "prod"        # ...и задача уходит на этап «Пром»
            ops.append({"type": "task.upsert", "entity": merged})
            updated += 1
        elif not is_closed:
            ops.append({"type": "task.upsert", "entity": fresh})
            added += 1
        # закрытый issue, которого нет в доске — пропускаем (не импортируем историю)
    return ops, added, updated, closed


def sync(db, repo=None, token=None):
    """db — модуль planner.db (get_board / apply_ops). Возвращает статистику.
    Тянет ВСЕ issue (open+closed): открытые импортируются/обновляются, закрытые —
    закрывают уже импортированные задачи (переводят в «Готово»)."""
    issues = fetch_issues("all", repo, token)
    board = db.get_board()
    ops, added, updated, closed = build_ops(issues, board)
    if ops:
        board = db.apply_ops(ops)
    return {"ok": True, "repo": repo or _repo(), "issues": len(issues),
            "added": added, "updated": updated, "closed": closed, "rev": board.get("rev")}
