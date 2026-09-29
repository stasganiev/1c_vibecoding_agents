#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сколько токенов стоит стартовый контекст сессии — во всех его слоях.

    python token_budget.py              # оценка без API
    python token_budget.py --api        # точно, через messages.count_tokens
    python token_budget.py --root PATH  # бюджет другого репозитория
    python token_budget.py --top 15     # длиннее список тяжёлых описаний
    python token_budget.py --window 200000  # окно модели (по умолчанию 1M)
    python token_budget.py --budget-chars 25000  # бюджет списка вручную (по /context)
    python token_budget.py --quiet      # строка, только если свои скиллы теряют описание

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

Список скиллов Claude Code режет сам. Весь список ограничен долей окна
контекста (skillListingBudgetFraction, по умолчанию 1%, в символах; переменная
SLASH_COMMAND_TOOL_CHAR_BUDGET задаёт число напрямую), каждое описание вместе
с when_to_use обрезается до skillListingMaxDescChars (1536). skillOverrides
и disable-model-invocation убирают скилл или его описание из списка. При
переполнении описания теряют скиллы, которые вызываются реже всего: имя
остаётся, но по смыслу запроса такой скилл почти не срабатывает. Скрипт читает
эти настройки и предсказывает, кто останется без описания. Порядок среди
скиллов с равным числом вызовов не документирован — такие показываются группой.
Плагинов skillOverrides не касается.

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
QUIET = "--quiet" in sys.argv

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

# Окно контекста текущих моделей (Opus 4.7+, Sonnet 5, Fable) — 1M. Для моделей
# с окном 200K бюджет списка скиллов впятеро меньше.
WINDOW = 1000000
if "--window" in sys.argv:
    i = sys.argv.index("--window")
    if i + 1 < len(sys.argv):
        try:
            WINDOW = int(sys.argv[i + 1])
        except ValueError:
            pass

MODEL = "claude-opus-5"
HOME_CLAUDE = os.path.join(os.path.expanduser("~"), ".claude")
USAGE_LOG = os.path.join(HOME_CLAUDE, "skill_usage.jsonl")

# Значения Claude Code по умолчанию (docs: skills, settings-reference).
DEFAULT_FRACTION = 0.01
DEFAULT_MAX_DESC = 1536
HIDDEN_STATES = ("off", "user-invocable-only")

# Документация называет бюджет «символьным, 1% окна». Буквально на окне 1M это
# 10 000 символов, но 27.09.2026 в сессии Opus 5.5 модель получила ~29 000
# символов описаний, и список при этом ещё переполнялся. Сходится, если 1% окна
# считается в токенах (~3 символа на токен). Калибровка по одному замеру:
# сверяй с /context и поправляй --budget-chars, если разойдётся.
CHARS_PER_BUDGET_UNIT = 3

# Встроенные скиллы Claude Code (claude-api, dataviz, code-review, loop, ...)
# на диске не лежат, но место в том же списке занимают: ~7 000 символов
# по списку той же сессии.
BUILTIN_RESERVE = 7000

BUDGET_CHARS = 0
if "--budget-chars" in sys.argv:
    i = sys.argv.index("--budget-chars")
    if i + 1 < len(sys.argv):
        try:
            BUDGET_CHARS = int(sys.argv[i + 1])
        except ValueError:
            pass


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


def frontmatter(path):
    """Поля фронтматтера верхнего уровня; многострочные значения склеиваются."""
    m = FM_RE.match(read(path))
    if not m:
        return {}
    fields, key = {}, None
    for line in m.group(1).splitlines():
        top = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if top:
            key = top.group(1)
            fields[key] = top.group(2).strip()
        elif key and line.startswith((" ", "\t")) and not line.strip().startswith("#"):
            # Многострочные значения: продолжение идёт с отступом.
            fields[key] += " " + line.strip()
        else:
            key = None
    return {k: v.strip().strip("'\"") for k, v in fields.items()}


def description_of(path):
    """Описание из фронтматтера — именно оно едет в контексте каждого запроса."""
    return frontmatter(path).get("description", "")


# Скиллы, которые Claude Code не показывает модели: (имя, почему).
HIDDEN = []


def collect(pattern, kind, source, settings=None):
    """[(имя, описание, вид, источник)] по glob-шаблону.

    Для скиллов с settings применяются правила списка Claude Code:
    when_to_use дописывается к описанию, текст обрезается до
    skillListingMaxDescChars, override "name-only" оставляет одно имя,
    "off" / "user-invocable-only" и disable-model-invocation скрывают скилл.
    """
    out = []
    for p in sorted(glob.glob(pattern)):
        is_skill = os.path.basename(p) == "SKILL.md"
        folder = os.path.basename(os.path.dirname(p)) if is_skill else os.path.basename(p)[:-3]
        fm = frontmatter(p)
        name = fm.get("name") or folder
        desc = fm.get("description", "")
        if not (is_skill and settings is not None):
            if desc:
                out.append((folder, desc, kind, source))
            continue
        if fm.get("when_to_use"):
            desc = (desc + " " + fm["when_to_use"]).strip()
        state = settings["overrides"].get(name, "on")
        if fm.get("disable-model-invocation", "").lower() == "true":
            HIDDEN.append((folder, "disable-model-invocation"))
            continue
        if state in HIDDEN_STATES:
            HIDDEN.append((folder, state))
            continue
        if state == "name-only":
            desc = ""
        out.append((folder, desc[:settings["max_desc"]], kind, source))
    return out


