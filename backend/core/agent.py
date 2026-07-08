# -*- coding: utf-8 -*-
"""Подключаемый ИИ-агент для чата Планера (@agent). Три режима (по .env):
  1) API   — любой OpenAI-совместимый chat/completions (PLANNER_AGENT_API_URL/KEY/MODEL);
  2) CLI   — внешняя команда PLANNER_AGENT_CMD (промпт на stdin, ответ на stdout);
  3) выкл  — если ничего не настроено, агент вежливо сообщает об этом.
Только stdlib (urllib) для API-режима — лишних зависимостей нет.
Точка входа — run_agent(question, context)."""
import json
import subprocess
import urllib.request

import config

_SYSTEM = ("Ты — ассистент планировщика ТИМ. Отвечай кратко и по делу, СТРОГО по данным доски, "
           "не выдумывай. Если данных не хватает — так и скажи. Отвечай на русском.")


def _run_via_api(prompt: str) -> str:
    payload = {
        "model": config.AGENT_MODEL,
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        config.AGENT_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + config.AGENT_API_KEY},
    )
    with urllib.request.urlopen(req, timeout=170) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()


def _run_via_cmd(prompt: str) -> str:
    proc = subprocess.run(config.AGENT_CMD, shell=True, input=prompt,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=170)
    return (proc.stdout or "").strip()


def run_agent(question: str, context: str = "") -> str:
    """Вернуть ответ агента (или человеко-понятное сообщение об ошибке/выключении)."""
    prompt = (("=== СНАПШОТ ДОСКИ ===\n" + context + "\n=== КОНЕЦ СНАПШОТА ===\n\n") if context else "") \
             + "Вопрос: " + question
    try:
        if config.AGENT_API_URL and config.AGENT_API_KEY:
            reply = _run_via_api(prompt)
        elif config.AGENT_CMD:
            reply = _run_via_cmd(prompt)
        else:
            return ("ИИ-агент не настроен. Задайте в .env PLANNER_AGENT_API_URL + "
                    "PLANNER_AGENT_API_KEY (любой OpenAI-совместимый API) или PLANNER_AGENT_CMD.")
        return reply or "Агент вернул пустой ответ."
    except subprocess.TimeoutExpired:
        return "Агент думал слишком долго — попробуйте ещё раз."
    except Exception as e:  # noqa: BLE001
        return "Ошибка агента: " + str(e)[:180]
