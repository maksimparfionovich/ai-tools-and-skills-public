#!/usr/bin/env bash
# Перепроверка карты хостов: один GET на каждый хост, без записи куда-либо.
# Печатает "host | http_code redirect content-type" и сохраняет копию во временную папку (или в $OUT).
#
#   bash probe-hosts.sh                # все группы из файла хостов
#   bash probe-hosts.sh prod           # только группа [prod]
#   bash probe-hosts.sh stands dead    # несколько групп
#   HOSTS_FILE=~/my-hosts.txt OUT=/tmp/probe.txt bash probe-hosts.sh
#
# Файл хостов — HOSTS_FILE или hosts.local.txt рядом со скриптом (пример — hosts.example.txt):
# группы в квадратных скобках, по хосту на строку, # — комментарий.
# Код 000 = не резолвится или таймаут (текст ошибки curl — в строке). 404 на корне у API — норма.
set -u
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
TIMEOUT=${TIMEOUT:-20}
PAR=${PAR:-12}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
HOSTS_FILE=${HOSTS_FILE:-$HERE/hosts.local.txt}

if [ ! -f "$HOSTS_FILE" ]; then
  echo "нет файла хостов $HOSTS_FILE — скопируйте hosts.example.txt в hosts.local.txt и впишите свои" >&2
  exit 2
fi

# hosts_of <группа>: хосты группы; без групп в аргументах — все
hosts_of() {
  awk -v want="$1" '
    /^[[:space:]]*(#|$)/ { next }
    /^\[.*\][[:space:]]*$/ { g = $0; gsub(/^\[|\][[:space:]]*$/, "", g); next }
    want == "" || g == want { gsub(/[[:space:]]/, ""); print }
  ' "$HOSTS_FILE" | tr -d '\r'
}

probe() {
  local h="$1"
  local r
  r=$(curl -sS -o /dev/null --max-time "$TIMEOUT" -A "$UA" \
        -w "%{http_code} %{redirect_url} %{content_type}" "https://$h/" 2>&1 | tr '\n' ' ')
  echo "$h | $r"
}
export -f probe
export UA TIMEOUT

if [ $# -eq 0 ]; then
  hosts=$(hosts_of "" | sort -u)
  groups="все"
else
  hosts=$(for g in "$@"; do hosts_of "$g"; done | sort -u)
  groups="$*"
fi
if [ -z "$hosts" ]; then
  echo "в $HOSTS_FILE нет хостов для групп: $groups" >&2
  exit 2
fi

out="${OUT:-${TMPDIR:-${TMP:-/tmp}}/stand-topology-probe-$(date +%F).txt}"
echo "$hosts" | xargs -P "$PAR" -I{} bash -c 'probe {}' | sort | tee "$out"
echo
echo "# сохранено: $out  (групп: $groups; хостов: $(echo "$hosts" | wc -l | tr -d ' '))"