# --- настройки Claude Code ----------------------------------------------------

def load_settings():
    """Настройки списка скиллов с учётом приоритета: user < project < local.

    Managed-настройки и флаг --settings не читаются: в личных репозиториях
    их нет. skillOverrides сливается по ключам, остальное перекрывается.
    """
    files = [
        os.path.join(HOME_CLAUDE, "settings.json"),
        os.path.join(ROOT, ".claude", "settings.json"),
        os.path.join(ROOT, ".claude", "settings.local.json"),
    ]
    result = {"overrides": {}, "fraction": DEFAULT_FRACTION,
              "max_desc": DEFAULT_MAX_DESC, "sources": []}
    for f in files:
        if not os.path.isfile(f):
            continue
        try:
            data = json.loads(read(f))
        except ValueError:
            continue
        rel = f.replace(HOME_CLAUDE, "~/.claude").replace(ROOT, ".").replace("\\", "/")
        if isinstance(data.get("skillOverrides"), dict):
            result["overrides"].update(data["skillOverrides"])
            result["sources"].append("skillOverrides: %s (%d)"
                                     % (rel, len(data["skillOverrides"])))
        if "skillListingBudgetFraction" in data:
            result["fraction"] = float(data["skillListingBudgetFraction"])
            result["sources"].append("skillListingBudgetFraction: %s" % rel)
        if "skillListingMaxDescChars" in data:
            result["max_desc"] = int(data["skillListingMaxDescChars"])
            result["sources"].append("skillListingMaxDescChars: %s" % rel)
        if isinstance(data.get("enabledPlugins"), dict):
            result.setdefault("enabled_plugins", [])
            result["enabled_plugins"] += [k for k, v in data["enabledPlugins"].items() if v]

    auto = max(0, int(WINDOW * result["fraction"] * CHARS_PER_BUDGET_UNIT) - BUILTIN_RESERVE)
    auto_from = ("%g%% окна %d, калибровка x%d, минус ~%d на встроенные"
                 % (result["fraction"] * 100, WINDOW, CHARS_PER_BUDGET_UNIT, BUILTIN_RESERVE))
    env = os.environ.get("SLASH_COMMAND_TOOL_CHAR_BUDGET", "").replace("_", "")
    try:
        if BUDGET_CHARS:
            result["budget"], result["budget_from"] = BUDGET_CHARS, "--budget-chars"
        elif env:
            result["budget"] = max(0, int(float(env)) - BUILTIN_RESERVE)
            result["budget_from"] = "SLASH_COMMAND_TOOL_CHAR_BUDGET, минус встроенные"
        else:
            result["budget"], result["budget_from"] = auto, auto_from
    except ValueError:
        result["budget"], result["budget_from"] = auto, auto_from
    return result


def invocations():
    """Число вызовов каждого скилла по логу skill-usage-logger (все проекты)."""
    counts = {}
    for line in read(USAGE_LOG).splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "skill":
            name = rec.get("name", "").split(":")[-1]
            counts[name] = counts.get(name, 0) + 1
    return counts


def entry_chars(name, desc):
    """Сколько символов строка скилла занимает в списке."""
    return len(name) + (len(desc) + 2 if desc else 0) + 3


def simulate_listing(skills, budget, calls):
    """Кто потеряет описание при переполнении.

    Claude Code снимает описания, начиная с реже всего вызываемых. Возвращает
    (размер списка, [точно без описания], [группа на границе], размер группы
    на границе, сколько из неё потеряют). Порядок внутри группы с равным числом
    вызовов не документирован, поэтому граничная группа отдаётся целиком.
    """
    size = sum(entry_chars(n, d) for n, d, _, _ in skills)
    if size <= budget:
        return size, [], [], 0
    by_calls = {}
    for item in skills:
        if item[1]:
            by_calls.setdefault(calls.get(item[0], 0), []).append(item)
    lost, over = [], size - budget
    for c in sorted(by_calls):
        group = by_calls[c]
        saved = sum(len(d) + 2 for _, d, _, _ in group)
        if saved <= over:
            lost.extend(group)
            over -= saved
            if over <= 0:
                return size, lost, [], 0
            continue
        # Граница проходит внутри группы: сколько штук минимум уйдёт.
        need, freed = 0, 0
        for _, d, _, _ in sorted(group, key=lambda x: -len(x[1])):
            if freed >= over:
                break
            freed += len(d) + 2
            need += 1
        return size, lost, group, need
    return size, lost, [], 0


