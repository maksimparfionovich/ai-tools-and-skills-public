#!/usr/bin/env bash
# Общие настройки и хелперы для туннеля Teleport к Jira.
# Только функции и переменные — sourced из ensure-tunnel.sh и watchdog.sh.
# Всё, что пишется в stdout вызывающего скрипта, ломает JSON-RPC MCP,
# поэтому здесь любой вывод идёт в stderr и в лог-файл.
#
# Работает в Git Bash на Windows, на Linux и на macOS (bash 3.2+): системные различия
# собраны в функциях ниже (процессы, stat, date, время загрузки, timeout).

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Настройки проекта: адрес Teleport и имя приложения Jira. Файл в git не идёт,
# пример — config.env.example. Переменные окружения сильнее файла.
if [ -f "${JIRA_MCP_CONFIG:-$LIB_DIR/config.env}" ]; then
  # shellcheck source=/dev/null
  . "${JIRA_MCP_CONFIG:-$LIB_DIR/config.env}"
fi

PROXY="${TELEPORT_PROXY:-}"
APP="${JIRA_APP:-}"
PORT="${JIRA_PORT:-8080}"
HEALTH_PATH="${JIRA_HEALTH_PATH:-/status}"

STATE_DIR="${JIRA_MCP_STATE:-$HOME/.local/state/jira-mcp}"
LOG="$STATE_DIR/tunnel.log"
PROXY_OUT="$STATE_DIR/proxy.out.log"
PROXY_ERR="$STATE_DIR/proxy.err.log"
PIDFILE="$STATE_DIR/tunnel.pid"
MARKER="$STATE_DIR/wanted"
LOCKDIR="$STATE_DIR/ensure.lock"

ALLOW_LOGIN="${JIRA_TUNNEL_LOGIN:-1}"        # 1 = можно запускать tsh login (откроет браузер)
LOGIN_TIMEOUT="${JIRA_LOGIN_TIMEOUT:-180}"   # сек, потолок ожидания SSO
WAIT_SECS="${JIRA_TUNNEL_WAIT:-25}"          # сек, ожидание готовности порта
CERT_MARGIN="${JIRA_CERT_MARGIN:-300}"       # сек, перелогин если сертификат истекает раньше
MARKER_TTL="${JIRA_MARKER_TTL:-57600}"       # сек (16 ч), после этого маркер считается просроченным
BOOT_TOLERANCE=120                           # сек, допуск при сравнении времени загрузки

mkdir -p "$STATE_DIR" 2>/dev/null || true

# --- система -----------------------------------------------------------------

is_windows() {
  case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; *) return 1 ;; esac
}

file_size() { wc -c <"$1" 2>/dev/null | tr -d ' ' || echo 0; }

file_mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || date +%s; }

