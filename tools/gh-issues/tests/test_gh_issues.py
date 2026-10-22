#!/usr/bin/env python3
"""Самодостаточный тест gh-issues: python3 tools/gh-issues/tests/test_gh_issues.py

Сети и настоящего gh не нужно: вместо gh — FakeGh, который держит задачи, доску и поле приоритета
в памяти и записывает каждый вызов. Проверяется разбор файла задач, проверки плана (без записи),
создание задачи с исполнителем, приоритетом и карточкой, вписывание ссылки и пропуск при повторе.
"""
import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gh_issues as gi  # noqa: E402

CFG = {**gi.DEFAULT_CONFIG, "org": "acme", "project": {"number": 1}, "default_status": "Входящие"}

FILE = """# Задачи для доски

Вступление: не задача, в задачи не попадает.

## 1. [payload-back] Login says "Invalid credentials" when the account is locked

Репо: `acme/payload-back` · Исполнитель: @dev1 · Приоритет: High · Метки: bug

**Что происходит.** Форма отвечает «Invalid credentials».

**Где воспроизведено.** qa2 22.10.2026.

---

## 2. [coin-feed] Ticker is lost after rename

Исполнитель: dev1
Приоритет: low

**Что происходит.** Тикер пропадает. В коде: `src/feed.ts:12`.

---

## Предположения

Это не задача.
"""


class FakeGh(gi.Gh):
    """gh в памяти: задачи, метки, исполнители, поле приоритета организации и доска со статусами."""

    def __init__(self, assignable=("dev1",), labels=("bug",), titles=(), fail_priority=False, auto_status=""):
        super().__init__("gh")
        self.assignable, self.labels, self.titles = set(assignable), set(labels), list(titles)
        self.fail_priority, self.auto_status = fail_priority, auto_status
        self.calls, self.issues, self.items, self.next = [], {}, {}, 100

    def writes(self):
        kinds = {("issue", "create"), ("issue", "edit"), ("project", "item-add"), ("project", "item-edit")}
        return [c for c in self.calls if tuple(c[0][:2]) in kinds or "mutation" in (c[1] or "")]

    def run(self, args, stdin=None, check=True):
        a = list(args)
        self.calls.append((a, stdin))
        code, out = self._dispatch(a, stdin)
        if check and code != 0:
            raise gi.GhError(f"gh {' '.join(a[:3])}: exit {code}")
        return code, out

    def _opt(self, a, flag, many=False):
        vals = [a[i + 1] for i, x in enumerate(a) if x == flag]
        return vals if many else (vals[0] if vals else None)

    def _dispatch(self, a, stdin):
        if a[:2] == ["repo", "view"]:
            return 0, json.dumps({"hasIssuesEnabled": True, "viewerPermission": "WRITE"})
        if a[:2] == ["label", "list"]:
            return 0, json.dumps([{"name": l} for l in sorted(self.labels)])
        if a[0] == "api" and "/assignees/" in a[1]:
            return (0 if a[1].rsplit("/", 1)[1] in self.assignable else 1), ""
        if a[:2] == ["issue", "list"]:
            return 0, json.dumps([{"number": 1, "title": t, "url": "https://github.com/acme/x/issues/1",
                                   "state": "OPEN"} for t in self.titles])
        if a[:2] == ["issue", "create"]:
            repo, self.next = self._opt(a, "-R"), self.next + 1
            url = f"https://github.com/{repo}/issues/{self.next}"
            self.issues[url] = {"id": f"I{self.next}", "url": url, "title": self._opt(a, "--title"),
                                "body": Path(self._opt(a, "--body-file")).read_text(encoding="utf-8"),
                                "assignees": self._opt(a, "--assignee", True),
                                "labels": self._opt(a, "--label", True), "priority": "", "item": None}
            return 0, url + "\n"
        if a[:2] == ["issue", "edit"]:
            url = f"https://github.com/{self._opt(a, '-R')}/issues/{a[2]}"
            self.issues[url]["assignees"] += self._opt(a, "--add-assignee", True)
            self.issues[url]["labels"] += self._opt(a, "--add-label", True)
            return 0, ""
        if a[:2] == ["project", "item-add"]:
            issue = self.issues[self._opt(a, "--url")]
            if not issue["item"]:
                issue["item"] = f"ITEM{issue['id']}"
                self.items[issue["item"]] = self.auto_status
            return 0, json.dumps({"id": issue["item"]})
        if a[:2] == ["project", "item-edit"]:
            names = {"S_IN": "Входящие", "S_WORK": "В работе"}
            self.items[self._opt(a, "--id")] = names[self._opt(a, "--single-select-option-id")]
            return 0, ""
        if a[:2] == ["api", "graphql"]:
            return 0, json.dumps(self._graphql(json.loads(stdin)))
        raise AssertionError(f"неожиданный вызов gh {a}")

    def _graphql(self, req):
        q, v = req["query"], req["variables"]
        if "setIssueFieldValue" in q:
            if self.fail_priority:
                return {"errors": [{"message": "field is not enabled for this repository"}]}
            assert v["field"] == "F_PRIO", v
            issue = next(i for i in self.issues.values() if i["id"] == v["issue"])
            issue["priority"] = {"O_HIGH": "High", "O_LOW": "Low"}[v["option"]]
            return {"data": {"setIssueFieldValue": {"clientMutationId": None}}}
        if "issueFields" in q:
            return {"data": {"organization": {"issueFields": {"nodes": [
                {"__typename": "IssueFieldDate"},
                {"__typename": "IssueFieldSingleSelect", "id": "F_PRIO", "name": "Priority",
                 "options": [{"id": "O_HIGH", "name": "High"}, {"id": "O_LOW", "name": "Low"}]}]}}}}
        if "projectV2(number" in q:
            return {"data": {"organization": {"projectV2": {
                "id": "P1", "title": "Team board", "url": "https://github.com/orgs/acme/projects/1",
                "field": {"id": "F_STATUS", "options": [{"id": "S_IN", "name": "Входящие"},
                                                        {"id": "S_WORK", "name": "В работе"}]}}}}}
        if "repository(owner" in q:
            url = f"https://github.com/{v['owner']}/{v['name']}/issues/{v['number']}"
            i = self.issues.get(url)
            if not i:
                return {"data": {"repository": {"issue": None}}}
            items = [] if not i["item"] else [{"id": i["item"], "project": {"id": "P1", "number": 1},
                                               "fieldValueByName": {"name": self.items[i["item"]]}
                                               if self.items[i["item"]] else None}]
            values = [{"__typename": "IssueFieldSingleSelectValue", "name": i["priority"],
                       "field": {"name": "Priority"}}] if i["priority"] else []
            return {"data": {"repository": {"issue": {
                "id": i["id"], "url": url, "title": i["title"], "state": "OPEN",
                "assignees": {"nodes": [{"login": x} for x in i["assignees"]]},
                "labels": {"nodes": [{"name": x} for x in i["labels"]]},
                "issueFieldValues": {"nodes": values}, "projectItems": {"nodes": items}}}}}
        raise AssertionError(f"неожиданный запрос GraphQL: {q[:80]}")


