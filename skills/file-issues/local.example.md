# Факты проекта для file-issues (пример)

Скопируйте в `local.md` рядом и заполните своими данными. `local.md` в git не идёт.

## Куда заводить

- Организация `example-org`, доска **Team board** — https://github.com/orgs/example-org/projects/1
  (настройки скрипта — `tools/gh-issues/config.local.json`).
- Задача по коду — issue в репозитории сервиса + карточка на доске.
- Инфраструктура — `example-org/infra-backlog`.
- Статусы доски: Todo → In progress → Done. Новая задача — Todo.

## Приоритет

Поле организации **Priority**: Urgent / High / Medium / Low.

| Приоритет | Когда |
| --- | --- |
| Urgent | Прод лежит или теряет данные сейчас |
| High | Дефект безопасности или потеря данных при условиях; блокер релиза |
| Medium | Функциональный дефект с обходом |
| Low | Тексты, косметика, техдолг |

## Люди (логины GitHub)

| Логин | Кто | Направления |
| --- | --- | --- |
| `alice` | Алиса, бэкенд | backend, worker |
| `bob` | Боб, фронтенд | frontend |
| `carol` | Кэрол, девопс | infra-backlog |

## Исполнитель для легаси без ветки

| Репозиторий | Предложить |
| --- | --- |
| backend | `alice` |
| frontend | `bob` |
| infra-backlog | `carol` |
