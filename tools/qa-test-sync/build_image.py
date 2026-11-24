#!/usr/bin/env python3
"""Сборка образа тестов после мержей ночной задачи — и больше ничего.

Пуш в main репозитория тестов образ не собирает: `build.yml` срабатывает на теги и на
ручной запуск. Ночные прогоны берут образ `:latest`, поэтому без сборки влитые задачей
тесты ночью не участвуют. У `gh workflow run` в настройках Claude Code стоит правило
`ask`, которое спрашивает подтверждение в любом режиме. Этот скрипт — узкое исключение:
он запускает только `build_workflow` из конфига, только в `repo` и только на
`base_branch`, и только после мержа PR задачи. Для любого другого запуска workflow
правило `ask` остаётся.

Использование:
  python tools/qa-test-sync/build_image.py --merge-commit <SHA>             # собрать и дождаться
  python tools/qa-test-sync/build_image.py --merge-commit <SHA> --dry-run   # только проверки

`--merge-commit` — коммит мержа последнего влитого PR задачи (поле `mergeCommit` в ответе
merge_pr.py). Отказ (код выхода 1), если:
  * коммит не принадлежит влитому в `base_branch` PR из ветки `branch_prefix` этого репозитория;
  * сборка не началась, не закончилась за `--timeout-min` или закончилась не `success`;
  * собранный коммит не содержит `--merge-commit`.

Если сборка на `base_branch` уже идёт и уже содержит нужный коммит — скрипт ждёт её и
новую не запускает. Если идёт более старая — ждёт её конца и только потом запускает свою:
иначе старая могла бы закончиться позже и перезаписать `:latest`.

Настройки — `config.local.json` рядом со скриптом (пример — `config.example.json`).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
POLL_SECONDS = 20
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")


def load_config():
    path = os.environ.get("QA_TEST_SYNC_CONFIG") or os.path.join(HERE, "config.local.json")
    if not os.path.exists(path):
        fail("нет файла настроек %s (пример — config.example.json)" % path)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("repo", "base_branch", "branch_prefix", "build_workflow"):
        if key not in cfg:
            fail("в %s нет ключа %s" % (path, key))
    return cfg


def fail(reason, **extra):
    print(json.dumps({"built": False, "reason": reason, **extra}, ensure_ascii=False))
    sys.exit(1)


def gh(*args):
    res = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        fail("gh %s: %s" % (" ".join(args[:3]), (res.stderr or res.stdout).strip()))
    return res.stdout


def gh_json(*args):
    return json.loads(gh(*args))


def sync_pr_of(cfg, sha):
    pulls = gh_json("api", "repos/%s/commits/%s/pulls" % (cfg["repo"], sha))
    for pr in pulls:
        if (pr.get("merged_at")
                and pr["base"]["ref"] == cfg["base_branch"]
                and pr["head"]["ref"].startswith(cfg["branch_prefix"])
                and (pr["head"].get("repo") or {}).get("full_name") == cfg["repo"]
                and (pr.get("merge_commit_sha") or "").startswith(sha)):
            return pr
    fail("коммит %s — не мерж PR задачи (влитого в %s из ветки %s*)"
         % (sha, cfg["base_branch"], cfg["branch_prefix"]))


def contains(cfg, head_sha, sha):
    cmp = gh_json("api", "repos/%s/compare/%s...%s" % (cfg["repo"], sha, head_sha), "--jq", "{status: .status}")
    return cmp["status"] in ("identical", "ahead")


def runs_on_branch(cfg):
    return gh_json("run", "list", "--repo", cfg["repo"], "--workflow", cfg["build_workflow"],
                   "--branch", cfg["base_branch"], "--limit", "10",
                   "--json", "databaseId,status,conclusion,headSha,createdAt,url")


def wait_run(cfg, run_id, deadline):
    while True:
        run = gh_json("run", "view", str(run_id), "--repo", cfg["repo"],
                      "--json", "databaseId,status,conclusion,headSha,url")
        if run["status"] == "completed":
            return run
        if time.time() > deadline:
            fail("сборка %s не закончилась за отведённое время" % run["url"], run=run["url"])
        time.sleep(POLL_SECONDS)


def dispatch(cfg, deadline):
    known = {r["databaseId"] for r in runs_on_branch(cfg)}
    out = gh("workflow", "run", cfg["build_workflow"], "--repo", cfg["repo"], "--ref", cfg["base_branch"])
    m = re.search(r"/actions/runs/(\d+)", out)
    if m:
        return int(m.group(1))
    while time.time() < deadline:
        fresh = [r for r in runs_on_branch(cfg) if r["databaseId"] not in known]
        if fresh:
            return fresh[0]["databaseId"]
        time.sleep(5)
    fail("сборка запрошена, но не появилась в списке запусков")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Сборка образа тестов после мержей ночной задачи")
    ap.add_argument("--merge-commit", required=True, help="коммит мержа последнего влитого PR задачи")
    ap.add_argument("--timeout-min", type=int, default=40, help="сколько ждать всего, минут")
    ap.add_argument("--dry-run", action="store_true", help="только проверки, без запуска сборки")
    a = ap.parse_args()
    if len(a.merge_commit) < 7:
        fail("--merge-commit короче 7 символов")

    cfg = load_config()
    deadline = time.time() + a.timeout_min * 60
    sha = gh("api", "repos/%s/commits/%s" % (cfg["repo"], a.merge_commit), "--jq", ".sha").strip()
    pr = sync_pr_of(cfg, sha)

    active = [r for r in runs_on_branch(cfg) if r["status"] in ACTIVE]
    covering = [r for r in active if contains(cfg, r["headSha"], sha)]
    stale = [r for r in active if r not in covering]

    if a.dry_run:
        print(json.dumps({"built": False, "dry_run": True, "pr": pr["html_url"],
                          "reuse": [r["url"] for r in covering], "wait_first": [r["url"] for r in stale]},
                         ensure_ascii=False))
        return

    if covering:
        run_id = covering[0]["databaseId"]
    else:
        for r in stale:
            wait_run(cfg, r["databaseId"], deadline)
        run_id = dispatch(cfg, deadline)

    run = wait_run(cfg, run_id, deadline)
    if run["conclusion"] != "success":
        fail("сборка закончилась с итогом %s" % run["conclusion"], run=run["url"])
    if not contains(cfg, run["headSha"], sha):
        fail("собран коммит %s, в нём нет %s" % (run["headSha"], a.merge_commit), run=run["url"])
    print(json.dumps({"built": True, "run": run["url"], "headSha": run["headSha"], "pr": pr["html_url"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
