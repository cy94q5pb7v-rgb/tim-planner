# -*- coding: utf-8 -*-
"""ТИМ Планер — почтовые уведомления о смене статуса/этапа задачи.

Назначение (будущий путь на сервере: web/planner/notify.py):
  planner_api.mutate() после apply_ops собирает список изменений и зовёт
  notify_task_changes(changes, actor, people, epics). Здесь мы в ФОНОВОМ потоке
  рендерим красивое email-safe письмо и шлём его через smtplib на один адрес.

Безопасность:
  - Единственный адрес-получатель захардкожен (NOTIFY_TO). Всё остальное —
    отправитель, пароль, хост, порт — читается в рантайме из окружения или из
    trendwatch_env.sh. Пароль живёт только в локальной переменной, НИКОГДА не
    логируется и не попадает в текст исключений.
  - Любой сбой SMTP гасится (try/except + logging), в вызывающий код не
    пробрасывается: почта не должна ронять сохранение доски.

Зависимостей вне stdlib нет.
"""

import os
import re
import ssl
import time
import smtplib
import logging
import threading
from datetime import datetime, timezone, timedelta
from email.utils import formataddr, formatdate, make_msgid
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

log = logging.getLogger("planner.notify")

import config

# получатель уведомлений — из конфигурации (env PLANNER_NOTIFY_TO)
NOTIFY_TO = config.NOTIFY_TO

MSK = timezone(timedelta(hours=3))

# --- антишторм: не больше N писем на вызов и ~30 в час ----------------------
_MAX_PER_CALL = 5
_MAX_PER_HOUR = 30
_sent_times = []            # список epoch-секунд отправленных писем за последний час
_sent_lock = threading.Lock()

# --- словари домена (мирроринг клиента) ------------------------------------
STAGE_LABELS = {"ba": "Бизнес-аналитика", "sa": "Системная аналитика",
                "dev": "Разработка", "test": "Тестирование", "prod": "Пром"}
STATUS_LABELS = {"todo": "Не начато", "doing": "В работе", "done": "Готово"}
STREAM_LABELS = {"feature": "Фича", "newfeature": "Новая фича", "ui": "Интерфейс",
                 "criteria": "Критериальная модель", "infra": "Инфраструктура"}

STATUS_COLORS = {"todo": "#6B7280", "doing": "#3D6BE5", "done": "#12805C"}
STAGE_COLORS = {"ba": "#D93A4A", "sa": "#3D6BE5", "dev": "#12A594",
                "test": "#E0900A", "prod": "#8E4EC6"}

_GREY = "#6B7280"
_DARK = "#1F2430"
_OVERDUE = "#D93A4A"


# ===========================================================================
#  Креды
# ===========================================================================
def _load_smtp():
    """(host, port, user, password) из конфигурации (env PLANNER_SMTP_*).
    Пароль живёт только в локальной переменной, никуда не пишется. Если пусто —
    отправка мягко провалится в notify-потоке (без исключения наружу)."""
    return config.SMTP_HOST, config.SMTP_PORT, config.SMTP_USER, config.SMTP_PASS


# ===========================================================================
#  Утилиты рендера
# ===========================================================================
def _esc(s):
    s = "" if s is None else str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _people_index(people):
    """people может быть list[dict] или dict{id:name}. Вернуть {id: name}."""
    idx = {}
    if isinstance(people, dict):
        for k, v in people.items():
            if isinstance(v, dict):
                idx[k] = v.get("name") or v.get("short") or k
            else:
                idx[k] = v or k
    elif isinstance(people, (list, tuple)):
        for p in people:
            if isinstance(p, dict) and p.get("id"):
                idx[p["id"]] = p.get("name") or p.get("short") or p["id"]
    return idx


def _epics_index(epics):
    """epics: list[dict] или dict{id:epic}. Вернуть {id: epic}."""
    if isinstance(epics, dict):
        return dict(epics)
    idx = {}
    if isinstance(epics, (list, tuple)):
        for e in epics:
            if isinstance(e, dict) and e.get("id"):
                idx[e["id"]] = e
    return idx


def _sp(task):
    e = task.get("est") or {}
    try:
        return sum(int(e.get(k, 0) or 0) for k in ("ba", "sa", "dev", "test"))
    except (TypeError, ValueError):
        return 0


def _fmt_msk(dt=None):
    dt = dt or datetime.now(MSK)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


