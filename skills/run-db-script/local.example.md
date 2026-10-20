# Факты проекта для run-db-script (пример)

Скопируйте в `local.md` рядом со `SKILL.md` и опишите свой проект. Файл в git не идёт.

- Доступ к кластеру стендов: `kubectl --context stands …`; сессия — `aws sso login` (живёт 12 ч).
- Namespace = имя стенда (`dev1`–`dev3`, `qa1`–`qa3`). Под бэкенда: `backend-<hash>-<hash>`.
- Свои стенды — `qa1`, `qa2`; остальные общие, только с согласия.
- Скрипты — `src/scripts/*.ts` в клоне `~/work/repos/backend`; реиндекс — `reindex-search.ts`.
- Проверка реиндекса: `https://cms-<стенд>.example.com/api/graphql`, запрос
  `SearchArticles(query:"bitcoin", locale:"en"){ totalDocs }` до и после.
- Read-only доступ к базе стенда: нет.
