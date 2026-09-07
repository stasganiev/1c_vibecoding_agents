#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Чем ты реально пользуешься: отчёт по логу вызовов скиллов и команд.

    python usage_report.py                 # всё, что накопилось
    python usage_report.py --days 30       # только за последние 30 дней
    python usage_report.py --project claude # только один проект
    python usage_report.py --unused        # что установлено, но ни разу не вызвано

Лог пишет хук ~/.claude/hooks/skill-usage-logger.js, подключённый в
~/.claude/settings.json. Он ловит вызовы во ВСЕХ проектах сразу, поэтому
отчёт отвечает на вопрос «чем я пользуюсь», а не «что я вызывал здесь».

Смотреть отчёт имеет смысл через пару недель наблюдения: за день картина
не набирается. Пустой раздел «не вызывался ни разу» на второй день ничего
не доказывает — просто данных ещё нет.
"""
import datetime as dt
import glob
import io
import json
import os
import sys

HOME_CLAUDE = os.path.join(os.path.expanduser("~"), ".claude")
LOG = os.path.join(HOME_CLAUDE, "skill_usage.jsonl")

DAYS = None
if "--days" in sys.argv:
    i = sys.argv.index("--days")
    if i + 1 < len(sys.argv):
        try:
            DAYS = int(sys.argv[i + 1])
        except ValueError:
            pass

PROJECT = None
if "--project" in sys.argv:
    i = sys.argv.index("--project")
    if i + 1 < len(sys.argv):
        PROJECT = sys.argv[i + 1]

SHOW_UNUSED = "--unused" in sys.argv


def load():
    if not os.path.isfile(LOG):
        return []
    cutoff = None
    if DAYS:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)
    rows = []
    for line in io.open(LOG, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if PROJECT and rec.get("project") != PROJECT:
            continue
        if cutoff:
            try:
                ts = dt.datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if ts < cutoff:
                continue
        rows.append(rec)
    return rows


def installed_skills():
    """Все скиллы, видимые в сессии: репозиторий, пользователь, плагины."""
    found = {}
    here = os.getcwd()
    patterns = [
        (os.path.join(here, ".claude", "skills", "*", "SKILL.md"), "repo"),
        (os.path.join(HOME_CLAUDE, "skills", "*", "SKILL.md"), "user"),
    ]
    for depth in ("*", "*/*", "*/*/*", "*/*/*/*"):
        patterns.append(
            (os.path.join(HOME_CLAUDE, "plugins", depth, "skills", "*", "SKILL.md"),
             "plugin"))
    for pattern, source in patterns:
        for p in glob.glob(pattern):
            name = os.path.basename(os.path.dirname(p))
            found.setdefault(name, source)
    return found


def table(title, counts, extra=None):
    if not counts:
        return
    print(title)
    width = max(len(k) for k in counts) if counts else 10
    width = min(max(width, 12), 40)
    for name, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        tail = ""
        if extra and name in extra:
            tail = "   %s" % extra[name]
        print("  %-*s %4d%s" % (width, name, n, tail))
    print()


def main():
    rows = load()
    if not rows:
        print("Лог пуст: %s" % LOG)
        print()
        print("Если хук только что подключён — это нормально, данные появятся")
        print("по мере работы. Если он подключён давно, проверь ~/.claude/settings.json:")
        print("нужен раздел hooks с событиями PostToolUse (matcher Skill),")
        print("UserPromptSubmit, SubagentStop и Stop.")
        return 0

    span = ""
    stamps = sorted(r["ts"] for r in rows if "ts" in r)
    if stamps:
        span = " с %s по %s" % (stamps[0][:10], stamps[-1][:10])
    scope = " проект %s," % PROJECT if PROJECT else ""
    print("Использование инструментов%s%s — %d событий\n"
          % (scope, span, len(rows)))

    skills, commands, subagents, projects = {}, {}, {}, {}
    errors = {}
    sessions = []
    for r in rows:
        t, name = r.get("type"), r.get("name", "?")
        proj = r.get("project", "?")
        if t == "skill":
            skills[name] = skills.get(name, 0) + 1
            projects[proj] = projects.get(proj, 0) + 1
            if r.get("status") == "error":
                errors[name] = errors.get(name, 0) + 1
        elif t == "command":
            commands[name] = commands.get(name, 0) + 1
            projects[proj] = projects.get(proj, 0) + 1
        elif t == "subagent":
            subagents[name] = subagents.get(name, 0) + 1
        elif t == "session":
            sessions.append(r)

    err_note = {k: "(ошибок: %d)" % v for k, v in errors.items()}
    table("Скиллы:", skills, err_note)
    table("Команды:", commands)
    table("Субагенты:", subagents)
    table("По проектам:", projects)

    if sessions:
        peaks = [s.get("peak_context_tokens", 0) for s in sessions]
        outs = [s.get("output_tokens", 0) for s in sessions]
        print("Сессии: %d" % len(sessions))
        print("  пик контекста, среднее   %7d ток." % (sum(peaks) // len(peaks)))
        print("  пик контекста, максимум  %7d ток." % max(peaks))
        print("  выход, суммарно          %7d ток." % sum(outs))
        print()

    if SHOW_UNUSED:
        used = set(skills)
        inst = installed_skills()
        unused = {k: v for k, v in inst.items() if k not in used}
        print("Установлено скиллов: %d, из них вызывались: %d"
              % (len(inst), len(used & set(inst))))
        if unused:
            print("\nНи разу не вызывались (%d):" % len(unused))
            by_source = {}
            for name, src in sorted(unused.items()):
                by_source.setdefault(src, []).append(name)
            for src in sorted(by_source):
                print("  [%s]" % src)
                for name in by_source[src]:
                    print("      %s" % name)
            print()
            print("Осторожно: это список НЕ вызванных за период наблюдения, а не")
            print("список бесполезных. Пара недель данных — минимум, ниже которого")
            print("вывод о ненужности делать рано.")
    else:
        print("Что установлено, но ни разу не вызывалось: --unused")

    return 0


if __name__ == "__main__":
    sys.exit(main())