# to_epoch <ISO-8601>: GNU date (Linux, Git Bash), иначе разбор вручную (macOS/BSD date):
# 2026-10-20T21:31:22.123456789+03:00 / …Z → секунды эпохи.
to_epoch() {
  local s="$1" base off sign hh mm e
  date -d "$s" +%s 2>/dev/null && return 0
  case "$s" in *T*) ;; *) return 1 ;; esac
  # дата + время до секунд, без долей и зоны: 2026-10-20T21:31:22
  base="${s%%T*}T$(printf '%s' "${s#*T}" | sed -E 's/^([0-9]{2}:[0-9]{2}:[0-9]{2}).*/\1/')"
  # смещение зоны: +03:00 → +0300; Z или без зоны — UTC
  off="$(printf '%s' "${s#*T}" | sed -nE 's/^[0-9:.]+([+-][0-9]{2}):?([0-9]{2})$/\1\2/p')"
  e=$(date -j -u -f '%Y-%m-%dT%H:%M:%S' "$base" +%s 2>/dev/null) || return 1
  if [ -n "$off" ]; then
    sign="${off:0:1}"; hh=$((10#${off:1:2})); mm=$((10#${off:3:2}))
    if [ "$sign" = "+" ]; then e=$(( e - hh * 3600 - mm * 60 )); else e=$(( e + hh * 3600 + mm * 60 )); fi
  fi
  printf '%s\n' "$e"
}

# run_with_timeout <сек> <команда…>: coreutils timeout / gtimeout (Homebrew), иначе без потолка.
run_with_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$secs" "$@"
  else "$@"
  fi
}

log() {
  local ts; ts=$(date '+%F %T')
  # Простая ротация: лог пишется годами по тику раз в 5 минут, расти бесконечно
  # ему нельзя. Больше 1 МБ — оставляем последние 256 КБ.
  if [ -f "$LOG" ] && [ "$(file_size "$LOG")" -gt 1048576 ]; then
    tail -c 262144 "$LOG" >"$LOG.rotated" 2>/dev/null && mv -f "$LOG.rotated" "$LOG"
  fi
  printf '%s [%s] %s\n' "$ts" "${LOG_TAG:-tunnel}" "$*" >>"$LOG" 2>/dev/null
  printf '[jira-%s] %s\n' "${LOG_TAG:-tunnel}" "$*" >&2
}

# require_config: без адреса Teleport и имени приложения поднимать нечего.
require_config() {
  if [ -z "$PROXY" ] || [ -z "$APP" ]; then
    log "не заданы TELEPORT_PROXY / JIRA_APP — заполните $LIB_DIR/config.env (пример — config.env.example)"
    return 1
  fi
  return 0
}

# --- состояние туннеля -------------------------------------------------------

tunnel_up() {
  local code
  code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$PORT$HEALTH_PATH" 2>/dev/null)
  [ "$code" = "200" ]
}

# pid_alive <pid>: жив ли процесс туннеля (и это именно tsh, а не чужой процесс с тем же PID).
pid_alive() {
  local pid="$1"
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  if is_windows; then
    tasklist //FI "PID eq $pid" //NH 2>/dev/null | grep -qi 'tsh\.exe'
  else
    kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o comm= 2>/dev/null | grep -q 'tsh'
  fi
}

kill_pid() {
  local pid="$1"
  if is_windows; then
    taskkill //PID "$pid" //F >/dev/null 2>&1
  else
    kill "$pid" 2>/dev/null
    sleep 1
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
  fi
  return 0
}

kill_stale_tunnel() {
  [ -f "$PIDFILE" ] || return 0
  local pid; pid=$(tr -d ' \r\n' <"$PIDFILE" 2>/dev/null)
  if pid_alive "$pid"; then
    log "порт мёртв, но процесс $pid жив — снимаю зависший туннель"
    kill_pid "$pid"
  fi
  rm -f "$PIDFILE"
}

# --- сертификат Teleport -----------------------------------------------------

cert_seconds_left() {
  local vu exp now
  vu=$(tsh status --format=json 2>/dev/null \
       | grep -o '"valid_until"[[:space:]]*:[[:space:]]*"[^"]*"' \
       | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
  [ -n "$vu" ] || return 1
  exp=$(to_epoch "$vu") || return 1
  now=$(date +%s)
  printf '%s\n' "$(( exp - now ))"
}

# --- маркер «Jira сейчас используется» ---------------------------------------

# boot_epoch: время загрузки системы, сек эпохи. Linux и Git Bash — /proc/uptime,
# macOS — sysctl kern.boottime ("{ sec = 1729400000, usec = … } …").
boot_epoch() {
  local up now sec
  local upf="${JIRA_UPTIME_FILE:-/proc/uptime}"   # переопределяется только в тестах
  if [ -r "$upf" ]; then
    up=$(cut -d' ' -f1 "$upf" 2>/dev/null)
    [ -n "$up" ] || return 1
    up=${up%.*}
    now=$(date +%s)
    printf '%s\n' "$(( now - up ))"
    return 0
  fi
  # в строке два поля: sec и usec — берём именно первое, sec
  sec=$(sysctl -n kern.boottime 2>/dev/null | sed -nE 's/^[{ ]*sec = ([0-9]+).*/\1/p')
  [ -n "$sec" ] || return 1
  printf '%s\n' "$sec"
}

mark_wanted() {
  local boot; boot=$(boot_epoch) || boot=0
  printf '%s %s\n' "$boot" "$(date +%s)" >"$MARKER" 2>/dev/null || true
}

# 0 = туннель нужен (маркер свежий), 1 = не нужен / просрочен
marker_is_fresh() {
  [ -f "$MARKER" ] || return 1
  local mboot mwritten boot now diff
  read -r mboot mwritten <"$MARKER" 2>/dev/null || return 1
  case "$mboot$mwritten" in ''|*[!0-9]*) return 1 ;; esac

  boot=$(boot_epoch) || boot=0
  if [ "$boot" != 0 ] && [ "$mboot" != 0 ]; then
    diff=$(( boot - mboot )); [ "$diff" -lt 0 ] && diff=$(( -diff ))
    if [ "$diff" -gt "$BOOT_TOLERANCE" ]; then
      log "маркер от прошлой загрузки системы — считаю просроченным"
      return 1
    fi
  fi

  now=$(date +%s)
  if [ "$(( now - mwritten ))" -gt "$MARKER_TTL" ]; then
    log "маркер старше $MARKER_TTL с — считаю просроченным"
    return 1
  fi
  return 0
}

# --- блокировка (wrapper и watchdog могут стартовать одновременно) -----------

acquire_lock() {
  local i=0 age
  while [ "$i" -lt 60 ]; do
    if mkdir "$LOCKDIR" 2>/dev/null; then
      trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT
      return 0
    fi
    # снять залипшую блокировку старше 5 минут
    age=$(( $(date +%s) - $(file_mtime "$LOCKDIR") ))
    if [ "$age" -gt 300 ]; then
      log "снимаю залипшую блокировку (${age}с)"
      rmdir "$LOCKDIR" 2>/dev/null || true
      continue
    fi
    sleep 1; i=$(( i + 1 ))
  done
  log "не дождался блокировки $LOCKDIR"
  return 1
}
