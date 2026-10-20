#!/usr/bin/env bash
# Только ЛЕЧИТ уже поднятый туннель — сам по своей инициативе не поднимает.
# Запускается тиком планировщика раз в 5 мин (Windows — Планировщик задач, macOS/Linux — cron),
# резидентного процесса нет.
#
# Логика:
#   маркера нет            -> выход 0 (Jira в этой сессии не использовали)
#   маркер просрочен       -> удалить маркер, выход 0 (не поднимать после перезагрузки/суток)
#   маркер свежий + порт жив  -> выход 0
#   маркер свежий + порт мёртв -> ensure-tunnel.sh (переживает и падение tsh proxy,
#                                 и 12-часовую границу сертификата)
set -uo pipefail

LOG_TAG=watchdog
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tunnel-lib.sh
. "$HERE/tunnel-lib.sh"

if [ ! -f "$MARKER" ]; then
  exit 0
fi

if ! marker_is_fresh; then
  rm -f "$MARKER"
  exit 0
fi

if tunnel_up; then
  exit 0
fi

log "туннель нужен, но порт $PORT мёртв — поднимаю заново"
exec "$HERE/ensure-tunnel.sh"
