#!/usr/bin/env python3
"""gh-issues — заводит задачи GitHub из файла задач (ISSUES-*.md): исполнитель, приоритет, карточка на доске.

  python tools/gh-issues/gh_issues.py ISSUES-тема.md              # план: всё проверить, ничего не создавать
  python tools/gh-issues/gh_issues.py ISSUES-тема.md --apply      # завести задачи из плана
  python tools/gh-issues/gh_issues.py ISSUES-тема.md --only 1,3   # только разделы 1 и 3
  python tools/gh-issues/gh_issues.py set org/repo#123 --priority High [--assignee login] [--status S] [--apply]

Формат файла — README.md. Каждая задача — раздел `## N. [сервис] Заголовок`, под ним строка
`Репо: … · Исполнитель: @login · Приоритет: High · Метки: bug`, дальше текст задачи. После создания
скрипт вписывает в раздел строку `Задача: <ссылка>`; такие разделы при повторном запуске пропускаются.

Приоритет — поле организации (issue field), не метка и не поле доски. Скрипт работает через `gh`
(нужен вход с правами repo и project). Коды выхода: 0 — всё хорошо, 1 — ошибки в задачах или при
записи, 2 — ошибка запуска (нет файла, настроек, gh).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

__version__ = "0.1.0"
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = {
    "org": "",
    "project": None,               # {"number": 1} — доска организации; null — без доски
    "default_status": "",          # статус новой карточки, если доска сама его не поставила
    "priority_field": "Priority",  # имя поля организации
    "required": ["assignee", "priority"],
    "repo_from_title": True,       # `[payload-back] …` → <org>/payload-back, если строки «Репо» нет
    "gh": "",                      # путь к gh, если его нет в PATH
    "timeout_s": 120,              # предел на один вызов gh
}
GH_FALLBACKS = [r"C:\Program Files\GitHub CLI\gh.exe", "/usr/local/bin/gh", "/opt/homebrew/bin/gh"]

META_KEYS = {
    "репо": "repo", "repo": "repo",
    "исполнитель": "assignee", "исполнители": "assignee", "assignee": "assignee", "assignees": "assignee",
    "приоритет": "priority", "priority": "priority",
    "метки": "labels", "метка": "labels", "labels": "labels",
    "статус": "status", "status": "status",
    "задача": "issue", "issue": "issue",
}
RE_HEADING = re.compile(r"^##\s+(?:(\d+)[.)]\s+)?(.+?)\s*$")
RE_META = re.compile(r"^\s*\**\s*([A-Za-zА-Яа-яЁё]+)\s*\**\s*:\s*\**\s*(.*?)\s*$")
RE_BARE_MARKER = re.compile(r"(?<![\w\[])\[(R|C|T|A|\?)\](?![\w(\[])")
RE_ISSUE_REF = re.compile(r"^(?:https://github\.com/)?([\w.-]+)/([\w.-]+)(?:#|/issues/)(\d+)/?$")


class UsageError(Exception):
    pass


class GhError(Exception):
    pass


# ---------- файл задач ----------

def split_meta(line):
    """Строка метаданных → {ключ: значение} или None, если строка — не метаданные."""
    parts = [p for p in re.split(r"\s+·\s+|\s+\|\s+", line.strip()) if p.strip()]
    if not parts:
        return None
    out = {}
    for part in parts:
        m = RE_META.match(part)
        if not m or m.group(1).lower() not in META_KEYS:
            return None
        out[META_KEYS[m.group(1).lower()]] = m.group(2).strip().strip("`*_ ").strip()
    return out


def parse_tasks(text):
    """Разделы-задачи файла. Задача — заголовок `## …`, начинающийся (после номера) с `[`."""
    lines = text.splitlines()
    heads = [i for i, l in enumerate(lines) if RE_HEADING.match(l)]
    tasks, ordinal = [], 0
    for k, start in enumerate(heads):
        m = RE_HEADING.match(lines[start])
        title = m.group(2).strip()
        if not title.startswith("["):
            continue
        ordinal += 1
        end = heads[k + 1] if k + 1 < len(heads) else len(lines)
        meta, meta_last, i = {}, None, start + 1
        while i < end:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            parsed = split_meta(line)
            if parsed is None:
                break
            meta.update(parsed)
            meta_last = i
            i += 1
        body = lines[i:end]
        while body and (not body[-1].strip() or body[-1].strip() == "---"):
            body.pop()
        while body and not body[0].strip():
            body.pop(0)
        tasks.append({
            "num": int(m.group(1)) if m.group(1) else ordinal,
            "title": title,
            "line": start,
            "meta_last": meta_last,
            "meta": meta,
            "body": "\n".join(body).strip() + "\n",
        })
    return tasks


def task_fields(task, cfg):
    """Нормализованные поля задачи из метаданных и настроек."""
    meta = task["meta"]
    repo = meta.get("repo", "")
    if not repo and cfg.get("repo_from_title"):
        m = re.match(r"^\[([\w.-]+)\]", task["title"])
        repo = m.group(1) if m else ""
    if repo and "/" not in repo:
        repo = f"{cfg['org']}/{repo}" if cfg.get("org") else repo
    split = lambda v: [x.strip().lstrip("@") for x in re.split(r"[,\s]+", v or "") if x.strip().lstrip("@")]
    return {
        "repo": repo,
        "assignees": split(meta.get("assignee")),
        "priority": (meta.get("priority") or "").strip(),
        "labels": [x.strip() for x in (meta.get("labels") or "").split(",") if x.strip()],
        "status": (meta.get("status") or "").strip(),
        "issue": (meta.get("issue") or "").strip(),
    }


def insert_issue_link(text, title, url):
    """Вписать `Задача: <url>` в раздел с этим заголовком (после метаданных или под заголовком)."""
    lines = text.splitlines()
    for task in parse_tasks(text):
        if task["title"] != title:
            continue
        if task["meta_last"] is not None:
            lines.insert(task["meta_last"] + 1, f"Задача: {url}")
        else:
            lines[task["line"] + 1:task["line"] + 1] = ["", f"Задача: {url}"]
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    raise ValueError(f"раздел не найден: {title}")


def text_problems(task, fields, cfg):
    """Проблемы задачи, которые видны без сети."""
    errors = []
    if not fields["repo"]:
        errors.append("нет репозитория: строка «Репо: …» или префикс [сервис] в заголовке")
    if "assignee" in cfg["required"] and not fields["assignees"]:
        errors.append("нет исполнителя: «Исполнитель: @login»")
    if "priority" in cfg["required"] and not fields["priority"]:
        errors.append("нет приоритета: «Приоритет: <вариант поля>»")
    if not task["body"].strip():
        errors.append("пустой текст задачи")
    plain = re.sub(r"`[^`]*`", "", task["body"])
    bare = sorted(set(RE_BARE_MARKER.findall(plain)))
    if bare:
        errors.append("голые маркеры " + ", ".join(f"[{b}]" for b in bare)
                      + " — в задаче для разработчиков писать словами («Воспроизведено на …», «В коде: …»)")
    return errors


# ---------- gh ----------

def find_gh(cfg):
    for cand in [os.environ.get("GH_ISSUES_GH"), cfg.get("gh"), shutil.which("gh"), *GH_FALLBACKS]:
        if cand and (shutil.which(cand) or os.path.isfile(cand)):
            return cand
    raise UsageError("gh не найден: поставьте GitHub CLI или укажите путь в config.local.json (\"gh\")")


class Gh:
    def __init__(self, exe, timeout_s=120):
        self.exe, self.timeout_s = exe, timeout_s

    def run(self, args, stdin=None, check=True):
        try:
            p = subprocess.run([self.exe, *args], input=stdin, capture_output=True, text=True, encoding="utf-8",
                               timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            raise GhError(f"gh {' '.join(args[:3])}… не ответил за {self.timeout_s} с")
        if check and p.returncode != 0:
            raise GhError(f"gh {' '.join(args[:3])}…: {(p.stderr or p.stdout).strip()[:400]}")
        return p.returncode, p.stdout

    def json(self, args):
        return json.loads(self.run(args)[1] or "null")

    def graphql(self, query, **variables):
        body = json.dumps({"query": query, "variables": variables}, ensure_ascii=False)
        data = json.loads(self.run(["api", "graphql", "--input", "-"], stdin=body)[1])
        if data.get("errors"):
            raise GhError("GraphQL: " + "; ".join(e.get("message", "?") for e in data["errors"]))
        return data["data"]


Q_ORG_FIELDS = """query($org:String!){organization(login:$org){issueFields(first:50){nodes{
  __typename ... on IssueFieldSingleSelect{id name options{id name}}}}}}"""
Q_PROJECT = """query($org:String!,$number:Int!){organization(login:$org){projectV2(number:$number){
  id title url field(name:"Status"){... on ProjectV2SingleSelectField{id options{id name}}}}}}"""
Q_ISSUE = """query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){
  id url title state assignees(first:20){nodes{login}} labels(first:30){nodes{name}}
  issueFieldValues(first:30){nodes{__typename ... on IssueFieldSingleSelectValue{name field{... on IssueFieldSingleSelect{name}}}}}
  projectItems(first:20){nodes{id project{id number} fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name}}}}}}}"""
M_SET_FIELD = """mutation($issue:ID!,$field:ID!,$option:ID!){setIssueFieldValue(input:{issueId:$issue,
  issueFields:[{fieldId:$field,singleSelectOptionId:$option}]}){clientMutationId}}"""


class Context:
    """Всё, что нужно знать об организации и доске; читается один раз."""

    def __init__(self, gh, cfg):
        self.gh, self.cfg = gh, cfg
        self._priority, self._project, self._repo_cache = None, None, {}

    def priority_field(self):
        if self._priority is None:
            data = self.gh.graphql(Q_ORG_FIELDS, org=self.cfg["org"])
            nodes = ((data.get("organization") or {}).get("issueFields") or {}).get("nodes") or []
            name = self.cfg["priority_field"].lower()
            field = next((n for n in nodes if (n.get("name") or "").lower() == name), None)
            if not field:
                raise UsageError(f"у организации {self.cfg['org']} нет поля «{self.cfg['priority_field']}»")
            self._priority = field
        return self._priority

    def priority_option(self, value):
        field = self.priority_field()
        return next((o for o in field["options"] if o["name"].lower() == value.lower()), None)

    def project(self):
        if self._project is None and self.cfg.get("project"):
            data = self.gh.graphql(Q_PROJECT, org=self.cfg["org"], number=int(self.cfg["project"]["number"]))
            proj = (data.get("organization") or {}).get("projectV2")
            if not proj:
                raise UsageError(f"доска №{self.cfg['project']['number']} у {self.cfg['org']} не найдена")
            self._project = proj
        return self._project

    def status_option(self, value):
        proj = self.project()
        field = (proj or {}).get("field") or {}
        return next((o for o in field.get("options", []) if o["name"].lower() == value.lower()), None)

    def repo(self, repo):
        if repo not in self._repo_cache:
            code, out = self.gh.run(["repo", "view", repo, "--json", "hasIssuesEnabled,viewerPermission"], check=False)
            info = json.loads(out) if code == 0 and out.strip() else None
            if info:
                _, labels = self.gh.run(["label", "list", "-R", repo, "--limit", "500", "--json", "name"])
                info["labels"] = {x["name"].lower() for x in json.loads(labels or "[]")}
            self._repo_cache[repo] = info
        return self._repo_cache[repo]

    def assignable(self, repo, login):
        return self.gh.run(["api", f"repos/{repo}/assignees/{login}"], check=False)[0] == 0

    def duplicates(self, repo, title):
        words = re.sub(r'["\[\]]', " ", title).split()  # кавычки и скобки ломают синтаксис поиска GitHub
        _, out = self.gh.run(["issue", "list", "-R", repo, "--state", "all", "--limit", "30",
                              "--search", f'"{" ".join(words)}" in:title', "--json", "number,title,url,state"])
        return [i for i in json.loads(out or "[]") if i["title"].strip().lower() == title.strip().lower()]


def remote_problems(ctx, fields, title, check_duplicates=True):
    """Проблемы, которые видны только через GitHub (только чтение)."""
    errors = []
    info = ctx.repo(fields["repo"]) if fields["repo"] else None
    if fields["repo"] and not info:
        return [f"репозиторий {fields['repo']} не найден или нет доступа"]
    if info and not info.get("hasIssuesEnabled"):
        errors.append(f"в {fields['repo']} выключены задачи (issues)")
    for login in fields["assignees"]:
        if not ctx.assignable(fields["repo"], login):
            errors.append(f"@{login} нельзя назначить в {fields['repo']}")
    missing = [l for l in fields["labels"] if info and l.lower() not in info["labels"]]
    if missing:
        errors.append("нет меток в репозитории: " + ", ".join(missing) + " (создавать метки — только по просьбе)")
    if fields["priority"] and not ctx.priority_option(fields["priority"]):
        names = ", ".join(o["name"] for o in ctx.priority_field()["options"])
        errors.append(f"нет варианта приоритета «{fields['priority']}» (есть: {names})")
    status = fields["status"] or ctx.cfg.get("default_status")
    if status and ctx.project() and not ctx.status_option(status):
        errors.append(f"на доске нет статуса «{status}»")
    if check_duplicates and fields["repo"] and info:
        for dup in ctx.duplicates(fields["repo"], title):
            errors.append(f"уже есть задача с таким заголовком: {dup['url']} ({dup['state'].lower()})")
    return errors


# ---------- запись ----------

def issue_state(gh, url):
    m = RE_ISSUE_REF.match(url.strip())
    if not m:
        raise UsageError(f"не похоже на задачу GitHub: {url}")
    data = gh.graphql(Q_ISSUE, owner=m.group(1), name=m.group(2), number=int(m.group(3)))
    issue = (data.get("repository") or {}).get("issue")
    if not issue:
        raise GhError(f"задача не найдена: {url}")
    return issue


def current_priority(issue, field_name):
    for v in issue["issueFieldValues"]["nodes"]:
        if ((v.get("field") or {}).get("name") or "").lower() == field_name.lower():
            return v.get("name") or ""
    return ""


def ensure_board(ctx, url, status):
    """Карточка на доске и её статус. Возвращает имя статуса после записи."""
    proj = ctx.project()
    if not proj:
        return ""
    issue = issue_state(ctx.gh, url)
    item = next((n for n in issue["projectItems"]["nodes"] if n["project"]["id"] == proj["id"]), None)
    if not item:
        added = ctx.gh.json(["project", "item-add", str(ctx.cfg["project"]["number"]), "--owner", ctx.cfg["org"],
                             "--url", issue["url"], "--format", "json"])
        # статус могла уже поставить сама доска (workflow «Item added») — перечитать, а не затирать
        issue = issue_state(ctx.gh, url)
        item = next((n for n in issue["projectItems"]["nodes"] if n["id"] == added["id"]),
                    {"id": added["id"], "fieldValueByName": None})
    have = (item.get("fieldValueByName") or {}).get("name") or ""
    want = status or (ctx.cfg.get("default_status") if not have else "")
    if want and want.lower() != have.lower():
        opt = ctx.status_option(want)
        ctx.gh.run(["project", "item-edit", "--id", item["id"], "--project-id", proj["id"],
                    "--field-id", proj["field"]["id"], "--single-select-option-id", opt["id"]])
        return want
    return have


def set_priority(ctx, issue_id, value):
    field = ctx.priority_field()
    ctx.gh.graphql(M_SET_FIELD, issue=issue_id, field=field["id"], option=ctx.priority_option(value)["id"])


def verify(ctx, url, fields):
    """Сверка задачи с тем, что хотели записать. Возвращает (строка итога, ошибки)."""
    issue = issue_state(ctx.gh, url)
    errors = []
    have_assignees = {n["login"].lower() for n in issue["assignees"]["nodes"]}
    lost = [a for a in fields["assignees"] if a.lower() not in have_assignees]
    if lost:
        errors.append("не назначены: " + ", ".join("@" + a for a in lost))
    have_labels = {n["name"].lower() for n in issue["labels"]["nodes"]}
    lost = [l for l in fields["labels"] if l.lower() not in have_labels]
    if lost:
        errors.append("нет меток: " + ", ".join(lost))
    prio = current_priority(issue, ctx.cfg["priority_field"])
    if fields["priority"] and prio.lower() != fields["priority"].lower():
        errors.append(f"приоритет «{prio or '—'}» вместо «{fields['priority']}»")
    status = ""
    proj = ctx.project()
    if proj:
        item = next((n for n in issue["projectItems"]["nodes"] if n["project"]["id"] == proj["id"]), None)
        if not item:
            errors.append("нет карточки на доске")
        else:
            status = (item.get("fieldValueByName") or {}).get("name") or ""
            want = fields["status"] or ctx.cfg.get("default_status")
            if want and status.lower() != want.lower() and fields["status"]:
                errors.append(f"статус «{status or '—'}» вместо «{want}»")
            if not status and want:
                errors.append("у карточки нет статуса")
    who = ", ".join("@" + n["login"] for n in issue["assignees"]["nodes"]) or "—"
    summary = f"{issue['url']} · {who} · приоритет {prio or '—'} · доска: {status or ('нет' if proj else 'не настроена')}"
    return summary, errors


def create_issue(ctx, task, fields):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(task["body"])
        body_file = f.name
    try:
        args = ["issue", "create", "-R", fields["repo"], "--title", task["title"], "--body-file", body_file]
        for a in fields["assignees"]:
            args += ["--assignee", a]
        for l in fields["labels"]:
            args += ["--label", l]
        _, out = ctx.gh.run(args)
    finally:
        os.unlink(body_file)
    url = next((l.strip() for l in out.splitlines() if "/issues/" in l), "")
    if not url:
        raise GhError(f"gh не вернул ссылку на задачу: {out.strip()[:200]}")
    return url


def apply_rest(ctx, url, fields):
    """Всё, что ставится после создания: приоритет и карточка. Ошибки — списком, чтобы дойти до сверки."""
    errors = []
    if fields["priority"]:
        try:
            set_priority(ctx, issue_state(ctx.gh, url)["id"], fields["priority"])
        except GhError as e:
            errors.append(f"приоритет не поставлен: {e}")
    try:
        ensure_board(ctx, url, fields["status"])
    except GhError as e:
        errors.append(f"доска: {e}")
    return errors


# ---------- команды ----------

def load_config(path):
    path = Path(path or os.environ.get("GH_ISSUES_CONFIG") or HERE / "config.local.json")
    if not path.is_file():
        raise UsageError(f"нет настроек {path} — скопируйте config.example.json в config.local.json")
    cfg = {**DEFAULT_CONFIG, **json.loads(path.read_text(encoding="utf-8"))}
    if not cfg["org"]:
        raise UsageError("в настройках не задан org")
    return cfg


def cmd_file(args, cfg, gh):
    path = Path(args.file)
    if not path.is_file():
        raise UsageError(f"нет файла {path}")
    text = path.read_text(encoding="utf-8")
    tasks = parse_tasks(text)
    if args.only:
        wanted = {int(x) for x in args.only.split(",") if x.strip()}
        tasks = [t for t in tasks if t["num"] in wanted]
    if not tasks:
        raise UsageError("в файле нет разделов-задач (`## N. [сервис] Заголовок`)")
    ctx = Context(gh, cfg)
    plan, failed = [], False
    for task in tasks:
        fields = task_fields(task, cfg)
        if fields["issue"]:
            print(f"#{task['num']} уже заведена: {fields['issue']} — пропускаю")
            continue
        errors = text_problems(task, fields, cfg)
        errors += remote_problems(ctx, fields, task["title"], check_duplicates=not args.allow_duplicate)
        plan.append((task, fields, errors))
        failed |= bool(errors)
    print_plan(plan, ctx)
    if not plan:
        return 0
    if failed:
        print("\nЕсть ошибки — ничего не создаю. Поправьте файл и запустите снова.")
        return 1
    if not args.apply:
        print("\nПлан чистый. Завести: тот же вызов с --apply (только после «да» пользователя на этот список).")
        return 0
    rc = 0
    for task, fields, _ in plan:
        url = create_issue(ctx, task, fields)
        text = insert_issue_link(path.read_text(encoding="utf-8"), task["title"], url)
        path.write_text(text, encoding="utf-8")
        errors = apply_rest(ctx, url, fields)
        summary, more = verify(ctx, url, fields)
        errors += more
        print(f"#{task['num']} {summary}")
        for e in errors:
            print(f"   ✗ {e}")
        rc |= bool(errors)
    if rc:
        print("\nЧасть полей не записалась — задачи созданы, ссылки в файле. Поправить: команда set.")
    return 1 if rc else 0


def print_plan(plan, ctx):
    proj = ctx.project() if plan else None
    for task, fields, errors in plan:
        status = fields["status"] or ctx.cfg.get("default_status") or "как поставит доска"
        board = f"доска «{proj['title']}» → {status}" if proj else "без доски"
        print(f"#{task['num']} {task['title']}")
        print(f"   {fields['repo'] or '—'} · исполнитель: {', '.join('@' + a for a in fields['assignees']) or '—'}"
              f" · приоритет: {fields['priority'] or '—'} · метки: {', '.join(fields['labels']) or '—'} · {board}")
        for e in errors:
            print(f"   ✗ {e}")


def cmd_set(args, cfg, gh):
    ctx = Context(gh, cfg)
    issue = issue_state(gh, args.issue)
    m = RE_ISSUE_REF.match(args.issue.strip())
    repo = f"{m.group(1)}/{m.group(2)}"
    fields = {"repo": repo, "assignees": [a.lstrip("@") for a in args.assignee], "labels": args.label,
              "priority": args.priority or "", "status": args.status or "", "issue": issue["url"]}
    errors = remote_problems(ctx, fields, issue["title"], check_duplicates=False)
    prio = current_priority(issue, cfg["priority_field"])
    print(f"{issue['url']} — {issue['title']}")
    print(f"   сейчас: {', '.join('@' + n['login'] for n in issue['assignees']['nodes']) or '—'} · приоритет {prio or '—'}")
    print(f"   станет: +{', '.join('@' + a for a in fields['assignees']) or '—'} · приоритет {fields['priority'] or prio or '—'}"
          f" · метки +{', '.join(fields['labels']) or '—'} · статус {fields['status'] or 'не менять'}")
    for e in errors:
        print(f"   ✗ {e}")
    if errors:
        return 1
    if not args.apply:
        print("План чистый. Записать: тот же вызов с --apply (после «да» пользователя).")
        return 0
    if fields["assignees"] or fields["labels"]:
        edit = ["issue", "edit", str(m.group(3)), "-R", repo]
        for a in fields["assignees"]:
            edit += ["--add-assignee", a]
        for l in fields["labels"]:
            edit += ["--add-label", l]
        gh.run(edit)
    if fields["priority"]:
        set_priority(ctx, issue["id"], fields["priority"])
    if ctx.project():
        ensure_board(ctx, issue["url"], fields["status"])
    summary, errors = verify(ctx, issue["url"], fields)
    print(summary)
    for e in errors:
        print(f"   ✗ {e}")
    return 1 if errors else 0


def version_string():
    """Версия и коммит, если скрипт лежит в git-клоне."""
    try:
        p = subprocess.run(["git", "-C", str(HERE), "log", "-1", "--format=%h %cs", "--", "."],
                           capture_output=True, text=True, timeout=5)
        commit = p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        commit = ""
    return f"gh-issues {__version__}" + (f" ({commit})" if commit else "")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="gh_issues.py", description="Задачи GitHub из ISSUES-*.md")
    p.add_argument("--version", action="version", version=version_string())
    p.add_argument("--config", help="настройки вместо config.local.json")
    p.add_argument("--apply", action="store_true", help="записать (без флага — только план)")
    if argv and argv[0] == "set":
        p.add_argument("cmd")
        p.add_argument("issue", help="org/repo#123 или ссылка на задачу")
        p.add_argument("--priority")
        p.add_argument("--assignee", action="append", default=[])
        p.add_argument("--label", action="append", default=[])
        p.add_argument("--status")
    else:
        p.add_argument("file")
        p.add_argument("--only", help="номера разделов через запятую")
        p.add_argument("--allow-duplicate", action="store_true", help="не считать ошибкой задачу с таким же заголовком")
    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config)
        gh = Gh(find_gh(cfg), int(cfg["timeout_s"]))
        return cmd_set(args, cfg, gh) if getattr(args, "cmd", None) == "set" else cmd_file(args, cfg, gh)
    except UsageError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    except GhError as e:
        print(f"Ошибка gh: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Прервано. Уже созданные задачи вписаны в файл, повторный запуск их пропустит.", file=sys.stderr)
        return 1
    except Exception as e:  # без трейсбека по умолчанию; GH_ISSUES_DEBUG=1 — с ним
        if os.environ.get("GH_ISSUES_DEBUG"):
            raise
        print(f"Сбой: {type(e).__name__}: {e} (подробности: GH_ISSUES_DEBUG=1)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
