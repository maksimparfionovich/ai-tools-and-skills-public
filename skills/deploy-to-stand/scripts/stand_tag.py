#!/usr/bin/env python3
"""Выкатка ветки на НЕпродовый стенд пушем одного git-тега.

    python3 .claude/skills/deploy-to-stand/scripts/stand_tag.py \
        --repo backend --branch feat/new-search --stand qa1 [--push]

Без --push — только план (dry-run): тег, SHA, что сделает CI. С --push — пушит тег
`git push origin <sha>:refs/tags/<тег>` (рабочую копию и ветки не трогает).

Тег: <стенд>-<ветка: / → ->-<UTC YYYYMMDD-HHMMSS>; в репозитории с проектами (kind=project)
спереди код проекта: <код>-<стенд>-<ветка>-<время>.

Коды выхода: 0 — план чистый / тег запушен; 2 — СТОП, нужно решение пользователя
(причина напечатана); 1 — ошибка (git, сеть, неизвестный репо, нет настроек).

Правила CI каждого репозитория — в config.local.json рядом со скриптом (или в файле из
DEPLOY_TO_STAND_CONFIG); пример и описание полей — config.example.json и SKILL.md. Если у
репозитория задан blob workflow, а в default-ветке он другой, скрипт останавливается: CI
изменился, правила надо перепроверить.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CONFIG_DEFAULTS = {
    "repos_root": "",            # где лежат клоны; относительный путь — от $CLAUDE_PROJECT_DIR или cwd
    "own_stands": [],            # свои стенды: катить без отдельного согласия
    "never_target": ["uat", "prod"],
    "release_substring": "release",
    "ci_tokens": [],             # все слова, на которые реагирует CI хоть одного репо (стенды, uat, prod)
    "max_tag": 100,              # docker-тег = тег + -sha7/8 ≤ 128
    "dead_runners": [],          # раннеры, которых больше нет: workflow с ними повиснет в очереди
    "github_actions_url": "",    # шаблон ссылки на Actions для подсказки, например https://github.com/<org>/{repo}/actions
    "repos": {},
    "excluded": {},
}


class Stop(Exception):
    """Нужно решение пользователя (exit 2)."""


def load_config():
    path = os.environ.get("DEPLOY_TO_STAND_CONFIG") or str(Path(__file__).with_name("config.local.json"))
    if not os.path.isfile(path):
        raise SystemExit(f"Нет настроек {path}. Скопируйте config.example.json в config.local.json и "
                         "опишите свои репозитории и стенды (SKILL.md, «Настройка»).")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Не прочитал настройки {path}: {exc}")
    cfg = {**CONFIG_DEFAULTS, **{k: v for k, v in data.items() if not k.startswith("_")}}
    base = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    root = Path(os.path.expanduser(cfg["repos_root"] or "."))
    cfg["repos_root"] = root if root.is_absolute() else Path(base) / root
    for name, r in cfg["repos"].items():
        r.setdefault("kind", "tag")
        r.setdefault("default", "main")
        r.setdefault("path", name)
    return cfg


def token_in(tag: str, tok: str) -> bool:
    return tag == tok or re.search(rf"(^|[._/-]){re.escape(tok)}($|[._/-])", tag) is not None


def slugify(text: str) -> str:
    s = text.lower().replace("/", "-")
    s = re.sub(r"[^a-z0-9._-]+", "-", s)
    s = re.sub(r"\.{2,}", ".", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-.")


def git(repo: Path, *args, check=True, timeout=180) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=timeout)
    out = p.stdout.decode("utf-8", "replace").strip()
    if check and p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git {' '.join(args)} → rc={p.returncode}\n{err}")
    return out


def simulate_tag(cfg: dict, rcfg: dict, tag: str) -> list:
    """kind=tag: CI матчит каждый токен из tag_tokens как (^|[._/-])tok($|[._/-]); релизная
    подстрока в любом месте тега → прод."""
    t = tag.lower()
    hits = [tok for tok in rcfg["tag_tokens"] if token_in(t, tok)]
    rel = cfg["release_substring"]
    if rel and rel in t:
        hits.append(f"PROD({rel})")
    return hits


def simulate_project(cfg: dict, rcfg: dict, tag: str) -> dict:
    """kind=project: у каждого проекта свой workflow, триггер — подстрока кода проекта в теге,
    стенд — токен `<код>-<стенд>`, несколько стендов = ошибка CI. Каталог (необязательный)
    триггерится тегами <код>, <код>-*, *-<код>, *-<код>-* и матчит голые стенды.
    Возвращает {код: [стенды] | 'ERROR: …'}."""
    t = tag.lower()
    res = {}
    for code, p in rcfg["projects"].items():
        if code in t:
            hits = [s for s in p["stands"] + list(cfg["never_target"]) if token_in(t, f"{code}-{s}")]
            res[code] = f"ERROR: несколько стендов {hits}" if len(hits) > 1 else hits
    cat = rcfg.get("catalog")
    if cat:
        c = cat["code"]
        if t == c or t.startswith(f"{c}-") or t.endswith(f"-{c}") or f"-{c}-" in t:
            hits = [s for s in cat["stands"] if token_in(t, s)]
            if token_in(t, "prod") or (cfg["release_substring"] and token_in(t, cfg["release_substring"])):
                hits.append("PROD")
            res[c] = hits
    return res


def slug_dangers(cfg: dict, slug: str, stand: str, rcfg: dict, proj: str = "") -> list:
    out = []
    rel = cfg["release_substring"]
    if rel and rel in slug:
        out.append(f"'{rel}' (подстрока) — в CI это выкатка на ПРОД")
    for tok in sorted(set(cfg["ci_tokens"]) | set(cfg["never_target"]) - {stand}):
        if tok != stand and token_in(slug, tok):
            what = "ПРОД/UAT" if tok in cfg["never_target"] else f"ещё и стенд {tok}"
            out.append(f"слово '{tok}' — CI может выкатить {what}")
    for word, msg in rcfg.get("special", {}).items():
        if token_in(slug, word):
            out.append(msg)
    if rcfg["kind"] == "project":
        for code in rcfg["projects"]:
            if code != proj and code in slug:            # свой код безопасен: тот же workflow
                out.append(f"'{code}' (подстрока) — запустит workflow проекта {code}")
        cat = rcfg.get("catalog")
        if cat and proj != cat["code"] and token_in(slug, cat["code"]):
            out.append(f"слово '{cat['code']}' — запустит каталог проектов")
    return out


def check_workflows(cfg: dict, repo: Path, default: str, sha: str, files: list, accept_diff: bool, notes: list):
    """files: [(путь, ожидаемый blob12 или "", critical)]. critical — workflow, который должен выкатить.
    Дрейф default-ветки → Stop; ветка ≠ default → Stop без --accept-workflow-diff."""
    drift, differs = [], []
    for path, blob12, critical in files:
        # --verify -q: на отсутствующий путь пусто (без него rev-parse эхом печатает аргумент)
        main_blob = git(repo, "rev-parse", "--verify", "-q", f"origin/{default}:{path}", check=False)
        if blob12 and not main_blob.startswith(blob12):
            drift.append(path)
        br_blob = git(repo, "rev-parse", "--verify", "-q", f"{sha}:{path}", check=False)
        if not br_blob:
            if critical:
                raise Stop(f"в коммите {sha[:8]} нет {path} — CI на тег не сработает. Ветку надо подлить из {default}.")
            notes.append(f"в ветке нет {path} (этот workflow на тег не запустится — это нормально)")
            continue
        content = git(repo, "show", f"{sha}:{path}")
        dead = [r for r in cfg["dead_runners"] if r in content]
        if dead and critical:
            raise Stop(f"{path} в ветке ссылается на раннер, которого больше нет: {dead} — сборка, скорее всего, "
                       f"повиснет в очереди. Нужно подлить {default} в ветку (отдельная операция с согласия пользователя).")
        if br_blob != main_blob:
            differs.append(path)
    if drift:
        raise Stop("CI в origin/%s изменился с момента, когда снимались правила: %s. Правила могут быть неверны — "
                   "перечитать workflow и обновить запись репозитория в config.local.json (tag_tokens, blob)."
                   % (default, ", ".join(drift)))
    if not all(b for _, b, _ in files):
        notes.append("у части workflow в настройках нет blob — изменение CI в default-ветке не отслеживается")
    if differs:
        msg = ("workflow в ветке отличается от origin/%s: %s. CI возьмёт логику из ветки, симуляция ниже — по %s.\n"
               "  Посмотреть: git -C %s diff origin/%s %s -- %s"
               % (default, ", ".join(differs), default, repo.as_posix(), default, sha[:10], " ".join(differs)))
        if not accept_diff:
            raise Stop(msg + "\n  Если разница не про выбор стенда/прод — перезапусти с --accept-workflow-diff.")
        notes.append(msg + " (принято --accept-workflow-diff)")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):           # на Windows вывод по умолчанию в cp1251
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Выкатка ветки на непродовый стенд пушем тега")
    ap.add_argument("--repo", help="имя репозитория из config.local.json")
    ap.add_argument("--branch", help="ветка на origin, например feat/new-search")
    ap.add_argument("--stand", help="стенд, например qa1")
    ap.add_argument("--project", help="для репозитория с проектами (kind=project): код проекта или каталога")
    ap.add_argument("--label", help="короткая метка вместо имени ветки (только по согласию пользователя)")
    ap.add_argument("--shared-stand-ok", action="store_true", help="пользователь разрешил общий стенд")
    ap.add_argument("--accept-workflow-diff", action="store_true", help="разница workflow ветки и default просмотрена")
    ap.add_argument("--push", action="store_true", help="запушить тег (без флага — только план)")
    ap.add_argument("--https", action="store_true", help="пушить по HTTPS (если SSH к GitHub режется)")
    ap.add_argument("--no-verify", action="store_true", help="пропустить pre-push хук (только с согласия пользователя)")
    ap.add_argument("--list", action="store_true", help="показать репозитории из настроек и выйти")
    a = ap.parse_args()

    cfg = load_config()
    if a.list:
        for name, r in sorted(cfg["repos"].items()):
            stands = (r.get("tag_tokens") if r["kind"] == "tag"
                      else sorted({s for p in r["projects"].values() for s in p["stands"]}))
            print(f"{name:20} {r['kind']:8} {r['path']}  стенды: {', '.join(s for s in stands if s not in cfg['never_target'])}")
        for name, why in sorted(cfg["excluded"].items()):
            print(f"{name:20} не катит: {why}")
        return 0
    if not (a.repo and a.branch and a.stand):
        ap.error("нужны --repo, --branch и --stand (или --list)")

    repo_key, stand = a.repo.strip(), a.stand.strip().lower()
    never = set(cfg["never_target"])
    own = set(cfg["own_stands"])
    try:
        if repo_key in cfg["excluded"]:
            raise Stop(f"{repo_key}: {cfg['excluded'][repo_key]}")
        if repo_key not in cfg["repos"]:
            print(f"Неизвестный репо '{repo_key}'. Известные: {', '.join(sorted(cfg['repos']))}", file=sys.stderr)
            return 1
        if stand in never:
            raise Stop(f"Выкатка на {stand.upper()} запрещена: прод и UAT катит человек.")
        rcfg = cfg["repos"][repo_key]
        repo = cfg["repos_root"] / rcfg["path"]
        default = rcfg["default"]

        # --- какие стенды вообще возможны по тегу
        proj = ""
        if rcfg["kind"] == "tag":
            if stand in rcfg.get("stops", {}):
                raise Stop(rcfg["stops"][stand])
            allowed = [s for s in rcfg["tag_tokens"] if s not in never]
            prefix = stand
            wf_files = [(f".github/workflows/{rcfg['wf']}", rcfg.get("blob", ""), True)]
        else:
            proj = (a.project or "").lower()
            cat = rcfg.get("catalog") or {}
            codes = list(rcfg["projects"]) + ([cat["code"]] if cat else [])
            if proj not in codes:
                raise Stop(f"{repo_key}: нужен --project {'|'.join(codes)}. Без кода проекта тег ничего не запустит.")
            allowed = (cat if cat and proj == cat["code"] else rcfg["projects"][proj])["stands"]
            prefix = f"{proj}-{stand}"
            wf_files = [(f".github/workflows/{p['wf']}", p.get("blob", ""), code == proj)
                        for code, p in rcfg["projects"].items()]
            if cat:
                wf_files.append((f".github/workflows/{cat['wf']}", cat.get("blob", ""), proj == cat["code"]))
        if stand not in allowed:
            raise Stop(f"{repo_key}: CI не выкатывает по тегу на {stand} — тег соберёт образ (или ничего) и никуда "
                       f"не уедет. Стенды по тегу: {', '.join(allowed)}.")
        if stand not in own and not a.shared_stand_ok:
            raise Stop(f"{stand} — общий стенд (свои: {', '.join(sorted(own)) or 'не заданы'}). Катить только если "
                       "пользователь назвал этот стенд сам или согласился; тогда --shared-stand-ok.")

        # --- имя тега
        source = a.label if a.label else a.branch
        slug = slugify(source)
        if not slug:
            raise Stop(f"из '{source}' не получилось имя для тега — нужен --label.")
        dangers = slug_dangers(cfg, slug, stand, rcfg, proj)
        if dangers:
            raise Stop(f"{'метка' if a.label else 'имя ветки'} '{source}' → '{slug}' опасно для тега:\n  - "
                       + "\n  - ".join(dangers)
                       + "\n  Спросить у пользователя короткую метку и перезапустить с --label <метка>.")
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        room = int(cfg["max_tag"]) - len(prefix) - len(ts) - 2
        notes = []
        if len(slug) > room:
            slug = slug[:room].rstrip("-.")
            notes.append(f"имя ветки обрезано до {room} символов (лимит docker-тега)")
        tag = f"{prefix}-{slug}-{ts}"

        # --- симуляция CI
        if rcfg["kind"] == "tag":
            hits = simulate_tag(cfg, rcfg, tag)
            if hits != [stand]:
                raise Stop(f"CI {repo_key} по тегу {tag} выкатит: {hits or 'ничего'} (ожидалось только {stand}).")
            ci_line = f"CI {repo_key}: выкатит только на {stand}"
        else:
            res = simulate_project(cfg, rcfg, tag)
            want = {proj: [stand]}
            if res != want:
                raise Stop(f"CI {repo_key} по тегу {tag} сделает: {res or 'ничего'} (ожидалось {want}).")
            ci_line = f"CI {repo_key}: workflow {proj} → только {stand}"

        # --- git: ветка, SHA, workflow
        if not repo.is_dir():
            print(f"Нет клона {repo}", file=sys.stderr)
            return 1
        if not git(repo, "ls-remote", "--heads", "origin", a.branch):
            print(f"Ветки '{a.branch}' нет на origin ({repo_key}).", file=sys.stderr)
            return 1
        git(repo, "fetch", "-q", "origin", default, a.branch)
        sha = git(repo, "rev-parse", f"refs/remotes/origin/{a.branch}^{{commit}}")
        subject = git(repo, "log", "-1", "--format=%h %s (%an, %ad)", "--date=short", sha)
        behind = git(repo, "rev-list", "--count", f"{sha}..origin/{default}")
        check_workflows(cfg, repo, default, sha, wf_files, a.accept_workflow_diff, notes)
        if git(repo, "ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
            print(f"Тег {tag} уже есть на origin — перезапусти через секунду.", file=sys.stderr)
            return 1

        print(f"Репо:     {repo_key} ({repo.as_posix()})")
        print(f"Ветка:    origin/{a.branch} @ {subject}")
        print(f"          отстаёт от origin/{default} на {behind} коммит(ов)")
        print(f"Стенд:    {stand}{'' if stand in own else ' (общий, согласие есть)'}")
        print(f"Тег:      {tag}")
        cat = rcfg.get("catalog") or {}
        unverified = rcfg.get("image_tag_unverified", False) or (
            rcfg["kind"] == "project" and proj == cat.get("code") and cat.get("image_tag_unverified", False))
        print(f"Образ:    ожидается тег образа {tag}-{sha[:7]}"
              + (" (схема тега образа не проверена)" if unverified else ""))
        print(f"{ci_line}; {cfg['release_substring'] or 'release'}/{'/'.join(sorted(never))} в теге нет")
        for n in notes:
            print(f"Заметка:  {n}")

        if not a.push:
            print("\nDRY-RUN: тег не создан. Для выкатки — тот же вызов с --push.")
            return 0

        remote = f"https://github.com/{git(repo, 'remote', 'get-url', 'origin').split(':')[-1].split('github.com/')[-1]}" \
            if a.https else "origin"
        push_args = ["push"] + (["--no-verify"] if a.no_verify else []) + [remote, f"{sha}:refs/tags/{tag}"]
        try:
            git(repo, *push_args, timeout=300)
        except RuntimeError as e:
            print(f"\nПуш не прошёл:\n{e}\nЕсли упал pre-push хук — спросить пользователя про --no-verify; "
                  f"если SSH режется — --https.", file=sys.stderr)
            return 1
        remote_sha = git(repo, "ls-remote", "--tags", "origin", f"refs/tags/{tag}").split()
        if not remote_sha or remote_sha[0] != sha:
            print(f"\nТег запушен, но на origin не виден с {sha[:8]}: {remote_sha}", file=sys.stderr)
            return 1
        actions = cfg["github_actions_url"].format(repo=repo_key) if cfg["github_actions_url"] else ""
        print(f"\nPUSHED: {tag} → {sha[:10]}. CI соберёт и выкатит; дальше — проверка пода (SKILL.md)."
              + (f"\nActions: {actions}" if actions else ""))
        return 0
    except Stop as s:
        print(f"СТОП: {s}")
        return 2
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
