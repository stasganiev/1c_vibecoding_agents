#!/usr/bin/env node
'use strict';

/*
 * Хук логирования использования slash-команд, скиллов и субагентов проекта.
 * Пишет одну JSON-строку (JSONL) на событие в .claude/use_tools.log.
 *
 * Подключается в .claude/settings.json на события:
 *   UserPromptSubmit -> команды (/analit, /dev, /review, ...)
 *   PostToolUse (matcher: Skill) -> скиллы
 *   SubagentStart / SubagentStop -> субагенты (для расчёта duration_ms)
 *
 * Хук никогда не блокирует выполнение: любые внутренние ошибки логируются
 * в stderr и завершаются exit code 0.
 */

const fs = require('fs');
const path = require('path');

const PROJECT_DIR = process.env.CLAUDE_PROJECT_DIR || process.cwd();
const LOG_FILE = path.join(PROJECT_DIR, '.claude', 'use_tools.log');
const STATE_DIR = path.join(PROJECT_DIR, '.claude', '.tool_use_state');

function readStdin() {
  try {
    const data = fs.readFileSync(0, 'utf8');
    return data ? JSON.parse(data) : {};
  } catch (err) {
    return {};
  }
}

function appendLog(entry) {
  const line = JSON.stringify(entry) + '\n';
  fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
  fs.appendFileSync(LOG_FILE, line, 'utf8');
}

function nowIso() {
  return new Date().toISOString();
}

// Извлекает "/имя_команды" и аргументы из сырого текста промпта пользователя.
function parseSlashCommand(promptText) {
  if (typeof promptText !== 'string') return null;
  const trimmed = promptText.trim();
  if (!trimmed.startsWith('/')) return null;
  const match = trimmed.match(/^\/([a-zA-Z0-9_-]+)\s*([\s\S]*)$/);
  if (!match) return null;
  return { name: match[1], args: match[2].trim() };
}

function truncate(str, max) {
  if (typeof str !== 'string') return str;
  return str.length > max ? str.slice(0, max) + '…' : str;
}

function handleUserPromptSubmit(input) {
  const promptText = input.prompt ?? input.prompt_text ?? input.text ?? '';
  const cmd = parseSlashCommand(promptText);
  if (!cmd) return; // обычный текстовый запрос - не логируем

  appendLog({
    ts: nowIso(),
    type: 'command',
    name: cmd.name,
    status: 'invoked',
    session_id: input.session_id || null,
    cwd: input.cwd || PROJECT_DIR,
    detail: truncate(cmd.args, 300),
  });
}

function handlePostToolUseSkill(input) {
  const toolInput = input.tool_input || {};
  const skillName = toolInput.skill || toolInput.name || toolInput.command || 'unknown';
  const isError = Boolean(input.tool_response && input.tool_response.is_error);

  appendLog({
    ts: nowIso(),
    type: 'skill',
    name: skillName,
    status: isError ? 'error' : 'ok',
    session_id: input.session_id || null,
    cwd: input.cwd || PROJECT_DIR,
    detail: truncate(toolInput.args || '', 300),
  });
}

function stateFilePath(agentId) {
  return path.join(STATE_DIR, `${agentId}.start`);
}

function handleSubagentStart(input) {
  const agentId = input.agent_id;
  if (!agentId) return;
  try {
    fs.mkdirSync(STATE_DIR, { recursive: true });
    fs.writeFileSync(stateFilePath(agentId), String(Date.now()), 'utf8');
  } catch (err) {
    // не критично - просто не будет duration_ms
  }
}

function handleSubagentStop(input) {
  const agentId = input.agent_id;
  const agentType = input.agent_type || 'unknown';

  let durationMs = null;
  if (agentId) {
    const statePath = stateFilePath(agentId);
    try {
      const startedAt = Number(fs.readFileSync(statePath, 'utf8'));
      if (Number.isFinite(startedAt)) {
        durationMs = Date.now() - startedAt;
      }
      fs.unlinkSync(statePath);
    } catch (err) {
      // файла старта нет/не читается - оставляем duration_ms = null
    }
  }

  appendLog({
    ts: nowIso(),
    type: 'subagent',
    name: agentType,
    status: 'completed',
    session_id: input.session_id || null,
    cwd: input.cwd || PROJECT_DIR,
    duration_ms: durationMs,
    detail: null,
  });
}

function main() {
  const input = readStdin();
  const event = input.hook_event_name;

  try {
    if (event === 'UserPromptSubmit') {
      handleUserPromptSubmit(input);
    } else if (event === 'PostToolUse' && input.tool_name === 'Skill') {
      handlePostToolUseSkill(input);
    } else if (event === 'SubagentStart') {
      handleSubagentStart(input);
    } else if (event === 'SubagentStop') {
      handleSubagentStop(input);
    }
  } catch (err) {
    process.stderr.write(`log_tool_use hook error: ${err && err.message}\n`);
  }

  process.exit(0);
}

main();