def gather(settings):
    """Все слои описаний, которые попадают в контекст."""
    layers = []

    layers.append(("скиллы репозитория", collect(
        os.path.join(ROOT, ".claude", "skills", "*", "SKILL.md"),
        "skill", "repo", settings)))

    layers.append(("скиллы пользователя", collect(
        os.path.join(HOME_CLAUDE, "skills", "*", "SKILL.md"),
        "skill", "user", settings)))

    # Скиллы, синхронизированные с claude.ai (anthropic-skills:*).
    layers.append(("скиллы claude.ai", collect(
        os.path.join(HOME_CLAUDE, "skills", "synced", "*", "*", "SKILL.md"),
        "skill", "claude.ai", settings)))

    # Плагины: только синхронизированные и включённые в enabledPlugins.
    # plugins/marketplaces/ — это каталоги, а не установленное: их не считать.
    # skillOverrides на плагины не действует: только обрезка описания.
    plugin_settings = dict(settings, overrides={})
    patterns = [os.path.join(HOME_CLAUDE, "plugins", "synced", "*", "*",
                             "skills", "*", "SKILL.md")]
    for key in settings.get("enabled_plugins", []):
        plugin, _, market = key.partition("@")
        patterns.append(os.path.join(HOME_CLAUDE, "plugins", "cache", market or "*",
                                     plugin, "*", "skills", "*", "SKILL.md"))
    plugin_skills = []
    for pattern in patterns:
        plugin_skills += collect(pattern, "skill", "plugin", plugin_settings)
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


def quiet_check():
    """Одна строка, только если скиллы репозитория теряют описание.

    Режим для завершения сессии: молчит, когда всё видно модели, и называет
    пострадавшие скиллы, когда нет. Код возврата 1 при потере.
    """
    settings = load_settings()
    skills = [it for _, items in gather(settings) for it in items if it[2] == "skill"]
    _, lost, border, need = simulate_listing(skills, settings["budget"], invocations())
    sure = sorted(n for n, _, _, s in lost if s == "repo")
    maybe = sorted(n for n, _, _, s in border if s == "repo")
    if not sure and not maybe:
        return 0
    line = "[ai-tools] Модель видит без описания скиллы репозитория: %s" % ", ".join(sure or maybe)
    if maybe and sure:
        line += "; под угрозой: %s" % ", ".join(maybe)
    elif maybe:
        line = ("[ai-tools] Под угрозой потери описания (%d из группы %d): %s"
                % (need, len(border), ", ".join(maybe)))
    print(line + ". Разбор: token_budget.py")
    return 1


def main():
    if QUIET:
        return quiet_check()
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

    # --- список скиллов после настроек и потолка Claude Code
    settings = load_settings()
    layers = gather(settings)
    skills = [it for _, items in layers for it in items if it[2] == "skill"]
    calls = invocations()
    size, lost, border, need = simulate_listing(skills, settings["budget"], calls)

    print("Список скиллов, который видит модель:")
    print("  бюджет %d симв. (%s)" % (settings["budget"], settings["budget_from"]))
    for src in settings["sources"]:
        print("  настройка  %s" % src)
    name_only = sum(1 for _, d, _, _ in skills if not d)
    print("  в списке %d скиллов, %d симв.; скрыто %d, только имя %d"
          % (len(skills), size, len(HIDDEN), name_only))
    if not lost and not border:
        print("  [ok] влезает целиком, все описания доходят до модели")
    else:
        print("  ! переполнение на %d симв.: описания снимаются с редко вызываемых"
              % (size - settings["budget"]))
        if lost:
            print("  без описания (%d):" % len(lost))
            print("    " + ", ".join(sorted("%s/%s" % (s, n) for n, _, _, s in lost)))
        if border:
            print("  на границе, из этих %d потеряют описание не меньше %d "
                  "(вызовов: %d; порядок внутри не документирован):"
                  % (len(border), need, calls.get(border[0][0], 0)))
            print("    " + ", ".join(sorted("%s/%s" % (s, n) for n, _, _, s in border)))
    print("  Проверка по факту: /context (строка Skills) или /skill-doctor.")
    print()

    # Урезанные описания в контексте не едут: считаем то, что дойдёт.
    dropped = set(n for n, _, _, _ in lost)
    dropped.update(n for n, _, _, _ in
                   sorted(border, key=lambda x: -len(x[1]))[:need])
    layers = [(title, [(n, "" if (k == "skill" and n in dropped) else d, k, s)
                       for n, d, k, s in items])
              for title, items in layers]

    print("Описания — едут в КАЖДОМ запросе, даже если инструмент не вызван:")
    grand = 0
    layer_totals = []
    for title, items in layers:
        if not items:
            continue
        n = count("\n".join(("%s: %s" % (nm, d)) if d else nm
                            for nm, d, _, _ in items))
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
        scored = sorted(((count(d), n, k, s) for n, d, k, s in everything if d),
                        reverse=True)
        for n, name, kind, source in scored[:TOP]:
            print("  %-34s %5d   %s/%s" % (name, n, source, kind))
        print()

    # --- где рычаг
    if layer_totals:
        top_layer = max(layer_totals, key=lambda x: x[1])
        print("Главный рычаг — «%s»: %d токенов в каждом запросе."
              % (top_layer[0], top_layer[1]))
        if any(w in top_layer[0] for w in ("пользовател", "плагин", "claude.ai", "агент")):
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
