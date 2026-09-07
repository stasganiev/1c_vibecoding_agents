#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Во что обошлась сессия: токены, вызовы инструментов, экономия на кеше.

    python session_stats.py                 # последняя сессия этого проекта
    python session_stats.py --all           # сводка по всем сессиям
    python session_stats.py --session ID    # конкретная сессия
    python session_stats.py --append        # дописать в ~/.claude/sessions_report.md
    python session_stats.py --json          # машинный вывод

ЧЕГО ЭТОТ СКРИПТ НЕ ДЕЛАЕТ И ПОЧЕМУ.

Разложить токены по отдельным инструментам невозможно: API возвращает usage
на запрос целиком, а не на вызов инструмента. Когда модель вызывает Bash,
отдельного счётчика для этого вызова не существует — результат просто вливается
в контекст следующего запроса. Любая цифра «Bash съел N токенов» была бы
выдумкой, поэтому её здесь нет.

Что посчитать МОЖНО и что здесь есть:
  - пик контекста      — сколько токенов держалось в памяти на максимуме;
  - выход              — сколько модель сгенерировала (это самое дорогое);
  - свежий вход        — то, что не попало в кеш;
  - чтение из кеша     — объём, который кеш спас от полной оплаты;
  - число запросов     — сколько раз ходили в API;
  - вызовы инструментов — сколько раз каждый вызывался (штуки, не токены).

Источник — транскрипт сессии в ~/.claude/projects/<проект>/<session>.jsonl.
Это данные самого Claude Code, а не наша оценка.
"""
import datetime as dt
import glob
import io
import json
import os
import sys

HOME_CLAUDE = os.path.join(os.path.expanduser("~"), ".claude")
REPORT = os.path.join(HOME_CLAUDE, "sessions_report.md")
USAGE_LOG = os.path.join(HOME_CLAUDE, "skill_usage.jsonl")

ALL = "--all" in sys.argv
APPEND = "--append" in sys.argv
AS_JSON = "--json" in sys.argv

SESSION = None
if "--session" in sys.argv:
    i = sys.argv.index("--session")
    if i + 1 < len(sys.argv):
        SESSION = sys.argv[i + 1]


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


def parse(path):
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

            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            if msg.get("model"):
                stats["model"] = msg["model"]

            u = msg.get("usage")
            if u:
                stats["requests"] += 1
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
    out.append("Сессия %s — проект %s" % (stats["session"][:8], project))
    if stats["started"]:
        out.append("  начата        %s, длительность %s"
                   % (stats["started"][:16].replace("T", " "), duration(stats)))
    out.append("")
    out.append("  пик контекста %8s   держалось в памяти на максимуме" % human(stats["peak_context"]))
    out.append("  выход         %8s   сгенерировано моделью" % human(stats["output"]))
    out.append("  свежий вход   %8s   не попало в кеш" % human(stats["fresh_input"]))
    out.append("  запись в кеш  %8s" % human(stats["cache_write"]))
    out.append("  чтение кеша   %8s   этот объём кеш спас от полной оплаты"
               % human(stats["cache_read"]))
    out.append("  запросов      %8d" % stats["requests"])

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


def append_report(stats, project):
    """Дописывает строку в общий отчёт вне git — источник истории по сессиям."""
    skills, commands = skills_of_session(stats["session"])
    day = (stats["started"] or dt.datetime.now().isoformat())[:10]

    header = ("# Отчёт по сессиям\n\n"
              "Пишется автоматически скриптом session_stats.py при завершении сессии.\n"
              "Лежит вне репозиториев, в git не попадает.\n\n"
              "Пик — сколько токенов держалось в контексте на максимуме. Выход —\n"
              "сколько сгенерировала модель. Чтение кеша — объём, который кеш спас\n"
              "от полной оплаты. Токены по отдельным инструментам не раскладываются:\n"
              "API отдаёт usage на запрос целиком, а не на вызов.\n\n"
              "| Дата | Проект | Сессия | Пик | Выход | Кеш прочитан | Запросов | Скиллы |\n"
              "|---|---|---|---|---|---|---|---|\n")

    existing = header
    replaced = False
    if os.path.isfile(REPORT):
        old = io.open(REPORT, encoding="utf-8", errors="replace").read()
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

    named = ", ".join(sorted(set(list(skills) + ["/" + c for c in commands]))) or "—"
    row = "| %s | %s | %s | %s | %s | %s | %d | %s |\n" % (
        day, project, stats["session"][:8], human(stats["peak_context"]),
        human(stats["output"]), human(stats["cache_read"]), stats["requests"], named)

    io.open(REPORT, "w", encoding="utf-8", newline="\n").write(existing + row)
    return "обновлена" if replaced else "записана"


def main():
    cwd = os.getcwd()
    project = os.path.basename(os.path.abspath(cwd))
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

    if ALL:
        rows = [s for s in (parse(f) for f in files) if s]
        if AS_JSON:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        print("Все сессии проекта %s: %d\n" % (project, len(rows)))
        print("  %-10s %8s %8s %9s %6s" % ("сессия", "пик", "выход", "кеш", "запр."))
        for s in rows:
            print("  %-10s %8s %8s %9s %6d"
                  % (s["session"][:8], human(s["peak_context"]), human(s["output"]),
                     human(s["cache_read"]), s["requests"]))
        tot_out = sum(s["output"] for s in rows)
        tot_cache = sum(s["cache_read"] for s in rows)
        print("\n  Суммарно выход %s, прочитано из кеша %s"
              % (human(tot_out), human(tot_cache)))
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
    except Exception as e:
        sys.stderr.write("session_stats.py: %s\n" % e)
        sys.exit(0)
