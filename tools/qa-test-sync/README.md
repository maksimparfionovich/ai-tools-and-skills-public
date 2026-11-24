# qa-test-sync

Узкий мерж PR и сборка образа для ночной задачи, которая актуализирует автотесты под влитые PR сервисов.

## Зачем

Ночная задача (планировщик десктоп-приложения Claude, задача `qa-test-sync-nightly`) сама вливает свои PR в main репозитория тестов. При этом у `gh pr merge` в `.claude/settings.json` стоит правило `ask`. Такое правило спрашивает подтверждение в любом режиме, в том числе в `auto` и `bypass`, а правило `allow` его не перебивает. `merge_pr.py` — единственная разрешённая без кнопки дорога к мержу. Для любого другого `gh pr merge` кнопка остаётся.

## Что проверяет перед мержем

Скрипт отказывает (код выхода 1, JSON с `reason`), если:

- PR не в `repo` из конфига, не открыт, черновик или его база не `base_branch`;
- ветка не начинается с `branch_prefix` или пришла из форка;
- голова PR не совпадает с `--head-sha`, то есть с тем SHA, который проверяли прогоном;
- GitHub не даёт влить PR без конфликтов;
- PR трогает путь из `forbidden_paths`: CI, конфиг Playwright, зависимости, гарды записи в прод, `CLAUDE.md`;
- в добавленных строках есть слово из `forbidden_added_words`: `@smoke`, `test.only`, `networkidle`;
- в удалённых строках есть слово из `forbidden_removed_words`: `@smoke`. Удаление smoke-теста или снятие тега убирает тест из постдеплойного гейта. Под отказ попадает и строка с `@smoke`, которую только переформатировали: такой PR вливает человек.

Мерж — только squash, с `--match-head-commit`.

## Запуск

```bash
python tools/qa-test-sync/merge_pr.py <номер PR> --head-sha <SHA> --dry-run   # только проверки
python tools/qa-test-sync/merge_pr.py <номер PR> --head-sha <SHA>             # проверки и мерж
```

Нужен авторизованный `gh`. Ответ — одна строка JSON: `{"merged": true, "url": …, "mergeCommit": …, "files": […]}` или `{"merged": false, "reason": …}`.

## Сборка образа: build_image.py

Пуш в main репозитория тестов образ не собирает: `build.yml` срабатывает только на теги и ручной запуск. Ночной регресс берёт образ `:latest`, и без сборки влитые тесты ночью не участвуют. На `gh workflow run` стоит правило `ask`. `build_image.py` — единственная разрешённая без кнопки дорога к сборке. Он запускает только `build_workflow` в `repo` на `base_branch`.

```bash
python tools/qa-test-sync/build_image.py --merge-commit <SHA> --dry-run   # только проверки
python tools/qa-test-sync/build_image.py --merge-commit <SHA>             # собрать и дождаться
```

`--merge-commit` — коммит мержа последнего влитого PR задачи, это `mergeCommit` из ответа `merge_pr.py`. Скрипт отказывает, если коммит не принадлежит влитому PR из ветки `branch_prefix`. Отказывает и в трёх случаях со сборкой: она не закончилась за `--timeout-min` (по умолчанию 40), закончилась не `success` или собрала коммит без `--merge-commit`.

Если сборка на `base_branch` уже идёт и в ней есть нужный коммит, скрипт ждёт её. Если идёт более старая сборка, сначала ждёт её конца, иначе она перезапишет `:latest` старыми тестами. Ответ — одна строка JSON: `{"built": true, "run": …, "headSha": …}` или `{"built": false, "reason": …}`.

## Настройки

`config.local.json` рядом со скриптами (пример — `config.example.json`) или путь в `QA_TEST_SYNC_CONFIG`. Ключи: `repo`, `base_branch`, `branch_prefix`, `build_workflow`, `forbidden_paths` (шаблоны `fnmatch`, `*` захватывает и `/`), `forbidden_added_words`, `forbidden_removed_words`.

## Данные задачи

Состояние, вопросы к человеку и журнал запусков ночная задача держит вне инструмента, в своей папке данных; скриптам они не нужны.
