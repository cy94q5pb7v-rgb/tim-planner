# ТИМ Планер — образ приложения (FastAPI + uvicorn)
FROM python:3.12-slim

WORKDIR /app

# зависимости отдельным слоем (кэш)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# код
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/

# данные (БД, файлы, пользователи) — на volume
ENV PLANNER_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000
WORKDIR /app/backend
# один воркер обязателен: presence и board-lock живут в памяти процесса
CMD ["uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