def _fmt_ddmm(iso):
    """'2026-07-10' -> '10.07'. Возвращает '' при неудаче."""
    try:
        d = datetime.strptime(str(iso)[:10], "%Y-%m-%d").date()
        return d.strftime("%d.%m")
    except (TypeError, ValueError):
        return ""


def _is_overdue(task):
    dl = task.get("deadline")
    if not dl or task.get("status") == "done":
        return False
    try:
        return datetime.strptime(str(dl)[:10], "%Y-%m-%d").date() < datetime.now(MSK).date()
    except (TypeError, ValueError):
        return False


def _badge(text, bg, fg="#FFFFFF", bold=True):
    """Email-safe бейдж: ячейка таблицы с bgcolor + padding (без flex/border-radius-хаков)."""
    weight = "bold" if bold else "normal"
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="display:inline-block;vertical-align:middle;">'
        '<tr><td bgcolor="{bg}" style="background:{bg};padding:4px 10px;border-radius:4px;'
        'font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:14px;'
        'font-weight:{w};color:{fg};white-space:nowrap;">{t}</td></tr></table>'
    ).format(bg=bg, fg=fg, w=weight, t=_esc(text))


def _clip(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ===========================================================================
#  Рендер письма
# ===========================================================================
def render_email(change, actor, people, epics_by_id):
    """Собрать (subject, html, text) по одному изменению.

    change = {task, old_status, old_stage}, где old_* = None если поле не менялось.
    """
    task = change.get("task") or {}
    old_status = change.get("old_status")
    old_stage = change.get("old_stage")

    people_idx = _people_index(people)
    epics_idx = _epics_index(epics_by_id)

    epic = epics_idx.get(task.get("epicId")) or {}
    epic_key = epic.get("key") or "—"
    epic_name = epic.get("name") or ""
    epic_color = epic.get("color") or _GREY

    title = task.get("title") or "(без названия)"
    cur_status = task.get("status") or "todo"
    cur_stage = task.get("stage") or "ba"

    status_changed = old_status is not None and old_status != cur_status
    stage_changed = old_stage is not None and old_stage != cur_stage

    # ---- тема ----
    subj_title = _clip(title, 60)
    parts = []
    if status_changed:
        parts.append("статус: " + STATUS_LABELS.get(cur_status, cur_status))
    if stage_changed:
        parts.append("этап: " + STAGE_LABELS.get(cur_stage, cur_stage))
    tail = " · ".join(parts) if parts else ("статус: " + STATUS_LABELS.get(cur_status, cur_status))
    subject = "ТИМ Планер · [%s] %s — %s" % (epic_key, subj_title, tail)

    # ---- прехедер ----
    if status_changed:
        pre = "[%s] %s: статус → %s" % (epic_key, subj_title, STATUS_LABELS.get(cur_status, cur_status))
    elif stage_changed:
        pre = "[%s] %s: этап → %s" % (epic_key, subj_title, STAGE_LABELS.get(cur_stage, cur_stage))
    else:
        pre = "[%s] %s" % (epic_key, subj_title)

    # ---- строки «Что изменилось» ----
    change_rows = []
    if status_changed:
        change_rows.append(("Статус",
                            _badge(STATUS_LABELS.get(old_status, old_status), _GREY),
                            _badge(STATUS_LABELS.get(cur_status, cur_status),
                                   STATUS_COLORS.get(cur_status, _GREY))))
    if stage_changed:
        change_rows.append(("Этап",
                            _badge(STAGE_LABELS.get(old_stage, old_stage), _GREY),
                            _badge(STAGE_LABELS.get(cur_stage, cur_stage),
                                   STAGE_COLORS.get(cur_stage, _GREY))))
    if not change_rows:  # страховка: письмо всё равно осмысленно
        change_rows.append(("Статус", _badge("—", _GREY),
                            _badge(STATUS_LABELS.get(cur_status, cur_status),
                                   STATUS_COLORS.get(cur_status, _GREY))))

    # ---- мета ----
    assignee = people_idx.get(task.get("assigneeId")) or task.get("assigneeId") or "—"
    stream = STREAM_LABELS.get(task.get("streamId"), task.get("streamId") or "—")
    sprint = task.get("sprint")
    sprint_txt = str(sprint) if sprint is not None else "—"
    sp = _sp(task)
    desc = _clip(task.get("description") or "", 300)

    dl = task.get("deadline")
    if dl and _is_overdue(task):
        deadline_html = ('<span style="color:%s;font-weight:bold;">просрочен %s</span>'
                         % (_OVERDUE, _esc(_fmt_ddmm(dl))))
    elif dl:
        deadline_html = _esc(_fmt_ddmm(dl)) or "—"
    else:
        deadline_html = "—"

    epic_cell = ('<span style="color:%s;">&#9679;</span> <b>%s</b> %s'
                 % (epic_color, _esc(epic_key), _esc(epic_name)))

    html = _build_html(pre, epic_key, epic_color, change_rows, title, desc,
                       epic_cell, stream, assignee, actor, sprint_txt, sp,
                       deadline_html, cur_status, cur_stage)
    text = _build_text(change_rows_plain(status_changed, stage_changed, old_status,
                                         cur_status, old_stage, cur_stage),
                       epic_key, epic_name, title, desc, stream, assignee, actor,
                       sprint_txt, sp, dl, task, cur_status, cur_stage)
    return subject, html, text


def change_rows_plain(status_changed, stage_changed, old_status, cur_status, old_stage, cur_stage):
    rows = []
    if status_changed:
        rows.append("Статус: %s -> %s" % (STATUS_LABELS.get(old_status, old_status),
                                          STATUS_LABELS.get(cur_status, cur_status)))
    if stage_changed:
        rows.append("Этап: %s -> %s" % (STAGE_LABELS.get(old_stage, old_stage),
                                        STAGE_LABELS.get(cur_stage, cur_stage)))
    if not rows:
        rows.append("Статус: -> %s" % STATUS_LABELS.get(cur_status, cur_status))
    return rows


def _build_html(pre, epic_key, epic_color, change_rows, title, desc, epic_cell,
                stream, assignee, actor, sprint_txt, sp, deadline_html,
                cur_status, cur_stage):
    now_txt = _fmt_msk()

    # блок «что изменилось»
    ch = []
    for label, old_b, new_b in change_rows:
        ch.append(
            '<tr><td style="padding:6px 0;font-family:Arial,Helvetica,sans-serif;">'
            '<span style="display:inline-block;min-width:64px;font-size:12px;color:%s;'
            'font-weight:bold;text-transform:uppercase;letter-spacing:.4px;">%s</span> '
            '%s <span style="color:%s;font-size:15px;padding:0 6px;">&#8594;</span> %s'
            '</td></tr>' % (_GREY, _esc(label), old_b, _GREY, new_b))
    change_block = "".join(ch)

    def meta_row(k, v):
        return ('<tr>'
                '<td style="padding:7px 12px 7px 0;font-family:Arial,Helvetica,sans-serif;'
                'font-size:12px;color:%s;white-space:nowrap;vertical-align:top;">%s</td>'
                '<td style="padding:7px 0;font-family:Arial,Helvetica,sans-serif;'
                'font-size:13px;color:%s;vertical-align:top;">%s</td></tr>'
                % (_GREY, _esc(k), _DARK, v))

    meta = "".join([
        meta_row("Эпик", epic_cell),
        meta_row("Стрим", _esc(stream)),
        meta_row("Исполнитель", _esc(assignee)),
        meta_row("Изменение внёс", _esc(actor or "—")),
        meta_row("Спринт", _esc(sprint_txt)),
        meta_row("Оценка", "%d SP" % sp),
        meta_row("Дедлайн", deadline_html),
    ])

    cur_badges = (_badge(STATUS_LABELS.get(cur_status, cur_status),
                         STATUS_COLORS.get(cur_status, _GREY))
                  + '<span style="display:inline-block;width:8px;"></span>'
                  + _badge(STAGE_LABELS.get(cur_stage, cur_stage),
                           STAGE_COLORS.get(cur_stage, _GREY)))

    return """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{subject_pre}</title></head>
<body style="margin:0;padding:0;background:#F4F5F7;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#F4F5F7;font-size:1px;line-height:1px;">{pre}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#F4F5F7" style="background:#F4F5F7;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background:#FFFFFF;border-radius:8px;overflow:hidden;">

  <!-- шапка -->
  <tr><td bgcolor="{dark}" style="background:{dark};padding:16px 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
      <td align="left" style="font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;
          letter-spacing:2px;color:#FFFFFF;">ТИМ&nbsp;ПЛАНЕР</td>
      <td align="right">{epic_badge}</td>
    </tr></table>
  </td></tr>

  <!-- что изменилось -->
  <tr><td style="padding:22px 24px 8px 24px;">
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;font-weight:bold;
        text-transform:uppercase;letter-spacing:.6px;color:{grey};padding-bottom:6px;">Что изменилось</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{change_block}</table>
  </td></tr>

  <!-- карточка задачи -->
  <tr><td style="padding:8px 24px 4px 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
      style="border:1px solid #E5E7EB;border-radius:8px;"><tr><td style="padding:16px 18px;">
      <div style="font-family:Arial,Helvetica,sans-serif;font-size:19px;font-weight:bold;
          color:{dark};line-height:1.3;">{title}</div>
      {desc_block}
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
        style="margin-top:12px;border-top:1px solid #EEF0F2;">{meta}</table>
    </td></tr></table>
  </td></tr>

  <!-- текущее состояние -->
  <tr><td style="padding:14px 24px 4px 24px;">
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;font-weight:bold;
        text-transform:uppercase;letter-spacing:.6px;color:{grey};padding-bottom:8px;">Текущее состояние</div>
    {cur_badges}
  </td></tr>

  <!-- футер -->
  <tr><td style="padding:18px 24px 22px 24px;">
    <div style="border-top:1px solid #EEF0F2;padding-top:12px;
        font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:1.6;color:{grey};">
      Изменение внесено {now_txt}<br>
      Автоматическое уведомление ТИМ Планера — отвечать не нужно.
    </div>
  </td></tr>

</table>
</td></tr></table>
</body></html>""".format(
        subject_pre=_esc(pre), pre=_esc(pre), dark=_DARK, grey=_GREY,
        epic_badge=_badge(epic_key, epic_color), change_block=change_block,
        title=_esc(title),
        desc_block=('<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
                    'color:%s;line-height:1.5;margin-top:6px;">%s</div>' % (_GREY, _esc(desc)))
                   if desc else "",
        meta=meta, cur_badges=cur_badges, now_txt=_esc(now_txt))


def _build_text(change_lines, epic_key, epic_name, title, desc, stream, assignee,
                actor, sprint_txt, sp, dl, task, cur_status, cur_stage):
    lines = ["ТИМ ПЛАНЕР — уведомление", ""]
    lines.append("[%s] %s" % (epic_key, title))
    lines.append("")
    lines.append("Что изменилось:")
    for c in change_lines:
        lines.append("  " + c)
    lines.append("")
    if desc:
        lines.append(desc)
        lines.append("")
    lines.append("Эпик: %s %s" % (epic_key, epic_name))
    lines.append("Стрим: %s" % stream)
    lines.append("Исполнитель: %s" % assignee)
    lines.append("Изменение внёс: %s" % (actor or "—"))
    lines.append("Спринт: %s" % sprint_txt)
    lines.append("Оценка: %d SP" % sp)
    if dl and _is_overdue(task):
        lines.append("Дедлайн: просрочен %s" % _fmt_ddmm(dl))
    elif dl:
        lines.append("Дедлайн: %s" % _fmt_ddmm(dl))
    else:
        lines.append("Дедлайн: —")
    lines.append("")
    lines.append("Текущее состояние: %s / %s" % (STATUS_LABELS.get(cur_status, cur_status),
                                                 STAGE_LABELS.get(cur_stage, cur_stage)))
    lines.append("")
    lines.append("Изменение внесено %s" % _fmt_msk())
    lines.append("Автоматическое уведомление ТИМ Планера — отвечать не нужно.")
    return "\n".join(lines)


# ===========================================================================
#  Отправка (фон)
# ===========================================================================
def _digest_change(changes, actor, people, epics):
    """Свести много изменений в одно письмо-дайджест."""
    people_idx = _people_index(people)
    epics_idx = _epics_index(epics)
    rows = []
    for ch in changes:
        t = ch.get("task") or {}
        epic = epics_idx.get(t.get("epicId")) or {}
        key = epic.get("key") or "—"
        title = _clip(t.get("title") or "(без названия)", 60)
        os_, ost = ch.get("old_status"), ch.get("old_stage")
        deltas = []
        if os_ is not None and os_ != t.get("status"):
            deltas.append("статус → " + STATUS_LABELS.get(t.get("status"), t.get("status") or ""))
        if ost is not None and ost != t.get("stage"):
            deltas.append("этап → " + STAGE_LABELS.get(t.get("stage"), t.get("stage") or ""))
        rows.append((key, title, ", ".join(deltas) or "изменение"))

    subject = "ТИМ Планер · %d изменений задач" % len(changes)
    pre = "Сводка: %d изменений в задачах" % len(changes)
    body_rows = "".join(
        '<tr><td style="padding:8px 0;border-bottom:1px solid #EEF0F2;'
        'font-family:Arial,Helvetica,sans-serif;font-size:13px;color:%s;">'
        '<b>[%s]</b> %s<br><span style="color:%s;font-size:12px;">%s</span></td></tr>'
        % (_DARK, _esc(k), _esc(tt), _GREY, _esc(d)) for k, tt, d in rows)
    html = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>{pre}</title></head>
<body style="margin:0;padding:0;background:#F4F5F7;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{pre}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#F4F5F7" style="background:#F4F5F7;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background:#FFFFFF;border-radius:8px;overflow:hidden;">
  <tr><td bgcolor="{dark}" style="background:{dark};padding:16px 24px;font-family:Arial,Helvetica,sans-serif;
      font-size:15px;font-weight:bold;letter-spacing:2px;color:#FFFFFF;">ТИМ&nbsp;ПЛАНЕР</td></tr>
  <tr><td style="padding:20px 24px 6px 24px;font-family:Arial,Helvetica,sans-serif;font-size:14px;
      font-weight:bold;color:{dark};">Изменено задач: {n}</td></tr>
  <tr><td style="padding:0 24px 12px 24px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table></td></tr>
  <tr><td style="padding:8px 24px 22px 24px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:{grey};">
      Изменения внесены {now}. Автоматическое уведомление ТИМ Планера — отвечать не нужно.</td></tr>
</table></td></tr></table></body></html>""".format(
        pre=_esc(pre), dark=_DARK, grey=_GREY, n=len(changes), rows=body_rows, now=_esc(_fmt_msk()))
    text = "ТИМ ПЛАНЕР — сводка (%d изменений)\n\n" % len(changes) + \
           "\n".join("[%s] %s — %s" % (k, tt, d) for k, tt, d in rows) + \
           "\n\nИзменения внесены %s" % _fmt_msk()
    return subject, html, text


def _hour_gate(n):
    """Пропустить не более _MAX_PER_HOUR писем в скользящее часовое окно.
    Возвращает сколько писем ещё можно отправить (0..n)."""
    now = time.time()
    with _sent_lock:
        cutoff = now - 3600
        _sent_times[:] = [t for t in _sent_times if t > cutoff]
        allowed = max(0, _MAX_PER_HOUR - len(_sent_times))
        take = min(n, allowed)
        _sent_times.extend([now] * take)
        return take


def _send_one(subject, html, text, host, port, user, password):
    """Собрать multipart/alternative и отправить. Пароль только тут, локально."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = formataddr((str(Header("ТИМ Планер", "utf-8")), user))
    msg["To"] = NOTIFY_TO
    msg["Date"] = formatdate(localtime=True)
    try:
        msg["Message-ID"] = make_msgid(domain=(user.split("@")[-1] or "planner"))
    except Exception:
        pass
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "All"
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as s:
            s.login(user, password)
            s.sendmail(user, [NOTIFY_TO], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(user, password)
            s.sendmail(user, [NOTIFY_TO], msg.as_string())


def _worker(changes, actor, people, epics):
    try:
        host, port, user, password = _load_smtp()
        if not (user and password):
            log.warning("notify: SMTP creds missing (user/pass empty) — skip %d change(s)", len(changes))
            return

        # антишторм: >5 на вызов → одно письмо-дайджест
        if len(changes) > _MAX_PER_CALL:
            take = _hour_gate(1)
            if take < 1:
                log.warning("notify: hourly cap reached — digest of %d changes dropped", len(changes))
                return
            subject, html, text = _digest_change(changes, actor, people, epics)
            try:
                _send_one(subject, html, text, host, port, user, password)
            except Exception as e:
                log.warning("notify: digest send failed: %s", type(e).__name__)
            return

        allowed = _hour_gate(len(changes))
        if allowed < len(changes):
            log.warning("notify: hourly cap — sending %d of %d", allowed, len(changes))
        for ch in changes[:allowed]:
            try:
                subject, html, text = render_email(ch, actor, people, epics)
                _send_one(subject, html, text, host, port, user, password)
            except Exception as e:
                # никаких секретов/трейсбека с кредами — только тип ошибки
                log.warning("notify: send failed for one change: %s", type(e).__name__)
    except Exception as e:
        log.warning("notify worker error: %s", type(e).__name__)


def notify_task_changes(changes, actor, people, epics):
    """Точка входа из planner_api. Никогда не бросает исключений в вызывающий код.
    Запускает фоновый daemon-поток и немедленно возвращает управление."""
    try:
        if not changes:
            return
        th = threading.Thread(target=_worker, args=(list(changes), actor, people, epics),
                              daemon=True)
        th.start()
    except Exception as e:
        log.warning("notify: failed to start thread: %s", type(e).__name__)
