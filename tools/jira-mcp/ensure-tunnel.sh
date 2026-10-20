#!/usr/bin/env bash
# Идемпотентно гарантирует, что туннель Teleport к Jira слушает localhost:$PORT.
# Если туннель уже жив — только обновляет маркер и сразу выходит (быстрый путь).
# Иначе: при необходимости tsh login (браузер) -> tsh apps login -> запуск
# отсоединённого `tsh proxy app` -> ожидание готовности порта.
#
# Коды выхода: 0 — туннель готов, 1 — не удалось поднять.
# stdout всегда пустой (используется как обёртка для stdio-MCP).
set -uo pipefail

LOG_TAG=ensure
# shellcheck source=tunnel-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tunnel-lib.sh"

ensure_login() {
  local left
  left=$(cert_seconds_left) || left=""
  if [ -z "$left" ] || [ "$left" -lt "$CERT_MARGIN" ]; then
    if [ "$ALLOW_LOGIN" != "1" ]; then
      log "сертификат Teleport истёк/отсутствует, нужен: tsh login --proxy=$PROXY (автологин выключен)"
      return 1
    fi
    log "сертификат Teleport истёк/отсутствует — запускаю tsh login, откроется браузер"
    if ! run_with_timeout "$LOGIN_TIMEOUT" tsh login --proxy="$PROXY" >>"$LOG" 2>&1; then
      log "tsh login не прошёл (отказ или таймаут ${LOGIN_TIMEOUT}с)"
      return 1
    fi
    log "tsh login выполнен"
  fi

  if ! tsh apps login "$APP" >>"$LOG" 2>&1; then
    log "tsh apps login $APP не прошёл"
    return 1
  fi
  return 0
}

# Процесс туннеля должен жить независимо от вызывающей оболочки (переживать и закрытие
# терминала, и завершение сессии Claude Code). stdout/stderr туннеля уходят в ФАЙЛЫ, stdin —
# из /dev/null: долгоживущий процесс не должен наследовать pipe вызывающего, иначе тот, кто
# читает этот pipe (MCP-клиент, подстановка $( )), висел бы до смерти туннеля.
spawn_tunnel_windows() {
  local tsh_win out_win err_win pid_win
  tsh_win=$(cygpath -w "$(command -v tsh)")
  out_win=$(cygpath -w "$PROXY_OUT")
  err_win=$(cygpath -w "$PROXY_ERR")
  pid_win=$(cygpath -w "$PIDFILE")
  # Start-Process отвязывает процесс от Git Bash; PID пишет сам PowerShell, а не подстановка
  # команды $( ) — иначе подстановка ждала бы унаследованный pipe.
  MSYS_NO_PATHCONV=1 powershell.exe -NoProfile -NonInteractive -InputFormat None -Command \
        "(Start-Process -FilePath '$tsh_win' -ArgumentList 'proxy','app','$APP','--port','$PORT' -WindowStyle Hidden -RedirectStandardOutput '$out_win' -RedirectStandardError '$err_win' -PassThru).Id | Set-Content -Path '$pid_win' -Encoding ASCII" \
        >>"$LOG" 2>&1 </dev/null
}

spawn_tunnel_posix() {
  # nohup: SIGHUP закрытого терминала процесс не заденет; в неинтерактивном скрипте фоновый
  # процесс не входит в job control, а `nohup` делает exec — поэтому $! — это PID самого tsh.
  nohup tsh proxy app "$APP" --port "$PORT" >"$PROXY_OUT" 2>"$PROXY_ERR" </dev/null &
  printf '%s\n' "$!" >"$PIDFILE"
  disown 2>/dev/null || true
}

start_tunnel() {
  local pid
  command -v tsh >/dev/null 2>&1 || { log "tsh не найден в PATH"; return 1; }
  rm -f "$PIDFILE"

  if is_windows; then
    spawn_tunnel_windows
  else
    spawn_tunnel_posix
  fi

  pid=$(tr -d ' \r\n' <"$PIDFILE" 2>/dev/null)
  case "${pid:-}" in
    ''|*[!0-9]*)
      log "не удалось запустить tsh proxy app (см. $PROXY_ERR)"
      return 1
      ;;
  esac
  log "туннель запущен, pid $pid, порт $PORT"

  local i=0
  while [ "$i" -lt "$WAIT_SECS" ]; do
    tunnel_up && { log "туннель готов (${i}с)"; return 0; }
    pid_alive "$pid" || { log "процесс туннеля $pid умер сразу, см. $PROXY_ERR"; return 1; }
    sleep 1; i=$(( i + 1 ))
  done
  log "туннель не ответил 200 за ${WAIT_SECS}с"
  return 1
}

main() {
  if tunnel_up; then
    mark_wanted
    exit 0
  fi

  require_config || exit 1
  acquire_lock || exit 1
  # пока ждали блокировку, туннель мог подняться другим процессом
  if tunnel_up; then
    mark_wanted
    exit 0
  fi

  kill_stale_tunnel
  ensure_login || exit 1
  if start_tunnel; then
    mark_wanted
    exit 0
  fi
  exit 1
}

main
