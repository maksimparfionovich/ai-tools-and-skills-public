#!/usr/bin/env python3
"""qa-report-lint — механическая проверка обоснованности вердиктов в QA-отчётах.

Проверяет не семантику, а механику: у каждого вердикта есть маркер основания,
PASS опирается на фактический прогон, [R] назван контуром и датой, [C] — путём
и строкой, итоговый PASS не соседствует с открытыми FAIL/BLOCKED.

Вердиктом считается ТОЛЬКО токен, стоящий первым в ячейке таблицы или сразу
после `Статус:` / `Вердикт:`. Проза вердиктом не является и не проверяется.

Единственные проверки прозы: E7 (находка F-n / BUG-n, определённая в тексте,
обязана иметь строку в таблице) и W2 (утверждение о причине без основания —
предупреждение). E8 требует у каждого FAIL причину в коде ([C]); если она не найдена — явное
«не найдена» ([?] с пояснением почему) и догадку ([A]) вместе. E9 требует у каждого FAIL
происхождение — ветка / легаси / легаси+ветка / не установлено — с основанием сравнения с базой.

Запуск:
  python3 tools/qa-report-lint/lint.py <файл.md> [...]     # CLI, exit 1 при ошибках
  python3 tools/qa-report-lint/lint.py --hook              # PostToolUse, JSON на stdin, exit 2
  python3 tools/qa-report-lint/lint.py --explain           # памятка по правилам
  --config <файл.json> — настройки вместо config.local.json (см. README.md, «Настройка»)
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Настройки проекта — config.local.json рядом с линтером, файл из QA_LINT_CONFIG или --config.
# Без файла работают значения по умолчанию.
DEFAULT_CONFIG = {
    # Корень проекта для хука и для поиска файла тест-плана. Пусто — $CLAUDE_PROJECT_DIR, затем
    # cwd из JSON хука, затем папка на два уровня выше линтера (<корень>/tools/qa-report-lint/).
    "root": "",
    # Папки (относительно корня), где хук проверяет .md. По умолчанию — только сам корень:
    # документация внутри вложенных репозиториев пишется по чужим правилам, и «| Test | Blocked by |»
    # в шапке таблицы — заголовок, а не вердикт BLOCKED без основания. Ручной вызов из CLI
    # ограничения не имеет: `lint.py <любой файл>` проверяет что попросили.
    "hook_dirs": ["."],
    "skip_files": ["CLAUDE.md", "AGENTS.md", "SKILL.md", "README.md", "MEMORY.md"],
    # Имена стендов вашего проекта (регулярные выражения) — в дополнение к встроенным:
    # devN, qaN, stageN, uatN, staging, uat, prod/прод, local/локально. Нужны правилу E3 и
    # подписи прогона словами («Воспроизведено на qa2 13.10:»).
    "stands": [],
    # Измерения «Покрытия» тест-плана: [название для сообщения, начало первой ячейки строки].
    # Пусто — девять встроенных (см. PLAN_DIMENSIONS_DEFAULT).
    "dimensions": [],
}


def load_config(path=None):
    cfg = dict(DEFAULT_CONFIG)
    path = path or os.environ.get("QA_LINT_CONFIG") or os.path.join(HERE, "config.local.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        except Exception as exc:
            # байты UTF-8: настройки читаются до перенастройки потоков в main(), а консоль Windows — cp1251
            sys.stderr.buffer.write(("qa-report-lint: не прочитал %s (%s), работаю со значениями по умолчанию\n"
                                     % (path, exc)).encode("utf-8"))
            sys.stderr.flush()
    return cfg


MARKERS = ("[T]", "[C]", "[R]", "[A]", "[?]")

# Основание словами (правило 2026-10-13). Отчёты читают разработчики, голые [R]/[C] им ничего не
# говорят. Поэтому основание можно писать подписью в начале фрагмента — «Воспроизведено на qa2
# 13.10: …», «В коде: `src/x.ts:41`», «Требование: AC-1», «Предположение: …», «Не проверено: …», —
# и она засчитывается как маркер. Фрагменты делятся по `;`, `|`, `·`, « — », «. », «(». Порядок
# важен — первое совпадение: «Причина в коде не найдена:» — это [?], а не [C].
BASIS_LABELS = (
    ("[?]", r"причина(?:\s+в\s+коде)?\s+не\s+(?:найдена|установлена)|не\s+провер\w*|не\s+удалось\s+\w+"
            r"|нет\s+доступа|недоступн\w*"),
    ("[A]", r"предположени\w*|догадк\w*|гипотез\w*"),
    ("[T]", r"требовани\w*|по\s+тикету|тикет\w*|по\s+спеке|спецификаци\w*"),
    ("[C]", r"(?:причина|место)\s+в\s+коде|в\s+коде|по\s+коду|код"),
    # [R] засчитывается, только если в подписи назван стенд или дата (см. label_matches):
    # «Воспроизведено на qa2 13.10:», а не «Воспроизведено:» и не шаг плана «на стенде: сменить слаг».
    ("[R]", r"(?:не\s+)?воспроизв\w*|проверено|проверил\w*|проверяли|прогон\w*|на\s+(?:стенде|проде)"),
)
RE_BASIS_LABEL = re.compile(
    r"(?:^|(?<=[;|·(])|(?<=\s[—–]\s)|(?<=\.\s))[\s*_`«\"]*(?:%s)(?!\w)[^:;|`]{0,60}:"
    % "|".join("(%s)" % p for _, p in BASIS_LABELS),
    re.IGNORECASE)
# E15: маркер буквами допустим только с расшифровкой в том же файле — строкой «[R] — проверено
# прогоном на стенде» (обычно в шапке: «Обозначения: …») или строкой таблицы `| [R] | … |`.
RE_MARKER_ANY = re.compile(r"\[(?:T|C|R|A|\?)\]")
RE_LEGEND_ITEM = re.compile(r"`?(\[(?:T|C|R|A|\?)\])`?\**\s*(?:—|–|-|:|=)\s*([^\[\]]*)")  # пояснение — до следующего маркера
RE_LETTER = re.compile(r"[А-Яа-яЁёA-Za-z]")
VERDICTS = ("PASS", "FAIL", "FAIL_EXPECTED", "PARTIAL", "NOT_VERIFIED", "BLOCKED", "STOP", "N/A")
BAD_VERDICT = ("FAIL", "BLOCKED", "STOP")

# Встроенные имена контуров; свои стенды проекта добавляются полем `stands` настроек (apply_config).
STANDS_BUILTIN = (r"(?<![A-Za-z])(?:(?:dev|qa|stage|uat)\d+|staging|uat)(?![A-Za-z])",
                  r"prod", r"прод", r"local", r"локально")
RE_STAND = None  # собирается в apply_config
RE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\b(?:0[1-9]|[12]\d|3[01])\.(?:0[1-9]|1[0-2])(?:\.(?:20)?\d{2})?\b")  # 2026-10-13 или 13.10[.2026]
RE_CODE_LOC = re.compile(r"[\w./@-]+\.\w{1,6}:\d+")
RE_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
RE_FENCE = re.compile(r"^\s*(```|~~~)")
RE_STATUS = re.compile(r"^\s*(?:[-*]\s*)?\*{0,2}(Статус|Вердикт|Verdict|Итог)\b[^:]*\*{0,2}\s*:\s*(.+)$", re.IGNORECASE)
RE_FINAL = re.compile(r"^\s*[-*]?\s*\*{0,2}(Вердикт|Итог|Verdict)\b", re.IGNORECASE)
RE_REPORT_NAME = re.compile(r"^(bugreport|bug|bugs|qa|review|testrun|report)[-_]|report|отчет|отчёт", re.IGNORECASE)

# E7: ID находки (F-1, BUG-7). Регистр как есть: `f-1` / `Bug-2` находкой не считаются.
RE_FINDING_ID = re.compile(r"\b(?:F|BUG)-\d+\b")
RE_FINDING_AT_START = re.compile(r"[\W_]*(?:F|BUG)-\d+\b")  # допускает `F-1`, **F-1**, «🛑 F-1»
# ID ОПРЕДЕЛЁН в прозе, если стоит в начале заголовка (`### F-1 …`) или в начале
# абзаца/пункта списка в жирном (`**F-1 …`, `- **F-1.`). Упоминание внутри текста — нет.
RE_DEF_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+[*_`]*((?:F|BUG)-\d+)\b")
RE_DEF_BOLD = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)])\s+)?\*\*\s*`?((?:F|BUG)-\d+)\b")
RE_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
RE_INLINE_CODE = re.compile(r"`[^`\n]*`")

# W2: утверждение о причине. Группа 1 — атрибуция причины (нужен [C]/[A]/[?] или
# path:line в той же строке), группа 2 — «было и до фикса / не из PR» (нужен [R]/[C]/[A]).
# Текст в `обратных кавычках` при поиске игнорируется (цитата UI/кода, не утверждение).
# Калибровка 2026-09-30: `из-за` в обычной прозе — шум (каждый второй файл), поэтому
# `из-за` проверяется только в строках таблиц и в определениях находок (F-n/BUG-n), а
# анафорическое «из-за этого/чего/которого» (причина названа в предыдущей фразе) не считается.
# `причина:` без текста после (метка над списком) и «Причина: …» внутри кавычек-цитаты не считаются.
W2_CAUSE = re.compile(
    r"поведени(?:е|я|ю|ем|и)\s+(?:payload|nuxt|next|react|vue|библиотеки|фреймворка)\b"
    r"|баг\s+(?:payload|nuxt|next|библиотеки|фреймворка)\b"
    r"|(?<![«\"“„])причина\**\s*(?:[—–:]|-(?=\s))\**\s*(?=[^\s*|])",
    re.IGNORECASE)
W2_IZZA = re.compile(r"\bиз-за\b(?!\s+(?:этого|чего|которо\w*|всего)\b)", re.IGNORECASE)
W2_BASELINE = re.compile(
    r"не\s+из\s+(?:этого\s+)?(?:pr|пр|фикса|ветки)\b"
    r"|(?:есть|было|воспроизводится|воспроизвелась|воспроизвёлся)\s+и\s+без\s+фикса"
    r"|pre-?existing"
    r"|до\s+фикса\s+тоже",
    re.IGNORECASE)
W2_CAUSE_OK = ("[C]", "[A]", "[?]")
W2_BASELINE_OK = ("[R]", "[C]", "[A]")

# E9: происхождение бага (правило 2026-10-12). Значение — в колонке «Происхождение» таблицы
# или в строке: `Происхождение: <значение>`. Колонка приоритетнее: в ячейке основания рядом с
# ней `происхождение: [C] …` — метка основания, а не значение.
RE_ORIGIN_HEADER = re.compile(r"происхождени|ветка\s*/\s*легаси", re.IGNORECASE)
RE_ORIGIN_INLINE = re.compile(r"происхождени[ея]\**\s*:\s*\**", re.IGNORECASE)
RE_ORIGIN_VALUE = re.compile(r"[\s*_`«\"(]*((?:легаси|legacy)\s*\+\s*ветка|ветка|легаси|legacy|не\s+установлено)(?![\w+])", re.IGNORECASE)
# E16 (правило 2026-10-14): метки «ветка / легаси / легаси+ветка» — наша стенография, разработчику
# без расшифровки непонятны. Метка допустима, если в файле есть её расшифровка (строка «легаси — дефект
# есть и без ветки …» вне таблиц проверок или таблица `| легаси | … |`), или если в той же строке рядом
# стоит пояснение словами. Происхождение можно писать и только словами — тогда расшифровка не нужна.
ORIGIN_WORDS = (
    ("легаси+ветка", r"(?:стар\w*\s+(?:причин|дефект)\w*|причина\s+старая)[^;|]{0,40}?ветка\s+(?:\w+\s+)?(?:проявила|усилила|открыла)"
                     r"|ветка\s+(?:\w+\s+)?(?:проявила|усилила|открыла)\s+стар\w*"),
    ("ветка", r"внес(?:ён|ено|ен|ла|ли)\s+(?:проверяемой\s+|этой\s+)?веткой|внесла\s+(?:проверяемая\s+|эта\s+)?ветка"
              r"|появил\w*\s+в\s+ветке|дефект\s+ветки"),
    ("легаси", r"есть\s+и\s+без\s+ветки|(?:так\s+)?был\w*\s+и\s+до\s+ветки|есть\s+и\s+на\s+(?:main|проде|базе)|старый\s+дефект"),
)
RE_ORIGIN_WORDS = re.compile(r"[\s*_`«\"(]*(?:%s)" % "|".join("(%s)" % p for _, p in ORIGIN_WORDS), re.IGNORECASE)
ORIGIN_LABELS = ("ветка", "легаси", "легаси+ветка")
RE_ORIGIN_LEGEND = re.compile(
    r"(?<![\w+])[`*_«\"]*((?:легаси|legacy)\s*\+\s*ветка|ветка|легаси|legacy)[`*_»\"]*\s*(?:—|–|=|\s-\s)", re.IGNORECASE)
RE_ORIGIN_LEGEND_STOP = re.compile(
    r"[;|]|\.\s|(?<![\w+])[`*_«\"]*(?:легаси|legacy|ветка)[`*_»\"]*\s*(?:—|–|=|\s-\s)", re.IGNORECASE)
# База сравнения названа: main/прод/baseline/diff/blame/релиз/PR/диапазон SHA. Механика, не
# семантика: линтер видит, что база названа, а не что сравнение действительно сделано.
RE_BASE_REF = re.compile(
    r"(?<![\w/.-])(?:origin/)?(?:main|master|develop)(?![\w-])"
    r"|\bprod\b|\bпрод(?:[еау]|ом)?\b"
    r"|baseline|\bбазов\w*"
    r"|\bdiff\b|\bblame\b|\bmerge-base\b|\bgit\s+(?:show|log)\b"
    r"|\b[0-9a-f]{7,40}\.\.\.?[0-9a-f]{7,40}\b"
    r"|\brelease-\d+|\bрелиз\w*|\bPR\s*#?\d+"
    r"|\b(?:без|до)\s+(?:ветки|фикса|PR|релиза)\b",
    re.IGNORECASE)

# Тест-план (правило 2026-10-13): план — до первого прогона; отчёт ссылается на план, и каждый
# кейс плана получает вердикт; в плане — блок «Контекст проверки» со строкой env, таблица кейсов
# с источником и раздел «Покрытие», где у каждого из девяти измерений есть кейсы или N/A с причиной.
RE_PLAN_NAME = re.compile(r"^testplan[-_.]", re.IGNORECASE)
RE_PLAN_TITLE = re.compile(r"тест-план|test\s*plan", re.IGNORECASE)
RE_BUGS_NAME = re.compile(r"^(bugreport|bugs?)[-_]", re.IGNORECASE)
RE_PLAN_LINK = re.compile(r"^\s*(?:[-*]\s*)?\*{0,2}тест-план\*{0,2}\s*:\s*\**\s*(.*)$", re.IGNORECASE)
RE_PLAN_PATH = re.compile(r"[^\s`'\"()\[\]<>|]+\.md(?:\.fixture)?(?![\w.])", re.IGNORECASE)
RE_PLAN_SECTION = re.compile(r"^[\W_]*(?:тест-план|test\s*plan)", re.IGNORECASE)  # `## Тест-план` в отчёте
RE_COVERAGE = re.compile(r"покрыти", re.IGNORECASE)
RE_CONTEXT = re.compile(r"контекст\s+проверки", re.IGNORECASE)
RE_ENV_LINE = re.compile(r"^\s*[-*]\s*[*_`]*\s*(?:env|переменн)", re.IGNORECASE)  # пункт `- env: …`
RE_NA = re.compile(r"^[\s*_`]*N/A", re.IGNORECASE)
RE_CASE_ID = re.compile(r"[A-Za-zА-Яа-я]{0,6}[-_.]?\d+[\w.-]*")
RE_CASE_HEADER = re.compile(r"^(?:check\s+)?id$|^кейс$", re.IGNORECASE)
PLAN_DIMENSIONS_DEFAULT = (  # (как называть в сообщении, начало первой ячейки строки «Покрытия»)
    ("Требования", "требован"),
    ("Изменения в коде", "изменени"),
    ("Негатив и границы", "негатив"),
    ("Локали и домены", "локал"),
    ("Регион и запись в БД", "регион"),
    ("Окружение", "окружени"),
    ("База (ветка или легаси)", "баз"),
    ("Регресс и автотесты", "регресс"),
    ("Интерфейс: desktop / mobile", "интерфейс"),
)
PLAN_DIMENSIONS = PLAN_DIMENSIONS_DEFAULT
CONFIG = dict(DEFAULT_CONFIG)


def apply_config(cfg):
    """Собирает из настроек то, что зависит от проекта: список стендов и измерения покрытия."""
    global CONFIG, RE_STAND, PLAN_DIMENSIONS
    CONFIG = cfg
    stands = list(STANDS_BUILTIN) + [s for s in cfg.get("stands") or [] if isinstance(s, str) and s]
    RE_STAND = re.compile("(?:%s)" % "|".join("(?:%s)" % s for s in stands), re.IGNORECASE)
    dims = [tuple(d) for d in cfg.get("dimensions") or [] if isinstance(d, (list, tuple)) and len(d) == 2]
    PLAN_DIMENSIONS = tuple((label, prefix.lower()) for label, prefix in dims) or PLAN_DIMENSIONS_DEFAULT

E7_MSG = "находка %s описана в тексте, но в таблице нет строки с её вердиктом — вынеси в таблицу (ID | суть | вердикт | основание)"

RULES = {
    "E1": "вердикт без основания — добавь [T]/[C]/[R]/[A]/[?] (с расшифровкой в шапке) или подпись словами: «Воспроизведено на qa2 13.10:», «В коде:», «Требование:», «Предположение:», «Не проверено:»",
    "E2": "PASS без [R] (или «Воспроизведено/Проверено на <стенд> <дата>:») — фактического прогона нет; это NOT_VERIFIED «по коду ожидается X»",
    "E3": "[R] без контура и даты — назови стенд (qa1, staging, prod, local или свой из поля stands настроек) и дату YYYY-MM-DD, в строке или в шапке",
    "E4": "[C] без path:line — укажи файл и строку",
    "E5": "итоговый вердикт PASS при открытых FAIL/BLOCKED/STOP в таблице",
    "E6": "NOT_VERIFIED/BLOCKED без причины — напиши, почему проверка недоступна",
    "E7": "находка F-n / BUG-n, определённая в тексте (заголовок `### F-1`, абзац/пункт `**F-1 …`), без строки в таблице — вынеси в таблицу (ID | суть | вердикт | основание); снятая находка тоже получает строку (N/A)",
    "E8": "FAIL без причины в коде — назови [C] path:line, откуда следует дефект; если причина не найдена — скажи это явно ([?] причина в коде не найдена, потому что …) и добавь догадку ([A] …)",
    "E8_NO_GUESS": "FAIL: сказано, что причина не найдена ([?]), но нет догадки — добавь [A] <гипотеза о причине>",
    "E8_NO_NOTFOUND": "FAIL: есть догадка [A], но не сказано явно, что причина в коде не найдена — добавь [?] причина в коде не найдена, потому что …",
    "E9": "FAIL без происхождения — баг ветки или легаси: колонка «Происхождение» или `Происхождение: <ветка | легаси | легаси+ветка | не установлено>` в строке, с основанием сравнения с базой",
    "E9_VALUE": "FAIL: неизвестное значение происхождения «%s» — допустимо: ветка, легаси, легаси+ветка, не установлено",
    "E9_BASIS": "FAIL: происхождение «%s» без основания — назови базу сравнения: [C] `path:line` в main `<sha>` / diff `<base>..<head>` / blame, или [R] baseline на проде / стенде с main; либо сошлись на находку с происхождением (см. F-n)",
    "E9_UNKNOWN": "FAIL: происхождение «не установлено» — нужны [?] почему не установлено (что мешает сравнить с базой) и [A] догадка",
    "E10": "отчёт без тест-плана — в шапке `Тест-план: TESTPLAN-<тема>.md`, или раздел `## Тест-план` в самом отчёте, или `Тест-план: N/A — <почему>` для отчёта без прогона (скил qa-evidence, «Тест-план»)",
    "E10_NA": "`Тест-план: N/A` без причины — напиши, почему у отчёта нет плана (например, ревью кода без прогона)",
    "E10_MISSING": "файл тест-плана «%s» не найден — путь относительно отчёта или корня проекта",
    "E11": "кейс тест-плана без вердикта ни в отчёте, ни в плане — каждый кейс плана получает строку в таблице проверок (N/A / NOT_VERIFIED / BLOCKED тоже вердикт, с причиной)",
    "E11_LIST": "кейсы тест-плана без вердикта ни в отчёте, ни в плане: %s — поставь строку в таблицу проверок (N/A / NOT_VERIFIED / BLOCKED тоже вердикт, с причиной)",
    "E12": "в тест-плане нет раздела «Покрытие» — таблица «Измерение | Кейсы или N/A — почему» по всем измерениям покрытия",
    "E12_DIM": "в «Покрытии» нет строки «%s» — укажи ID кейсов или `N/A — почему`",
    "E12_EMPTY": "в «Покрытии» у измерения «%s» нет ни ID кейса из таблицы кейсов, ни `N/A — почему`",
    "E12_NA": "в «Покрытии» у измерения «%s» стоит N/A без причины — напиши, почему измерение не нужно",
    "E13": "в тест-плане нет блока «Контекст проверки» (скил stand-topology) — он идёт до кейсов",
    "E13_ENV": "в блоке «Контекст проверки» нет строки env — результат `env-diff` или `env: N/A — почему`",
    "E14": "в тест-плане нет таблицы кейсов — шапка `ID | Что проверяем | Источник | Тип | Контур / локали | Ожидание | Способ`",
    "E14_SRC": "кейс %s без источника — маркер [T] пункт требования / [C] path:line / [A] ожидание сформулировал я (или словами: «Требование: …», «В коде: …», «Предположение: …»)",
    "E16": "происхождение %s без расшифровки — разработчику метка ничего не говорит: добавь в шапку строку «Происхождение: ветка — дефект внесён проверяемой веткой, на main и проде его нет; легаси — дефект есть и без ветки, на main или проде; легаси+ветка — старая причина, которую ветка проявила или усилила» или пиши словами рядом с меткой либо вместо неё («легаси — есть и без ветки», «внесён веткой», «старая причина, ветка проявила»)",
    "E15": "маркеры %s без расшифровки — разработчику буквы ничего не говорят: добавь в шапку строку «Обозначения: [T] — требование, [C] — место в коде, [R] — проверено прогоном на стенде, [A] — предположение, [?] — не проверено» или пиши основание словами («Воспроизведено на qa2 13.10: …», «В коде: path:line», «Требование: …», «Предположение: …», «Не проверено: …»)",
    "W0": "нет ни одной вердиктной строки — вердикты отчёта должны жить в таблице проверок",
    "W1": "PASS «в пределах доступного» — это PARTIAL: часть проверок не закрыта",
    "W2": "утверждение о причине без основания в той же строке: «поведение Payload», «причина —», «из-за» требуют [C]/[A]/[?]; «не из PR», «есть и без фикса», «pre-existing» требуют [R]/[C]/[A]. Раздел «Предположения» и ``` -блоки не проверяются",
}


def strip_fences(lines):
    """Возвращает [(номер, текст)] без содержимого fenced-блоков."""
    out, inside = [], False
    for i, raw in enumerate(lines, 1):
        if RE_FENCE.match(raw):
            inside = not inside
            continue
        if not inside:
            out.append((i, raw.rstrip("\n")))
    return out


def first_token(cell):
    """Вердикт-токен, если он стоит ПЕРВЫМ значимым словом ячейки/значения."""
    s = cell.strip().lstrip("*_`").strip()
    up = s.upper()
    for v in sorted(VERDICTS, key=len, reverse=True):
        if not up.startswith(v):
            continue
        tail = s[len(v):]
        # граница слова: `failed:` / `passed` / `n/available` вердиктами не являются
        if tail and (tail[0].isalnum() or tail[0] == "_"):
            continue
        return v
    return None


def verdict_lines(clean):
    """[(номер, токен, текст_строки, остаток_после_токена, is_final)]"""
    found = []
    for num, text in clean:
        if text.lstrip().startswith("|"):
            cells = [c for c in text.strip().strip("|").split("|")]
            if all(set(c.strip()) <= set("-: ") for c in cells if c.strip()):
                continue  # разделитель таблицы
            for cell in cells:
                tok = first_token(cell)
                if tok:
                    head, _, tail = text.partition(cell.strip())
                    rest = cell.strip()[len(tok):] + " " + tail
                    found.append((num, tok, text, rest, False))
                    break
            continue
        m = RE_STATUS.match(text)
        if m:
            tok = first_token(m.group(2))
            if tok:
                rest = m.group(2).strip()[len(tok):]
                found.append((num, tok, text, rest, bool(RE_FINAL.match(text))))
    return found


def header_has_context(clean, limit=15):
    head = " ".join(t for n, t in clean if n <= limit)
    return bool(RE_STAND.search(head)), bool(RE_DATE.search(head))


def label_matches(text):
    """[(маркер, конец подписи)] для подписей основания словами в строке."""
    out = []
    for m in RE_BASIS_LABEL.finditer(text):
        idx = next(i for i, g in enumerate(m.groups()) if g is not None)
        marker = BASIS_LABELS[idx][0]
        if marker == "[R]" and not (RE_STAND.search(m.group(0)) or RE_DATE.search(m.group(0))):
            continue
        out.append((marker, m.end()))
    return out


def basis(text):
    """Маркеры основания в строке: буквами ([R]) и подписями словами («Воспроизведено на qa2 13.10:»)."""
    return {m for m in MARKERS if m in text} | {mk for mk, _ in label_matches(text)}


def has_marker_note(text, marker):
    """Маркер (`[?]`, `[A]`) с непустым пояснением после него: для [X] — до следующего маркера,
    для подписи словами — до конца фрагмента."""
    tails = [re.split(r"\[[TCRA?]\]", text[m.end():], maxsplit=1)[0]
             for m in re.finditer(re.escape(marker), text)]
    tails += [re.split(r"[;|]|\[[TCRA?]\]", text[end:], maxsplit=1)[0]
              for mk, end in label_matches(text) if mk == marker]
    return any(len(re.sub(r"[\s\-—–|*`:;,.]", "", t)) >= 8 for t in tails)


def legend_errors(clean):
    """E15: маркер буквами без расшифровки в файле — разработчику [R] ничего не говорит."""
    first_use, explained = {}, set()
    for num, text in clean:
        if text.lstrip().startswith("|"):
            cells = cells_of(text)
            head = cells[0].strip("*_` ")
            if head in MARKERS and len(cells) > 1 and len(RE_LETTER.findall(cells[1])) >= 3:
                explained.add(head)
        elif not RE_STATUS.match(text):
            explained.update(m.group(1) for m in RE_LEGEND_ITEM.finditer(text) if len(RE_LETTER.findall(m.group(2))) >= 3)
        for mk in RE_MARKER_ANY.findall(text):
            first_use.setdefault(mk, num)
    missing = sorted((num, mk) for mk, num in first_use.items() if mk not in explained)
    if not missing:
        return []
    return [("E15", missing[0][0], RULES["E15"] % ", ".join(mk for _, mk in missing))]


def has_reason_note(text):
    """`[?]` с непустым пояснением: явно сказано, что причина не найдена и почему."""
    return has_marker_note(text, "[?]")


def cause_problem(text):
    """E8: чего не хватает FAIL-у. None — причина названа ([C]) или честно не найдена
    ([?] с пояснением) и при этом есть догадка ([A] с пояснением)."""
    if "[C]" in basis(text):
        return None
    not_found = has_reason_note(text)
    guess = has_marker_note(text, "[A]")
    if not_found and guess:
        return None
    if not_found:
        return "E8_NO_GUESS"
    if guess:
        return "E8_NO_NOTFOUND"
    return "E8"


def cells_of(text):
    return [c.strip() for c in text.strip().strip("|").split("|")]


def row_finding_id(text):
    """ID находки (F-1, BUG-7), если строка таблицы с него начинается."""
    if not text.lstrip().startswith("|"):
        return None
    first = cells_of(text)[0].strip("*_`").strip()
    if not RE_FINDING_AT_START.match(first):
        return None
    m = RE_FINDING_ID.search(first)
    return m.group(0) if m else None


def table_headers(clean):
    """{номер строки тела таблицы: ячейки шапки}. Шапка — строка над разделителем `| --- |`."""
    out, header, prev, last_row = {}, None, None, None
    for num, text in clean:
        if not text.lstrip().startswith("|"):
            header, prev, last_row = None, None, None
            continue
        if last_row != num - 1:
            header, prev = None, None
        last_row = num
        cells = cells_of(text)
        if all(set(c) <= set("-: ") for c in cells if c):
            header = prev
            continue
        if header is not None:
            out[num] = header
        prev = cells
    return out


def origin_canon(label):
    v = re.sub(r"\s*\+\s*", "+", re.sub(r"\s+", " ", label.lower()))
    return v.replace("legacy", "легаси")


def origin_value(s):
    """(значение, вид) происхождения в начале s или None. Вид: «label» — голая метка (нужна
    расшифровка в файле, E16), «words» — пояснено словами («легаси — есть и без ветки» или сразу
    «есть и без ветки»)."""
    m = RE_ORIGIN_VALUE.match(s)
    if m:
        value = origin_canon(m.group(1))
        if value != "не установлено" and RE_ORIGIN_WORDS.search(s[m.end():m.end() + 120]):
            return value, "words"
        return value, "label"
    m = RE_ORIGIN_WORDS.match(s)
    if m:
        idx = next(i for i, g in enumerate(m.groups()) if g is not None)
        return ORIGIN_WORDS[idx][0], "words"
    return None


def snippet(s):
    return re.split(r"\s[—–-]\s|[;|]", s, maxsplit=1)[0].strip(" *_`")[:40]


def origin_info(text, header):
    """(значение, сырое, вид): значение — каноническое или None; сырое — что стоит на месте
    значения, если оно не распознано (None — происхождение не указано вовсе); вид — label/words."""
    raw = None
    if header:
        cells = cells_of(text)
        for i, h in enumerate(header):
            if RE_ORIGIN_HEADER.search(h) and i < len(cells):
                found = origin_value(cells[i])
                if found:
                    return found[0], None, found[1]
                raw = snippet(cells[i]) or None
                break
    for m in RE_ORIGIN_INLINE.finditer(text):
        found = origin_value(text[m.end():])
        if found:
            return found[0], None, found[1]
        if raw is None:
            raw = snippet(text[m.end():]) or None
    return None, raw, None


def origin_of(text, header):
    value, raw, _ = origin_info(text, header)
    return value, raw


def origin_legend_errors(clean, verdicts):
    """E16: метка происхождения у FAIL без расшифровки в файле и без пояснения словами рядом."""
    headers = table_headers(clean)
    used = {}
    for num, tok, text, _, fin in verdicts:
        if tok != "FAIL" or fin:
            continue
        value, _, kind = origin_info(text, headers.get(num))
        if kind == "label" and value in ORIGIN_LABELS:
            used.setdefault(value, num)
    if not used:
        return []
    explained = set()
    for num, text in clean:
        if text.lstrip().startswith("|"):
            cells = cells_of(text)
            head = origin_canon(cells[0].strip("*_`«»\"() "))
            if head in ORIGIN_LABELS and len(cells) > 1 and len(RE_LETTER.findall(cells[1])) >= 8:
                explained.add(head)
            continue
        if RE_STATUS.match(text):
            continue
        for m in RE_ORIGIN_LEGEND.finditer(text):
            tail = RE_ORIGIN_LEGEND_STOP.split(text[m.end():], maxsplit=1)[0]
            if len(RE_LETTER.findall(tail)) >= 8:
                explained.add(origin_canon(m.group(1)))
    missing = sorted((num, v) for v, num in used.items() if v not in explained)
    if not missing:
        return []
    return [("E16", missing[0][0], RULES["E16"] % ", ".join("«%s»" % v for _, v in missing))]


def origin_problem(text, header, linked=frozenset(), own_id=None):
    """E9: (ключ RULES, значение) — чего не хватает FAIL-у по происхождению, или None."""
    value, raw = origin_of(text, header)
    if value is None:
        return ("E9_VALUE", raw) if raw else ("E9", None)
    if value == "не установлено":
        if has_marker_note(text, "[?]") and has_marker_note(text, "[A]"):
            return None
        return ("E9_UNKNOWN", value)
    if basis(text) & {"[C]", "[R]"} and RE_BASE_REF.search(text):
        return None
    if (set(RE_FINDING_ID.findall(text)) - {own_id}) & linked:
        return None
    return ("E9_BASIS", value)


def origin_errors(clean, verdicts):
    """E9: у каждого FAIL (кроме итогового) — происхождение: ветка / легаси / легаси+ветка /
    не установлено, с основанием. Строка проверок может сослаться на находку («см. F-1»),
    у которой происхождение обосновано."""
    headers = table_headers(clean)
    rows = [(num, text) for num, tok, text, _, fin in verdicts if tok == "FAIL" and not fin]
    problems = {num: origin_problem(text, headers.get(num), own_id=row_finding_id(text)) for num, text in rows}
    linked = {row_finding_id(text) for num, text in rows if problems[num] is None} - {None}
    out = []
    for num, text in rows:
        problem = problems[num]
        if problem and problem[0] == "E9_BASIS" and linked:
            problem = origin_problem(text, headers.get(num), linked, row_finding_id(text))
        if problem:
            key, value = problem
            msg = RULES[key] % value if "%s" in RULES[key] else RULES[key]
            out.append(("E9", num, msg))
    return out


def finding_errors(clean):
    """E7: находка F-n/BUG-n, определённая в прозе, без строки таблицы, где она первая ячейка."""
    in_table = set()
    for num, text in clean:
        if not text.lstrip().startswith("|"):
            continue
        first = cells_of(text)[0].strip("*_`").strip()
        if RE_FINDING_AT_START.match(first):
            in_table.update(RE_FINDING_ID.findall(first))
    out, seen = [], set()
    for num, text in clean:
        if text.lstrip().startswith("|"):
            continue
        m = RE_DEF_HEADING.match(text) or RE_DEF_BOLD.match(text)
        if m and m.group(1) not in in_table and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(("E7", num, E7_MSG % m.group(1)))
    return out


def cause_warnings(clean):
    """W2: утверждение о причине без основания в той же строке (вне «Предположений» и ``` -блоков)."""
    out, skip_level = [], None
    for num, text in clean:
        h = RE_HEADING.match(text)
        if h:
            level = len(h.group(1))
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if skip_level is None and "предположени" in h.group(2).lower():
                skip_level = level
            continue
        if skip_level is not None:
            continue
        probe = RE_INLINE_CODE.sub(" ", text)
        m = W2_CAUSE.search(probe)
        if not m and (text.lstrip().startswith("|") or RE_DEF_HEADING.match(text) or RE_DEF_BOLD.match(text)):
            m = W2_IZZA.search(probe)
        if m and not basis(text) & set(W2_CAUSE_OK) and not RE_CODE_LOC.search(text):
            out.append(("W2", num, "утверждение о причине без основания: «%s» — добавь [C] path:line или пометь [A]" % m.group(0).strip(" *")))
        m = W2_BASELINE.search(probe)
        if m and not basis(text) & set(W2_BASELINE_OK):
            out.append(("W2", num, "утверждение о причине без основания: «%s» — добавь [R] baseline-прогон, [C] diff или [A]" % m.group(0)))
    return out


def is_separator(cells):
    return all(set(c) <= set("-: ") for c in cells if c)


def case_id(cell):
    """ID кейса из первой ячейки строки таблицы (`c1`, `A-1`, **G12**) или None."""
    s = cell.strip().strip("*_`").strip()
    return s if RE_CASE_ID.fullmatch(s) else None


def table_ids(clean):
    """ID из первых ячеек всех строк таблиц (шапки и разделители не дают ID)."""
    out = set()
    for num, text in clean:
        if text.lstrip().startswith("|"):
            cid = case_id(cells_of(text)[0])
            if cid:
                out.add(cid)
    return out


def case_rows(clean):
    """[(ID, номер, текст)] строк таблиц кейсов: шапка начинается с `ID`/`Check ID`/`Кейс`
    и содержит колонку «Ожидание»."""
    headers = table_headers(clean)
    out = []
    for num, text in clean:
        header = headers.get(num)
        if not header or not RE_CASE_HEADER.match(header[0].strip("*_` ")):
            continue
        if not any("ожидан" in h.lower() for h in header):
            continue
        cid = case_id(cells_of(text)[0])
        if cid:
            out.append((cid, num, text))
    return out


def section_range(clean, heading_re, min_level=1):
    """(номер заголовка, номер последней строки) первого раздела не выше min_level, чей заголовок
    совпал с heading_re, — до заголовка того же или более высокого уровня (``` -блоки в конце
    раздела входят в него); None, если раздела нет."""
    start, level = None, None
    for num, text in clean:
        h = RE_HEADING.match(text)
        if h and start is not None and len(h.group(1)) <= level:
            return start, num - 1
        if h and start is None and len(h.group(1)) >= min_level and heading_re.search(h.group(2)):
            start, level = num, len(h.group(1))
    if start is None:
        return None
    return start, 10 ** 9  # до конца файла; context_errors обрезает по длине файла


def coverage_errors(clean, known_ids, anchor):
    """E12: «Покрытие» — у каждого из девяти измерений ID кейса или N/A с причиной. Таблица —
    в разделе «Покрытие» или любая таблица с шапкой «Измерение | …» (план внутри отчёта)."""
    rng = section_range(clean, RE_COVERAGE)
    rows = []
    if rng is not None:
        for num, text in clean:
            if rng[0] < num <= rng[1] and text.lstrip().startswith("|"):
                cells = cells_of(text)
                if not is_separator(cells):
                    rows.append((num, cells))
    else:
        headers = table_headers(clean)
        rows = [(num, cells_of(text)) for num, text in clean
                if num in headers and headers[num][0].strip("*_` ").lower().startswith("измерени")]
        if not rows:
            return [("E12", anchor, RULES["E12"])]
        rng = (rows[0][0], rows[-1][0])
    out = []
    for label, prefix in PLAN_DIMENSIONS:
        row = next(((n, c) for n, c in rows if c[0].strip("*_` ").lower().startswith(prefix)), None)
        if row is None:
            out.append(("E12", rng[0], RULES["E12_DIM"] % label))
            continue
        num, cells = row
        rest = " | ".join(cells[1:])
        if RE_NA.match(rest):
            reason = RE_NA.sub("", rest, count=1)
            if len(re.sub(r"[\s\-—–|*`:;,.]", "", reason)) < 8:
                out.append(("E12", num, RULES["E12_NA"] % label))
            continue
        if not any(re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(cid), rest) for cid in known_ids):
            out.append(("E12", num, RULES["E12_EMPTY"] % label))
    return out


