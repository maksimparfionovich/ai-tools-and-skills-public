#!/usr/bin/env python3
"""Сид меню CMS на Payload: снять меню с прода, проверить данные, сравнить с сидом в репозитории,
сверить стенд с сидом.

    python3 tools/menu-seed/menu_seed.py refresh                 # снять прод, отчёт, диф с origin/main — ничего не пишет
    python3 tools/menu-seed/menu_seed.py refresh --write --branch chore/x   # то же + записать сид (репозиторий CMS на chore/x)
    python3 tools/menu-seed/menu_seed.py verify --stand qa2      # сверить меню всех локалей стенда с сидом из origin/main
    python3 tools/menu-seed/menu_seed.py verify --stand qa2 --locale en

Только стандартная библиотека. Чтение прода и стенда — анонимный GraphQL, то есть только активные
пункты (`isActive: true`); сид тоже содержит только активные, поэтому сравнение честное.
Адреса, локали и путь к сиду — config.local.json рядом со скриптом (или файл из MENU_SEED_CONFIG /
--config); пример — config.example.json. Подробности и ловушки — README.md рядом.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

CONFIG_DEFAULTS = {
    "prod_graphql": "",                       # GraphQL прода, например https://cms.example.com/api/graphql
    "stand_graphql": "",                      # шаблон для стенда, {stand} подставляется
    "repo": "",                               # рабочая копия репозитория CMS (с сидом)
    "seed_path": "src/scripts/data/menu-seed.json",
    "locales": ["en"],                        # в порядке ключей сида
    "copy_from": {},                          # локаль без меню на проде -> откуда взять структуру
    "user_agent": "curl/8.9.1",               # дефолтный `Python-urllib` часть WAF режет 403
    "category_from_graphql": {"mobile_left": "mobile-left", "mobile_right": "mobile-right"},
}

# Заполняются из настроек в load_config().
LOCALES: list[str] = []
COPY_FROM: dict[str, str] = {}
PROD_GRAPHQL = ""
STAND_GRAPHQL = ""
SEED_PATH = ""
DEFAULT_REPO = ""
USER_AGENT = ""
# GraphQL отдаёт enum без дефисов; в коллекции и в сиде категории с дефисом.
CATEGORY_FROM_GRAPHQL: dict[str, str] = {}


def load_config(path: str | None) -> None:
    global LOCALES, COPY_FROM, PROD_GRAPHQL, STAND_GRAPHQL, SEED_PATH, DEFAULT_REPO, USER_AGENT, CATEGORY_FROM_GRAPHQL
    path = path or os.environ.get("MENU_SEED_CONFIG") or str(Path(__file__).with_name("config.local.json"))
    cfg = dict(CONFIG_DEFAULTS)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cfg.update({k: v for k, v in json.load(fh).items() if k in CONFIG_DEFAULTS})
        except (OSError, ValueError) as exc:
            out(f"не прочитал настройки {path}: {exc}")
            sys.exit(2)
    LOCALES = list(cfg["locales"])
    COPY_FROM = dict(cfg["copy_from"])
    PROD_GRAPHQL = cfg["prod_graphql"]
    STAND_GRAPHQL = cfg["stand_graphql"]
    SEED_PATH = cfg["seed_path"]
    DEFAULT_REPO = os.path.expanduser(cfg["repo"]) if cfg["repo"] else ""
    USER_AGENT = cfg["user_agent"]
    CATEGORY_FROM_GRAPHQL = dict(cfg["category_from_graphql"])

QUERY = (
    'query{ Menus(where:{parent:{equals:null}}, sort:"position", limit:200, locale: %s){ docs{ '
    "id title link category position openInNewTab nofollow "
    'children(sort:"position"){ docs{ id title link position openInNewTab nofollow } } } } }'
)


def out(*parts: object) -> None:
    print(*parts, flush=True)


def fetch_menus(endpoint: str, locale: str) -> list[dict]:
    """Корневые пункты одной локали с детьми. Явный User-Agent: дефолтный `Python-urllib` часть WAF режет 403."""
    req = urllib.request.Request(
        f"{endpoint}?locale={locale}",
        data=json.dumps({"query": QUERY % locale}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise RuntimeError(f"{locale}: GraphQL errors: {body['errors']}")
    return body["data"]["Menus"]["docs"]


def to_seed(docs: list[dict]) -> list[dict]:
    """Ответ GraphQL -> форма сида. Позиции — как на проде, дыры не закрываем (это отключённые пункты)."""
    def node(src: dict, category: str) -> dict:
        return {
            "title": src["title"],
            "link": src["link"],
            "category": category,
            "position": src["position"],
            "openInNewTab": src["openInNewTab"],
            "nofollow": src["nofollow"],
        }

    result = []
    for item in sorted(docs, key=lambda d: (CATEGORY_FROM_GRAPHQL.get(d["category"], d["category"]), d["position"])):
        category = CATEGORY_FROM_GRAPHQL.get(item["category"], item["category"])
        entry = node(item, category)
        entry["children"] = [node(c, category) for c in (item.get("children") or {}).get("docs", [])]
        result.append(entry)
    return result


def count(tree: list[dict]) -> tuple[int, int]:
    return len(tree), len(tree) + sum(len(i["children"]) for i in tree)


def quality_issues(locale: str, tree: list[dict]) -> list[str]:
    """То, что на проде уже встречалось и что сид унаследовал бы как есть."""
    issues = []
    roots = collections.Counter()
    for item in tree:
        roots[(item["category"], item["position"])] += 1
        for n in [item] + item["children"]:
            link = n["link"]
            if n["title"] != n["title"].strip():
                issues.append(f"{locale} {item['category']}: пробел по краям заголовка {n['title']!r}")
            if link and link.startswith("http://"):
                issues.append(f"{locale} {item['category']}: http:// в {n['title']!r} -> {link}")
            if link and not (link.startswith("/") or link.startswith("https://")):
                issues.append(f"{locale} {item['category']}: в link не URL {n['title']!r} -> {link!r}")
        for pos, k in collections.Counter(c["position"] for c in item["children"]).items():
            if k > 1:
                names = [c["title"] for c in item["children"] if c["position"] == pos]
                issues.append(f"{locale} {item['category']} / {item['title']}: дубль position {pos} у {names}")
    for (category, pos), k in roots.items():
        if k > 1:
            issues.append(f"{locale} {category}: дубль position {pos} у корневых пунктов")
    return issues


def flat(tree: list[dict]) -> dict:
    """Пункт -> (position, openInNewTab, nofollow, link). Ключ — категория, заголовок родителя, ссылка, заголовок."""
    m = {}
    for item in tree:
        m[(item["category"], None, item["link"] or "", item["title"])] = (
            item["position"], item["openInNewTab"], item["nofollow"], item["link"])
        for c in item["children"]:
            m[(item["category"], item["title"], c["link"] or "", c["title"])] = (
                c["position"], c["openInNewTab"], c["nofollow"], c["link"])
    return m


def diff(old: list[dict], new: list[dict]) -> list[str]:
    a, b = flat(old), flat(new)
    where = lambda k: f"{k[0]} / {k[1] or '(корень)'} / {k[3]!r}"
    lines = [f"  + {where(k)}  {b[k]}" for k in sorted(set(b) - set(a), key=str)]
    lines += [f"  - {where(k)}  {a[k]}" for k in sorted(set(a) - set(b), key=str)]
    lines += [f"  ~ {where(k)}: {a[k]} -> {b[k]}" for k in sorted(set(a) & set(b), key=str) if a[k] != b[k]]
    return lines


def git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr.decode('utf-8', 'replace').strip()}")
    return res.stdout.decode("utf-8")


def load_seed(repo: Path, ref: str | None, seed_file: Path | None) -> tuple[dict, str]:
    if seed_file:
        return json.loads(seed_file.read_text(encoding="utf-8")), str(seed_file)
    sha = git(repo, "rev-parse", "--short", ref).strip()
    return json.loads(git(repo, "show", f"{ref}:{SEED_PATH}")), f"{ref} ({sha})"


def cmd_refresh(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    if args.write:
        # Рабочая копия репозитория CMS часто стоит на чужой фича-ветке. Ветку записи называют явно,
        # и проверяем это до похода на прод, а не после.
        branch = git(repo, "branch", "--show-current").strip()
        if not args.branch:
            out("СТОП: --write требует --branch <имя ветки, в которую пишем сид>.")
            return 2
        if branch != args.branch or branch in ("main", "master"):
            out(f"СТОП: репозиторий CMS сейчас на {branch or 'detached HEAD'!r}, а для записи названа {args.branch!r}. "
                "Сначала создай/выкачай фича-ветку под обновление сида (не main).")
            return 2
    baseline, source = load_seed(repo, args.ref, None)
    out(f"Снимок прода: {PROD_GRAPHQL}, {len(LOCALES)} локалей. Сравнение с сидом из {source}.")

    prod = {loc: to_seed(fetch_menus(PROD_GRAPHQL, loc)) for loc in LOCALES if loc not in COPY_FROM}
    new = {loc: prod[COPY_FROM.get(loc, loc)] for loc in LOCALES}

    out("\n== Объёмы: сид -> прод (корневых/всего) ==")
    for loc in LOCALES:
        before, after = count(baseline.get(loc, [])), count(new[loc])
        out(f"  {loc:<3} {before[0]:>2}/{before[1]:<4} -> {after[0]:>2}/{after[1]:<4} {'' if before == after else '<-- изменилось'}")

    out("\n== Поэлементные изменения ==")
    total = 0
    for loc in LOCALES:
        lines = diff(baseline.get(loc, []), new[loc])
        if lines:
            total += len(lines)
            out(f"-- {loc} --" + (f"  (копия {COPY_FROM[loc]})" if loc in COPY_FROM else ""))
            for line in lines:
                out(line)
    out(f"изменений: {total}" + ("  — сид актуален, писать нечего" if total == 0 else ""))

    out("\n== Данные прода, которые сид унаследует как есть ==")
    issues = [i for loc in LOCALES if loc not in COPY_FROM for i in quality_issues(loc, prod[loc])]
    out("\n".join("  " + i for i in issues) if issues else "  чисто")
    if issues:
        out("  Дубли position чинятся на проде перетаскиванием в админке (коллекция меню) — см. README.")

    if not args.write:
        out("\nНичего не записано (нет --write).")
        return 0

    branch = args.branch
    seed = {"_meta": dict(baseline.get("_meta", {}))}
    seed["_meta"]["capturedAt"] = datetime.date.today().isoformat()
    seed.update(new)
    target = repo / SEED_PATH
    # newline="\n": Python на Windows по умолчанию пишет CRLF.
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(seed, ensure_ascii=False, indent=2) + "\n")
    out(f"\nЗаписано: {target} (ветка {branch}), capturedAt {seed['_meta']['capturedAt']}, узлов {sum(count(new[l])[1] for l in LOCALES)}.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    seed, source = load_seed(repo, args.ref, Path(args.seed) if args.seed else None)
    endpoint = STAND_GRAPHQL.format(stand=args.stand)
    locales = LOCALES if args.locale == "all" else [args.locale]
    out(f"Стенд {args.stand} ({endpoint}) против сида из {source}.")

    key = lambda tree: sorted(
        ((i["title"], i["link"], i["category"], i["position"], i["openInNewTab"], i["nofollow"],
          tuple((c["title"], c["link"], c["position"], c["openInNewTab"], c["nofollow"]) for c in i["children"]))
         for i in tree),
        key=lambda t: (t[2], t[3]),
    )
    failed = 0
    for loc in locales:
        live = to_seed(fetch_menus(endpoint, loc))
        got, want = key(live), key(seed[loc])
        head = f"  {loc:<3} стенд {count(live)[0]}/{count(live)[1]}, сид {count(seed[loc])[0]}/{count(seed[loc])[1]}"
        if got == want:
            out(head + " -> совпадает")
            continue
        failed += 1
        out(head + " -> РАСХОДИТСЯ")
        for line in diff(seed[loc], live):
            out("    " + line)
    total = sum(count(seed[l])[1] for l in locales)
    out(f"\nРасходится локалей: {failed} из {len(locales)}. Ожидаемое число пунктов в базе по этим локалям: {total} "
        "(сверить авторизованным счётом — анонимное чтение отключённые пункты не видит).")
    return 1 if failed else 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # арабский/CJK в консоли cp1251
    except AttributeError:
        pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", help="рабочая копия репозитория CMS (по умолчанию — поле repo настроек)")
    p.add_argument("--config", help="файл настроек вместо config.local.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh", help="снять прод, отчёт и диф с сидом; --write записывает сид")
    r.add_argument("--ref", default="origin/main", help="откуда брать текущий сид для сравнения (git ref)")
    r.add_argument("--write", action="store_true", help="записать новый сид в рабочую копию репозитория CMS")
    r.add_argument("--branch", help="обязателен с --write: имя текущей ветки репозитория CMS, не main")
    r.set_defaults(func=cmd_refresh)

    v = sub.add_parser("verify", help="сверить меню стенда с сидом")
    v.add_argument("--stand", required=True, help="стенд, например qa2 (подставляется в stand_graphql)")
    v.add_argument("--locale", default="all", help="all или одна локаль из настроек")
    v.add_argument("--ref", default="origin/main", help="откуда брать сид (git ref)")
    v.add_argument("--seed", help="или путь к файлу сида — например, когда в под залит сид из ветки")
    v.set_defaults(func=cmd_verify)

    args = p.parse_args()
    load_config(args.config)
    args.repo = args.repo or DEFAULT_REPO
    need = {"refresh": [("prod_graphql", PROD_GRAPHQL)], "verify": [("stand_graphql", STAND_GRAPHQL)]}[args.cmd]
    missing = [k for k, v in need if not v] + ([] if args.repo or getattr(args, "seed", None) else ["repo"])
    if missing:
        out(f"нет настроек: {', '.join(missing)} — заполните config.local.json (пример — config.example.json)")
        return 2
    if args.cmd == "verify" and args.locale != "all" and args.locale not in LOCALES:
        out(f"неизвестная локаль {args.locale!r}; в настройках: {', '.join(LOCALES)}")
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
