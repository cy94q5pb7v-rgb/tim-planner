# -*- coding: utf-8 -*-
"""ТИМ Планер — самостоятельное приложение (FastAPI).

Точка входа: `uvicorn web_app:app`. Здесь: авторизация (JWT-cookie), вход/выход,
раздача SPA под /planner/ с гейтом доступа, редирект неавторизованных на /login,
подключение JSON-API планера (routes в planner/planner_api.py).

Важно про порядок: имена _require_auth/_find_user определяются ДО импорта
planner_api (который делает `from web_app import ...`) — так решается круговой импорт."""
import html
import mimetypes
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import (HTMLResponse, RedirectResponse, JSONResponse,
                               FileResponse, Response)
from starlette.exceptions import HTTPException as StarletteHTTPException

import config
from core.auth import _require_auth, _require_admin, _make_token, _verify_password  # noqa: F401
from core.users import _find_user, _load_users                                     # noqa: F401

_FRONTEND = Path(config.FRONTEND_DIR)

app = FastAPI(title="ТИМ Планер")


# ---------------------------------------------------------------------------
# Неавторизованная браузерная навигация → редирект на /login (а не «голый» 401).
# XHR/fetch (Sec-Fetch-Dest != document) продолжают получать 401 — SPA не ломается.
# ---------------------------------------------------------------------------
def _wants_html_nav(request: Request) -> bool:
    return request.method == "GET" and request.headers.get("sec-fetch-dest") == "document"