def context_errors(lines, first, last, anchor):
    """E13: блок «Контекст проверки» (в т. ч. внутри ``` -блока) со строкой env — в строках
    first..last исходного файла (номера с 1)."""
    last = min(last, len(lines))
    found = None
    for i in range(first - 1, last):
        if not RE_CONTEXT.search(lines[i]):
            continue
        found = i + 1 if found is None else found
        # окно — строки блока до следующего заголовка раздела (заголовок `## Контекст проверки`
        # и сам блок под ним — обычная пара, поэтому смотрим каждое вхождение)
        for w in lines[i + 1:min(i + 13, last)]:
            if re.match(r"\s{0,3}#{1,6}\s", w):
                break
            if RE_ENV_LINE.search(w):
                return []
    if found is None:
        return [("E13", anchor, RULES["E13"])]
    return [("E13", found, RULES["E13_ENV"])]


def plan_errors(lines, clean):
    """E12/E13/E14 для файла тест-плана."""
    anchor = next((n for n, t in clean if RE_HEADING.match(t)), 1)
    out = context_errors(lines, 1, len(lines), anchor)
    rows = case_rows(clean)
    if not rows:
        out.append(("E14", anchor, RULES["E14"]))
    for cid, num, text in rows:
        if not basis(text):
            out.append(("E14", num, RULES["E14_SRC"] % cid))
    out.extend(coverage_errors(clean, {cid for cid, _, _ in rows}, anchor))
    return out


