# -*- coding: utf-8 -*-
"""Единая конфигурация ТИМ Планера. Всё берётся из переменных окружения (.env),
секретов в коде нет. Разумные значения по умолчанию — приложение стартует «из коробки»."""
import os
from pathlib import Path

# --- пути ---
BASE_DIR = Path(__file__).resolve().parent                       # .../backend
FRONTEND_DIR = Path(os.environ.get("PLANNER_FRONTEND_DIR", BASE_DIR.parent / "frontend")).resolve()
DATA_DIR = Path(os.environ.get("PLANNER_DATA_DIR", BASE_DIR.parent / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = str(Path(os.environ.get("PLANNER_DB_PATH", DATA_DIR / "planner.db")).resolve())
FILES_DIR = str(Path(os.environ.get("PLANNER_FILES_DIR", DATA_DIR / "planner_files")).resolve())
USERS_FILE = str(Path(os.environ.get("PLANNER_USERS_FILE", DATA_DIR / "users.json")).resolve())

# --- авторизация (JWT в httpOnly-cookie) ---
# ВАЖНО: задайте свой PLANNER_SECRET в проде. Дефолт годится только для локальной разработки.
SECRET = os.environ.get("PLANNER_SECRET", "dev-insecure-change-me-in-production")
JWT_ALG = "HS256"
COOKIE = os.environ.get("PLANNER_COOKIE", "planner_session")
TOKEN_TTL_DAYS = int(os.environ.get("PLANNER_TOKEN_TTL_DAYS", "7"))

# --- ИИ-агент чата (@agent) — опционально, любой OpenAI-совместимый API или CLI ---
# Вариант A (API): PLANNER_AGENT_API_URL + PLANNER_AGENT_API_KEY + PLANNER_AGENT_MODEL
# Вариант B (CLI): PLANNER_AGENT_CMD — команда, получает промпт на stdin, печатает ответ на stdout
AGENT_API_URL = os.environ.get("PLANNER_AGENT_API_URL", "").strip()
AGENT_API_KEY = os.environ.get("PLANNER_AGENT_API_KEY", "").strip()
AGENT_MODEL = os.environ.get("PLANNER_AGENT_MODEL", "gpt-4o-mini").strip()
AGENT_CMD = os.environ.get("PLANNER_AGENT_CMD", "").strip()

# --- почтовые уведомления (опционально, по умолчанию выключены) ---
SMTP_HOST = os.environ.get("PLANNER_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("PLANNER_SMTP_PORT", "465"))
SMTP_USER = os.environ.get("PLANNER_SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("PLANNER_SMTP_PASS", "").strip()
SMTP_FROM = os.environ.get("PLANNER_SMTP_FROM", SMTP_USER).strip()
NOTIFY_TO = os.environ.get("PLANNER_NOTIFY_TO", "").strip()
NOTIFY_ENABLED = os.environ.get("PLANNER_NOTIFY_ENABLED", "0") == "1"

# --- GitHub-интеграция (опционально) ---
GITHUB_TOKEN = os.environ.get("PLANNER_GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN", "")).strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()          # формат: owner/repo