def run_file(gh, path, apply=False, only=None):
    args = argparse.Namespace(file=str(path), only=only, apply=apply, allow_duplicate=False)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = gi.cmd_file(args, CFG, gh)
    return rc, out.getvalue()


class ParseTests(unittest.TestCase):
    def test_only_bracketed_headings_are_tasks(self):
        tasks = gi.parse_tasks(FILE)
        self.assertEqual([t["num"] for t in tasks], [1, 2])
        self.assertTrue(tasks[0]["title"].startswith("[payload-back] Login says"))

    def test_meta_on_one_line_and_on_separate_lines(self):
        first, second = (gi.task_fields(t, CFG) for t in gi.parse_tasks(FILE))
        self.assertEqual(first, {"repo": "acme/payload-back", "assignees": ["dev1"], "priority": "High",
                                 "labels": ["bug"], "status": "", "issue": ""})
        self.assertEqual(second["repo"], "acme/coin-feed")  # из префикса заголовка
        self.assertEqual((second["assignees"], second["priority"]), (["dev1"], "low"))

    def test_body_excludes_meta_and_separator(self):
        body = gi.parse_tasks(FILE)[0]["body"]
        self.assertTrue(body.startswith("**Что происходит.**"))
        self.assertNotIn("Исполнитель", body)
        self.assertNotIn("---", body)
        self.assertTrue(body.rstrip().endswith("qa2 22.10.2026."))

    def test_link_goes_after_meta_and_task_is_then_skipped(self):
        title = gi.parse_tasks(FILE)[0]["title"]
        text = gi.insert_issue_link(FILE, title, "https://github.com/acme/payload-back/issues/7")
        lines = text.splitlines()
        meta = next(i for i, l in enumerate(lines) if l.startswith("Репо:"))
        self.assertEqual(lines[meta + 1], "Задача: https://github.com/acme/payload-back/issues/7")
        task = gi.parse_tasks(text)[0]
        self.assertEqual(gi.task_fields(task, CFG)["issue"], "https://github.com/acme/payload-back/issues/7")
        self.assertNotIn("Задача:", task["body"])

    def test_link_without_meta_goes_under_heading(self):
        text = "## [svc] Title\n\nТекст.\n"
        out = gi.insert_issue_link(text, "[svc] Title", "https://github.com/acme/svc/issues/1")
        self.assertEqual(out, "## [svc] Title\n\nЗадача: https://github.com/acme/svc/issues/1\n\nТекст.\n")


class TextProblemTests(unittest.TestCase):
    def problems(self, text):
        task = gi.parse_tasks(text)[0]
        return gi.text_problems(task, gi.task_fields(task, CFG), CFG)

    def test_assignee_and_priority_are_required(self):
        errors = self.problems("## [svc] Title\n\nТекст.\n")
        self.assertTrue(any("исполнителя" in e for e in errors), errors)
        self.assertTrue(any("приоритета" in e for e in errors), errors)

    def test_bare_markers_fail_but_code_and_links_do_not(self):
        head = "## [svc] T\n\nИсполнитель: @a · Приоритет: Low\n\n"
        self.assertTrue(any("голые маркеры [R]" in e for e in self.problems(head + "Вывод [R] qa2.\n")))
        self.assertEqual(self.problems(head + "Код `[R]` и ссылка [C](https://x) — норма.\n"), [])


class FileCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "ISSUES-x.md"
        self.path.write_text(FILE, encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()


class PlanTests(FileCase):
    def test_clean_plan_writes_nothing(self):
        gh = FakeGh()
        rc, out = run_file(gh, self.path)
        self.assertEqual(rc, 0, out)
        self.assertEqual(gh.writes(), [])
        self.assertIn("доска «Team board» → Входящие", out)
        self.assertEqual(self.path.read_text(encoding="utf-8"), FILE)

    def test_plan_reports_every_remote_problem_and_writes_nothing(self):
        title = gi.parse_tasks(FILE)[0]["title"]
        gh = FakeGh(assignable=(), labels=(), titles=[title.upper()])  # дубль — без учёта регистра
        self.path.write_text(FILE.replace("Приоритет: High", "Приоритет: Highest"), encoding="utf-8")
        rc, out = run_file(gh, self.path, apply=True, only="1")
        self.assertEqual(rc, 1)
        for needle in ("@dev1 нельзя назначить", "нет меток в репозитории: bug", "нет варианта приоритета «Highest»",
                       "уже есть задача с таким заголовком", "ничего не создаю"):
            self.assertIn(needle, out)
        self.assertEqual(gh.writes(), [])
        search = next(c[0] for c in gh.calls if c[0][:2] == ["issue", "list"])
        self.assertNotIn('"Invalid', search[search.index("--search") + 1])  # кавычки заголовка не ломают поиск


class ApplyTests(FileCase):
    def test_apply_creates_assigns_prioritises_and_puts_on_board(self):
        gh = FakeGh()
        rc, out = run_file(gh, self.path, apply=True)
        self.assertEqual(rc, 0, out)
        first, second = gh.issues.values()
        self.assertEqual((first["assignees"], first["labels"], first["priority"]), (["dev1"], ["bug"], "High"))
        self.assertTrue(first["body"].startswith("**Что происходит.**"))
        self.assertEqual(second["priority"], "Low")  # «low» в файле → вариант поля «Low»
        self.assertEqual(sorted(gh.items.values()), ["Входящие", "Входящие"])
        self.assertIn("приоритет High · доска: Входящие", out)
        saved = self.path.read_text(encoding="utf-8")
        self.assertIn(f"Задача: {first['url']}", saved)
        self.assertIn(f"Задача: {second['url']}", saved)

    def test_rerun_skips_created_tasks(self):
        gh = FakeGh()
        run_file(gh, self.path, apply=True)
        created = len(gh.issues)
        rc, out = run_file(gh, self.path, apply=True)
        self.assertEqual((rc, len(gh.issues)), (0, created))
        self.assertEqual(out.count("уже заведена"), 2)

    def test_board_status_set_by_board_is_kept(self):
        gh = FakeGh(auto_status="В работе")
        rc, _ = run_file(gh, self.path, apply=True, only="1")
        self.assertEqual(rc, 0)
        self.assertEqual(list(gh.items.values()), ["В работе"])
        self.assertFalse([c for c in gh.calls if c[0][:2] == ["project", "item-edit"]])

    def test_failed_priority_is_reported_and_link_still_saved(self):
        gh = FakeGh(fail_priority=True)
        rc, out = run_file(gh, self.path, apply=True, only="1")
        self.assertEqual(rc, 1)
        self.assertIn("приоритет не поставлен", out)
        self.assertIn("Задача: https://github.com/acme/payload-back/issues/101", self.path.read_text(encoding="utf-8"))


class SetTests(unittest.TestCase):
    def test_set_dry_run_then_apply(self):
        gh = FakeGh()
        url = "https://github.com/acme/payload-ai/issues/62"
        gh.issues[url] = {"id": "I62", "url": url, "title": "[payload-ai] T", "body": "", "assignees": ["dev1"],
                          "labels": ["bug"], "priority": "", "item": "ITEM62"}
        gh.items["ITEM62"] = "Входящие"
        args = argparse.Namespace(issue="acme/payload-ai#62", priority="High", assignee=[], label=[], status=None,
                                  apply=False)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(gi.cmd_set(args, CFG, gh), 0)
        self.assertEqual(gh.writes(), [])
        args.apply = True
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(gi.cmd_set(args, CFG, gh), 0)
        self.assertEqual(gh.issues[url]["priority"], "High")
        self.assertIn("приоритет High", out.getvalue())


class MainTests(unittest.TestCase):
    def test_missing_config_is_exit_2_without_traceback(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = gi.main(["ISSUES-x.md", "--config", str(Path(tempfile.gettempdir()) / "нет-такого.json")])
        self.assertEqual(rc, 2)
        self.assertIn("нет настроек", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
