# jira-mcp — Jira за Teleport для Claude

Подключение Claude к self-hosted Jira (Server / Data Center), которая опубликована как приложение
[Teleport](https://goteleport.com) (`tsh apps`). Сам MCP-сервер — community-проект
[`sooperset/mcp-atlassian`](https://github.com/sooperset/mcp-atlassian) с авторизацией по Personal
Access Token. Здесь — обёртка, которая **сама поднимает туннель Teleport и держит его живым**, так
что Jira в Claude работает без ручного `tsh proxy app` в отдельном окне.

Официальный Atlassian Remote MCP (`mcp.atlassian.com`) сюда не подходит — он только для Jira Cloud
(`*.atlassian.net`).

Два слоя доступа:
1. **Teleport** (сеть, mTLS) — туннель `tsh proxy app` пробрасывает Jira на `localhost:8080`.
2. **Jira PAT** — авторизация в самой Jira, заголовок `Authorization: Bearer <PAT>`.

## Установка

Нужны `tsh`, `curl`, Python 3.10+ и bash (на Windows — Git Bash).

```bash
python3 -m pip install mcp-atlassian            # на Windows: python -m pip install mcp-atlassian
cp tools/jira-mcp/config.env.example tools/jira-mcp/config.env   # вписать TELEPORT_PROXY и JIRA_APP
chmod +x tools/jira-mcp/*.sh                    # macOS / Linux
```

Имя приложения Jira в Teleport — из `tsh apps ls`. `config.env` в git не идёт.

PAT создаётся в Jira: аватар → Profile → Personal Access Tokens → Create token. Держите его в
переменной окружения `JIRA_PAT`, а не в файлах:

- macOS / Linux: `export JIRA_PAT='...'` в `~/.zshrc` / `~/.bashrc`;
- Windows: `setx JIRA_PAT "..."` и полный перезапуск приложения (GUI не наследует `export` из Git Bash).

## Подключение к Claude Code

`.mcp.json` в корне проекта (Claude Code подхватывает его сам). Токен подставляется из окружения:
`${JIRA_PAT}`.

macOS / Linux:

```json
{
  "mcpServers": {
    "jira": {
      "command": "bash",
      "args": ["/полный/путь/к/проекту/tools/jira-mcp/mcp-jira.sh"],
      "env": {
        "JIRA_URL": "http://localhost:8080",
        "JIRA_PERSONAL_TOKEN": "${JIRA_PAT}",
        "JIRA_SSL_VERIFY": "false",
        "READ_ONLY_MODE": "true"
      }
    }
  }
}
```

Windows: `"command": "C:\\Program Files\\Git\\bin\\bash.exe"`, в `args` — путь в стиле Git Bash:
`"/c/путь/к/проекту/tools/jira-mcp/mcp-jira.sh"`.

Холодный старт занимает ~15 секунд (`tsh apps login` и запуск прокси), а при истёкшем сертификате —
ещё и ожидание входа в браузере. Стандартных 30 секунд на старт MCP-сервера может не хватить:
поднимите `MCP_TIMEOUT` (мс), например `MCP_TIMEOUT=240000 claude` или `"env": {"MCP_TIMEOUT":
"240000"}` в `.claude/settings.json`.

## Каждый рабочий сеанс

**Ничего делать не нужно.** Туннель поднимается сам при старте MCP-сервера `jira` и лечится, если
упал. От вас остаётся вход через SSO в браузере, когда истекает сертификат Teleport: браузер
открывается сам (`tsh login`), набирать ничего не надо.

Проверка туннеля вручную: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/status` → `200`.

## Автоподъём туннеля

| Файл | Роль |
| ---- | ---- |
| `tunnel-lib.sh` | общие настройки и хелперы: проверка порта, срок сертификата, маркер, блокировка, различия Windows / macOS / Linux |
| `ensure-tunnel.sh` | идемпотентно доводит туннель до рабочего состояния: порт жив — сразу выходит (в Git Bash на Windows это ~1–2 с: запуск bash и curl); иначе `tsh login` (если сертификат истёк) → `tsh apps login` → отсоединённый `tsh proxy app` → ожидание HTTP 200 |
| `mcp-jira.sh` | обёртка для `.mcp.json`: вызывает `ensure-tunnel.sh`, затем через `exec` **становится** сервером `mcp-atlassian` (лишнего процесса не остаётся) |
| `watchdog.sh` | только **лечит** уже поднятый туннель, сам не поднимает; тик раз в 5 минут |
| `install-watchdog.sh` | macOS / Linux: ставит тик в `cron` (`--remove` — снять) |
| `install-watchdog.ps1` | Windows: регистрирует задачу Планировщика (`-Remove` — снять); генерирует скрытый VBS-запуск, иначе Git Bash мигал бы окном раз в 5 минут |

Поставить автолечение (один раз):

```bash
bash tools/jira-mcp/install-watchdog.sh                                   # macOS / Linux
powershell -ExecutionPolicy Bypass -File tools/jira-mcp/install-watchdog.ps1   # Windows
```

**Почему watchdog не поднимает туннель сам.** `ensure-tunnel.sh` при успехе пишет маркер `wanted` с
временем загрузки системы. Watchdog без свежего маркера выходит немедленно: до первого обращения к
Jira не запускается ничего, туннель не висит «на всякий случай». Маркер считается просроченным после
перезагрузки или через 16 часов (`JIRA_MARKER_TTL`) — иначе туннель поднимался бы вечно, включая ночь.

**Что это стоит по памяти.** Сам `tsh proxy app` — ~57 МБ (столько же, сколько при ручном запуске).
Обёртка не добавляет ничего (`exec`), watchdog не держит резидентного процесса: cron или Планировщик
уже работают, каждый тик живёт доли секунды.

Состояние — в `~/.local/state/jira-mcp/` (`JIRA_MCP_STATE`): `tunnel.log` (общий лог, ротация на
1 МБ), `proxy.out.log` / `proxy.err.log` (вывод `tsh proxy`), `tunnel.pid`, `wanted` (маркер),
`ensure.lock`.

Ручные команды:

```bash
tools/jira-mcp/ensure-tunnel.sh                 # поднять/проверить туннель
tools/jira-mcp/watchdog.sh                      # прогнать тик вручную
tail -f ~/.local/state/jira-mcp/tunnel.log      # смотреть лог
```

Переменные-ручки (в `config.env` или окружении; окружение сильнее): `TELEPORT_PROXY`, `JIRA_APP`
(обязательны), `JIRA_PORT`, `JIRA_HEALTH_PATH`, `JIRA_TUNNEL_LOGIN` (`0` — не открывать браузер,
только сообщить), `JIRA_LOGIN_TIMEOUT`, `JIRA_TUNNEL_WAIT`, `JIRA_CERT_MARGIN`, `JIRA_MARKER_TTL`,
`JIRA_MCP_PYTHON`, `JIRA_MCP_STATE`, `JIRA_MCP_CONFIG` (другой путь к `config.env`).

**Как это устроено на разных системах.** Туннель должен пережить закрытие терминала и конец сессии
Claude: на Windows его запускает PowerShell `Start-Process`, на macOS и Linux — `nohup` в фоне; вывод
`tsh` уходит в файлы, а не в pipe вызывающего (иначе клиент MCP ждал бы, пока туннель умрёт). Живой ли
процесс — `tasklist` на Windows, `kill -0` + `ps` на macOS/Linux. Время загрузки — `/proc/uptime`
(Linux, Git Bash) или `sysctl kern.boottime` (macOS). Срок сертификата — `date -d` или разбор ISO-даты
для BSD `date`. Потолок ожидания SSO — `timeout` (на macOS — `gtimeout` из Homebrew coreutils, если
есть; без него ожидание без потолка).

## Claude Desktop

В конфиге приложения (`claude_desktop_config.json`) сервер добавляется той же **обёрткой**, а не
`python` напрямую: command — `bash` (Windows — `C:\Program Files\Git\bin\bash.exe`), args — путь к
`mcp-jira.sh`, env — как в `.mcp.json`. Минус: подстановки `${JIRA_PAT}` в этом конфиге нет, PAT
пришлось бы положить в файл открытым текстом. Обходится доработкой обёртки (брать токен из
переменной окружения) — по желанию.

## Заметки

- `READ_ONLY_MODE=true` — только чтение: задачи, комментарии, JQL, связи, поля. Инструменты записи
  (создание и правка задач, комментарии) отключены. Разрешить запись — убрать переменную или `false`.
- `JIRA_SSL_VERIFY=false` — потому что ходим на `http://localhost:8080`: TLS терминирует туннель.
- **Прикреплённые PR (dev-панель Bitbucket/GitHub)** сервер штатно не отдаёт — это отдельный Jira
  `dev-status` API.

## Confluence через тот же сервер

`mcp-atlassian` — один сервер на Jira и Confluence. Чтобы добавить Confluence, нужны свой PAT в
Confluence и второй туннель:

1. PAT: `https://<confluence>/plugins/personalaccesstokens/usertokens.action` → Create token.
2. Туннель рядом с jira-туннелем: `tsh apps login <confluence-app>` и
   `tsh proxy app <confluence-app> --port 8081` (порт 8080 занят Jira). Проверка:
   `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8081/rest/api/user/current` → `200`.
3. `export CONFLUENCE_PAT='...'` и в `env` блока `jira` в `.mcp.json`:
   `"CONFLUENCE_URL": "http://localhost:8081"`, `"CONFLUENCE_PERSONAL_TOKEN": "${CONFLUENCE_PAT}"`,
   `"CONFLUENCE_SSL_VERIFY": "false"`.

`"TOOLSETS": "all"` в `env` включает все наборы инструментов `mcp-atlassian`; `READ_ONLY_MODE=true`
сильнее и всё равно убирает запись. Туннель Confluence скрипты пока не ведут — он ручной.

## Проверено

Windows 11 (Git Bash): подъём туннеля и лечение watchdog на живом Teleport. Linux (Ubuntu в WSL 2):
полный цикл с поддельным `tsh` и cron. macOS — не проверялся. Подробнее — раздел «Проверено» в
README репозитория.
