#!/usr/bin/env python3
"""env-diff: какие переменные окружения ждёт код ветки сервиса и каких нет в gitops стенда.

    python3 tools/env-diff/env_diff.py --service backend --stand qa1
    python3 tools/env-diff/env_diff.py --service frontend --stand prod --ref origin/feat/x
    python3 tools/env-diff/env_diff.py --list

Код читается через `git grep` по дереву ref — рабочую копию трогать и переключать не нужно.
Gitops читается из рабочих копий gitops-репозиториев (перед прогоном стоит `git pull`).
Сервисы, gitops-репозитории и раскладка — в config.local.json рядом со скриптом (или в файле из
ENV_DIFF_CONFIG / --config); пример — config.example.json, описание — README.md.
Коды выхода: 0 — новых непокрытых переменных нет; 1 — есть, но у всех default в коде;
2 — ветка добавила переменную без default, которой нет в gitops стенда (блокер прогона);
3 — нет PyYAML или настроек.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("нужен PyYAML: python3 -m pip install pyyaml", file=sys.stderr)
    sys.exit(3)

# Консоль Git Bash/cmd на Windows по умолчанию cp1251 — кириллица и «→» ломаются.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent

# Раскладка gitops по умолчанию: ArgoCD ApplicationSet, файлы кластеров с переменной стенда и ключами
# включения приложений, values-файлы приложения в <apps_dir>/<app>/ (общий + по стенду).
LAYOUT_DEFAULTS = {
    "apps_dir": "apps",                    # <apps_dir>/<app>/<common_values>, <apps_dir>/<app>/<стенд>/<stand_values>
    "clusters_glob": "clusters/**/*.yml",  # файлы кластеров (стендов)
    "appsets_glob": "appset/**/*.yml",     # шаблоны ApplicationSet
    "stand_var": "stand_code",             # переменная шаблона {{ .stand_code }} и ключ файла кластера
    "stand_alt_key": "",                   # запасной ключ имени стенда в файле кластера
    "franchise_var": "",                   # переменная базы региона {{ .<имя> }}; пусто — регионов нет
    "enable_suffix": "-enabled",           # ключ включения приложения: <app>[-api|-worker]<suffix>: true
    "app_variants": ["-api", "-worker"],   # суффиксы вариантов приложения
    "common_values": "env.yml",            # общий values-файл приложения (все стенды)
    "stand_values": "value.yml",           # values-файл стенда
}

# Переменные, которые даёт платформа/сборка, а не gitops: в диффе только шум.
PLATFORM_VARS = {
    "NODE_ENV", "HOSTNAME", "PORT", "CI", "TZ", "HOME", "PATH", "PWD", "NODE_OPTIONS",
    "NEXT_RUNTIME", "NEXT_TELEMETRY_DISABLED", "PAYLOAD_MIGRATING", "APP_VERSION", "BUILD_TIME",
    "GIT_BRANCH", "GIT_COMMIT", "VERSION", "POD_NAME", "POD_NAMESPACE", "KUBERNETES_SERVICE_HOST",
    "SENTRY_AUTH_TOKEN", "SENTRY_ORG", "SENTRY_PROJECT", "SENTRY_URL", "SENTRY_RELEASE",
    "SENTRY_COMMIT_SHA", "NUXT_SENTRY_SOURCEMAPS", "GOOGLE_APPLICATION_CREDENTIALS",
}
SECRETISH = re.compile(r"(SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|_KEY$|_KEY_|DSN|CREDENTIALS|DATABASE_URL|_URI$|PRIVATE)")

# ---------------------------------------------------------------- извлечение из кода: шаблоны

TS_DOT = r"process\.env\.([A-Z][A-Z0-9_]+)"
# `process.env.X ?? d`, `process.env.X || d`, а также `helper('X', process.env.X, d)` — default следующим аргументом
TS_DEFAULT = re.compile(r"process\.env\.([A-Z][A-Z0-9_]+)\s*(\?\?|\|\||,\s*[^,)\s])")
GO_HELPERS_REQUIRED = {"mustEnv", "required"}
GO_HELPERS_RAW = {"os.Getenv", "os.LookupEnv"}
GO_CALL = re.compile(
    r"\b(os\.Getenv|os\.LookupEnv|mustEnv|required|optional|envOr|env|envInt|envBool|envDuration|"
    r"envStringSlice|envPositiveInt|envPositiveFloat|envPositiveDurationMillis|envDurationMillis|"
    r"parse[A-Za-z0-9]*Or|parseDuration|parseInt|parseBool|parseMillis)\(\s*\"([A-Z][A-Z0-9_]+)\""
)

# ---------------------------------------------------------------- настройки


@dataclass
class Service:
    repo: str                      # относительно repos_root
    gitops_apps: list[str]         # имена папок <apps_dir>/<app>
    kind: str                      # ts | nuxt | go | vite | nest | ts+go
    paths: list[str]               # pathspec для git grep (включая :(exclude)…)
    go_paths: list[str] = field(default_factory=list)   # для kind ts+go
    dynamic_literals: bool = False  # process.env[name] через хелперы с литералом имени
    ignore: set[str] = field(default_factory=set)
    note: str = ""


@dataclass
class GitopsRepo:
    key: str
    path: Path
    label: str
    kind: str                      # stands — только стенды; regional — центр и регионы (прод)
    stands: set[str]
    aliases: dict[str, str]        # как называют контур в разговоре → значение переменной стенда
    center: str                    # для regional: контур-центр (его values наследуют регионы)
    secrets_hint: str
    common_hint: str               # что накрывает общий values-файл — для подсказки «куда добавлять»
    layout: dict


CFG: dict = {}
SERVICES: dict[str, Service] = {}
GITOPS: dict[str, GitopsRepo] = {}


def load_config(path: str | None) -> None:
    path = path or os.environ.get("ENV_DIFF_CONFIG") or str(HERE / "config.local.json")
    if not os.path.isfile(path):
        print(f"нет настроек {path}: скопируйте config.example.json в config.local.json и опишите сервисы "
              "и gitops-репозитории (README.md, «Настройка»)", file=sys.stderr)
        sys.exit(3)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"не прочитал настройки {path}: {exc}", file=sys.stderr)
        sys.exit(3)
    base = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    root = Path(os.path.expanduser(data.get("repos_root") or "."))
    CFG["repos_root"] = root if root.is_absolute() else Path(base) / root
    PLATFORM_VARS.update(data.get("platform_vars") or [])
    layout = {**LAYOUT_DEFAULTS, **(data.get("layout") or {})}
    for name, s in (data.get("services") or {}).items():
        SERVICES[name] = Service(
            repo=s["repo"], gitops_apps=list(s.get("gitops_apps") or [name]), kind=s.get("kind", "ts"),
            paths=list(s.get("paths") or ["."]), go_paths=list(s.get("go_paths") or []),
            dynamic_literals=bool(s.get("dynamic_literals")), ignore=set(s.get("ignore") or []),
            note=s.get("note", ""))
    for key, g in (data.get("gitops") or {}).items():
        GITOPS[key] = GitopsRepo(
            key=key, path=CFG["repos_root"] / g["path"], label=g.get("label", key), kind=g.get("kind", "stands"),
            stands=set(g.get("stands") or []), aliases=dict(g.get("aliases") or {}), center=g.get("center", "prod"),
            secrets_hint=g.get("secrets_hint", ""), common_hint=g.get("common_hint", ""),
            layout={**layout, **(g.get("layout") or {})})

# ---------------------------------------------------------------- git helpers


def git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if check and r.returncode not in (0, 1):  # grep: 1 = ничего не найдено
        raise SystemExit(f"git {' '.join(args)} в {repo}: {r.stderr.strip()}")
    return r.stdout


def ref_exists(repo: Path, ref: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                          capture_output=True).returncode == 0


def default_base(repo: Path) -> str:
    for cand in ("origin/main", "origin/master", "main", "master"):
        if ref_exists(repo, cand):
            return cand
    return "HEAD"


TEST_EXCLUDES = [":(exclude)**/*.test.ts", ":(exclude)**/*.spec.ts", ":(exclude)**/*_test.go",
                 ":(exclude)**/__tests__/**", ":(exclude)**/node_modules/**"]


def git_grep_lines(repo: Path, ref: str, pattern: str, paths: list[str], files_only: bool = False) -> list[str]:
    paths = [*paths, *(e for e in TEST_EXCLUDES if e not in paths)]
    args = ["grep", "-I", "-E", "-l" if files_only else "-n", pattern, ref, "--", *paths]
    out = git(repo, *args)
    lines = [ln for ln in out.splitlines() if ln]
    # формат: REF:path:line:content → срезаем префикс ref
    return [ln[len(ref) + 1:] if ln.startswith(ref + ":") else ln for ln in lines]


# ---------------------------------------------------------------- извлечение из кода


@dataclass
class Var:
    name: str
    where: list[str] = field(default_factory=list)   # path:line
    has_default: bool = False
    required: bool = False


def extract_ts(repo: Path, ref: str, paths: list[str], dynamic: bool) -> dict[str, Var]:
    found: dict[str, Var] = {}
    for ln in git_grep_lines(repo, ref, TS_DOT, paths):
        path, line, content = ln.split(":", 2)
        for m in re.finditer(TS_DOT, content):
            v = found.setdefault(m.group(1), Var(m.group(1)))
            v.where.append(f"{path}:{line}")
        for m in TS_DEFAULT.finditer(content):
            found[m.group(1)].has_default = True
    if dynamic:
        files = git_grep_lines(repo, ref, r"process\.env\[", paths, files_only=True)
        if files:
            lit = r"['\"]([A-Z][A-Z0-9]*(_[A-Z0-9]+)+)['\"]"
            for ln in git_grep_lines(repo, ref, lit, files):
                path, line, content = ln.split(":", 2)
                for m in re.finditer(lit, content):
                    name = m.group(1)
                    v = found.setdefault(name, Var(name))
                    v.where.append(f"{path}:{line} (динамически)")
                    # хелперы вида envMs('NAME', fallback) → второй аргумент = default
                    if re.search(rf"['\"]{name}['\"]\s*,\s*[^)]", content):
                        v.has_default = True
    return found


def camel_to_snake(s: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", s).upper()


def nuxt_runtime_config_vars(repo: Path, ref: str) -> dict[str, Var]:
    """Ключи runtimeConfig из nuxt.config.ts → имена NUXT_[PUBLIC_]<SNAKE>, которыми Nitro их переопределяет.

    Разбор грубый, по строкам и скобкам: достаточно для плоских/вложенных объектов с ключами-идентификаторами.
    """
    text = git(repo, "show", f"{ref}:nuxt.config.ts", check=False)
    m = re.search(r"\bruntimeConfig\s*:\s*\{", text)
    if not m:
        return {}
    i = m.end()
    depth = 1
    body_start = i
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    body = text[body_start:i - 1]
    out: dict[str, Var] = {}
    stack: list[str] = []
    line_no = text[:body_start].count("\n") + 1
    for raw in body.splitlines():
        line_no += 1
        ln = raw.split("//")[0].strip()
        km = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", ln)
        if km:
            key, rest = km.group(1), km.group(2)
            if rest.startswith("{") and not rest.rstrip(",").endswith("}"):
                stack.append(key)
                continue
            path = [*stack, key]
            if path[0] == "public":
                name = "NUXT_PUBLIC_" + "_".join(camel_to_snake(p) for p in path[1:])
            else:
                name = "NUXT_" + "_".join(camel_to_snake(p) for p in path)
            v = out.setdefault(name, Var(name))
            v.where.append(f"nuxt.config.ts:{line_no} (runtimeConfig.{'.'.join(path)})")
            v.has_default = True   # значение всегда есть — из самого объекта runtimeConfig
        elif ln.startswith("}") and stack:
            stack.pop()
    return out


def extract_nuxt(repo: Path, ref: str, paths: list[str]) -> dict[str, Var]:
    found = extract_ts(repo, ref, paths, dynamic=False)
    pat = r"import\.meta\.env\.([A-Z][A-Z0-9_]+)"
    for ln in git_grep_lines(repo, ref, pat, ["nuxt.config.ts"]):
        path, line, content = ln.split(":", 2)
        for m in re.finditer(pat, content):
            found.setdefault(m.group(1), Var(m.group(1))).where.append(f"{path}:{line} (build-time)")
    for name, v in nuxt_runtime_config_vars(repo, ref).items():
        if name in found:
            found[name].where += v.where
            found[name].has_default |= v.has_default
        else:
            found[name] = v
    # встроенные ключи Nuxt (app.cdnURL, app.baseURL) переопределяются теми же именами
    for builtin in ("NUXT_APP_CDN_URL", "NUXT_APP_BASE_URL"):
        found.setdefault(builtin, Var(builtin, where=["(встроенный ключ Nuxt app.*)"], has_default=True))
    return found


def extract_go(repo: Path, ref: str, paths: list[str]) -> dict[str, Var]:
    found: dict[str, Var] = {}
    for ln in git_grep_lines(repo, ref, r'\("[A-Z][A-Z0-9_]+"', paths):
        path, line, content = ln.split(":", 2)
        for m in GO_CALL.finditer(content):
            helper, name = m.group(1), m.group(2)
            v = found.setdefault(name, Var(name))
            v.where.append(f"{path}:{line}")
            if helper in GO_HELPERS_REQUIRED:
                v.required = True
            elif helper not in GO_HELPERS_RAW:
                v.has_default = True   # хелпер с fallback вторым аргументом
    return found


def extract_vite(repo: Path, ref: str, paths: list[str]) -> dict[str, Var]:
    found: dict[str, Var] = {}
    pat = r"\bVITE_[A-Z0-9_]+"
    for ln in git_grep_lines(repo, ref, pat, paths):
        path, line, content = ln.split(":", 2)
        for m in re.finditer(pat, content):
            # значения приходят из .env.production при сборке — отсутствие в gitops само по себе не блокер
            found.setdefault(m.group(0), Var(m.group(0), has_default=True)).where.append(f"{path}:{line}")
    return found


def extract_nest(repo: Path, ref: str, paths: list[str]) -> dict[str, Var]:
    found = extract_ts(repo, ref, paths, dynamic=False)
    pat = r"[cC]onfig(Service)?\.get(OrThrow)?(<[^>]*>)?\(['\"]([A-Z][A-Z0-9_]+)['\"]"
    for ln in git_grep_lines(repo, ref, r"\.get(OrThrow)?(<[^>]*>)?\(['\"][A-Z][A-Z0-9_]+['\"]", paths):
        path, line, content = ln.split(":", 2)
        for m in re.finditer(pat, content):
            found.setdefault(m.group(4), Var(m.group(4))).where.append(f"{path}:{line}")
    return found


def extract(svc: Service, repo: Path, ref: str) -> dict[str, Var]:
    if svc.kind == "ts":
        out = extract_ts(repo, ref, svc.paths, svc.dynamic_literals)
    elif svc.kind == "nuxt":
        out = extract_nuxt(repo, ref, svc.paths)
    elif svc.kind == "go":
        out = extract_go(repo, ref, svc.paths)
    elif svc.kind == "vite":
        out = extract_vite(repo, ref, svc.paths)
    elif svc.kind == "nest":
        out = extract_nest(repo, ref, svc.paths)
    elif svc.kind == "ts+go":
        out = extract_ts(repo, ref, svc.paths, False)
        for k, v in extract_go(repo, ref, svc.go_paths).items():
            if k in out:
                out[k].where += v.where
                out[k].has_default |= v.has_default
            else:
                out[k] = v
    else:
        raise SystemExit(f"неизвестный kind {svc.kind}")
    for k in list(out):
        if k in PLATFORM_VARS or k in svc.ignore:
            del out[k]
    return out



# ---------------------------------------------------------------- gitops


@dataclass
class GitopsEnv:
    repo_key: str
    repo: Path
    cluster_file: Path | None
    enabled: bool | None            # <app><enable_suffix>: true в файле кластера
    appsets: list[Path]
    files: list[Path]               # value-файлы в порядке мержа (существующие)
    env: dict[str, list[str]]       # KEY → откуда (файлы/appset)
    env_raw: dict[str, str]
    secrets: list[str]
    job_env: dict[str, str]
    stand_code: str
    franchise: str | None
    where_to_add: list[str]


def pick_gitops_repo(stand: str) -> tuple[GitopsRepo, str]:
    s = stand.lower()
    for g in GITOPS.values():
        if s in g.stands:
            return g, s
    for g in GITOPS.values():
        if s in g.aliases:
            return g, g.aliases[s]
    known = "; ".join(f"{g.label} = {sorted(g.stands | set(g.aliases))}" for g in GITOPS.values())
    raise SystemExit(f"неизвестный контур «{stand}»: {known or 'gitops-репозитории не настроены'}")


def tpl(var: str) -> str:
    """Регулярка для `{{ .var }}` в шаблоне ApplicationSet."""
    return r"\{\{\s*\." + re.escape(var) + r"\s*\}\}"


def variants(layout: dict) -> str:
    return "(%s)?" % "|".join(re.escape(v) for v in layout["app_variants"]) if layout["app_variants"] else ""


def load_yaml_first(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return {"__env_fallback__": scan_env_block(text)}
    for d in docs:
        if isinstance(d, dict):
            return d
    return {}


def scan_env_block(text: str) -> dict[str, None]:
    """Ключи под строками `env:` без парсинга YAML — для appset-шаблонов с {{ }}."""
    keys: dict[str, None] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == "env:" or ln.strip() == "env: |":
            base = len(ln) - len(ln.lstrip())
            i += 1
            while i < len(lines):
                cur = lines[i]
                if cur.strip() and not cur.strip().startswith("#") and not cur.strip().startswith("{{"):
                    ind = len(cur) - len(cur.lstrip())
                    if ind <= base:
                        break
                    m = re.match(r"\s*([A-Z][A-Z0-9_]+)\s*:", cur)
                    if m:
                        keys[m.group(1)] = None
                i += 1
            continue
        i += 1
    return keys


def enable_keys(data: dict, app: str, layout: dict) -> dict[str, bool]:
    """Ключи включения приложения в файле кластера: `<app><suffix>`, `<app>-api<suffix>`, …"""
    pat = re.compile(rf"^{re.escape(app)}{variants(layout)}{re.escape(layout['enable_suffix'])}$")
    return {k: str(v).lower() == "true" for k, v in data.items() if isinstance(k, str) and pat.match(k)}


def find_cluster_file(g: GitopsRepo, stand_code: str, app: str) -> tuple[Path | None, dict]:
    lay = g.layout
    best: tuple[Path | None, dict] = (None, {})
    for f in sorted(g.path.glob(lay["clusters_glob"])):
        if "archive" in f.parts:
            continue
        data = load_yaml_first(f)
        if not isinstance(data, dict):
            continue
        names = {str(data.get(lay["stand_var"], ""))}
        if lay["stand_alt_key"]:
            names.add(str(data.get(lay["stand_alt_key"], "")))
        if stand_code not in names:
            continue
        if any(enable_keys(data, app, lay).values()):
            return f, data
        if best[0] is None:
            best = (f, data)
    return best


def find_appsets(g: GitopsRepo, app: str) -> list[Path]:
    """Appset-ы самого приложения: `<app>.yml`, `<app><suffix>.yml`, `<app>-api[<suffix>].yml`, …

    Соседи, которые лишь подмешивают его values (воркеры, secrets), не считаются.
    """
    lay = g.layout
    suffix = f"({re.escape(lay['enable_suffix'])})?" if lay["enable_suffix"] else ""
    own = re.compile(rf"^{re.escape(app)}{variants(lay)}{suffix}$")
    rel = re.compile(rf"releaseName:\s*{re.escape(app)}{variants(lay)}\s*$", re.M)
    hits: list[Path] = []
    for f in sorted(g.path.glob(lay["appsets_glob"])):
        if "archive" in str(f).lower():
            continue
        if own.match(f.stem):
            hits.append(f)
            continue
        if rel.search(f.read_text(encoding="utf-8", errors="replace")):
            hits.append(f)
    return hits


def value_files(appset_text: str, stand_code: str, franchise: str | None, layout: dict) -> list[str]:
    out: list[str] = []
    fvar = layout["franchise_var"]
    # путь до конца строки; пробелы допустимы только внутри {{ … }}: `{{ .stand_code }}`
    for m in re.finditer(r"^\s*-\s*['\"]?\$values/((?:[^\s'\"{]|\{\{[^}]*\}\})+)['\"]?\s*$", appset_text, re.M):
        p = m.group(1)
        p = re.sub(tpl(layout["stand_var"]), stand_code, p)
        if fvar and fvar in p:
            if not franchise:
                continue
            p = re.sub(tpl(fvar), franchise, p)
        if "{{" in p:
            continue
        if p not in out:
            out.append(p)
    return out


def read_gitops(stand: str, app: str) -> GitopsEnv:
    g, stand_code = pick_gitops_repo(stand)
    repo, lay = g.path, g.layout
    if not repo.exists():
        raise SystemExit(f"нет gitops-репо {repo}")
    cluster_file, cluster = find_cluster_file(g, stand_code, app)
    enabled = None
    franchise = None
    if cluster:
        keys = enable_keys(cluster, app, lay)
        enabled = any(keys.values()) if keys else None
        if lay["franchise_var"]:
            franchise = cluster.get(lay["franchise_var"])
            franchise = str(franchise) if franchise else None
    appsets = find_appsets(g, app)
    stand_templates: list[str] = []   # value-файлы с переменной стенда — подсказка «куда добавлять»
    env: dict[str, list[str]] = {}
    env_raw: dict[str, str] = {}
    job_env: dict[str, str] = {}
    secrets: list[str] = []
    files: list[Path] = []
    stand_tpl = re.compile(r"\$values/(" + re.escape(lay["apps_dir"]) + r"/[^\s'\"]*" + tpl(lay["stand_var"])
                           + r"[^\s'\"]*)")
    for aps in appsets:
        text = aps.read_text(encoding="utf-8", errors="replace")
        for key in scan_env_block(text):
            env.setdefault(key, []).append(f"appset:{aps.relative_to(repo).as_posix()}")
        for m in stand_tpl.finditer(text):
            t = re.sub(tpl(lay["stand_var"]), stand_code, m.group(1))
            if t not in stand_templates:
                stand_templates.append(t)
        for rel in value_files(text, stand_code, franchise, lay):
            f = repo / rel
            if not f.exists():
                continue
            files.append(f)
            data = load_yaml_first(f)
            rel_posix = f.relative_to(repo).as_posix()
            if "__env_fallback__" in data:
                for key in data["__env_fallback__"]:
                    env.setdefault(key, []).append(rel_posix)
                continue
            for key in (data.get("env") or {}):
                env.setdefault(str(key), []).append(rel_posix)
            for item in (data.get("envRaw") or []):
                if isinstance(item, dict) and item.get("name"):
                    env_raw[item["name"]] = rel_posix
            sec = (data.get("secrets") or {}).get("vaultSecrets") if isinstance(data.get("secrets"), dict) else None
            if isinstance(sec, list):
                secrets = [s.get("secretName") for s in sec if isinstance(s, dict) and s.get("secretName")]
            for kind in ("jobs", "cronjobs"):
                res = (data.get(kind) or {}).get("resources") if isinstance(data.get(kind), dict) else None
                for r in res or []:
                    if isinstance(r, dict):
                        for key in (r.get("env") or {}):
                            job_env[str(key)] = f"{kind}/{r.get('name')}"
                        for item in (r.get("envRaw") or []):
                            if isinstance(item, dict) and item.get("name"):
                                job_env[item["name"]] = f"{kind}/{r.get('name')}"
    where = where_to_add(g, app, stand_code, franchise, stand_templates)
    return GitopsEnv(g.key, repo, cluster_file, enabled, appsets, files, env, env_raw, secrets, job_env,
                     stand_code, franchise, where)


def where_to_add(g: GitopsRepo, app: str, stand_code: str, franchise: str | None, stand_files: list[str]) -> list[str]:
    lay = g.layout
    base = f"{g.label}/{lay['apps_dir']}/{app}"
    stand_files = stand_files or [f"{lay['apps_dir']}/{app}/{stand_code}/{lay['stand_values']}"]
    only_here = ", ".join(f"{g.label}/{p}" for p in stand_files)
    if g.kind != "regional":
        out = [f"{only_here} — только этот стенд (создать, если файла нет)",
               f"{base}/{lay['common_values']} — {g.common_hint or 'все стенды сразу'}"]
    else:
        out = [f"{base}/{lay['common_values']} — {g.common_hint or 'центр и все регионы этого репозитория'}"]
        if stand_code == g.center:
            out.append(f"{only_here} — центр; регионы наследуют его как базу")
        elif franchise:
            out.append(f"{only_here} — только регион {stand_code} (база регионов — {base}/{franchise}/{lay['stand_values']})")
        else:
            out.append(f"{only_here} — только {stand_code}")
    if g.secrets_hint:
        out.append(g.secrets_hint)
    return out


# ---------------------------------------------------------------- отчёт


def gitops_date(repo: Path) -> str:
    return git(repo, "log", "-1", "--format=%cs %h", check=False).strip() or "?"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--service", "-s", help="имя сервиса из реестра (--list)")
    ap.add_argument("--stand", "-t", help="контур: стенд или алиас из настроек (--list покажет)")
    ap.add_argument("--ref", default="HEAD", help="ветка/коммит кода (по умолчанию HEAD рабочей копии)")
    ap.add_argument("--base", help="база для «новое в ветке» (по умолчанию origin/main)")
    ap.add_argument("--repo", help="путь к репо сервиса, если не стандартный")
    ap.add_argument("--app", action="append", help="имя приложения gitops вместо реестра (можно несколько)")
    ap.add_argument("--all", action="store_true", help="показать все непокрытые, а не только новые в ветке")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true", help="список сервисов и контуров и выход")
    ap.add_argument("--config", help="файл настроек вместо config.local.json")
    a = ap.parse_args()
    load_config(a.config)

    if a.list:
        for k, s in SERVICES.items():
            print(f"{k:16} {s.kind:6} repo={s.repo}  gitops={','.join(s.gitops_apps)}" + (f"  — {s.note}" if s.note else ""))
        for g in GITOPS.values():
            print(f"контуры {g.label} ({g.kind}): {', '.join(sorted(g.stands | set(g.aliases)))}")
        return 0
    if not a.service or not a.stand:
        ap.error("нужны --service и --stand (или --list)")
    if a.service not in SERVICES:
        ap.error(f"неизвестный сервис {a.service}; см. --list")
    svc = SERVICES[a.service]
    repo = Path(a.repo) if a.repo else CFG["repos_root"] / svc.repo
    if not (repo / ".git").exists():
        raise SystemExit(f"нет git-репо {repo}")
    if not ref_exists(repo, a.ref):
        raise SystemExit(f"ref «{a.ref}» не найден в {repo} (git fetch?)")
    base = a.base or default_base(repo)
    apps = a.app or svc.gitops_apps

    code = extract(svc, repo, a.ref)
    base_names = set(extract(svc, repo, base)) if base != a.ref and ref_exists(repo, base) else set()
    new_names = sorted(set(code) - base_names) if base_names else []

    gitops: list[GitopsEnv] = [read_gitops(a.stand, app) for app in apps]
    configured: dict[str, list[str]] = {}
    for g in gitops:
        for k, src in g.env.items():
            configured.setdefault(k, []).extend(src)
        for k, src in g.env_raw.items():
            configured.setdefault(k, []).append(f"envRaw:{src}")
    secrets = sorted({s for g in gitops for s in g.secrets})

    missing = {k: v for k, v in code.items() if k not in configured}
    new_missing = [k for k in new_names if k in missing]
    blockers = [k for k in new_missing if not code[k].has_default]
    orphans = sorted(k for k in configured if k not in code and not k.startswith("NUXT_PUBLIC_SENTRY"))

    result = {
        "service": a.service, "repo": str(repo), "ref": a.ref, "base": base, "stand": a.stand,
        "gitops": [{"repo": g.repo_key, "head": gitops_date(g.repo), "cluster_file": g.cluster_file.relative_to(g.repo).as_posix() if g.cluster_file else None,
                    "enabled": g.enabled, "stand_code": g.stand_code, "franchise": g.franchise,
                    "appsets": [p.relative_to(g.repo).as_posix() for p in g.appsets],
                    "files": [p.relative_to(g.repo).as_posix() for p in g.files]} for g in gitops],
        "code_vars": len(code), "configured_vars": len(configured), "secrets": secrets,
        "new_in_branch": {k: {"has_default": code[k].has_default, "configured": k in configured, "where": code[k].where[:3]} for k in new_names},
        "missing_all": {k: {"has_default": v.has_default, "required": v.required, "secretish": bool(SECRETISH.search(k)), "where": v.where[:2]} for k, v in sorted(missing.items())},
        "orphans": orphans,
        "blockers": blockers,
    }
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if blockers else (1 if new_missing else 0)

    # --- текст
    print(f"env-diff · {a.service} · код {a.ref} (база {base}) · контур {a.stand}")
    for g in gitops:
        app_label = f"{g.repo_key}:{g.appsets[0].stem if g.appsets else '?'}"
        flag = {True: "включён", False: "ВЫКЛЮЧЕН", None: "ключ не найден"}[g.enabled]
        print(f"  gitops {app_label} @ {gitops_date(g.repo)} · кластер {g.cluster_file.relative_to(g.repo).as_posix() if g.cluster_file else '— нет файла'} · {flag}"
              + (f" · franchise={g.franchise}" if g.franchise else ""))
        for f in g.files:
            print(f"    - {f.relative_to(g.repo).as_posix()}")
        if not g.files:
            print("    (value-файлов не найдено — проверь имя приложения через --app)")
    print(f"  в коде: {len(code)} переменных · в gitops контура: {len(configured)} · секреты (ключи не в git): {', '.join(secrets) or '—'}")
    if svc.note:
        print(f"  ! {svc.note}")
    print()

    print("Новое в ветке относительно базы:" if base_names else "Базу для сравнения не нашёл — секция «новое в ветке» пропущена.")
    if base_names and not new_names:
        print("  переменных ветка не добавляет")
    for k in new_names:
        v = code[k]
        state = "есть в gitops" if k in configured else ("НЕТ в gitops, default в коде есть" if v.has_default else "НЕТ в gitops, default НЕТ → блокер")
        print(f"  {k:40} {state:42} {v.where[0] if v.where else ''}")
    print()

    if a.all or not base_names:
        print("Все переменные кода без значения в gitops контура:")
        groups = [("без default (проверить: Vault-секрет или забыли)", [k for k, v in missing.items() if not v.has_default]),
                  ("с default в коде (инфо)", [k for k, v in missing.items() if v.has_default])]
        for title, keys in groups:
            if not keys:
                continue
            print(f"  — {title}: {len(keys)}")
            for k in sorted(keys):
                tag = " [похоже на секрет]" if SECRETISH.search(k) else ""
                print(f"      {k:40}{tag}  {missing[k].where[0] if missing[k].where else ''}")
        print()
    else:
        n_nodef = sum(1 for v in missing.values() if not v.has_default)
        print(f"Всего в коде без значения в gitops: {len(missing)} (без default: {n_nodef}) — подробно с --all")
        print()

    if orphans:
        print(f"В gitops, но код по ref не читает ({len(orphans)}): {', '.join(orphans[:25])}{' …' if len(orphans) > 25 else ''}")
        print()
    print("Куда добавлять значение:")
    for line in gitops[0].where_to_add:
        print(f"  · {line}")
    print()
    if blockers:
        print(f"env-diff: БЛОКЕР — {len(blockers)} новых переменных без default и без значения на {a.stand}: {', '.join(blockers)}")
        return 2
    if new_missing:
        print(f"env-diff: ВНИМАНИЕ — {len(new_missing)} новых переменных нет в gitops {a.stand}, но у них есть default: {', '.join(new_missing)}")
        return 1
    print("env-diff: OK — новых непокрытых переменных нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
