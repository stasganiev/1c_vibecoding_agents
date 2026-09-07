#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сколько токенов стоит стартовый контекст сессии — во всех его слоях.

    python token_budget.py              # оценка без API
    python token_budget.py --api        # точно, через messages.count_tokens
    python token_budget.py --root PATH  # бюджет другого репозитория
    python token_budget.py --top 15     # длиннее список тяжёлых описаний

Зачем. Часть контекста грузится в КАЖДЫЙ запрос независимо от того, нужна она
или нет: инструкции проекта и описания всех доступных скиллов, агентов и
команд. Это плата, которую платишь всегда — за каждое «привет» тоже. Скрипт
показывает, из чего она складывается и что даст наибольшую экономию.

Слои, которые учитываются:
  1. Инструкции репозитория   — CLAUDE.md и то, что он подключает.
  2. Инструкция проекта       — если проекты лежат в подпапках.
  3. Скиллы репозитория       — .claude/skills/
  4. Скиллы пользователя      — ~/.claude/skills/, видны во ВСЕХ проектах.
  5. Скиллы плагинов          — ~/.claude/plugins/
  6. Агенты и команды         — их описания тоже едут в контексте.

Работает в любом репозитории без настройки.

Для --api нужен доступ к Claude API (ANTHROPIC_API_KEY или `ant auth login`)
и пакет anthropic. Без него — оценка по символам: пропорции верны, абсолют
приблизителен.
"""
import glob
import io
import json
import os
import re
import sys

USE_API = "--api" in sys.argv

ROOT = None
if "--root" in sys.argv:
    i = sys.argv.index("--root")
    if i + 1 < len(sys.argv):
        ROOT = os.path.abspath(sys.argv[i + 1])

TOP = 10
if "--top" in sys.argv:
    i = sys.argv.index("--top")
    if i + 1 < len(sys.argv):
        try:
            TOP = int(sys.argv[i + 1])
        except ValueError:
            pass

MODEL = "claude-opus-5"
HOME_CLAUDE = os.path.join(os.path.expanduser("~"), ".claude")


def find_root(start):
    cur = os.path.abspath(start)
    while True:
        for marker in (".git", ".claude"):
            if os.path.exists(os.path.join(cur, marker)):
                return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.abspath(start)
        cur = parent


if ROOT is None:
    ROOT = find_root(os.getcwd())


def read(path):
    try:
        return io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


# --- подсчёт токенов --------------------------------------------------------

# Кириллица токенизируется примерно втрое хуже латиницы: ~2 символа на токен
# против ~4. Берём взвешенное среднее по доле кириллицы в тексте — грубее
# API, но заметно точнее единого коэффициента на все языки.
CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")


def estimate(text):
    if not text:
        return 0
    cyr = len(CYRILLIC.findall(text))
    share = cyr / float(len(text))
    chars_per_token = 2.2 * share + 3.8 * (1 - share)
    return int(len(text) / chars_per_token)


class Counter(object):
    """Считает токены: точно через API, если он доступен, иначе оценкой."""

    def __init__(self, use_api):
        self.client = None
        self.reason = ""
        if not use_api:
            return
        try:
            import anthropic
        except ImportError:
            self.reason = "нет пакета anthropic (pip install anthropic)"
            return
        try:
            client = anthropic.Anthropic()
            client.messages.count_tokens(
                model=MODEL, messages=[{"role": "user", "content": "ping"}])
            self.client = client
        except Exception as e:
            self.reason = "API недоступен: %s" % str(e)[:120]

    def __call__(self, text):
        if not text:
            return 0
        if self.client is None:
            return estimate(text)
        try:
            r = self.client.messages.count_tokens(
                model=MODEL, messages=[{"role": "user", "content": text}])
            return r.input_tokens
        except Exception:
            return estimate(text)

    @property
    def mode(self):
        return "точно, через count_tokens" if self.client \
            else "оценка по символам (пропорции верны, абсолют приблизителен)"


count = Counter(USE_API)

# --- сбор описаний ----------------------------------------------------------

FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def description_of(path):
    """Описание из фронтматтера — именно оно едет в контексте каждого запроса."""
    m = FM_RE.match(read(path))
    if not m:
        return ""
    desc, collecting = "", False
    for line in m.group(1).splitlines():
        if line.startswith("description:"):
            desc = line[len("description:"):].strip()
            collecting = True
        elif collecting:
            # Многострочные описания: продолжение идёт с отступом.
            if line.startswith((" ", "\t")) and not line.strip().startswith("#"):
                desc += " " + line.strip()
            else:
                break
    return desc.strip().strip("'\"")


def collect(pattern, kind, source):
    """[(имя, описание, вид, источник)] по glob-шаблону."""
    out = []
    for p in sorted(glob.glob(pattern)):
        if os.path.basename(p) == "SKILL.md":
            name = os.path.basename(os.path.dirname(p))
        else:
            name = os.path.basename(p)[:-3]
        desc = description_of(p)
        if desc:
            out.append((name, desc, kind, source))
    return out


def gather():
    """Все слои описаний, которые попадают в контекст."""
    layers = []

    layers.append(("скиллы репозитория", collect(
        os.path.join(ROOT, ".claude", "skills", "*", "SKILL.md"),
        "skill", "repo")))

    layers.append(("скиллы пользователя", collect(
        os.path.join(HOME_CLAUDE, "skills", "*", "SKILL.md"),
        "skill", "user")))

    plugin_skills = []
    for depth in ("*", "*/*", "*/*/*", "*/*/*/*"):
        plugin_skills += collect(
            os.path.join(HOME_CLAUDE, "plugins", depth, "skills", "*", "SKILL.md"),
            "skill", "plugin")
    seen, uniq = set(), []
    for item in plugin_skills:
        if item[0] not in seen:
            seen.add(item[0])
            uniq.append(item)
    layers.append(("скиллы плагинов", uniq))

    agents = collect(os.path.join(ROOT, ".claude", "agents", "*.md"), "agent", "repo")
    agents += collect(os.path.join(HOME_CLAUDE, "agents", "*.md"), "agent", "user")
    layers.append(("агенты", agents))

    cmds = collect(os.path.join(ROOT, ".claude", "commands", "*.md"), "command", "repo")
    cmds += collect(os.path.join(HOME_CLAUDE, "commands", "*.md"), "command", "user")
    layers.append(("команды", cmds))

    return layers


# --- инструкции -------------------------------------------------------------

LINK_RE = re.compile(r"^@([\w./\\-]+\.md)\s*$", re.M)


def instruction_files():
    """CLAUDE.md и файлы, которые он подключает через @ссылки."""
    out = []
    for name in ("CLAUDE.md", "CLAUDE.local.md"):
        p = os.path.join(ROOT, name)
        if os.path.isfile(p):
            out.append(p)
            for link in LINK_RE.findall(read(p)):
                target = os.path.join(ROOT, link.replace("\\", "/"))
                if os.path.isfile(target) and target not in out:
                    out.append(target)
    return out


def project_instructions():
    """Инструкции проектов, если они лежат в подпапках верхнего уровня."""
    found = []
    for entry in sorted(os.listdir(ROOT)):
        base = os.path.join(ROOT, entry)
        if entry.startswith(".") or not os.path.isdir(base):
            continue
        for p in sorted(glob.glob(os.path.join(base, "*", "CLAUDE.local.md"))):
            found.append((os.path.basename(os.path.dirname(p)), p))
    return found


# --- вывод ------------------------------------------------------------------

def bar(value, total, width=28):
    if total <= 0:
        return ""
    filled = int(round(width * value / float(total)))
    return "█" * filled + "·" * (width - filled)


def main():
    if USE_API and count.client is None:
        print("! %s" % count.reason)
        print("  Продолжаю в режиме оценки.\n")

    print("Бюджет стартового контекста — %s" % count.mode)
    print("Репозиторий: %s\n" % ROOT)

    # --- инструкции
    instr_total = 0
    instr_rows = []
    for p in instruction_files():
        n = count(read(p))
        instr_total += n
        instr_rows.append((os.path.relpath(p, ROOT).replace("\\", "/"), n))

    if instr_rows:
        print("В КАЖДОЙ сессии этого репозитория:")
        for name, n in instr_rows:
            print("  %-36s %7d" % (name, n))
        print("  %-36s %7d" % ("итого инструкции", instr_total))
        print()

    # --- проекты
    projects = project_instructions()
    if projects:
        rows = sorted(((count(read(p)), name) for name, p in projects), reverse=True)
        print("Плюс инструкция проекта (топ-5, добавляется к каждой сессии):")
        for n, name in rows[:5]:
            print("  %-36s %7d   (старт: %d)" % (name, n, instr_total + n))
        print()

    # --- описания по слоям
    layers = gather()
    print("Описания — едут в КАЖДОМ запросе, даже если инструмент не вызван:")
    grand = 0
    layer_totals = []
    for title, items in layers:
        if not items:
            continue
        n = count("\n".join(d for _, d, _, _ in items))
        grand += n
        layer_totals.append((title, n, len(items)))

    biggest = max((n for _, n, _ in layer_totals), default=1) or 1
    for title, n, cnt in layer_totals:
        print("  %-22s %7d   %-28s %3d шт." % (title, n, bar(n, biggest), cnt))
    print("  %-22s %7d" % ("ИТОГО описания", grand))
    print()

    # --- итог
    total_start = instr_total + grand
    print("Постоянная плата за запрос: %d токенов" % total_start)
    if grand and total_start:
        print("  из них описания инструментов: %d%%" % round(100.0 * grand / total_start))
    print()

    # --- самые тяжёлые
    everything = []
    for _, items in layers:
        everything.extend(items)
    if everything:
        print("Самые тяжёлые описания (кандидаты на сокращение или вынос):")
        scored = sorted(((count(d), n, k, s) for n, d, k, s in everything),
                        reverse=True)
        for n, name, kind, source in scored[:TOP]:
            print("  %-34s %5d   %s/%s" % (name, n, source, kind))
        print()

    # --- где рычаг
    if layer_totals:
        top_layer = max(layer_totals, key=lambda x: x[1])
        print("Главный рычаг — «%s»: %d токенов в каждом запросе."
              % (top_layer[0], top_layer[1]))
        if "пользовател" in top_layer[0] or "плагин" in top_layer[0]:
            print("Этот слой виден во ВСЕХ проектах сразу, не только в этом.")
            print("Отключается через настройки Claude Code, а не удалением папки репо.")
        else:
            print("Убирается удалением неиспользуемых скиллов из .claude/skills/.")
        print()
        print("Прежде чем резать — посмотри, чем реально пользуешься:")
        print("  python %s"
              % os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "usage_report.py"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
