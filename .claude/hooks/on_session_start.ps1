# Проверка целостности репозитория и AI-инструментов при старте сессии.
#
# Запускает аудит из скилла ai-tools-audit и, если тот нашёл проблемы, печатает
# их — вывод попадает в контекст сессии. Если всё чисто, молчит: хук, который
# шумит на каждом старте, начинают игнорировать.
#
# Скрипт живёт в скилле, а не в results/: тот же файл переносится в любой
# репозиторий копированием папки скилла. Структурные соглашения этого
# воркспейса — в .claude/audit.config.json.
#
# Хук никогда не роняет старт сессии: любая ошибка гасится и выводится
# как одна строка-предупреждение.

$ErrorActionPreference = "Stop"

try {
    # Корень репозитория — на два уровня выше этого файла (.claude/hooks/)
    $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $script = Join-Path $root ".claude\skills\ai-tools-audit\scripts\audit.py"

    if (-not (Test-Path $script)) { exit 0 }

    # Именно "python": python3 в PATH — заглушка Microsoft Store, она молчит.
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { exit 0 }

    Push-Location $root
    try {
        $out = & $python $script --quiet 2>&1 | Out-String
        $failed = ($LASTEXITCODE -ne 0)
    }
    finally { Pop-Location }

    if ($failed -and $out.Trim()) {
        Write-Output "[audit] Целостность репозитория — есть замечания:"
        Write-Output $out.Trim()
        Write-Output "[audit] Подробности: python .claude/skills/ai-tools-audit/scripts/audit.py"
    }

    # --- AI-инструменты, изменённые но не прошедшие аудит ---------------------
    #
    # Скилл zavershenie-sessii проводит аудит .claude/ — но только если о нём
    # вспомнят. Здесь страховка: если в рабочем дереве лежат незакоммиченные
    # изменения в AI-инструментах, напоминаем об аудите одной строкой.
    #
    # Ориентир — незакоммиченность, а не «был ли аудит»: закоммиченное считаем
    # закрытым, потому что коммит в этом репозитории делается осознанно и
    # только по просьбе Стаса.
    Push-Location $root
    try {
        $changed = @(& git status --porcelain -- ".claude" ".mcp.json" 2>$null |
                     Where-Object { $_ -and $_.Trim() })
    }
    finally { Pop-Location }

    if ($changed.Count -gt 0) {
        $files = $changed | ForEach-Object { ($_ -replace '^..\s+', '') } |
                 Select-Object -First 6
        Write-Output "[ai-tools] Незакоммиченные изменения в AI-инструментах ($($changed.Count)):"
        foreach ($f in $files) { Write-Output "    $f" }
        if ($changed.Count -gt 6) { Write-Output "    … и ещё $($changed.Count - 6)" }
        Write-Output "[ai-tools] Перед закрытием сессии — /zavershenie-sessii (аудит инструментов)"
    }
}
catch {
    Write-Output "[audit] проверка не выполнилась: $($_.Exception.Message)"
}

# Всегда 0: проблемы в репозитории не повод блокировать старт сессии.
exit 0
