#!/usr/bin/env python3
"""Squash-мерж одного PR автоматической актуализации тестов — и больше ничего.

Ночная задача актуализации тестов вливает свои PR без человека, а у `gh pr merge` в
настройках Claude Code стоит правило `ask`, которое спрашивает подтверждение в любом
режиме. Этот скрипт — узкое исключение: он вливает только PR, который прошёл все
проверки ниже, а для любой другой команды мержа правило `ask` остаётся.

Использование:
  python tools/qa-test-sync/merge_pr.py <номер PR> --head-sha <SHA>   # проверить и влить
  python tools/qa-test-sync/merge_pr.py <номер PR> --head-sha <SHA> --dry-run

Отказ (код выхода 1), если хоть что-то не так:
  * PR не в репозитории из конфига, не открыт, черновик, база не `base_branch`;
  * ветка PR не начинается с `branch_prefix` или пришла из форка;
  * голова PR не равна `--head-sha` (в ветку успели дописать то, что не проверялось);
  * GitHub не считает PR сливаемым без конфликтов;
  * PR трогает путь из `forbidden_paths` (CI, конфиг Playwright, гарды записи в прод);
  * в добавленных строках есть слово из `forbidden_added_words` (например, `@smoke`).

Настройки — `config.local.json` рядом со скриптом (пример — `config.example.json`).
"""
import argparse
import fnmatch
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    path = os.environ.get("QA_TEST_SYNC_CONFIG") or os.path.join(HERE, "config.local.json")
    if not os.path.exists(path):
        fail("нет файла настроек %s (пример — config.example.json)" % path)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("repo", "base_branch", "branch_prefix", "forbidden_paths", "forbidden_added_words"):
        if key not in cfg:
            fail("в %s нет ключа %s" % (path, key))
    return cfg


def fail(reason):
    print(json.dumps({"merged": False, "reason": reason}, ensure_ascii=False))
    sys.exit(1)


def gh(*args):
    res = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        fail("gh %s: %s" % (" ".join(args[:3]), (res.stderr or res.stdout).strip()))
    return res.stdout


def added_lines(diff_text):
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            yield line[1:]


def check(pr, cfg, number, head_sha):
    repo = cfg["repo"]
    owner = repo.split("/")[0]
    if pr["state"] != "OPEN":
        fail("PR #%s не открыт: %s" % (number, pr["state"]))
    if pr["isDraft"]:
        fail("PR #%s — черновик" % number)
    if pr["baseRefName"] != cfg["base_branch"]:
        fail("база PR #%s — %s, а не %s" % (number, pr["baseRefName"], cfg["base_branch"]))
    if not pr["headRefName"].startswith(cfg["branch_prefix"]):
        fail("ветка %s не начинается с %s" % (pr["headRefName"], cfg["branch_prefix"]))
    if pr.get("isCrossRepository") or (pr.get("headRepositoryOwner") or {}).get("login") != owner:
        fail("ветка PR #%s пришла не из %s" % (number, repo))
    if not pr["headRefOid"].startswith(head_sha) or len(head_sha) < 7:
        fail("голова PR #%s — %s, а проверялся %s" % (number, pr["headRefOid"], head_sha))
    if pr["mergeable"] != "MERGEABLE":
        fail("GitHub не даёт влить PR #%s без конфликтов: mergeable=%s" % (number, pr["mergeable"]))

    touched = [f["path"] for f in pr["files"]]
    blocked = [p for p in touched if any(fnmatch.fnmatch(p, pat) for pat in cfg["forbidden_paths"])]
    if blocked:
        fail("PR #%s трогает запрещённые для автомержа пути: %s" % (number, ", ".join(blocked)))

    diff_text = gh("pr", "diff", str(number), "--repo", repo)
    for line in added_lines(diff_text):
        for word in cfg["forbidden_added_words"]:
            if word in line:
                fail("PR #%s добавляет `%s`: %s" % (number, word, line.strip()[:160]))
    return touched


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Squash-мерж PR автоматической актуализации тестов")
    ap.add_argument("number", type=int)
    ap.add_argument("--head-sha", required=True, help="SHA головы, которую проверяли прогоном")
    ap.add_argument("--dry-run", action="store_true", help="только проверки, без мержа")
    a = ap.parse_args()

    cfg = load_config()
    repo = cfg["repo"]
    fields = "state,isDraft,baseRefName,headRefName,headRefOid,isCrossRepository,headRepositoryOwner,mergeable,files,url"
    for _ in range(6):
        pr = json.loads(gh("pr", "view", str(a.number), "--repo", repo, "--json", fields))
        # GitHub считает mergeable лениво: сразу после push отвечает UNKNOWN.
        if pr["mergeable"] != "UNKNOWN":
            break
        time.sleep(5)
    touched = check(pr, cfg, a.number, a.head_sha)

    if a.dry_run:
        print(json.dumps({"merged": False, "dry_run": True, "url": pr["url"], "files": touched}, ensure_ascii=False))
        return

    gh("pr", "merge", str(a.number), "--repo", repo, "--squash", "--match-head-commit", pr["headRefOid"])
    merged = json.loads(gh("pr", "view", str(a.number), "--repo", repo, "--json", "state,mergeCommit"))
    if merged["state"] != "MERGED":
        fail("после мержа PR #%s в состоянии %s" % (a.number, merged["state"]))
    print(json.dumps({"merged": True, "url": pr["url"], "mergeCommit": (merged.get("mergeCommit") or {}).get("oid"),
                      "files": touched}, ensure_ascii=False))


if __name__ == "__main__":
    main()
