# -*- coding: utf-8 -*-
"""ТИМ Планер — серверное хранилище вложений задач.

Файлы лежат на диске в data/planner_files/<id>, метаданные — в сайдкаре <id>.json.
Загрузка приходит как base64 (без зависимости python-multipart). id — hex-хеш, поэтому
безопасен для имени файла и не допускает path traversal. Только stdlib."""
import os, re, json, time, base64, hashlib
import config

_DIR = config.FILES_DIR  # каталог вложений из конфигурации (env PLANNER_FILES_DIR)
_MAX = 15 * 1024 * 1024  # 15 МБ на файл
_TEXT_EXT = {
    ".txt", ".md", ".log", ".csv", ".tsv", ".json", ".yml", ".yaml", ".xml", ".ini",
    ".cfg", ".conf", ".toml", ".env", ".py", ".js", ".ts", ".tsx", ".jsx", ".html",
    ".css", ".scss", ".sh", ".bash", ".sql", ".java", ".go", ".rs", ".c", ".cpp",
    ".h", ".hpp", ".rb", ".php", ".kt", ".swift", ".gradle", ".properties",
}


def _ensure():
    os.makedirs(_DIR, exist_ok=True)


def _clean_id(fid):
    return re.sub(r"[^a-f0-9]", "", str(fid or ""))[:40]  # id — hex (sha1), режем всё лишнее


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def save(name, ftype, b64):
    """Сохранить файл из base64. Возвращает запись {id,url,name,type,size}."""
    _ensure()
    raw = base64.b64decode(b64, validate=False)
    if len(raw) > _MAX:
        raise ValueError("file too large (> 15 MB)")
    fid = hashlib.sha1(((name or "") + "|" + str(len(raw)) + "|" + repr(time.time())).encode("utf-8")).hexdigest()
    with open(os.path.join(_DIR, fid), "wb") as f:
        f.write(raw)
    meta = {"id": fid, "name": (name or "file")[:200], "type": ftype or "", "size": len(raw), "ts": _now()}
    with open(os.path.join(_DIR, fid + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return {"id": fid, "url": "/api/planner/file/" + fid, "name": meta["name"], "type": meta["type"], "size": meta["size"]}


def meta(fid):
    p = os.path.join(_DIR, _clean_id(fid) + ".json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def read_bytes(fid):
    p = os.path.join(_DIR, _clean_id(fid))
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as f:
        return f.read()


def is_text(name):
    ext = os.path.splitext(name or "")[1].lower()
    return ext in _TEXT_EXT


def read_text(fid, maxbytes=100000):
    """Прочитать текстовый файл как строку (обрезка по maxbytes). None, если не текст/нет файла."""
    b = read_bytes(fid)
    if b is None:
        return None
    if b"\x00" in b[:8192]:
        return None  # похоже на бинарник — не инлайним
    truncated = len(b) > maxbytes
    if truncated:
        b = b[:maxbytes]
    for enc in ("utf-8", "cp1251"):
        try:
            s = b.decode(enc)
            return s + ("\n…(обрезано)" if truncated else "")
        except Exception:
            continue
    return None
