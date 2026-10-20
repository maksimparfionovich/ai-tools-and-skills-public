#!/usr/bin/env python3
"""PreToolUse-хук Claude Code: не даёт агенту выкатить прод или UAT из shell-команды.

Claude Code передаёт в stdin JSON вызова инструмента. Если команда похожа на выкатку
прода — причина пишется в stderr, код выхода 2: вызов блокируется, причина уходит агенту.
Всё остальное пропускается (код 0).

Что считается выкаткой прода — типичные схемы CI на тегах и gitops:
  * тег или ветка с подстрокой `release` (даже внутри слова) катит прод;
  * слово `prod`/`uat` в теге или ветке катит прод/UAT;
  * `git push --tags/--mirror/--follow-tags` уносит пачку тегов, среди них могут быть релизные;
  * в gitops-репозитории (по умолчанию в команде или рабочей папке есть `argocd`/`gitops`)
    выкатывает то, что лежит в main: прямой пуш в main/master, пуш без явной ветки, `--all`
    и мерж PR запрещены; ветки пушить можно — изменения в main попадают через PR, его мержит человек;
  * опубликованный GitHub Release катит прод;
  * ручной запуск workflow / dispatch / создание тега через API с prod/uat/release.

Настройки — в `config.local.json` рядом со скриптом или в файле из `DEPLOY_GUARD_CONFIG`
(пример — `config.example.json`, описание полей — README.md). Без файла работают значения
по умолчанию.
"""
import json
import os
import re
import shlex
import sys

DEFAULTS = {
    "release_substring": "release",
    "prod_words": ["prod", "uat"],
    "gitops_markers": ["argocd", "gitops"],
    "gitops_protected_branches": ["main", "master"],
    "block_github_release": True,
}

PUSH = re.compile(r"\bgit\b.*\bpush\b")
PUSH_ALL_TAGS = re.compile(r"(?<!\S)--(tags|mirror|follow-tags)\b")
GH_RELEASE = re.compile(r"\bgh\s+release\s+(create|edit|upload)\b")
GH_WORKFLOW_RUN = re.compile(r"\bgh\s+workflow\s+run\b")
GH_PR_MERGE = re.compile(r"\bgh\s+pr\s+merge\b")
API_WRITE = re.compile(
    r"(-x\s*(post|patch|put)\b|--request\s+(post|patch|put)\b|--method\s+(post|patch|put)\b"
    r"|(?<!\S)(-d|--data\S*|-f|-F|--field|--raw-field|--input)(?=\s|=))",
    re.IGNORECASE,
)
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
# Опции `git push`, у которых значение идёт отдельным словом.
PUSH_OPTS_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


def load_config():
    """Значения по умолчанию, поверх — config.local.json или DEPLOY_GUARD_CONFIG.
    Битый файл не ломает хук: остаются значения по умолчанию, предупреждение — в stderr."""
    cfg = dict(DEFAULTS)
    path = os.environ.get("DEPLOY_GUARD_CONFIG") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.local.json")
    if not os.path.isfile(path):
        return cfg
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except Exception as exc:
        sys.stderr.write("deploy-guard: не прочитал %s (%s), работаю со значениями по умолчанию\n" % (path, exc))
    return cfg


def word_re(words):
    words = [re.escape(w.lower()) for w in words if w]
    if not words:
        return None
    return re.compile(r"(?<![a-z0-9])(%s)(?![a-z0-9])" % "|".join(words))


def push_args(seg):
    """(refspecs, флаги) из `git … push …` в сегменте или None, если это не git push.
    Первый позиционный аргумент — remote, остальные — refspec."""
    try:
        toks = shlex.split(seg)
    except ValueError:
        toks = seg.split()
    low = [t.lower() for t in toks]
    git_at = next((i for i, t in enumerate(low) if re.search(r"(^|[\\/])git(\.exe)?$", t)), None)
    if git_at is None or "push" not in low[git_at + 1:]:
        return None
    i = low.index("push", git_at + 1) + 1
    positional, flags = [], set()
    while i < len(toks):
        t = toks[i]
        if t == "--":
            positional += toks[i + 1:]
            break
        if t.startswith("-"):
            flags.add(t.split("=", 1)[0])
            if t in PUSH_OPTS_WITH_VALUE:
                i += 1
        else:
            positional.append(t)
        i += 1
    return positional[1:], flags


