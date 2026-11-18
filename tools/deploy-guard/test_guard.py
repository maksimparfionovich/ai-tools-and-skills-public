#!/usr/bin/env python3
"""Прогон кейсов deploy-guard: python3 tools/deploy-guard/test_guard.py

Кейсы лежат в файле, а не в командной строке: иначе включённый хук
заблокировал бы саму тестовую команду. Тесты не зависят от вашего
config.local.json: первая серия идёт на значениях по умолчанию, вторая —
на временном конфиге.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

GUARD = Path(__file__).with_name("guard.py")

BLOCK = [
    "git push origin release-12",
    "git -C backend push origin abc123:refs/tags/qa1-feat-prerelease-banner-20261008-072014",
    "cd /home/me/frontend && git push origin HEAD:refs/tags/qa1-fix-uat-20261008-072014",
    "git push origin 1a2b3c:refs/tags/stage1-fix-prod-config-20261008-072014",
    "git push --tags",
    "git push origin --follow-tags",
    "git push --mirror origin",
    "git push origin fix/landing-prod-banner",
    "cd ~/repos/argocd-apps && git push origin master",
    "git -C devops/gitops-prod push",
    "gh release create v2.0.10 --target master",
    "gh release edit v4.3.2-rc1 --draft=false",
    "gh workflow run build.yml -f deploy_target=prod",
    "curl -X POST -H 'Authorization: Bearer x' https://api.github.com/repos/example-org/site/releases -d '{\"tag_name\":\"v1\"}'",
    "gh api repos/example-org/site/releases -f tag_name=v3.0.8",
    "curl -X POST https://api.github.com/repos/example-org/backend/actions/workflows/build.yml/dispatches -d '{\"ref\":\"main\",\"inputs\":{\"deploy_target\":\"prod\"}}'",
    "gh api repos/example-org/backend/git/refs -f ref=refs/tags/release-99 -f sha=abc",
    "Git Push origin RELEASE-1",
    # gh не из PATH: gh.exe и полный путь (Windows, сессия запущена до установки gh)
    "\"/c/Program Files/GitHub CLI/gh.exe\" release create v2.0.10 --target master",
    "\"C:\\Program Files\\GitHub CLI\\gh.exe\" workflow run build.yml -f deploy_target=prod",
    "gh.exe api repos/example-org/site/releases -f tag_name=v3.0.8",
]

ALLOW = [
    "git push origin 1a2b3c4d:refs/tags/qa1-test-published-index-pr596-20261008-072014",
    "git -C frontend push origin feat/my-branch",
    "git fetch origin --tags",
    "git ls-remote --tags origin 'release*'",
    "git log --oneline release-1..main",
    "gh release list",
    "gh api repos/example-org/site/releases",
    "curl -s https://api.github.com/repos/example-org/site/releases",
    "kubectl get pods -n qa1",
    "git push origin 1a2b3c4d:refs/tags/stage1-production-banner-20261008-072014",
    "echo prod",
    "",
    "\"/c/Program Files/GitHub CLI/gh.exe\" release list",
    "gh.exe issue list -R example-org/site --search release",
]

# Gitops-репозиторий (маркер в команде или в рабочей папке — второй элемент пары): ветки пушить
# можно, main/master, пуш без явной ветки, --all и мерж PR — нельзя.
GITOPS_BLOCK = [
    ("git -C devops/argocd/argocd-apps push origin main", ""),
    ("git -C argocd-apps push origin HEAD:main", ""),
    ("git -C argocd-apps push origin HEAD:refs/heads/main", ""),
    ("git -C argocd-apps push -u origin +HEAD:main", ""),
    ("git -C argocd-apps push origin :main", ""),
    ("git -C argocd-apps push --delete origin main", ""),
    ("git -C argocd-versions push origin master", ""),
    ("git -C argocd-apps push --all origin", ""),
    ("git -C argocd-apps push origin HEAD", ""),
    ("git -C argocd-apps push", ""),
    ("git -C argocd-apps push origin", ""),
    ("git push origin main", "/home/me/devops/argocd/argocd-apps"),
    ("git push", "/home/me/argocd-apps"),
    ("gh pr merge 12 --squash -R example-org/argocd-apps", ""),
    ("gh api -X PUT repos/example-org/argocd-apps/pulls/12/merge", ""),
    ("\"/c/Program Files/GitHub CLI/gh.exe\" pr merge 12 --squash -R example-org/argocd-apps", ""),
]
GITOPS_ALLOW = [
    ("git -C devops/argocd/argocd-apps push origin qa2/backend-env", ""),
    ("git -C argocd-apps push -u origin fix/sitemap-hook", ""),
    ("git -C argocd-apps push origin HEAD:refs/heads/feat/env", ""),
    ("git -C argocd-apps push origin HEAD:feat/env", ""),
    ("git -C argocd-apps push --force-with-lease origin feat/env", ""),
    ("git -C argocd-apps push --delete origin feat/old", ""),
    ("git push origin feat/values", "/home/me/argocd-apps"),
    ("gh pr create -R example-org/argocd-apps --base main --title x", ""),
    ("git -C argocd-apps fetch origin main", ""),
    ("git push origin main", "/home/me/frontend"),   # не gitops: это правило его не касается
]

# Свой конфиг: релизное слово «ship», прод-слова «live», gitops-маркер «deploy-repo», защищённая ветка «trunk».
CUSTOM = {"release_substring": "ship", "prod_words": ["live"], "gitops_markers": ["deploy-repo"],
          "gitops_protected_branches": ["trunk"], "block_github_release": False}
CUSTOM_BLOCK = [
    "git push origin v1-ship-it",
    "git push origin fix/go-live",
    "git -C deploy-repo push",
    "git -C deploy-repo push origin trunk",
]
CUSTOM_ALLOW = [
    "git push origin release-12",          # «release» в этом конфиге не релизное слово
    "git push origin fix/prod-banner",
    "gh release create v1",                # блокировка GitHub Release выключена
    "git -C argocd-apps push",             # argocd в этом конфиге не gitops-маркер
    "git -C deploy-repo push origin main",  # main здесь не защищена
]


def make_repo(root, name, urls, worktree=False):
    """Папка с .git/config (remote origin → urls). worktree=True — .git-файл, как у `git worktree`."""
    repo = os.path.join(root, name)
    os.makedirs(os.path.join(repo, "tests"), exist_ok=True)
    lines = ['[core]\n\tbare = false\n', '[remote "origin"]\n']
    lines += [f"\t{key} = {url}\n" for key, url in urls]
    lines.append("\tfetch = +refs/heads/*:refs/remotes/origin/*\n")
    if worktree:
        common = os.path.join(root, name + "-main.git")
        wt = os.path.join(common, "worktrees", name)
        os.makedirs(wt)
        with open(os.path.join(wt, "commondir"), "w", encoding="utf-8") as fh:
            fh.write("../..\n")
        with open(os.path.join(repo, ".git"), "w", encoding="utf-8") as fh:
            fh.write(f"gitdir: {wt}\n")
        config = os.path.join(common, "config")
    else:
        os.makedirs(os.path.join(repo, ".git"))
        config = os.path.join(repo, ".git", "config")
    with open(config, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    return Path(repo).as_posix()


def safe_repo_cases(root):
    """Репозитории с «безопасными» именами веток: e2e-tests (и worktree) — да, site — нет."""
    qa = make_repo(root, "e2e-tests", [("url", "git@github.com:example-org/e2e-tests.git")])
    wt = make_repo(root, "e2e-tests-wt", [("url", "https://github.com/example-org/e2e-tests")], worktree=True)
    site = make_repo(root, "site", [("url", "git@github.com:example-org/site.git")])
    mixed = make_repo(root, "mixed", [("url", "git@github.com:example-org/e2e-tests.git"),
                                      ("pushurl", "git@github.com:example-org/site.git")])
    rootp = Path(root).as_posix()
    block = [
        ("git push origin fix/api-tests-prod-readonly", site),
        (f"git -C {site} push origin fix/prod-x", ""),
        (f"cd {qa} && cd ../site && git push origin fix/prod-x", ""),
        (f"cd {qa} && git push https://github.com/example-org/site.git fix/prod-x", ""),
        ("git push origin fix/prod-x", ""),                          # папка неизвестна
        ("git push upstream fix/prod-x", qa),                        # такого remote нет
        ("git -c core.x=y push origin fix/prod-x", qa),              # другие опции git — не угадываем
        ("git push --repo=git@github.com:example-org/site.git fix/prod-x", qa),
        ("cd $REPO && git push origin fix/prod-x", qa),
        ("git push origin fix/prod-x", mixed),                       # pushurl ведёт в site
        ("git push origin release-1", qa),                           # release проверяется и тут
        ("git push --tags", qa),
    ]
    allow = [
        ("git push origin fix/api-tests-prod-readonly", qa),
        ("git push -u origin HEAD:refs/heads/fix/uat-x", qa + "/tests"),
        (f"cd {qa} && git push origin fix/prod-x", ""),
        (f"git -C {qa} push origin fix/prod-x", ""),
        (f"cd {rootp} && git -C e2e-tests push origin fix/prod-x", ""),
        (f"cd {qa}; git push origin fix/prod-x", site),
        ("git push origin fix/prod-x", wt),
        ("git push git@github.com:example-org/e2e-tests.git fix/prod-x", ""),
        (f"git -C {qa.replace('/', chr(92))} push origin fix/prod-x", ""),  # путь с обратными слешами
    ]
    m = re.match(r"^([A-Za-z]):/(.*)$", qa)
    if os.name == "nt" and m:  # Git Bash: /c/Users/…
        allow.append((f"cd /{m.group(1).lower()}/{m.group(2)} && git push origin fix/prod-x", ""))
    return block, allow


def run(cmd, env, cwd=""):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd})
    p = subprocess.run([sys.executable, str(GUARD)], input=payload.encode("utf-8"),
                       capture_output=True, env=env)
    return p.returncode


def series(title, env, block, allow):
    """Элементы списков — команда или пара (команда, рабочая папка)."""
    bad = 0
    print("--", title)
    for want, items in ((2, block), (0, allow)):
        for item in items:
            cmd, cwd = item if isinstance(item, tuple) else (item, "")
            rc = run(cmd, env, cwd)
            bad += rc != want
            where = f"  [cwd {cwd}]" if cwd else ""
            print(f"{'ok  ' if rc == want else 'FAIL'} {'block' if want else 'allow'} rc={rc}: {cmd}{where}")
    return bad, len(block) + len(allow)


def main():
    for stream in (sys.stdout, sys.stderr):  # на Windows вывод в трубу по умолчанию в cp1251
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    total = bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        default_env = dict(os.environ, DEPLOY_GUARD_CONFIG=os.path.join(tmp, "absent.json"))
        b, n = series("значения по умолчанию", default_env, BLOCK, ALLOW)
        bad, total = bad + b, total + n
        b, n = series("gitops: ветки можно, main — нет", default_env, GITOPS_BLOCK, GITOPS_ALLOW)
        bad, total = bad + b, total + n

        # не-JSON на входе не должен ломать shell-вызов
        p = subprocess.run([sys.executable, str(GUARD)], input=b"not json", capture_output=True, env=default_env)
        bad += p.returncode != 0
        total += 1
        print(f"{'ok  ' if p.returncode == 0 else 'FAIL'} garbage stdin rc={p.returncode}")

        custom_path = os.path.join(tmp, "custom.json")
        with open(custom_path, "w", encoding="utf-8") as fh:
            json.dump(CUSTOM, fh)
        b, n = series("свой конфиг", dict(os.environ, DEPLOY_GUARD_CONFIG=custom_path), CUSTOM_BLOCK, CUSTOM_ALLOW)
        bad, total = bad + b, total + n

        safe_path = os.path.join(tmp, "safe.json")
        with open(safe_path, "w", encoding="utf-8") as fh:
            json.dump({"ref_names_safe_repos": ["e2e-tests"]}, fh)
        safe_block, safe_allow = safe_repo_cases(os.path.join(tmp, "repos"))
        b, n = series("ref_names_safe_repos: prod в ветке репо автотестов можно",
                      dict(os.environ, DEPLOY_GUARD_CONFIG=safe_path), safe_block, safe_allow)
        bad, total = bad + b, total + n
        # без настройки те же пуши в e2e-tests блокируются
        b, n = series("без ref_names_safe_repos", default_env, safe_allow[:4], [])
        bad, total = bad + b, total + n

        broken = os.path.join(tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        rc = run("git push origin release-1", dict(os.environ, DEPLOY_GUARD_CONFIG=broken))
        bad += rc != 2
        total += 1
        print(f"{'ok  ' if rc == 2 else 'FAIL'} битый конфиг → значения по умолчанию, rc={rc}")

    print(f"\n{total - bad}/{total} ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