def plan_verdicts(plan_path):
    """{ID кейса: есть ли у него вердикт в самом плане} — по таблицам кейсов плана."""
    with open(plan_path, encoding="utf-8") as fh:
        clean = strip_fences(fh.readlines())
    return {cid: any(first_token(c) for c in cells_of(text)[1:]) for cid, _, text in case_rows(clean)}


def project_root(payload=None):
    """Корень проекта: поле root настроек → $CLAUDE_PROJECT_DIR → cwd из JSON хука →
    папка на два уровня выше линтера (раскладка <корень>/tools/qa-report-lint/lint.py)."""
    for cand in (CONFIG.get("root"), os.environ.get("CLAUDE_PROJECT_DIR"), (payload or {}).get("cwd")):
        if cand and os.path.isdir(cand):
            return os.path.abspath(cand)
    return os.path.dirname(os.path.dirname(HERE))


def resolve_plan(path, value):
    """Путь к файлу плана из значения `Тест-план:` — относительно отчёта, затем корня проекта."""
    m = RE_PLAN_PATH.search(value)
    if not m:
        return None, None
    rel = m.group(0)
    for base in (os.path.dirname(os.path.abspath(path)), project_root()):
        candidate = os.path.join(base, rel)
        if os.path.isfile(candidate):
            return rel, candidate
    return rel, None


