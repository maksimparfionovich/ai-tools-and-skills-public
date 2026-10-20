#!/usr/bin/env bash
# Обёртка для MCP-сервера jira: сначала гарантирует туннель, потом СТАНОВИТСЯ
# сервером mcp-atlassian через exec (лишнего процесса не остаётся).
#
# Прописывается в .mcp.json проекта: command bash (на Windows — bash.exe из Git for Windows)
# + путь к этому файлу.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1>&2 — страховка: любой случайный stdout из ensure-скрипта не должен попасть
# в канал JSON-RPC, который слушает Claude Code.
if ! "$HERE/ensure-tunnel.sh" 1>&2; then
  printf '[jira-mcp] туннель поднять не удалось — сервер стартует, но вызовы Jira будут падать\n' >&2
fi

# Интерпретатор с установленным mcp-atlassian: JIRA_MCP_PYTHON, иначе python3
# (в Git Bash на Windows python3 часто оказывается заглушкой Microsoft Store — там python).
PY="${JIRA_MCP_PYTHON:-}"
if [ -z "$PY" ]; then
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) PY=python ;;
    *) PY=python3 ;;
  esac
fi
exec "$PY" -c 'from mcp_atlassian import main; main()'
