#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Во что обошлась сессия: токены, стоимость, эффективность, экономия на кеше.

    python session_stats.py                 # текущая сессия (или последняя)
    python session_stats.py --all           # сводка по всем сессиям проекта
    python session_stats.py --session ID    # конкретная сессия
    python session_stats.py --last          # последняя по времени, не текущая
    python session_stats.py --append        # дописать в ~/.claude/sessions_report.md
    python session_stats.py --rebuild       # пересобрать журнал из ВСЕХ транскриптов
    python session_stats.py --period week   # сводка за период: week|month|all|N|YYYY-MM
    python session_stats.py --json          # машинный вывод

--rebuild нужен, когда менялся формат таблицы или когда сессии закрывались
без --append: транскрипты на диске есть всегда, журнал — только с той поры,
как его начали вести.

ЧЕГО ЭТОТ СКРИПТ НЕ ДЕЛАЕТ И ПОЧЕМУ.

Разложить токены по отдельным инструментам невозможно: API возвращает usage
на запрос целиком, а не на вызов инструмента. Когда модель вызывает Bash,
отдельного счётчика для этого вызова не существует — результат просто вливается
в контекст следующего запроса. Любая цифра «Bash съел N токенов» была бы
выдумкой, поэтому её здесь нет.

Что посчитать МОЖНО и что здесь есть:
  - пик контекста      — сколько держалось в памяти на максимуме (множитель цены);
  - выход              — сколько модель сгенерировала (дороже входа в 5 раз за токен);
  - свежий вход        — то, что не попало в кеш;
  - запись/чтение кеша — что кеш стоил и что он спас от полной оплаты;
  - число запросов     — сколько раз ходили в API;
  - реплики и запр/репл — сколько машинной работы стоила одна мысль человека;
  - активное время     — сумма промежутков без пауз: календарная длительность врёт
                         (бывает «1872ч» на сессию, где работы был час);
  - модель             — Opus дороже Sonnet примерно в 5 раз, без этого цифры несравнимы;
  - стоимость в у.е.   — нормировка всего перечисленного в одно число (формула
                         в шапке журнала и в блоке весов ниже);
  - ошибки API и 429   — упор в лимит подписки: самый честный сигнал расхода;
  - вызовы инструментов — сколько раз каждый вызывался (штуки, не токены).

