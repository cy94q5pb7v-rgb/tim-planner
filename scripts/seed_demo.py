#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Наполнить доску демо-данными (эпики + задачи), чтобы сразу увидеть Планер в деле.
Идемпотентно: перезаписывает демо-сущности по фиксированным id.

Запуск:  python scripts/seed_demo.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
from planner import db  # noqa: E402

EPICS = [
    {"id": "ep-onb", "key": "EP1", "name": "Онбординг клиентов", "color": "#3D6BE5",
     "status": "doing", "planned": True, "start": "2026-07-02", "due": "2026-07-25", "order": 1024},
    {"id": "ep-an", "key": "EP2", "name": "Аналитика и отчётность", "color": "#12A594",
     "status": "todo", "planned": True, "start": "2026-07-06", "due": "2026-08-15", "order": 2048},
]


def _task(i, epic, title, stream, stage, status, start, dur, deadline):
    return {"id": "demo-t%d" % i, "epicId": epic, "title": title, "streamId": stream,
            "stage": stage, "status": status, "assigneeId": None,
            "est": {"ba": 2, "sa": 1, "dev": 3, "test": 1},
            "start": start, "dur": dur, "deadline": deadline, "order": i * 16,
            "stageDone": {"ba": stage in ("sa", "dev", "test", "prod")}}


TASKS = [
    _task(1, "ep-onb", "Форма регистрации нового клиента", "feature", "dev", "doing", "2026-07-02", 4, "2026-07-08"),
    _task(2, "ep-onb", "Проверка документов по API", "ui", "ba", "todo", "2026-07-06", 3, "2026-07-10"),
    _task(3, "ep-onb", "Экран приветствия и онбординг-тур", "ui", "test", "done", "2026-07-02", 2, "2026-07-06"),
    _task(4, "ep-an", "Дашборд ключевых метрик", "feature", "dev", "doing", "2026-07-13", 5, "2026-07-20"),
    _task(5, "ep-an", "Экспорт сводного отчёта в PDF", "infra", "ba", "todo", "2026-07-21", 2, "2026-07-24"),
]


def main():
    ops = [{"type": "epic.upsert", "entity": e} for e in EPICS] + \
          [{"type": "task.upsert", "entity": t} for t in TASKS]
    board = db.apply_ops(ops)
    print("OK: демо-данные загружены. rev=%s, задач=%d, эпиков=%d"
          % (board.get("rev"), len(board.get("tasks", [])), len(board.get("epics", []))))


if __name__ == "__main__":
    main()
