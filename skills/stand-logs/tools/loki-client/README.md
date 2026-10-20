# loki-client (`lokilogs`)

Тонкий клиент к [Grafana Loki](https://grafana.com/oss/loki/) по mTLS: логи подов по стенду
(namespace), сервису, времени и фильтру. Им пользуется скил `stand-logs`, но он работает и сам по
себе.

Почему свой бинарь, а не `curl`: системный `curl` на macOS (LibreSSL) не умеет клиентские
сертификаты Ed25519, а Go умеет. Внешних зависимостей нет.

## Сборка

Нужен Go 1.26+ только на машине сборки:

```bash
cd skills/stand-logs/tools/loki-client
go build -o lokilogs .          # macOS / Linux
go build -o lokilogs.exe .      # Windows
go test ./...                   # проверка без сети
```

Собрать под другую систему: `GOOS=darwin GOARCH=arm64 go build -o lokilogs .` (Mac на Apple
Silicon), `GOOS=linux GOARCH=amd64 …`. Бинарь в git не хранится.

## Настройка

**Доступ** — переменные окружения, обычно в файле `~/.config/loki-client/env` (`chmod 600`), который
подключается перед запуском: `. ~/.config/loki-client/env`.

```bash
export LOKI_URL=https://loki.example.com
export LOKI_CERT=$HOME/.config/loki-client/client.pem
export LOKI_KEY=$HOME/.config/loki-client/client.key
# export LOKI_ORG=tenant-id            # X-Scope-OrgID, если Loki мультитенантный
# export LOKI_SERVER_CA=$HOME/.config/loki-client/ca.pem   # включает проверку сертификата сервера
# export LOKI_PRESETS=$HOME/.config/loki-client/presets.json
```

Сертификаты выдаёт тот, кто администрирует Loki, лично вам; в репозиторий они не входят. Без
`LOKI_SERVER_CA` сертификат сервера не проверяется (для самоподписанного Loki), клиентская
аутентификация по mTLS при этом полная.

**Пресеты** — данные вашей инфраструктуры: алиасы стендов, группы сервисов, какие сервисы смотреть
по умолчанию в каком namespace. Файл — `presets.local.json` рядом с бинарём или путь в
`LOKI_PRESETS`; пример — `presets.example.json`. Без пресетов `--stand` — это просто namespace, а
`--app auto` смотрит весь namespace.

| Поле | Что задаёт |
| --- | --- |
| `stand_aliases` | Короткое имя → список namespace: `"prod": ["shop-prod-eu", "shop-prod-us"]` |
| `app_groups` | Группа → список сервисов (лейбл `app`); `null` — весь namespace |
| `namespace_groups` | Правила для `--app auto`, по порядку: `names` (точные имена) или `match` (регулярка) → `group` |
| `syslog_scale_apps` | Сервисы со шкалой уровней ×10000 (`err=30000`, меньше = серьёзнее); сравнение по префиксу имени |

`lokilogs presets` показывает, какой файл подхватился и что в нём.

## Команды

```bash
lokilogs query  --stand qa1 [--app auto] [--since 1h] [--grep RE] [--level error] [--limit 200]
lokilogs query  --query '{namespace="qa1"} |~ "(?i)timeout"' --since 30m
lokilogs labels [--name namespace|app]
lokilogs presets
```

Флаги `query`: `--stand` (namespace или алиас, через запятую), `--app` (`auto`, `all`, группа или
имена), `--since` (`30m`, `2h`, `1d`), `--grep` (регулярка без учёта регистра), `--level`, `--limit`
(до 5000), `--direction backward|forward`, `--raw` (не распаковывать JSON-конверт), `--json` (сырой
ответ Loki), `--quiet` (без шапки), `--utc`. Шапка (LogQL, окно, число строк) — в stderr, строки
логов — в stdout.

**Уровни.** `--level` понимает три шкалы: pino (`50=error`, `40=warn`, `30=info`, иногда ×1000),
RFC 5424 (`3=error`, `4=warning`, `6=info`) и syslog-подобную ×10000 (`30000=err`, `40000=warning`,
`60000=info` — у сервисов из `syslog_scale_apps`). Числа пересекаются, поэтому фильтр и метка в
выводе выбираются по имени сервиса. Уровень ищется и в корне JSON-конверта (`level`), и во вложенном
объекте (`message_level`).

**Вывод.** JSON-конверт лога (Vector/Promtail: `message`, `app_name`, `kubernetes.pod_name`)
распаковывается: `время [сервис/под] сообщение`. Если `message` — объект, он сводится к строке:
`[warn]  queue full  component=cache  statusCode=503`.

## Запасной клиент без Go — `lq.py`

Только стандартная библиотека Python 3, те же переменные окружения. Флаги другие: сырой LogQL и
абсолютные границы окна в UTC.

```bash
python3 lq.py --query '{namespace="qa1", app=~"frontend|backend"}' \
  --start 2026-10-20T09:00:00Z --end 2026-10-20T10:00:00Z --limit 200
```

`--instant` — точечный запрос (момент — `--end`), `--raw` — не распаковывать конверт.

## Происхождение

Клиент написан коллегой (запрос по одному namespace, фиксированный список сервисов, распаковка
строкового `message`). Доработано у нас:

- несколько namespace в одном запросе и алиасы стендов (`prod` → все прод-namespace сразу);
- `--app auto`: набор сервисов подбирается по namespace; группы сервисов;
- разные шкалы уровней по имени сервиса — и в фильтре `--level`, и в метке вывода; поиск уровня во
  вложенном `message_level`;
- читаемый вывод вложенных JSON-сообщений вместо сырого объекта;
- пресеты вынесены из кода в JSON (`presets.local.json`), команда `presets`, тесты `go test`;
- `lq.py`: поддержка `LOKI_ORG`, без адреса Loki по умолчанию.
