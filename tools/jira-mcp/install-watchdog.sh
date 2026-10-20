#!/usr/bin/env bash
# Ставит (или снимает) тик watchdog.sh раз в N минут через cron — для macOS и Linux.
# На Windows — install-watchdog.ps1 (Планировщик задач).
#
#   bash install-watchdog.sh            # поставить, интервал 5 мин
#   bash install-watchdog.sh 10         # интервал 10 мин
#   bash install-watchdog.sh --remove   # снять
#
# Резидентного процесса нет: cron уже работает, каждый тик живёт доли секунды и, если маркер
# «Jira используется» не свежий, сразу выходит. Строка в crontab помечена комментарием
# `# jira-mcp-watchdog`, по нему же снимается. У cron свой короткий PATH, поэтому в строку
# записывается PATH из системных папок и папок, где сейчас лежат tsh, curl и python3 (весь
# текущий PATH не годится: cron отвергает слишком длинную строку).
set -euo pipefail

TAG="# jira-mcp-watchdog"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHDOG="$HERE/watchdog.sh"

case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*) echo "на Windows используйте install-watchdog.ps1" >&2; exit 1 ;;
esac
command -v crontab >/dev/null 2>&1 || { echo "нет crontab — поставьте cron или запускайте watchdog.sh своим планировщиком" >&2; exit 1; }

current=$(crontab -l 2>/dev/null || true)
without=$(printf '%s\n' "$current" | grep -vF "$TAG" || true)

if [ "${1:-}" = "--remove" ]; then
  if [ "$current" = "$without" ]; then
    echo "[skip] строки $TAG в crontab нет"
  else
    printf '%s\n' "$without" | sed '/^$/d' | crontab -
    echo "[ok] строка $TAG снята"
  fi
  exit 0
fi

interval="${1:-5}"
case "$interval" in ''|*[!0-9]*) echo "интервал — целое число минут" >&2; exit 1 ;; esac
[ -x "$WATCHDOG" ] || chmod +x "$WATCHDOG" "$HERE/ensure-tunnel.sh" "$HERE/mcp-jira.sh" 2>/dev/null || true
command -v tsh >/dev/null 2>&1 || echo "[warn] tsh сейчас не в PATH — watchdog не сможет поднять туннель" >&2

cron_path="/usr/local/bin:/usr/bin:/bin"
[ -d /opt/homebrew/bin ] && cron_path="/opt/homebrew/bin:$cron_path"
for c in tsh curl python3; do
  p=$(command -v "$c" 2>/dev/null) || continue
  d=$(dirname "$p")
  case ":$cron_path:" in *":$d:"*) ;; *) cron_path="$d:$cron_path" ;; esac
done

line="*/$interval * * * * PATH=\"$cron_path\" HOME=\"$HOME\" /bin/bash \"$WATCHDOG\" >/dev/null 2>&1 $TAG"
{ printf '%s\n' "$without" | sed '/^$/d'; printf '%s\n' "$line"; } | crontab -
echo "[ok] cron: раз в $interval мин — $WATCHDOG"
echo "     проверка: crontab -l | grep jira-mcp-watchdog"
echo "     лог:      ${JIRA_MCP_STATE:-$HOME/.local/state/jira-mcp}/tunnel.log"
