# Факты проекта для stand-logs (пример)

Скопируйте в `local.md` рядом со `SKILL.md` и опишите свой проект. Файл в git не идёт.

## Доступ

- Env: `~/.config/loki-client/env` (`LOKI_URL=https://loki.example.com`, сертификаты рядом). Срок
  сертификата — до 2027-06-30, потом просить новый у администратора Loki.
- Бинарь: `.claude/skills/stand-logs/tools/loki-client/lokilogs`, пресеты — `presets.local.json`
  рядом с ним.

## Namespace

- Стенды: `dev1`–`dev3`, `qa1`–`qa3` (namespace = имя стенда), `demo`.
- Прод: `shop-prod-eu` и `shop-prod-us` — алиас `prod` покрывает оба; `prod-eu`/`prod-us` — по одному.
- Ингресс: `ingress-nginx` (алиас `ingress`) — объём большой, только с узким `--grep`; сшивается с
  логом пода по `request_id`.

## Сервисы и уровни

- `frontend` пишет шкалу ×10000 (в `syslog_scale_apps`), `backend` — pino.
- У `backend` JSON с полем `level` пишет только часть компонентов, остальное — текст: для ошибок
  `--grep 'error|fatal'`, а не `--level`.

## Фон, который не считается инцидентом

- `frontend`: `Page not found`, `statusCode: 404` — боты и старые ссылки; инцидент — всплеск 5xx.
- Стенды: `Error verifying mail transport` — почта на стендах не настроена.
- База: `WAL file not found in the recovery object store` — штатная проверка бэкапов.