Источник — транскрипт сессии в ~/.claude/projects/<проект>/<session>.jsonl.
Это данные самого Claude Code, а не наша оценка.
"""
import datetime as dt
import glob
import io
import json
import os
import re
import sys

HOME_CLAUDE = os.path.join(os.path.expanduser("~"), ".claude")
REPORT = os.path.join(HOME_CLAUDE, "sessions_report.md")
USAGE_LOG = os.path.join(HOME_CLAUDE, "skill_usage.jsonl")

ALL = "--all" in sys.argv
APPEND = "--append" in sys.argv
AS_JSON = "--json" in sys.argv
REBUILD = "--rebuild" in sys.argv
LAST = "--last" in sys.argv

SESSION = None
if "--session" in sys.argv:
    i = sys.argv.index("--session")
    if i + 1 < len(sys.argv):
        SESSION = sys.argv[i + 1]


def current_session_id():
    """Сессия, из которой скрипт запущен, — по переменной Claude Code.

    Без неё выбиралась просто последняя по времени правки транскрипта, а это
    не одно и то же: параллельные сессии пишут одновременно, и при завершении
    одной в журнал уходила строка по чужой. Переменную ставит сам Claude Code,
    субагенты наследуют её от родителя — то есть работа субагента посчитается
    в родительскую сессию, а не заведёт отдельную.

    Вне Claude Code (ручной запуск из обычного терминала) переменной нет —
    тогда поведение прежнее, последняя по времени.
    """
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    return sid or None


# --- Нормированная стоимость -------------------------------------------------
#
# Токены разных типов стоят по-разному, поэтому складывать их напрямую нельзя.
# Веса — отношение цен внутри одного прайса Anthropic, а не деньги:
#   выход        x5.0   — самая дорогая строка
#   свежий вход  x1.0   — база отсчёта
#   запись кеша  x1.25  — наценка за укладывание в кеш
#   чтение кеша  x0.1   — кеш дешевле входа примерно в 10 раз
#
# Результат умножается на коэффициент модели: Opus дороже Sonnet примерно в 5 раз.
# Единица (1 у.е.) = 1000 токенов свежего входа на Sonnet.
#
# Это НЕ деньги. Подписка Claude Code не тарифицируется по токенам, и точную
# цену запроса отсюда не получить. Смысл величины — сравнивать сессии между
# собой: во сколько раз одна дороже другой и куда уходит вес.

W_OUTPUT = 5.0
W_FRESH = 1.0
W_CACHE_WRITE = 1.25
W_CACHE_READ = 0.1

MODEL_WEIGHT = (
    ("opus", 5.0),
    ("sonnet", 1.0),
    ("fable", 1.0),
    ("haiku", 0.3),
)
DEFAULT_MODEL_WEIGHT = 5.0   # неизвестную модель считаем дорогой, чтобы не занизить


IDLE_GAP_MIN = 15   # пауза длиннее — считаем, что человек ушёл


def active_minutes(stamps):
    """Время реальной работы: календарная длительность врёт.

    Сессия, растянутая на два месяца, показывает «1872ч» — в ней человек
    спал, работал и уезжал. Складываем только промежутки между соседними
    событиями короче IDLE_GAP_MIN: это и есть время за клавиатурой.
    """
    if len(stamps) < 2:
        return 0
    pts = []
    for s in stamps:
        try:
            pts.append(dt.datetime.fromisoformat(s.replace("Z", "+00:00")))
        except (ValueError, TypeError):
            continue
    pts.sort()
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        gap = (b - a).total_seconds() / 60.0
        if 0 < gap <= IDLE_GAP_MIN:
            total += gap
    return int(total)


def fmt_minutes(mins):
    if not mins:
        return "—"
    return "%dч %02dм" % (mins // 60, mins % 60) if mins >= 60 else "%dм" % mins


def model_weight(name):
    n = (name or "").lower()
    for key, w in MODEL_WEIGHT:
        if key in n:
            return w
    return DEFAULT_MODEL_WEIGHT


def short_model(name):
    """claude-opus-5 -> opus-5; claude-haiku-4-5-20251001 -> haiku-4-5."""
    n = (name or "").replace("claude-", "")
    parts = [x for x in n.split("-") if not (x.isdigit() and len(x) >= 6)]
    return "-".join(parts) or "?"


def model_label(counter):
    """`opus-5` или `opus-4-7 +3` — главная модель и сколько ещё было."""
    real = dict((k, v) for k, v in counter.items() if k != "<synthetic>")
    if not real:
        return "—"
    top = max(real, key=lambda k: real[k])
    others = len(real) - 1
    lbl = short_model(top)
    return "%s +%d" % (lbl, others) if others else lbl


SUB_RE = re.compile(r"(?:projects|results)[/\\]([A-Za-z0-9_.-]+)", re.I)


def subproject_names(cwd):
    """Имена подпроектов, реально существующие на диске.

    Подпроекты считаем только там, где есть ОБЕ папки пары (projects/ и results/):
    это признак мультипроекта. В обычном репозитории подпроектов нет, и колонка
    остаётся пустой — прочерк здесь значит «их не бывает», а не «не нашли».
    """
    if not cwd or not os.path.isdir(cwd):
        return None
    names = set()
    roots = 0
    for root in ("projects", "results"):
        d = os.path.join(cwd, root)
        if os.path.isdir(d):
            roots += 1
            try:
                for n in os.listdir(d):
                    if os.path.isdir(os.path.join(d, n)):
                        names.add(n)
            except OSError:
                pass
    return names if roots == 2 else None


def subproject_label(counter):
    """`family_routine +1` — главный подпроект и сколько ещё затронуто."""
    if not counter:
        return "—"
    top, _ = counter.most_common(1)[0]
    others = len(counter) - 1
    return "%s +%d" % (top, others) if others else top


def project_dir_key(path):
    """Имя папки транскриптов для проекта: C:\\AI_dev\\claude -> C--AI-dev-claude."""
    p = os.path.abspath(path)
    p = p.replace(":", "-").replace("\\", "-").replace("/", "-")
    p = p.replace("_", "-")
    return p


def find_transcripts(cwd):
    """Транскрипты текущего проекта, новые первыми."""
    base = os.path.join(HOME_CLAUDE, "projects")
    if not os.path.isdir(base):
        return []

    key = project_dir_key(cwd).lower()
    exact = os.path.join(base, project_dir_key(cwd))
    dirs = []
    if os.path.isdir(exact):
        dirs.append(exact)
    else:
        # Claude Code нормализует имя папки по-разному в разных версиях;
        # сверяем без учёта регистра, чтобы не зависеть от точной формы.
        for d in os.listdir(base):
            if d.lower() == key and os.path.isdir(os.path.join(base, d)):
                dirs.append(os.path.join(base, d))

    files = []
    for d in dirs:
        files.extend(glob.glob(os.path.join(d, "*.jsonl")))
    return sorted(files, key=os.path.getmtime, reverse=True)


def subagent_files(path):
    """Транскрипты субагентов этой сессии: <session>/subagents/*.jsonl рядом с ней."""
    d = os.path.join(os.path.dirname(path), os.path.basename(path)[:-6], "subagents")
    return sorted(glob.glob(os.path.join(d, "*.jsonl"))) if os.path.isdir(d) else []


def merge_subagents(stats, path):
    """Вливает работу субагентов в цифры родительской сессии.

    Субагент — это отдельные запросы к API, и они оплачиваются. Без них расход
    занижается: в замерах доля субагентов доходила до двух третей стоимости
    сессии. Транскрипт субагента лежит в подпапке рядом с родительским, поэтому
    обход верхнего уровня их не видит.

    Что НЕ складывается:
      - активное время — субагент работает параллельно с родителем, сумма дала
        бы время больше календарного;
      - реплики — их подаёт человек, у субагента их нет;
      - пик контекста — это максимум ОДНОГО окна, у субагента оно своё.
    """
    subs = subagent_files(path)
    if not subs:
        stats["subagents"] = 0
        return
    n = 0
    for f in subs:
        s = parse(f, with_subagents=False)
        if not s:
            continue
        n += 1
        stats["requests"] += s["requests"]
        stats["output"] += s["output"]
        stats["fresh_input"] += s["fresh_input"]
        stats["cache_read"] += s["cache_read"]
        stats["cache_write"] += s["cache_write"]
        stats["cost"] += s["cost"]
        stats["api_errors"] += s["api_errors"]
        stats["rate_limits"] += s["rate_limits"]
        for k, v in s["tools"].items():
            stats["tools"][k] = stats["tools"].get(k, 0) + v
        for k, v in s["models"].items():
            stats["models"][k] = stats["models"].get(k, 0) + v
    stats["subagents"] = n


def parse(path, with_subagents=True):
    """Статистика одного транскрипта."""
    stats = {
        "session": os.path.basename(path)[:-6],
        "file": path,
        "requests": 0,
        "peak_context": 0,
        "output": 0,
        "fresh_input": 0,
        "cache_read": 0,
        "cache_write": 0,
        "tools": {},
        "started": None,
        "ended": None,
        "model": None,
        "branch": None,
        "cwd": None,
        "turns": 0,
        "api_errors": 0,
        "rate_limits": 0,
        "aborted": 0,
        "active_minutes": 0,
        "_stamps": [],
        "cost": 0.0,
        "models": {},
        "project": None,
        "subproject": "—",
        "subagents": 0,
        "_sub_hits": None,
    }
    try:
        fh = io.open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue

            ts = rec.get("timestamp")
            if ts:
                if stats["started"] is None or ts < stats["started"]:
                    stats["started"] = ts
                if stats["ended"] is None or ts > stats["ended"]:
                    stats["ended"] = ts
            if rec.get("gitBranch"):
                stats["branch"] = rec["gitBranch"]
            if rec.get("cwd") and not stats["cwd"]:
                stats["cwd"] = rec["cwd"]

            # Упоминания подпроектов ищем и в моих сообщениях, и в аргументах
            # инструментов: путь к файлу в вызове Bash — самый надёжный признак,
            # над чем шла работа.
            msg_any = rec.get("message") or {}
            c_any = msg_any.get("content")
            texts = []
            if isinstance(c_any, str):
                texts.append(c_any)
            elif isinstance(c_any, list):
                for blk in c_any:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "text":
                        texts.append(blk.get("text") or "")
                    elif blk.get("type") == "tool_use":
                        texts.append(json.dumps(blk.get("input") or {}, ensure_ascii=False))
            if texts:
                if stats["_sub_hits"] is None:
                    stats["_sub_hits"] = {}
                for t in texts:
                    for m in SUB_RE.finditer(t):
                        name = m.group(1)
                        stats["_sub_hits"][name] = stats["_sub_hits"].get(name, 0) + 1

            # Настоящая реплика пользователя — не результат инструмента и не служебная.
            # Это знаменатель главной метрики эффективности: сколько машинной
            # работы стоила одна мысль человека.
            if rec.get("type") == "user":
                um = rec.get("message") or {}
                uc = um.get("content")
                is_tool_result = isinstance(uc, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in uc)
                if not is_tool_result and not rec.get("isMeta"):
                    stats["turns"] += 1

            if rec.get("type") != "assistant":
                continue

            if rec.get("isApiErrorMessage"):
                stats["api_errors"] += 1
                q = rec.get("quotaLimits") or {}
                if rec.get("apiErrorStatus") == 429 or q.get("status") == "rejected":
                    stats["rate_limits"] += 1
            if rec.get("isAbortedMidStream"):
                stats["aborted"] += 1
            if ts:
                stats["_stamps"].append(ts)
            msg = rec.get("message") or {}
            if msg.get("model"):
                stats["model"] = msg["model"]

            u = msg.get("usage")
            if u:
                stats["requests"] += 1
                mname = msg.get("model") or "?"
                stats["models"][mname] = stats["models"].get(mname, 0) + 1
                # Стоимость считаем на КАЖДОМ запросе по весу ЕГО модели:
                # в сессии со сменой модели усреднение по доминирующей врало бы.
                mw = model_weight(mname)
                stats["cost"] += mw * (
                    W_OUTPUT * (u.get("output_tokens") or 0)
                    + W_FRESH * (u.get("input_tokens") or 0)
                    + W_CACHE_WRITE * (u.get("cache_creation_input_tokens") or 0)
                    + W_CACHE_READ * (u.get("cache_read_input_tokens") or 0)
                ) / 1000.0
                ctx = ((u.get("input_tokens") or 0)
                       + (u.get("cache_read_input_tokens") or 0)
                       + (u.get("cache_creation_input_tokens") or 0))
                stats["peak_context"] = max(stats["peak_context"], ctx)
                stats["output"] += u.get("output_tokens") or 0
                stats["fresh_input"] += u.get("input_tokens") or 0
                stats["cache_read"] += u.get("cache_read_input_tokens") or 0
                stats["cache_write"] += u.get("cache_creation_input_tokens") or 0

            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        stats["tools"][name] = stats["tools"].get(name, 0) + 1

    stats["active_minutes"] = active_minutes(stats.pop("_stamps", []))
    if with_subagents:
        merge_subagents(stats, path)
    else:
        stats["subagents"] = 0

    cwd = stats["cwd"]
    stats["project"] = os.path.basename(cwd.rstrip('/\\')) if cwd else None

    allowed = subproject_names(cwd)
    hits = stats.pop("_sub_hits", None) or {}
    if allowed:
        import collections as _c
        cnt = _c.Counter({k: v for k, v in hits.items() if k in allowed})
        stats["subproject"] = subproject_label(cnt)
    else:
        stats["subproject"] = "—"

    return stats if stats["requests"] else None


def skills_of_session(session_id):
    """Скиллы и команды этой сессии — из лога хука, если он есть."""
    if not os.path.isfile(USAGE_LOG):
        return {}, {}
    skills, commands = {}, {}
    for line in io.open(USAGE_LOG, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if session_id and rec.get("session") != session_id:
            continue
        name = rec.get("name", "?")
        if rec.get("type") == "skill":
            skills[name] = skills.get(name, 0) + 1
        elif rec.get("type") == "command":
            commands[name] = commands.get(name, 0) + 1
    return skills, commands


def human(n):
    if n >= 1000000:
        return "%.1fM" % (n / 1000000.0)
    if n >= 1000:
        return "%dk" % (n // 1000)
    return str(n)


def duration(stats):
    if not (stats["started"] and stats["ended"]):
        return "?"
    try:
        a = dt.datetime.fromisoformat(stats["started"].replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat(stats["ended"].replace("Z", "+00:00"))
        mins = int((b - a).total_seconds() // 60)
        return "%dч %02dм" % (mins // 60, mins % 60) if mins >= 60 else "%dм" % mins
    except (ValueError, TypeError):
        return "?"


def render(stats, project):
    skills, commands = skills_of_session(stats["session"])
    out = []
    title = "Сессия %s — проект %s" % (stats["session"][:8], stats["project"] or project)
    if stats["subproject"] and stats["subproject"] != "—":
        title += " / %s" % stats["subproject"]
    out.append(title)
    if stats["started"]:
        line = ("  начата        %s, длительность %s, активно %s"
                % (stats["started"][:16].replace("T", " "), duration(stats),
                   fmt_minutes(stats["active_minutes"])))
        if stats["branch"]:
            line += ", ветка %s" % stats["branch"]
        out.append(line)
    out.append("")
    out.append("  пик контекста %8s   держалось в памяти на максимуме" % human(stats["peak_context"]))
    out.append("  выход         %8s   сгенерировано моделью" % human(stats["output"]))
    out.append("  свежий вход   %8s   не попало в кеш" % human(stats["fresh_input"]))
    out.append("  запись в кеш  %8s" % human(stats["cache_write"]))
    out.append("  чтение кеша   %8s   этот объём кеш спас от полной оплаты"
               % human(stats["cache_read"]))
    out.append("  запросов      %8d" % stats["requests"])
    per = (float(stats["requests"]) / stats["turns"]) if stats["turns"] else 0
    out.append("  ваших реплик  %8d   %s"
               % (stats["turns"],
                  ("%.1f запроса на реплику" % per) if per else ""))
    out.append("  модель        %8s" % model_label(stats["models"]))
    out.append("  стоимость     %8s   у.е. (см. формулу в шапке отчёта)"
               % ("%.0f" % stats["cost"]))

    if stats["api_errors"] or stats["aborted"]:
        out.append("")
        warn = []
        if stats["rate_limits"]:
            warn.append("упор в лимит подписки: %d" % stats["rate_limits"])
        other = stats["api_errors"] - stats["rate_limits"]
        if other > 0:
            warn.append("прочих ошибок API: %d" % other)
        if stats["aborted"]:
            warn.append("прервано: %d" % stats["aborted"])
        out.append("  ! " + ", ".join(warn))

    if stats["tools"]:
        out.append("")
        out.append("  Инструменты (число вызовов, НЕ токены — их по вызовам не разложить):")
        for name, n in sorted(stats["tools"].items(), key=lambda x: -x[1]):
            out.append("    %-22s %4d" % (name, n))

    if skills:
        out.append("")
        out.append("  Скиллы:")
        for name, n in sorted(skills.items(), key=lambda x: -x[1]):
            out.append("    %-22s %4d" % (name, n))
    if commands:
        out.append("  Команды:")
        for name, n in sorted(commands.items(), key=lambda x: -x[1]):
            out.append("    %-22s %4d" % (name, n))

    return "\n".join(out)


def report_header():
    """Шапка журнала. Общая для --append и --rebuild."""
    return ("# Отчёт по сессиям\n\n"
              "Пишется скриптом session_stats.py при завершении сессии.\n"
              "Лежит вне репозиториев, в git не попадает.\n\n"
              "**Пик** — сколько токенов держалось в контексте на максимуме.\n"
              "**Выход** — сколько сгенерировала модель, самая дорогая строка.\n"
              "**Кеш прочитан** — объём, который кеш спас от полной оплаты.\n"
              "**Подпроект** — где велась работа; прочерк значит, что в этом\n"
              "репозитории подпроектов нет (нет пары projects/ + results/).\n\n"
              "**У.е. — нормированная стоимость.** Токены разных типов стоят\n"
              "по-разному, складывать их напрямую нельзя. Формула, по запросам:\n\n"
              "```\n"
              "у.е. = SUM по запросам: вес_модели * (\n"
              "           5.00 * выход\n"
              "         + 1.00 * свежий вход\n"
              "         + 1.25 * запись в кеш\n"
              "         + 0.10 * чтение кеша ) / 1000\n\n"
              "вес_модели: opus 5.0 | sonnet 1.0 | fable 1.0 | haiku 0.3\n"
              "```\n\n"
              "1 у.е. = 1000 токенов свежего входа на Sonnet. Это НЕ деньги:\n"
              "подписка не тарифицируется по токенам. Величина нужна, чтобы\n"
              "сравнивать сессии между собой и видеть, куда уходит вес.\n\n"
              "Токены по отдельным инструментам не раскладываются: API отдаёт\n"
              "usage на запрос целиком, а не на вызов.\n\n"
              "| Дата | Проект | Подпроект | Сессия | Модель | Пик | Выход | Кеш | Запр. | Реплик | Запр/репл | Активно | У.е. | Инструменты | Скиллы | !|\n"
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")



def report_row(stats, project, day=None):
    """Одна строка таблицы. Общая для --append и --rebuild, чтобы форматы не разошлись."""
    skills, commands = skills_of_session(stats["session"])
    day = day or (stats["started"] or dt.datetime.now().isoformat())[:10]
    named = ", ".join(sorted(set(list(skills) + ["/" + c for c in commands]))) or "—"
    per_turn = ("%.1f" % (float(stats["requests"]) / stats["turns"])) if stats["turns"] else "—"
    tools_str = ", ".join("%s:%d" % (k, v) for k, v in
                          sorted(stats["tools"].items(), key=lambda x: -x[1])[:4]) or "—"
    flags = []
    if stats["rate_limits"]:
        flags.append("429x%d" % stats["rate_limits"])
    other_err = stats["api_errors"] - stats["rate_limits"]
    if other_err > 0:
        flags.append("err%d" % other_err)
    if stats["aborted"]:
        flags.append("abort%d" % stats["aborted"])
    if stats.get("subagents"):
        flags.append("sub%d" % stats["subagents"])
    flags = " ".join(flags) or "—"
    return "| %s | %s | %s | %s | %s | %s | %s | %s | %d | %d | %s | %s | %s | %s | %s | %s |\n" % (
        day, stats["project"] or project, stats["subproject"], stats["session"][:8],
        model_label(stats["models"]), human(stats["peak_context"]), human(stats["output"]),
        human(stats["cache_read"]), stats["requests"], stats["turns"], per_turn,
        fmt_minutes(stats["active_minutes"]), "%.0f" % stats["cost"], tools_str, named, flags)


def all_sessions():
    """Разбирает ВСЕ транскрипты всех репозиториев. Общий источник для --rebuild и --period.

    Источник намеренно транскрипты, а не готовый журнал: журнал полон только с той
    поры, как его начали вести, и в нём нет сессий, закрытых без --append. Сводка
    за период, собранная по журналу, занижала бы расход ровно там, где сессию
    забыли закрыть. Транскрипты на диске есть всегда.

    Обход только верхнего уровня: подпапки <session>/subagents/ разбирает parse()
    и вливает в родительскую сессию, отдельными строками они не идут.
    """
    base = os.path.join(HOME_CLAUDE, "projects")
    out, skipped = [], 0
    if not os.path.isdir(base):
        return out, skipped
    for d_ in sorted(os.listdir(base)):
        full = os.path.join(base, d_)
        if not os.path.isdir(full):
            continue
        for f in glob.glob(os.path.join(full, "*.jsonl")):
            s = parse(f)
            if s:
                s["_dir"] = d_
                out.append(s)
            else:
                skipped += 1
    out.sort(key=lambda s: ((s["started"] or "")[:10], s["session"]))
    return out, skipped


def rebuild_report():
    """Пересобирает журнал из ВСЕХ транскриптов всех репозиториев.

    Нужен, когда менялся формат таблицы или когда сессии закрывались без
    --append: транскрипты на диске лежат всегда, журнал — только с той поры,
    как его начали вести.
    """
    sessions, skipped = all_sessions()
    rows = [report_row(s, s["project"] or s["_dir"], (s["started"] or "")[:10] or "-")
            for s in sessions]
    io.open(REPORT, "w", encoding="utf-8", newline=chr(10)).write(
        report_header() + "".join(rows))
    return len(rows), skipped


def append_report(stats, project):
    """Дописывает строку в общий отчёт вне git — источник истории по сессиям."""
    day = (stats["started"] or dt.datetime.now().isoformat())[:10]

    header = report_header()
    existing = header
    replaced = False
    if os.path.isfile(REPORT):
        old = io.open(REPORT, encoding="utf-8", errors="replace").read()
        # Формат таблицы менялся: старые строки без колонки «Подпроект» разъезжаются
        # под новой шапкой. Такой журнал не чинится дописыванием — его пересобирают
        # заново из транскриптов (--rebuild), поэтому здесь просто предупреждаем.
        if "| Подпроект |" not in old and "|" in old:
            sys.stderr.write(
                "session_stats.py: журнал в старом формате (без колонки «Подпроект»).\n"
                "  Пересобрать из транскриптов: session_stats.py --rebuild\n")
        # Строку этой сессии перезаписываем, а не пропускаем: если отчёт снимали
        # в середине работы, сохранённые цифры уже устарели, и молча оставить
        # их — значит держать в журнале заведомо неверные данные.
        kept = []
        for line in old.splitlines(True):
            if line.startswith("|") and ("| %s |" % stats["session"][:8]) in line:
                replaced = True
                continue
            kept.append(line)
        existing = "".join(kept)

    io.open(REPORT, "w", encoding="utf-8", newline="\n").write(
        existing + report_row(stats, project, day))
    return "обновлена" if replaced else "записана"


def parse_period(argv):
    """--period week|month|all|N (дней) | YYYY-MM. Возвращает (метка, дата_с, дата_по) или None."""
    if "--period" not in argv:
        return None
    i = argv.index("--period")
    val = argv[i + 1].lower() if i + 1 < len(argv) and not argv[i + 1].startswith("-") else "week"
    today = dt.date.today()
    if val == "week":
        return "неделя", today - dt.timedelta(days=6), today
    if val == "month":
        return "месяц", today - dt.timedelta(days=29), today
    if val == "all":
        return "всё время", dt.date(1970, 1, 1), today
    if val.isdigit():
        n = int(val)
        return "%d дн." % n, today - dt.timedelta(days=n - 1), today
    m = re.match(r"^(\d{4})-(\d{2})$", val)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        first = dt.date(y, mo, 1)
        last = dt.date(y + (mo == 12), (mo % 12) + 1, 1) - dt.timedelta(days=1)
        return val, first, last
    # Отказ, а не молчаливый откат к отчёту по одной сессии: увидев цифры,
    # их приняли бы за сводку за период.
    raise ValueError("не понял период %r; допустимо week|month|all|N|YYYY-MM" % val)


def bar(share, width=20):
    """Полоска доли: глазом видно перекос, которого в столбце цифр не заметно."""
    filled = int(round(share * width))
    return "#" * filled + "." * (width - filled)


def period_report(label, since, until):
    """Сводка за период: куда ушёл расход и где аномалии.

    Смотрим на хвост, а не на среднее: расход концентрируется в единицах
    сессий, и медиана про них ничего не говорит.
    """
    sessions, _ = all_sessions()
    rows = []
    for s in sessions:
        day = (s["started"] or "")[:10]
        if not day:
            continue
        try:
            d = dt.date(int(day[:4]), int(day[5:7]), int(day[8:10]))
        except ValueError:
            continue
        if since <= d <= until:
            rows.append((day, s))

    out = []
    title = "Сводка за период: %s (%s .. %s)" % (label, since.isoformat(), until.isoformat())
    out.append(title)
    out.append("=" * len(title))
    if not rows:
        out.append("")
        out.append("  Сессий за этот период нет.")
        return "\n".join(out)

    total = sum(s["cost"] for _, s in rows)
    out.append("")
    out.append("  Сессий           %d" % len(rows))
    out.append("  Стоимость        %s у.е." % ("%.0f" % total))
    out.append("  Средняя сессия   %s у.е." % ("%.0f" % (total / len(rows))))
    out.append("  Активное время   %s" % fmt_minutes(sum(s["active_minutes"] for _, s in rows)))
    out.append("  Запросов         %d" % sum(s["requests"] for _, s in rows))
    out.append("  Реплик           %d" % sum(s["turns"] for _, s in rows))

    # Куда уходит вес: доля по типам токенов. Ответ обычно неожиданный —
    # в длинных сессиях большую часть даёт чтение кеша, а не выход модели.
    parts = [
        ("выход", W_OUTPUT * sum(s["output"] for _, s in rows)),
        ("свежий вход", W_FRESH * sum(s["fresh_input"] for _, s in rows)),
        ("запись кеша", W_CACHE_WRITE * sum(s["cache_write"] for _, s in rows)),
        ("чтение кеша", W_CACHE_READ * sum(s["cache_read"] for _, s in rows)),
    ]
    base = sum(v for _, v in parts) or 1
    out.append("")
    out.append("  Из чего сложилась стоимость (без веса модели):")
    for name, v in sorted(parts, key=lambda x: -x[1]):
        out.append("    %-14s %s %5.1f%%" % (name, bar(v / base), 100.0 * v / base))

    # По проектам: сколько стоил каждый и сколько сессий на него ушло.
    by_proj = {}
    for _, s in rows:
        key = s["project"] or s["_dir"]
        cur = by_proj.setdefault(key, {"cost": 0.0, "n": 0, "mins": 0, "subs": {}})
        cur["cost"] += s["cost"]
        cur["n"] += 1
        cur["mins"] += s["active_minutes"]
        if s["subproject"] and s["subproject"] != "—":
            top = s["subproject"].split(" +")[0]
            cur["subs"][top] = cur["subs"].get(top, 0) + 1
    out.append("")
    out.append("  По проектам:")
    out.append("    %-24s %8s %6s %5s  %s" % ("проект", "у.е.", "сес.", "доля", "подпроекты"))
    for name in sorted(by_proj, key=lambda k: -by_proj[k]["cost"]):
        v = by_proj[name]
        subs = ", ".join(k for k, _ in sorted(v["subs"].items(), key=lambda x: -x[1])[:3]) or "—"
        out.append("    %-24s %8s %6d %4.0f%%  %s"
                   % (name[:24], "%.0f" % v["cost"], v["n"], 100.0 * v["cost"] / (total or 1), subs))

    # Хвост: расход концентрируется в единицах сессий, среднее его прячет.
    top = sorted(rows, key=lambda r: -r[1]["cost"])[:5]
    out.append("")
    out.append("  Самые дорогие сессии:")
    for day, s in top:
        out.append("    %s  %-8s %-20s %8s у.е.  %4.0f%%  %s"
                   % (day, s["session"][:8], (s["subproject"] or "—")[:20],
                      "%.0f" % s["cost"], 100.0 * s["cost"] / (total or 1),
                      fmt_minutes(s["active_minutes"])))
    half = 0.0
    n_half = 0
    for _, s in sorted(rows, key=lambda r: -r[1]["cost"]):
        half += s["cost"]
        n_half += 1
        if half >= total / 2:
            break
    out.append("    -> половину расхода дают %d сессий из %d" % (n_half, len(rows)))

    # Аномалии: сессии дороже среднего втрое. Это и есть то, что стоит разобрать.
    avg = total / len(rows)
    odd = [(d, s) for d, s in rows if s["cost"] > 3 * avg]
    if odd:
        out.append("")
        out.append("  Аномально дорогие (>3x среднего):")
        for day, s in sorted(odd, key=lambda r: -r[1]["cost"]):
            out.append("    %s  %-8s %8s у.е. — в %.1f раза выше средней"
                       % (day, s["session"][:8], "%.0f" % s["cost"], s["cost"] / avg))

    # Упор в лимит подписки — самый честный сигнал расхода.
    limits = sum(s["rate_limits"] for _, s in rows)
    errs = sum(s["api_errors"] - s["rate_limits"] for _, s in rows)
    if limits or errs:
        out.append("")
        out.append("  Упор в лимит (429): %d;  прочие ошибки API: %d" % (limits, errs))

    return "\n".join(out)


def main():
    cwd = os.getcwd()
    project = os.path.basename(os.path.abspath(cwd))

    period = parse_period(sys.argv)
    if period:
        print(period_report(*period))
        return 0

    if REBUILD:
        n, skipped = rebuild_report()
        print("Журнал пересобран из транскриптов: %d сессий" % n)
        if skipped:
            print("  пропущено без данных об использовании: %d" % skipped)
        print("  %s" % REPORT)
        return 0

    files = find_transcripts(cwd)

    if not files:
        print("Транскриптов для проекта %s не найдено." % project)
        print("Искал в %s" % os.path.join(HOME_CLAUDE, "projects"))
        return 0

    if SESSION:
        files = [f for f in files if SESSION in os.path.basename(f)]
        if not files:
            print("Сессия %s не найдена." % SESSION)
            return 1
    elif not ALL and not LAST:
        # По умолчанию считаем ТЕКУЩУЮ сессию, а не последнюю по времени.
        cur = current_session_id()
        if cur:
            mine = [f for f in files if cur in os.path.basename(f)]
            if mine:
                files = mine
            else:
                # Транскрипта текущей сессии в этом проекте нет (запуск из
                # другого репозитория, облачная сессия Cowork). Молча взять
                # чужую строку хуже, чем сказать вслух: именно так в журнал
                # и попадает чужая сессия.
                print("Транскрипт текущей сессии (%s) в проекте %s не найден.\n"
                      "Беру последнюю по времени. Если нужна именно она — "
                      "это ожидаемо, иначе укажите --session ID.\n"
                      % (cur[:8], project))

    if ALL:
        rows = [s for s in (parse(f) for f in files) if s]
        if AS_JSON:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        print("Все сессии проекта %s: %d\n" % (project, len(rows)))
        print("  %-9s %-18s %-10s %8s %8s %9s %6s %9s"
              % ("сессия", "подпроект", "модель", "пик", "выход", "кеш", "запр.", "у.е."))
        for s in rows:
            print("  %-9s %-18s %-10s %8s %8s %9s %6d %9s"
                  % (s["session"][:8], (s["subproject"] or "—")[:18],
                     model_label(s["models"])[:10],
                     human(s["peak_context"]), human(s["output"]),
                     human(s["cache_read"]), s["requests"], "%.0f" % s["cost"]))
        tot_out = sum(s["output"] for s in rows)
        tot_cache = sum(s["cache_read"] for s in rows)
        print("\n  Суммарно выход %s, прочитано из кеша %s, стоимость %.0f у.е."
              % (human(tot_out), human(tot_cache), sum(s["cost"] for s in rows)))
        return 0

    stats = parse(files[0])
    if not stats:
        print("В транскрипте нет данных об использовании.")
        return 0

    if AS_JSON:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    print(render(stats, project))

    if APPEND:
        print()
        what = append_report(stats, project)
        print("Строка %s: %s" % (what, REPORT))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as e:
        # Ошибка в аргументах — вина вызывающего, о ней надо знать: код 2.
        sys.stderr.write("session_stats.py: %s\n" % e)
        sys.exit(2)
    except Exception as e:
        # Свой сбой не должен ронять ритуал завершения сессии, из которого
        # скрипт вызывается: жалуемся в stderr и выходим успешно.
        sys.stderr.write("session_stats.py: %s\n" % e)
        sys.exit(0)