def inline_plan_errors(lines, clean, rng):
    """E12/E13 для раздела `## Тест-план` внутри отчёта: кейсы — строки таблиц отчёта."""
    section = [(n, t) for n, t in clean if rng[0] <= n <= rng[1]]
    return context_errors(lines, rng[0], rng[1], rng[0]) + coverage_errors(section, table_ids(clean), rng[0])


def report_plan_errors(path, lines, clean):
    """E10/E11 (и E12/E13 для раздела `## Тест-план` внутри отчёта)."""
    link = next(((n, m.group(1)) for n, t in clean for m in [RE_PLAN_LINK.match(t)] if m), None)
    inline = section_range(clean, RE_PLAN_SECTION, min_level=2)
    if link is None:
        return inline_plan_errors(lines, clean, inline) if inline else [("E10", 1, RULES["E10"])]
    num, value = link
    if RE_NA.match(value):
        if len(re.sub(r"[\s\-—–|*`:;,.]", "", RE_NA.sub("", value, count=1))) < 8:
            return [("E10", num, RULES["E10_NA"])]
        return []
    if not RE_PLAN_PATH.search(value) and inline:  # «Тест-план: раздел ниже»
        return inline_plan_errors(lines, clean, inline)
    rel, plan = resolve_plan(path, value)
    if rel is None:
        return [("E10", num, RULES["E10"])]
    if plan is None:
        return [("E10", num, RULES["E10_MISSING"] % rel)]
    if RE_BUGS_NAME.search(os.path.basename(path)):
        return []  # файл багов держит только открытые дефекты — кейсы плана в нём не ищем
    reported = table_ids(clean)
    missing = [cid for cid, done in plan_verdicts(plan).items() if not done and cid not in reported]
    if missing:
        shown = ", ".join(missing[:12]) + (" …" if len(missing) > 12 else "")
        return [("E11", num, RULES["E11_LIST"] % shown)]
    return []


