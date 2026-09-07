#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Аудит AI-инструментов репозитория. Работает в любом проекте без настройки.

    python audit.py                 # полный отчёт
    python audit.py --quiet         # молчит, если всё чисто (для хуков)
    python audit.py --json          # машинный вывод
    python audit.py --root PATH     # аудит другого репозитория

Что проверяет:
  1. Инвентарь .claude/ — сколько скиллов, агентов, команд, хуков.
  2. Фронтматтер скиллов и агентов — обязательные поля, дубли имён.
  3. Хуки — файл на месте, синтаксис интерпретатора, нет ли осиротевших.
  4. Ссылки в .md — существуют ли файлы, на которые ссылаются инструменты.
  5. Окружение — заявленные в скиллах инструменты реально доступны.
  6. Структурные соглашения — только если описаны в конфиге.

Конфиг необязателен. Если рядом лежит .claude/audit.config.json, из него
берутся вещи, которые нельзя угадать: парные папки, файлы памяти, список
инструментов окружения. Без конфига работают проверки 1-5.

Код возврата: 0 — чисто, 1 — есть замечания. Никогда не бросает исключение
наружу: сломанный аудит не должен блокировать работу.
"""
import io
import json
import os
import re
import subprocess
import sys

# --- разбор аргументов ------------------------------------------------------

QUIET = "--quiet" in sys.argv
AS_JSON = "--json" in sys.argv
INIT = "--init" in sys.argv
FORCE = "--force" in sys.argv

ROOT = None
if "--root" in sys.argv:
    i = sys.argv.index("--root")
    if i + 1 < len(sys.argv):
        ROOT = os.path.abspath(sys.argv[i + 1])


def find_root(start):
    """Корень репозитория: вверх до .git или .claude, иначе — стартовая папка."""
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
    # Скрипт лежит в .claude/skills/<name>/scripts/ — но запускать его могут
    # откуда угодно. Ищем от текущей папки, а не от расположения скрипта:
    # так один скилл обслуживает любой репозиторий, где его вызвали.
    ROOT = find_root(os.getcwd())

CLAUDE_DIR = os.path.join(ROOT, ".claude")

# --- накопитель результатов -------------------------------------------------

problems = []   # список (категория, текст)
notes = []      # информационные строки, не проблемы


def problem(category, text):
    problems.append((category, text))


def read(path):
    try:
        return io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def rel(path):
    try:
        return os.path.relpath(path, ROOT).replace("\\", "/")
    except ValueError:
        return path


# --- конфиг (необязательный) ------------------------------------------------

DEFAULT_CONFIG = {
    # Пары папок, где каждой подпапке слева должна соответствовать подпапка
    # справа: [["projects", "results"]]. Пусто — проверка не выполняется.
    "pairs": [],
    # Файлы, обязательные внутри каждой подпапки пары: ["memory.local.md"].
    "pair_required_files": {},
    # Корневые папки, ссылки на которые проверяются в .md: ["projects", "docs"].
    # Пусто — определяются автоматически по папкам верхнего уровня.
    "link_roots": [],
    # Инструменты, заявленные в скиллах: {"python": "python --version"}.
    # Пусто — берётся встроенный список кандидатов.
    "env_tools": {},
    # Что не аудировать: имена скиллов/папок, поставленных извне.
    "ignore_skills": [],
    # Файл общей памяти, наличие которого проверяется: "results/memory.md".
    "memory_file": "",
    # Должен ли каждый проект быть упомянут в файле памяти. Заведённый и
    # забытый проект — типовая потеря: он есть на диске, но следующая сессия
    # о нём не узнает. Требует memory_file и pairs.
    "require_mention_in_memory": False,
    # Факты об окружении, заявленные в инструкциях. Богаче автоопределения тем,
    # что указывает ГДЕ заявлено — без этого непонятно, что чинить.
    # Формат: [{"name":…, "probe":"cmd:python"|"path:…", "expect":true, "where":…}]
    "env_facts": [],
    # Ссылки, которые заведомо не существуют, с обязательной причиной.
    # {"projects/foo/": "будущее имя, вынос не состоялся"}. Причина обязательна:
    # без неё список превращается в свалку, куда прячут настоящие поломки.
    "known_missing": {},
    # Где живут проекты/задачи, если они не описаны парой папок: "projects/tasks".
    # Читают скиллы начала и завершения сессии; аудит проверяет наличие памяти.
    "project_root": "",
    # Имя файла памяти внутри папки проекта: "memory.local.md" или "memory.md".
    "project_memory": "",
    # Имя файла инструкции внутри папки проекта: "CLAUDE.local.md".
    "project_instruction": "",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(CLAUDE_DIR, "audit.config.json")
    if not os.path.isfile(path):
        return cfg, False
    try:
        user = json.loads(read(path))
    except ValueError as e:
        problem("конфиг", "audit.config.json не читается как JSON: %s" % e)
        return cfg, False
    for k, v in user.items():
        if k in cfg:
            cfg[k] = v
        elif not k.startswith("_"):
            # Ключи с подчёркиванием — комментарии: JSON своих не имеет.
            notes.append("конфиг: неизвестный ключ %r — проигнорирован" % k)
    return cfg, True


CONFIG, HAS_CONFIG = load_config()

# Скиллы, установленные пакетными менеджерами: их фронтматтер — не наша забота,
# чинить чужое всё равно нельзя, а шум они дают изрядный.
VENDOR_MARKERS = ("skills-lock.json",)


def vendor_skill_names(skill_items):
    """Имена скиллов, поставленных извне.

    Определяются тремя независимыми признаками — ни один в одиночку не полон:
      1. lock-файл пакетного менеджера (skills-lock.json);
      2. префикс в поле name (`ckm:brand`) — так метят себя маркетплейсы;
      3. признаки установки: файл .skill-meta, папка .git внутри скилла.

    Плюс явный список ignore_skills из конфига. Чужие скиллы исключаются
    из проверок: чинить их нельзя, а шумят они за всех сразу.
    """
    names = set(CONFIG.get("ignore_skills") or [])

    for marker in VENDOR_MARKERS:
        path = os.path.join(ROOT, marker)
        if not os.path.isfile(path):
            continue
        try:
            data = json.loads(read(path))
        except ValueError:
            continue
        names.update(data.get("skills", {}).keys())

    for name, path in skill_items:
        folder = os.path.dirname(path)
        if os.path.exists(os.path.join(folder, ".skill-meta")) or \
           os.path.isdir(os.path.join(folder, ".git")):
            names.add(name)
            continue
        fm = parse_frontmatter(read(path)) or {}
        # Маркетплейсы метят скиллы префиксом: name: ckm:brand
        if ":" in fm.get("name", ""):
            names.add(name)

    names.update(bulk_added_skills(skill_items))
    return names


def bulk_added_skills(skill_items, threshold=5):
    """Скиллы, добавленные пачкой одним коммитом, — почти наверняка чужие.

    Свой скилл рождается отдельным осмысленным коммитом; пачку из десятков
    вливают одним «Extended skills». Порог в 5 штук на коммит разделяет эти
    два случая. Признак нужен потому, что lock-файлы неполны, а структурных
    отличий у скачанного скилла может не быть вовсе.

    Без git (или в репозитории без истории) возвращает пустое множество —
    тогда работают остальные признаки.
    """
    by_commit = {}
    for name, path in skill_items:
        try:
            r = subprocess.run(
                ["git", "-C", ROOT, "log", "--diff-filter=A", "--format=%H",
                 "--", os.path.relpath(path, ROOT)],
                capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return set()
        if r.returncode != 0:
            return set()
        lines = (r.stdout or b"").decode("utf-8", "replace").split()
        if not lines:
            continue                      # не закоммичен — считаем своим
        by_commit.setdefault(lines[-1], []).append(name)

    out = set()
    for names in by_commit.values():
        if len(names) >= threshold:
            out.update(names)
    return out


# Заполняется в main(), когда известен инвентарь: определение вендорных
# скиллов читает их фронтматтер, а для этого нужен разбор, объявленный ниже.
VENDOR = set()


# --- 1. инвентарь -----------------------------------------------------------

def list_dir(name, pattern_file=None):
    """Подпапки .claude/<name>/ (если pattern_file) или файлы .md в ней."""
    base = os.path.join(CLAUDE_DIR, name)
    if not os.path.isdir(base):
        return []
    out = []
    for entry in sorted(os.listdir(base)):
        full = os.path.join(base, entry)
        if pattern_file:
            f = os.path.join(full, pattern_file)
            if os.path.isfile(f):
                out.append((entry, f))
        elif entry.endswith(".md"):
            out.append((entry[:-3], full))
    return out


def inventory():
    skills = list_dir("skills", "SKILL.md")
    agents = list_dir("agents")
    commands = list_dir("commands")
    hooks = []
    hdir = os.path.join(CLAUDE_DIR, "hooks")
    if os.path.isdir(hdir):
        for entry in sorted(os.listdir(hdir)):
            if entry.endswith((".ps1", ".js", ".sh", ".py", ".mjs", ".cjs")):
                hooks.append((entry, os.path.join(hdir, entry)))
    return skills, agents, commands, hooks


# --- 2. фронтматтер ---------------------------------------------------------

FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def parse_frontmatter(text):
    """Минимальный разбор YAML-фронтматтера: только верхнеуровневые key: value."""
    m = FM_RE.match(text)
    if not m:
        return None
    fm = {}
    for line in m.group(1).splitlines():
        if line.startswith((" ", "\t", "#")) or ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip().strip("'\"")
    return fm


def check_frontmatter(items, kind, required=("name", "description")):
    """Проверяет фронтматтер и возвращает {имя: описание} для своих элементов."""
    seen = {}
    descriptions = {}
    for name, path in items:
        if name in VENDOR:
            continue
        text = read(path)
        fm = parse_frontmatter(text)
        if fm is None:
            problem(kind, "%s: нет фронтматтера (--- в начале файла)" % rel(path))
            continue
        for field in required:
            if not fm.get(field):
                problem(kind, "%s: пустое или отсутствует поле %s" % (rel(path), field))
        declared = fm.get("name", "")
        if declared and declared != name:
            problem(kind, "%s: name=%r не совпадает с именем папки/файла %r"
                    % (rel(path), declared, name))
        if declared:
            if declared in seen:
                problem(kind, "дубль name=%r: %s и %s"
                        % (declared, seen[declared], rel(path)))
            seen[declared] = rel(path)
        desc = fm.get("description", "")
        descriptions[name] = desc
        # Описание едет в каждом запросе — длинное стоит токенов постоянно.
        if len(desc) > 700:
            problem(kind, "%s: описание %d символов — оно грузится в КАЖДОМ "
                          "запросе, стоит сократить" % (rel(path), len(desc)))
    return descriptions


# --- 3. хуки ----------------------------------------------------------------

def settings_files():
    out = []
    for name in ("settings.json", "settings.local.json"):
        p = os.path.join(CLAUDE_DIR, name)
        if os.path.isfile(p):
            out.append(p)
    return out


def hook_commands():
    """Все команды хуков из settings*.json — плоским списком строк."""
    cmds = []
    for path in settings_files():
        try:
            data = json.loads(read(path))
        except ValueError as e:
            problem("настройки", "%s не читается как JSON: %s" % (rel(path), e))
            continue
        for event, groups in (data.get("hooks") or {}).items():
            for group in groups or []:
                for h in group.get("hooks", []) or []:
                    parts = [h.get("command", "")]
                    parts.extend(h.get("args", []) or [])
                    cmds.append((event, " ".join(str(p) for p in parts), rel(path)))
    return cmds


def check_hooks(hooks):
    """Хуки подключены и запускаются; на диске нет забытых."""
    cmds = hook_commands()
    joined = " ".join(c for _, c, _ in cmds)

    # Осиротевшие: файл есть, в настройках не упомянут.
    for name, path in hooks:
        if name not in joined:
            problem("хуки", "%s лежит на диске, но не подключён ни в одном "
                            "settings.json — либо подключи, либо удали" % rel(path))

    # Битые ссылки: в настройках указан файл, которого нет.
    for event, cmd, src in cmds:
        for token in re.findall(r'["\']?([^"\'\s]+\.(?:ps1|js|sh|py|mjs|cjs))["\']?', cmd):
            candidate = token.replace("${CLAUDE_PROJECT_DIR}", ROOT)
            candidate = os.path.expanduser(candidate.replace("~", "~"))
            if not os.path.isabs(candidate):
                candidate = os.path.join(ROOT, candidate)
            if not os.path.exists(candidate):
                problem("хуки", "%s: событие %s ссылается на %s — файла нет"
                        % (src, event, token))

    # Синтаксис: только для своих хуков и только там, где есть чем проверить.
    checkers = {
        ".js": ["node", "--check"],
        ".cjs": ["node", "--check"],
        ".py": [sys.executable, "-m", "py_compile"],
    }
    for name, path in hooks:
        ext = os.path.splitext(name)[1]
        cmd = checkers.get(ext)
        if not cmd:
            continue
        try:
            r = subprocess.run(cmd + [path], capture_output=True, timeout=30)
            if r.returncode != 0:
                err = (r.stderr or b"").decode("utf-8", "replace").strip()
                problem("хуки", "%s: синтаксическая ошибка\n    %s"
                        % (rel(path), err.splitlines()[0] if err else "?"))
        except (OSError, subprocess.SubprocessError):
            pass  # нет интерпретатора — не наша проблема, ловится в env


# --- 4. ссылки на файлы -----------------------------------------------------

def top_level_dirs():
    """Папки верхнего уровня — кандидаты в корни ссылок."""
    out = []
    for entry in sorted(os.listdir(ROOT)):
        if entry.startswith(".") or entry in ("node_modules", "venv", "__pycache__"):
            continue
        if os.path.isdir(os.path.join(ROOT, entry)):
            out.append(entry)
    return out


# Плейсхолдеры и обрывки, путями не являющиеся.
PLACEHOLDER = re.compile(
    r"<[^>]+>|\$\w+|\{[^}]*\}|\*|YYYY|MM|DD|NN|_$|[\[\]()]|\.\.\."
)


def check_links(link_roots):
    """Ссылки вида projects/foo/bar.md в .md-файлах инструментов."""
    if not link_roots:
        return
    alt = "|".join(re.escape(r) for r in link_roots)
    link_re = re.compile(r"(?<![\w./\\-])((?:%s)[/\\][\w./\\-]+\.\w{1,5})" % alt)

    targets = []
    for base in (CLAUDE_DIR,):
        for dirpath, dirnames, names in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", "__pycache__")]
            # Вендорные скиллы пропускаем: чужие ссылки чинить всё равно нельзя.
            parts = rel(dirpath).split("/")
            if len(parts) > 2 and parts[1] == "skills" and parts[2] in VENDOR:
                continue
            for n in names:
                if n.endswith(".md"):
                    targets.append(os.path.join(dirpath, n))
    for n in ("CLAUDE.md", "README.md"):
        p = os.path.join(ROOT, n)
        if os.path.isfile(p):
            targets.append(p)

    known = CONFIG.get("known_missing") or {}

    broken = 0
    for path in targets:
        for line_no, line in enumerate(read(path).splitlines(), 1):
            for match in link_re.findall(line):
                if PLACEHOLDER.search(match):
                    continue
                if any(match.startswith(k) for k in known):
                    continue           # заявлено в конфиге с причиной
                if not os.path.exists(os.path.join(ROOT, match.replace("\\", "/"))):
                    problem("ссылки", "%s:%d → %s (файла нет)"
                            % (rel(path), line_no, match))
                    broken += 1
                    if broken >= 25:
                        problem("ссылки", "… дальнейшие битые ссылки не показаны")
                        return


# --- 5. окружение -----------------------------------------------------------

# Инструменты, которые скиллы обычно вызывают. Проверяем только те, что
# реально упоминаются в текстах скиллов — иначе отчёт превратится в список
# всего софта на свете.
TOOL_CHECKS = {
    "python": ["python", "--version"],
    "node": ["node", "--version"],
    "npx": ["npx", "--version"],
    "git": ["git", "--version"],
    "ffmpeg": ["ffmpeg", "-version"],
    "pandoc": ["pandoc", "--version"],
    "docker": ["docker", "--version"],
    "gh": ["gh", "--version"],
    "dotnet": ["dotnet", "--version"],
    "java": ["java", "-version"],
}

CALL_RE_CACHE = {}


def mentioned_tools():
    """Инструменты, вызываемые в своих скиллах/агентах/командах/хуках."""
    wanted = dict(CONFIG.get("env_tools") or {})
    found = dict(wanted)
    if wanted:
        return found

    texts = []
    for sub in ("skills", "agents", "commands", "hooks"):
        base = os.path.join(CLAUDE_DIR, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, names in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", "__pycache__")]
            parts = rel(dirpath).split("/")
            if len(parts) > 2 and parts[1] == "skills" and parts[2] in VENDOR:
                continue
            for n in names:
                if n.endswith((".md", ".ps1", ".js", ".sh", ".py", ".mjs")):
                    texts.append(read(os.path.join(dirpath, n)))
    blob = "\n".join(texts)

    for tool in TOOL_CHECKS:
        if tool not in CALL_RE_CACHE:
            # Вызов: в начале строки, после пайпа, амперсанда или в бэктиках.
            CALL_RE_CACHE[tool] = re.compile(
                r"(?:^|[|&;`$(\s])" + re.escape(tool) + r"\s+[\w./-]", re.M)
        if CALL_RE_CACHE[tool].search(blob):
            found[tool] = TOOL_CHECKS[tool]
    return found


def check_env_facts():
    """Сверяет факты об окружении, ЗАЯВЛЕННЫЕ в инструкциях, с реальностью.

    Отличается от check_env() тем, что знает, где утверждение записано: без
    этого непонятно, что чинить. Такие факты протухают молча — фраза «в системе
    нет Python» способна прожить месяц после установки Python и всё это время
    заставлять обходиться костылями.
    """
    import shutil
    for fact in CONFIG.get("env_facts") or []:
        if not isinstance(fact, dict):
            problem("конфиг", "env_facts: ожидается объект, получено %r" % (fact,))
            continue
        probe = fact.get("probe", "")
        if ":" not in probe:
            problem("конфиг", "env_facts[%s]: probe должен быть "
                              "'cmd:имя' или 'path:путь'" % fact.get("name", "?"))
            continue
        kind, value = probe.split(":", 1)
        if kind == "cmd":
            actual = bool(shutil.which(value))
        elif kind == "path":
            actual = os.path.exists(os.path.join(ROOT, value))
        else:
            problem("конфиг", "env_facts[%s]: неизвестный вид проверки %r"
                    % (fact.get("name", "?"), kind))
            continue
        expect = bool(fact.get("expect", True))
        if actual != expect:
            problem("окружение", "%s: %s  ← заявлено в %s"
                    % (fact.get("name", value),
                       "нет, а ожидается" if expect else "есть, а не должно быть",
                       fact.get("where", "?")))


def check_env():
    for tool, cmd in sorted(mentioned_tools().items()):
        if isinstance(cmd, str):
            cmd = cmd.split()
        try:
            # На Windows часть инструментов — .cmd/.bat обёртки, а рядом лежит
            # ещё и расширение-less shell-скрипт для Git Bash. Прямой запуск
            # второго проваливается, поэтому shell=True: он разрешает по
            # PATHEXT так же, как это сделает вызывающий скилл.
            if os.name == "nt":
                r = subprocess.run(" ".join(cmd), capture_output=True,
                                   timeout=30, shell=True)
            else:
                r = subprocess.run(cmd, capture_output=True, timeout=30)
            ok = r.returncode == 0
            # Заглушка Microsoft Store: возвращает 0 и пустой вывод.
            out = (r.stdout or b"") + (r.stderr or b"")
            if ok and not out.strip():
                problem("окружение", "%s: отвечает пустотой — вероятно заглушка "
                                     "(например Microsoft Store для python3)" % tool)
                continue
            if not ok:
                problem("окружение", "%s: скиллы его вызывают, но запуск вернул "
                                     "код %d" % (tool, r.returncode))
        except FileNotFoundError:
            problem("окружение", "%s: скиллы его вызывают, но в PATH его нет" % tool)
        except (OSError, subprocess.SubprocessError) as e:
            problem("окружение", "%s: проверить не удалось (%s)" % (tool, e))


# --- 6. структурные соглашения (только по конфигу) --------------------------

def check_pairs():
    pairs = CONFIG.get("pairs") or []
    required = CONFIG.get("pair_required_files") or {}
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            problem("конфиг", "pairs: ожидается [[\"a\",\"b\"], …], получено %r" % (pair,))
            continue
        left, right = pair
        ldir = os.path.join(ROOT, left)
        if not os.path.isdir(ldir):
            problem("структура", "в конфиге заявлена папка %s/ — её нет" % left)
            continue
        for name in sorted(os.listdir(ldir)):
            if name.startswith(".") or not os.path.isdir(os.path.join(ldir, name)):
                continue
            counterpart = os.path.join(ROOT, right, name)
            if not os.path.isdir(counterpart):
                problem("структура", "%s/%s есть, а %s/%s нет"
                        % (left, name, right, name))
                continue
            for side, files in required.items():
                base = ldir if side == left else os.path.join(ROOT, right)
                for f in files if isinstance(files, list) else [files]:
                    target = os.path.join(base, name, f)
                    if not os.path.isfile(target):
                        problem("структура", "%s/%s/%s — файла нет"
                                % (side, name, f))
                    elif not read(target).strip():
                        # Пустой файл памяти хуже отсутствующего: он выглядит
                        # заполненным и не вызывает вопросов.
                        problem("структура", "%s/%s/%s — файл пуст"
                                % (side, name, f))

    # Структура без пары папок: задачи лежат в одной папке, у каждой своя
    # память рядом. Проверяем то же, что и для пар: память есть и не пуста.
    proot = CONFIG.get("project_root")
    pmem = CONFIG.get("project_memory")
    if proot and pmem:
        base = os.path.join(ROOT, proot)
        if not os.path.isdir(base):
            problem("структура", "в конфиге заявлена папка %s/ — её нет" % proot)
        else:
            for name in sorted(os.listdir(base)):
                folder = os.path.join(base, name)
                if name.startswith(".") or not os.path.isdir(folder):
                    continue
                target = os.path.join(folder, pmem)
                if not os.path.isfile(target):
                    problem("структура", "%s/%s/%s — файла нет" % (proot, name, pmem))
                elif not read(target).strip():
                    problem("структура", "%s/%s/%s — файл пуст" % (proot, name, pmem))

    mem = CONFIG.get("memory_file")
    mem_path = os.path.join(ROOT, mem) if mem else None
    if mem and not os.path.isfile(mem_path):
        problem("структура", "файл памяти %s не найден" % mem)
        return

    # Проект, заведённый и не упомянутый в памяти, теряется: он есть на диске,
    # но следующая сессия о нём не узнает.
    if mem and CONFIG.get("require_mention_in_memory") and pairs:
        index = read(mem_path)
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            ldir = os.path.join(ROOT, pair[0])
            if not os.path.isdir(ldir):
                continue
            for name in sorted(os.listdir(ldir)):
                if name.startswith(".") or not os.path.isdir(os.path.join(ldir, name)):
                    continue
                if name not in index:
                    problem("память", "проект %s не упомянут в %s" % (name, mem))


# --- вывод ------------------------------------------------------------------

MEMORY_NAMES = ("memory.local.md", "memory.md", "MEMORY.md", "context.md")
INSTRUCTION_NAMES = ("CLAUDE.local.md", "CLAUDE.md", "AGENTS.md")


def _subdirs(path):
    if not os.path.isdir(path):
        return []
    return [d for d in sorted(os.listdir(path))
            if os.path.isdir(os.path.join(path, d)) and not d.startswith(".")]


def _file_in_most(base, subs, names):
    """Какое из имён встречается в большинстве подпапок. Иначе — пусто.

    Большинство, а не «хоть где-то»: один случайный memory.md в одной задаче
    не делает его соглашением репозитория, а ложное соглашение породит поток
    замечаний по папкам, которые ему и не должны следовать.
    """
    if not subs:
        return ""
    for name in names:
        hits = sum(1 for s in subs if os.path.isfile(os.path.join(base, s, name)))
        if hits * 2 > len(subs):
            return name
    return ""


def detect_structure():
    """Угадывает уклад репозитория. Возвращает (конфиг, список пояснений)."""
    cfg, notes_out = {}, []
    tops = [d for d in _subdirs(ROOT) if d not in ("node_modules", "venv", "dist", "build")]

    # 1. Пара папок: две папки верхнего уровня с сильно пересекающимися именами
    #    подпапок — это уклад «источники + результаты».
    best = None
    for i, left in enumerate(tops):
        for right in tops[i + 1:]:
            a, b = set(_subdirs(os.path.join(ROOT, left))), set(_subdirs(os.path.join(ROOT, right)))
            common = a & b
            if len(common) >= 2 and len(common) * 2 >= min(len(a), len(b)):
                score = len(common)
                if best is None or score > best[0]:
                    best = (score, left, right)
    if best:
        _, left, right = best
        cfg["pairs"] = [[left, right]]
        notes_out.append("пара папок %s/ + %s/ — %d общих подпапок"
                         % (left, right, best[0]))
        required = {}
        for side in (left, right):
            base = os.path.join(ROOT, side)
            subs = _subdirs(base)
            found = [n for n in (_file_in_most(base, subs, INSTRUCTION_NAMES),
                                 _file_in_most(base, subs, MEMORY_NAMES)) if n]
            if found:
                required[side] = found
        if required:
            cfg["pair_required_files"] = required
            notes_out.append("обязательные файлы в подпапках: %s"
                             % "; ".join("%s → %s" % (k, ", ".join(v))
                                         for k, v in required.items()))
    else:
        # 2. Без пары: ищем папку задач — вложенную папку с однотипными
        #    подпапками, у большинства которых есть файл памяти.
        for top in tops:
            for mid in [""] + _subdirs(os.path.join(ROOT, top)):
                base = os.path.join(ROOT, top, mid) if mid else os.path.join(ROOT, top)
                subs = _subdirs(base)
                if len(subs) < 2:
                    continue
                mem = _file_in_most(base, subs, MEMORY_NAMES)
                if not mem:
                    continue
                rel_path = (top + "/" + mid) if mid else top
                cfg["project_root"] = rel_path
                cfg["project_memory"] = mem
                instr = _file_in_most(base, subs, INSTRUCTION_NAMES)
                if instr:
                    cfg["project_instruction"] = instr
                cfg["pairs"] = []
                notes_out.append("папка задач %s/ — у большинства подпапок есть %s"
                                 % (rel_path, mem))
                break
            if "project_root" in cfg:
                break

    # 3. Общая память: файл памяти в корне или внутри найденной папки результатов.
    for candidate in MEMORY_NAMES:
        if os.path.isfile(os.path.join(ROOT, candidate)):
            cfg["memory_file"] = candidate
            notes_out.append("общая память — %s" % candidate)
            break
    else:
        if cfg.get("pairs"):
            right = cfg["pairs"][0][1]
            for candidate in MEMORY_NAMES:
                if os.path.isfile(os.path.join(ROOT, right, candidate)):
                    cfg["memory_file"] = "%s/%s" % (right, candidate)
                    notes_out.append("общая память — %s/%s" % (right, candidate))
                    break

    # 4. Корни ссылок — папки верхнего уровня, где реально лежат .md.
    roots = []
    for top in tops:
        for _, _, names in os.walk(os.path.join(ROOT, top)):
            if any(n.endswith(".md") for n in names):
                roots.append(top)
                break
    if roots:
        cfg["link_roots"] = roots

    # 5. Инструменты, вызываемые в своих скиллах, — заготовка env_facts.
    #    Заполняем where шаблоном: указать источник может только человек,
    #    а пустое поле в отчёте выглядит как ошибка конфига.
    facts = []
    for tool in sorted(mentioned_tools()):
        facts.append({"name": tool, "probe": "cmd:" + tool, "expect": True,
                      "where": "УКАЖИ, где это заявлено"})
    if facts:
        cfg["env_facts"] = facts
        notes_out.append("инструменты, вызываемые в скиллах: %s"
                         % ", ".join(f["name"] for f in facts))

    return cfg, notes_out


def do_init():
    """Создаёт заготовку .claude/audit.config.json по структуре репозитория."""
    path = os.path.join(CLAUDE_DIR, "audit.config.json")
    if os.path.isfile(path) and not FORCE:
        print("Конфиг уже есть: %s" % rel(path))
        print("Перезаписать — с флагом --force (текущий будет потерян).")
        return 1

    cfg, found = detect_structure()

    print("Определение структуры: %s\n" % ROOT)
    if found:
        for n in found:
            print("  + %s" % n)
    else:
        print("  ничего специфичного не найдено — базовых проверок хватит")
    print()

    body = {"_comment": "Заготовка от audit.py --init. ПРОВЕРЬ перед использованием: "
                        "структура определена по догадкам, а не по твоим правилам."}
    body.update(cfg)

    if "env_facts" in body:
        body["_env_facts"] = ("Заполни поле where — где именно заявлено, что "
                              "инструмент есть. Без источника непонятно, что чинить, "
                              "когда факт протухнет. Лишние строки удали.")
    if "pairs" in body and body["pairs"]:
        body["_require_mention"] = ("Раскомментируй require_mention_in_memory, если "
                                    "каждый проект должен быть упомянут в файле памяти.")

    os.makedirs(CLAUDE_DIR, exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="\n").write(
        json.dumps(body, ensure_ascii=False, indent=2) + "\n")

    print("Записано: %s\n" % rel(path))
    print("Дальше:")
    print("  1. Открой файл и проверь догадки — особенно поля where в env_facts.")
    print("  2. Удали строки, которые к твоему репозиторию не относятся.")
    # Путь показываем от корня репо, только если скрипт лежит внутри него:
    # иначе rel() выдаёт цепочку ../.., которую невозможно набрать.
    script = os.path.abspath(__file__)
    shown = rel(script)
    if shown.startswith(".."):
        shown = script
    print("  3. Прогони аудит: python %s" % shown)
    return 0


def main():
    if not os.path.isdir(CLAUDE_DIR) and not INIT:
        if not QUIET:
            print("В %s нет папки .claude/ — аудировать нечего." % ROOT)
        return 0

    if INIT:
        return do_init()

    skills, agents, commands, hooks = inventory()

    global VENDOR
    VENDOR = vendor_skill_names(skills)
    own_skills = [(n, p) for n, p in skills if n not in VENDOR]

    check_frontmatter(own_skills, "скиллы")
    check_frontmatter(agents, "агенты")
    check_frontmatter(commands, "команды", required=("description",))
    check_hooks(hooks)

    roots = CONFIG.get("link_roots") or top_level_dirs()
    check_links(roots)
    check_env()
    check_env_facts()
    check_pairs()

    if AS_JSON:
        print(json.dumps({
            "root": ROOT,
            "inventory": {
                "skills_own": len(own_skills),
                "skills_vendor": len(skills) - len(own_skills),
                "agents": len(agents),
                "commands": len(commands),
                "hooks": len(hooks),
            },
            "problems": [{"category": c, "text": t} for c, t in problems],
            "notes": notes,
        }, ensure_ascii=False, indent=2))
        return 1 if problems else 0

    if QUIET and not problems:
        return 0

    if not QUIET:
        print("Аудит AI-инструментов: %s" % ROOT)
        print("Конфиг: %s\n" % (".claude/audit.config.json" if HAS_CONFIG
                                else "нет — работают проверки, не зависящие от структуры"))
        print("Инвентарь:")
        print("  скиллы своих      %3d" % len(own_skills))
        print("  скиллы вендорных  %3d" % (len(skills) - len(own_skills)))
        print("  агенты            %3d" % len(agents))
        print("  команды           %3d" % len(commands))
        print("  хуки              %3d" % len(hooks))
        print()

    if problems:
        by_cat = {}
        for cat, text in problems:
            by_cat.setdefault(cat, []).append(text)
        for cat in sorted(by_cat):
            print("[!] %s (%d):" % (cat, len(by_cat[cat])))
            for text in by_cat[cat]:
                print("    %s" % text)
            print()
    elif not QUIET:
        print("[ok] замечаний нет")

    for n in notes:
        if not QUIET:
            print("    %s" % n)

    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # аудит не должен ронять то, из чего его вызвали
        sys.stderr.write("audit.py: внутренняя ошибка: %s\n" % e)
        sys.exit(0)