def gitops_push_problem(seg, protected):
    """Причина отказа для пуша в gitops-репозиторий или None: ветки можно, main/master — нет."""
    parsed = push_args(seg)
    if parsed is None:
        return None
    refspecs, flags = parsed
    names = "/".join(protected) or "main"
    if flags & {"--all", "--mirror", "--branches"}:
        return ("git push --all/--mirror в gitops-репозиторий запрещён: уйдёт и %s, а это выкатка. "
                "Пушь одну ветку: git push origin <ветка>." % names)
    if not refspecs:
        return ("git push в gitops-репозиторий без явной ветки запрещён: пушится текущая ветка, а она может "
                "оказаться %s. Укажи ветку: git push origin <ветка>." % names)
    for spec in refspecs:
        spec = spec.lstrip("+")
        dst = spec.split(":", 1)[1] if ":" in spec else spec
        if dst.startswith("refs/tags/"):
            continue
        branch = dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst
        if branch.upper() in ("HEAD", "@"):
            return ("git push в gitops-репозиторий с HEAD запрещён: HEAD может указывать на %s. "
                    "Укажи ветку явно: git push origin <ветка>." % names)
        if branch.lower() in (p.lower() for p in protected):
            return ("git push в %s gitops-репозитория запрещён: то, что лежит в %s, выкатывается. "
                    "Пушь ветку и открывай PR — мержит человек." % (branch, branch))
    return None


def check_segment(seg, whole, cwd, cfg):
    low = seg.lower()
    release = (cfg["release_substring"] or "").lower()
    prod_word = word_re(cfg["prod_words"])
    deploy_word = (release and release in low) or (prod_word and prod_word.search(low))
    where = (whole + " " + cwd).lower()
    gitops = next((g for g in cfg["gitops_markers"] if g and g.lower() in where), None)
    protected = [b for b in cfg["gitops_protected_branches"] if b]

    if PUSH.search(low):
        if PUSH_ALL_TAGS.search(low):
            return ("git push --tags/--mirror/--follow-tags запрещён: уходит сразу пачка тегов, "
                    "среди них могут быть релизные. Тег на стенд пушится по одному: "
                    "git push origin <sha>:refs/tags/<тег>.")
        if release and release in low:
            return ("в git push есть '%s': в CI тег или ветка с этой подстрокой (даже внутри слова) "
                    "катит ПРОД. Такие пуши делает человек." % release)
        m = prod_word.search(low) if prod_word else None
        if m:
            return ("в git push есть слово '%s': в CI оно в теге или ветке катит прод/UAT. "
                    "Такие пуши делает человек." % m.group(1))
        if gitops:
            reason = gitops_push_problem(seg, protected)
            if reason:
                return reason

    if gitops and GH_PR_MERGE.search(low):
        return ("мерж PR в gitops-репозиторий запрещён: после мержа main выкатывается. "
                "PR открыт — мержит человек.")

    if cfg["block_github_release"] and GH_RELEASE.search(low):
        return ("публикация или правка GitHub Release запрещена: в части CI опубликованный релиз "
                "уезжает на ПРОД.")

    if GH_WORKFLOW_RUN.search(low) and deploy_word:
        return "ручной запуск workflow с prod/uat/release запрещён — прод катит человек."

    if "api.github.com" in low or re.search(r"\bgh\s+api\b", low):
        if cfg["block_github_release"] and "/releases" in low and API_WRITE.search(seg):
            return "создание или правка GitHub Release через API запрещены: релиз может уехать на прод."
        if "/dispatches" in low and deploy_word:
            return "workflow_dispatch с prod/uat/release через API запрещён — прод катит человек."
        if "/git/refs" in low and API_WRITE.search(seg) and deploy_word:
            return "создание тега с release/prod/uat через API запрещено — такой тег катит прод."
        if gitops and API_WRITE.search(seg) and (
                re.search(r"/pulls/\d+/merge|/merges\b", low)
                or any(re.search(r"(heads/|[?&]branch=|\"branch\"\s*:\s*\")%s\b" % re.escape(b.lower()), low)
                       for b in protected)):
            return "запись в %s gitops-репозитория через API запрещена — мержит человек." % "/".join(protected)

    return None


def main():
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0  # не наш формат — не мешаем
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        return 0
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""

    cfg = load_config()
    for seg in SEGMENT_SPLIT.split(command):
        reason = check_segment(seg, command, cwd, cfg)
        if reason:
            # bytes в UTF-8: на Windows sys.stderr по умолчанию в кодировке консоли, Claude Code читает UTF-8
            sys.stderr.buffer.write((
                "deploy-guard: команда заблокирована — " + reason +
                "\nЗапрет выкатки прода задан владельцем проекта (tools/deploy-guard). "
                "Не обходить; сообщить пользователю.\n"
            ).encode("utf-8"))
            sys.stderr.flush()
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