def lint(path):
    """-> (errors, warnings); каждый элемент: (код, строка, текст)"""
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    clean = strip_fences(lines)
    verdicts = verdict_lines(clean)
    errors, warnings = [], []

    errors.extend(finding_errors(clean))
    warnings.extend(cause_warnings(clean))

    base = os.path.basename(path)
    title = next((h.group(2) for _, t in clean for h in [RE_HEADING.match(t)] if h), "")
    is_report = bool(RE_REPORT_NAME.search(base))
    is_plan = bool(RE_PLAN_NAME.match(base)) or (not is_report and bool(RE_PLAN_TITLE.search(title)))
    if is_plan:
        errors.extend(plan_errors(lines, clean))
    elif is_report and verdicts:
        errors.extend(report_plan_errors(path, lines, clean))
    errors.extend(legend_errors(clean))

    if not verdicts:
        errors.sort(key=lambda e: e[1])
        if RE_REPORT_NAME.search(os.path.basename(path)):
            warnings.append(("W0", 0, RULES["W0"]))
        return errors, warnings

    head_stand, head_date = header_has_context(clean)
    open_bad = [(n, tok) for n, tok, _, _, fin in verdicts if tok in BAD_VERDICT and not fin]

    for num, tok, text, rest, is_final in verdicts:
        marks = basis(text)

        if tok != "N/A" and not marks and not is_final:
            errors.append(("E1", num, "%s — %s" % (tok, RULES["E1"])))

        if tok == "PASS" and "[R]" not in marks and not is_final:
            errors.append(("E2", num, RULES["E2"]))

        if tok == "FAIL" and not is_final:
            problem = cause_problem(text)
            if problem:
                errors.append(("E8", num, RULES[problem]))

        if "[R]" in marks:
            if not (RE_STAND.search(text) or head_stand) or not (RE_DATE.search(text) or head_date):
                errors.append(("E3", num, RULES["E3"]))

        if "[C]" in marks and not (RE_CODE_LOC.search(text) or RE_SHA.search(text)):
            errors.append(("E4", num, RULES["E4"]))

        if tok in ("NOT_VERIFIED", "BLOCKED") and len(re.sub(r"[\s\-—–|*`]", "", rest)) < 12:
            errors.append(("E6", num, RULES["E6"]))

        if is_final and tok == "PASS" and re.search(r"в пределах|частично|кроме|не закрыт", rest, re.IGNORECASE):
            warnings.append(("W1", num, RULES["W1"]))

        if is_final and tok == "PASS" and open_bad:
            where = ", ".join("стр. %d %s" % (n, t) for n, t in open_bad[:5])
            errors.append(("E5", num, "%s (%s)" % (RULES["E5"], where)))

    errors.extend(origin_errors(clean, verdicts))
    errors.extend(origin_legend_errors(clean, verdicts))

    errors.sort(key=lambda e: e[1])
    warnings.sort(key=lambda e: e[1])
    return errors, warnings


