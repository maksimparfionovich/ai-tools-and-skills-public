#!/usr/bin/env python3
"""Проверка логики stand_tag.py без сети и без git: python3 test_stand_tag.py

Идёт на config.example.json. Вызов git подменён: если скрипт дошёл до git — план чистый
(дальше он смотрел бы ветку на origin). Проверяется всё, что решается до этого: стопы на
прод/UAT, общий стенд, опасные слова в имени ветки, симуляция CI по тегу.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["DEPLOY_TO_STAND_CONFIG"] = str(HERE / "config.example.json")


class ReachedGit(Exception):
    pass


spec = importlib.util.spec_from_file_location("stand_tag", HERE / "stand_tag.py")
st = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st)


def fake_git(repo, *args, **kw):
    raise ReachedGit(" ".join(args[:2]))


class FixedDT(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return dt.datetime(2026, 10, 20, 12, 0, 0, tzinfo=tz)


st.git = fake_git
st.dt = type("dtmod", (), {"datetime": FixedDT, "timezone": dt.timezone})
st.Path.is_dir = lambda self: True  # клонов из примера нет — считаем, что есть


def run(*argv):
    sys.argv = ["stand_tag.py", *argv]
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            rc = st.main()
        except ReachedGit:
            rc = "git"
    return rc, out.getvalue()


# (аргументы, ожидаемый код: 2 — СТОП, "git" — план чистый, дальше git; подстрока в выводе)
CASES = [
    (["--repo", "backend", "--branch", "feat/search", "--stand", "qa1"], "git", ""),
    (["--repo", "backend", "--branch", "feat/search", "--stand", "prod"], 2, "запрещена"),
    (["--repo", "backend", "--branch", "feat/search", "--stand", "uat"], 2, "запрещена"),
    (["--repo", "backend", "--branch", "feat/search", "--stand", "qa3"], 2, "общий стенд"),
    (["--repo", "backend", "--branch", "feat/search", "--stand", "qa3", "--shared-stand-ok"], "git", ""),
    (["--repo", "backend", "--branch", "feat/search", "--stand", "demo"], 2, "нет demo"),
    (["--repo", "backend", "--branch", "feat/search", "--stand", "qa9"], 2, "не выкатывает"),
    (["--repo", "backend", "--branch", "fix/prerelease-bug", "--stand", "qa1"], 2, "'release'"),
    (["--repo", "backend", "--branch", "fix/prod-banner", "--stand", "qa1"], 2, "'prod'"),
    (["--repo", "backend", "--branch", "feat/qa2-only", "--stand", "qa1"], 2, "стенд qa2"),
    (["--repo", "backend", "--branch", "fix/prod-banner", "--stand", "qa1", "--label", "banner"], "git", ""),
    (["--repo", "frontend", "--branch", "feat/storybook-up", "--stand", "qa1"], 2, "Storybook"),
    (["--repo", "landings", "--branch", "feat/x", "--stand", "qa1"], 2, "--project"),
    (["--repo", "landings", "--branch", "feat/x", "--stand", "qa1", "--project", "promo"], "git", ""),
    (["--repo", "landings", "--branch", "feat/x", "--stand", "demo", "--project", "promo"], 2, "не выкатывает"),
    (["--repo", "landings", "--branch", "feat/event-x", "--stand", "qa1", "--project", "promo"], 2, "event"),
    (["--repo", "landings", "--branch", "feat/x", "--stand", "qa1", "--project", "lp"], "git", ""),
    (["--repo", "landings", "--branch", "feat/lp-x", "--stand", "qa1", "--project", "promo"], 2, "каталог"),
    (["--repo", "legacy-site", "--branch", "feat/x", "--stand", "qa1"], 2, "GitHub Release"),
    (["--repo", "nope", "--branch", "feat/x", "--stand", "qa1"], 1, "Неизвестный"),
    (["--repo", "backend", "--branch", "x" * 150, "--stand", "qa1"], "git", ""),
]


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    bad = 0
    for argv, want_rc, want_text in CASES:
        rc, out = run(*argv)
        ok = rc == want_rc and want_text in out
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} rc={rc!s:4} {' '.join(a if len(a) < 30 else a[:27] + '...' for a in argv)}")
        if not ok:
            print("     ожидалось rc=%s и «%s», вывод:\n     %s" % (want_rc, want_text, out.strip().replace("\n", "\n     ")))
    # симуляция CI: тег из чистого плана выкатывает ровно на один стенд
    cfg = st.load_config()
    tag = "qa1-feat-search-20261020-120000"
    hits = st.simulate_tag(cfg, cfg["repos"]["backend"], tag)
    bad += hits != ["qa1"]
    print(f"{'ok  ' if hits == ['qa1'] else 'FAIL'} simulate_tag({tag}) = {hits}")
    proj = st.simulate_project(cfg, cfg["repos"]["landings"], "promo-qa1-feat-x-20261020-120000")
    bad += proj != {"promo": ["qa1"]}
    print(f"{'ok  ' if proj == {'promo': ['qa1']} else 'FAIL'} simulate_project = {proj}")
    total = len(CASES) + 2
    print(f"\n{total - bad}/{total} ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
