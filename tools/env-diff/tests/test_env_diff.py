#!/usr/bin/env python3
"""Самодостаточный тест env-diff: python3 tools/env-diff/tests/test_env_diff.py

Во временной папке собирается крошечный TS-сервис (git, две ветки) и gitops-репозиторий в
раскладке по умолчанию (clusters/, appset/, apps/<app>/env.yml и apps/<app>/<стенд>/value.yml).
Проверяется, что env-diff находит новую переменную ветки и правильно выбирает код выхода:
0 — всё покрыто, 1 — новая переменная с default, 2 — новая переменная без default (блокер).
Нужны git и PyYAML; сети не нужно.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ENV_DIFF = Path(__file__).resolve().parents[1] / "env_diff.py"


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def make_fixture(root: Path):
    svc = root / "repos" / "shop-api"
    write(svc / "src" / "config.ts", "export const url = process.env.DB_URL;\nexport const port = process.env.PORT ?? 3000;\n")
    sh(svc, "git", "init", "-q", "-b", "main")
    sh(svc, "git", "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
    sh(svc, "git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "base")
    sh(svc, "git", "checkout", "-qb", "feat/cache")
    write(svc / "src" / "cache.ts",
          "export const ttl = process.env.CACHE_TTL || 60;\nexport const host = process.env.CACHE_HOST;\n")
    sh(svc, "git", "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
    sh(svc, "git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "cache")

    ops = root / "repos" / "gitops"
    write(ops / "clusters" / "qa1.yml", "stand_code: qa1\nshop-api-enabled: true\n")
    write(ops / "clusters" / "qa2.yml", "stand_code: qa2\nshop-api-enabled: true\n")
    write(ops / "appset" / "shop-api.yml",
          "spec:\n  template:\n    spec:\n      sources:\n        - helm:\n            valueFiles:\n"
          "              - $values/apps/shop-api/env.yml\n"
          "              - $values/apps/shop-api/{{ .stand_code }}/value.yml\n")
    write(ops / "apps" / "shop-api" / "env.yml", "env:\n  DB_URL: postgres://db\n")
    write(ops / "apps" / "shop-api" / "qa2" / "value.yml", "env:\n  CACHE_HOST: valkey\n")

    cfg = {
        "repos_root": str(root / "repos"),
        "gitops": {"stands": {"path": "gitops", "label": "gitops", "kind": "stands", "stands": ["qa1", "qa2"]}},
        "services": {"shop-api": {"repo": "shop-api", "kind": "ts", "paths": ["src"]}},
    }
    write(root / "config.json", json.dumps(cfg))
    return svc, root / "config.json"


def run(cfg, *args):
    p = subprocess.run([sys.executable, str(ENV_DIFF), "--config", str(cfg), *args],
                       capture_output=True, text=True, encoding="utf-8")
    return p.returncode, p.stdout + p.stderr


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        svc, cfg = make_fixture(Path(tmp))
        checks = [
            # (аргументы, код выхода, что должно быть в выводе)
            (["--service", "shop-api", "--stand", "qa1", "--ref", "main"], 0, "новых непокрытых переменных нет"),
            (["--service", "shop-api", "--stand", "qa1", "--ref", "feat/cache", "--base", "main"], 2, "CACHE_HOST"),
            (["--service", "shop-api", "--stand", "qa2", "--ref", "feat/cache", "--base", "main"], 1, "CACHE_TTL"),
            (["--service", "shop-api", "--stand", "qa1", "--ref", "feat/cache", "--base", "main", "--json"], 2,
             '"blockers": [\n    "CACHE_HOST"'),
            (["--service", "shop-api", "--stand", "qa9"], 1, "неизвестный контур"),
            (["--list"], 0, "shop-api"),
        ]
        for args, want_rc, want_text in checks:
            rc, out = run(cfg, *args)
            ok = rc == want_rc and want_text in out
            bad += not ok
            print(f"{'ok  ' if ok else 'FAIL'} rc={rc} {' '.join(args)}")
            if not ok:
                print("     ожидалось rc=%s и «%s», вывод:\n     %s" % (want_rc, want_text, out.strip().replace("\n", "\n     ")))
        rc, out = run(cfg, "--service", "shop-api", "--stand", "qa1", "--ref", "feat/cache", "--base", "main")
        hint = "gitops/apps/shop-api/qa1/value.yml — только этот стенд"
        ok = hint in out
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} подсказка «куда добавлять» называет файл стенда")
    total = 7
    print(f"\n{total - bad}/{total} ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