@app.exception_handler(StarletteHTTPException)
async def _auth_aware_http_exc(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 401 and _wants_html_nav(request):
        nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?next=" + quote(nxt, safe=""), status_code=303)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    resp = await call_next(request)
    try:
        if resp.headers.get("content-type", "").startswith("text/html"):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
    except Exception:
        pass
    return resp


# ---------------------------------------------------------------------------
# Вход / выход
# ---------------------------------------------------------------------------
LOGIN_HTML = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>ТИМ Планер — вход</title>
<style>
:root{--accent:#3D6BE5}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;background:#eef1f6;color:#1a1e27;display:grid;place-items:center;min-height:100vh;margin:0}
.card{background:#fff;border:1px solid rgba(20,23,30,.09);border-radius:18px;padding:34px 34px 28px;width:360px;max-width:calc(100vw - 32px);box-shadow:0 18px 50px -18px rgba(20,23,30,.28)}
h1{font-size:20px;margin:0 0 4px;letter-spacing:-.3px}
.sub{color:#7a828f;font-size:13px;margin:0 0 22px}
label{display:block;font-size:12px;font-weight:700;color:#565b66;margin:14px 0 6px;letter-spacing:.02em}
input{width:100%;border:1px solid #e2e6ec;border-radius:10px;padding:11px 13px;font-size:14px;outline:none;transition:border-color .15s}
input:focus{border-color:var(--accent)}
button{width:100%;margin-top:20px;border:none;background:var(--accent);color:#fff;font-size:14px;font-weight:700;padding:12px;border-radius:10px;cursor:pointer}
button:hover{filter:brightness(1.05)}
.err{background:#fdecee;color:#c02434;border:1px solid #f6c9cf;border-radius:9px;padding:9px 12px;font-size:12.5px;font-weight:600;margin-top:16px;display:__ERRSHOW__}
.logo{width:44px;height:44px;border-radius:12px;background:var(--accent);display:grid;place-items:center;color:#fff;font-weight:800;font-size:20px;margin-bottom:16px}
</style></head><body>
<form class=card method=post action="/login">
  <div class=logo>Т</div>
  <h1>ТИМ Планер</h1>
  <p class=sub>Командная доска планирования</p>
  <input type=hidden name=next value="__NEXT__">
  <label>Логин</label>
  <input name=username autocomplete=username autofocus value="__USER__">
  <label>Пароль</label>
  <input name=password type=password autocomplete=current-password>
  <div class=err>__ERR__</div>
  <button type=submit>Войти</button>
</form></body></html>"""


def _login_next(raw: str) -> str:
    """Whitelist для параметра next: только '/' или пути под '/planner'. Иначе → '/planner/'."""
    raw = (raw or "").strip()
    if raw == "/" or raw.startswith("/planner"):
        return raw
    return "/planner/"


def _render_login(user="", err="", nxt="/planner/"):
    return (LOGIN_HTML
            .replace("__USER__", html.escape(user, quote=True))
            .replace("__NEXT__", html.escape(nxt, quote=True))
            .replace("__ERR__", html.escape(err))
            .replace("__ERRSHOW__", "block" if err else "none"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    tok = request.cookies.get(config.COOKIE)
    from core.auth import _decode_token
    if tok and _decode_token(tok):
        return RedirectResponse("/planner/", status_code=303)
    return HTMLResponse(_render_login())


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    nxt = _login_next(request.query_params.get("next"))
    tok = request.cookies.get(config.COOKIE)
    from core.auth import _decode_token
    if tok and _decode_token(tok):
        return RedirectResponse(nxt, status_code=303)
    return HTMLResponse(_render_login(nxt=nxt))


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...), next: str = Form("")):
    nxt = _login_next(next)
    uname = (username or "").strip().lower()
    u = _find_user(uname)
    if not u or not _verify_password(password, u.get("password_hash", "")):
        return HTMLResponse(_render_login(user=username or "", err="Неверный логин или пароль", nxt=nxt),
                            status_code=401)
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(config.COOKIE, _make_token(uname), httponly=True, samesite="lax",
                    max_age=config.TOKEN_TTL_DAYS * 24 * 3600)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(config.COOKIE)
    return resp


# ---------------------------------------------------------------------------
# Раздача SPA под /planner/ (гейт: авторизация + planner_access|is_admin)
# ---------------------------------------------------------------------------
_NO_ACCESS = (
    "<!doctype html><html lang=ru><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'><title>Планер</title>"
    "<style>body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#eef2ee;color:#0e2018;"
    "display:grid;place-items:center;min-height:100vh;margin:0}.c{background:#fff;border:1px solid rgba(10,40,28,.12);"
    "border-radius:16px;padding:30px 34px;max-width:440px;text-align:center}h1{font-size:19px;margin:0 0 10px}"
    "p{color:#586b61;line-height:1.55;margin:8px 0}a{color:#0e9c4d;font-weight:600;text-decoration:none}</style>"
    "</head><body><div class=c><h1>Нет доступа к Планеру</h1>"
    "<p>Доступ выдаёт администратор (флаг <code>planner_access</code> у пользователя).</p>"
    "<p><a href='/logout'>Выйти</a></p></div></body></html>"
)


def _has_planner(user: str) -> bool:
    u = _find_user(user)
    return bool(u and (u.get("is_admin") or u.get("planner_access")))


@app.get("/planner")
async def planner_root(user: str = Depends(_require_auth)):
    return RedirectResponse("/planner/", status_code=303)


@app.get("/planner/", response_class=HTMLResponse)
async def planner_index(user: str = Depends(_require_auth)):
    if not _has_planner(user):
        return HTMLResponse(_NO_ACCESS, status_code=200)
    return FileResponse(str(_FRONTEND / "index.html"), media_type="text/html")


@app.get("/planner/{path:path}")
async def planner_asset(path: str, user: str = Depends(_require_auth)):
    if not _has_planner(user):
        raise HTTPException(status_code=403, detail="planner access denied")
    target = (_FRONTEND / path).resolve()
    try:
        target.relative_to(_FRONTEND.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    mt = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(str(target), media_type=mt)


# ---------------------------------------------------------------------------
# Админка: страница /admin (только is_admin) + API управления пользователями
# ---------------------------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(user: str = Depends(_require_auth)):
    u = _find_user(user)
    if not u or not u.get("is_admin"):
        return RedirectResponse("/planner/", status_code=303)
    return FileResponse(str(_FRONTEND / "admin.html"), media_type="text/html")


# ---------------------------------------------------------------------------
# Подключаем роутеры (импорт ПОСЛЕ определения имён выше)
# ---------------------------------------------------------------------------
from planner import planner_api  # noqa: E402
app.include_router(planner_api.router)

import admin_api  # noqa: E402
app.include_router(admin_api.router)
