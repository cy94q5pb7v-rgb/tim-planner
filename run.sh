#!/usr/bin/env bash
# Локальный запуск ТИМ Планера (Linux/macOS) без Docker.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "→ создаю виртуальное окружение .venv"
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip >/dev/null
  ./.venv/bin/pip install -r requirements.txt
fi

if [ ! -f .env ]; then
  echo "→ .env не найден: копирую из .env.example (не забудьте задать PLANNER_SECRET)"
  cp .env.example .env
fi

# подхватить переменные из .env
set -a; . ./.env; set +a

if [ ! -f "${PLANNER_USERS_FILE:-data/users.json}" ]; then
  echo "→ нет пользователей. Создайте админа:"
  echo "   ./.venv/bin/python scripts/make_user.py admin --password 'СВОЙ_ПАРОЛЬ' --admin"
fi

cd backend
exec ../.venv/bin/uvicorn web_app:app --host 127.0.0.1 --port 8000 --workers 1