def report(path, errors, warnings, stream):
    rel = os.path.relpath(path)
    for code, num, msg in errors:
        stream.write("%s:%d: %s %s\n" % (rel, num, code, msg))
    for code, num, msg in warnings:
        stream.write("%s:%d: %s %s\n" % (rel, num, code, msg))


def main():
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = sys.argv[1:]
    if "--config" in args:
        i = args.index("--config")
        if i + 1 >= len(args):
            print("--config: нужен путь к файлу настроек")
            return 1
        apply_config(load_config(args[i + 1]))
        del args[i:i + 2]

    if "--explain" in args:
        for code in sorted(c for c in RULES if "_" not in c):  # E8_* — варианты сообщения E8
            print("%s  %s" % (code, RULES[code]))
        return 0

    if "--hook" in args:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            return 0
        path = (payload.get("tool_input") or {}).get("file_path") or ""
        if not path.lower().endswith(".md") or not os.path.isfile(path):
            return 0
        root = project_root(payload)
        dirs = {os.path.normcase(os.path.abspath(os.path.join(root, d))) for d in CONFIG.get("hook_dirs") or ["."]}
        if os.path.normcase(os.path.dirname(os.path.abspath(path))) not in dirs:
            return 0
        if os.path.basename(path) in (CONFIG.get("skip_files") or []):
            return 0
        try:
            errors, warnings = lint(path)
        except Exception:
            return 0
        if not errors:
            if warnings:
                report(path, [], warnings, sys.stderr)
            return 0
        codes = {code for code, _, _ in errors}
        sys.stderr.write("qa-report-lint: отчёт не прошёл проверку (правила — скил qa-evidence)\n")
        report(path, errors, warnings, sys.stderr)
        if codes & {"E15", "E16"}:
            sys.stderr.write("Отчёты читают разработчики: голые [R]/[C]/[T]/[A]/[?] и метки «ветка/легаси» им ничего "
                             "не говорят. Либо расшифровка в шапке, либо словами (скил qa-evidence).\n")
        if codes - {"E10", "E11", "E12", "E13", "E14", "E15", "E16"}:
            sys.stderr.write("Исправь основания или понизь вердикт до NOT_VERIFIED/BLOCKED с причиной. "
                             "Если основание добывается доступной проверкой — сначала проверь, а не понижай.\n")
        if any(code in ("E10", "E11", "E12", "E13", "E14") for code, _, _ in errors):
            sys.stderr.write("Тестирование начинается с тест-плана (скил qa-evidence, «Тест-план»; шаблон "
                             "tools/qa-report-lint/templates/testplan.md): контекст проверки со строкой env, "
                             "кейсы с источником, покрытие по всем измерениям; отчёт ссылается на план, "
                             "каждый кейс плана получает вердикт.\n")
        if any(code in ("E7", "E8", "E9") for code, _, _ in errors):
            sys.stderr.write("Каждая находка F-n/BUG-n — строка таблицы (ID | суть | вердикт | основание); "
                             "у каждого FAIL — причина в коде [C] path:line либо [?] с объяснением, почему она не установлена, "
                             "и происхождение (ветка / легаси / легаси+ветка / не установлено) со сравнением с базой.\n")
        return 2

    files = [a for a in args if not a.startswith("-")]
    if not files:
        print(__doc__)
        return 0
    total_e = 0
    for path in files:
        if not os.path.isfile(path):
            print("%s: файл не найден" % path)
            continue
        errors, warnings = lint(path)
        total_e += len(errors)
        if errors or warnings:
            report(path, errors, warnings, sys.stdout)
        else:
            print("%s: OK" % os.path.relpath(path))
    return 1 if total_e else 0


apply_config(load_config())

if __name__ == "__main__":
    sys.exit(main())
