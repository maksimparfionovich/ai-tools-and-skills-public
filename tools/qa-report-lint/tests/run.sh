#!/usr/bin/env bash
# Регресс линтера. Ожидание: bad — ошибки есть, остальные — чисто.
cd "$(dirname "$0")/../../.." || exit 1
# Интерпретатор: $PYTHON, иначе первый рабочий из python3 / python (в Git Bash на Windows
# python3 бывает заглушкой Microsoft Store — она не запускается).
PY=${PYTHON:-}
if [ -z "$PY" ]; then
  for c in python3 python; do
    if "$c" -c "import sys" >/dev/null 2>&1; then PY=$c; break; fi
  done
fi
[ -n "$PY" ] || { echo "нужен Python 3: python3 или python"; exit 1; }
# Тесты идут на значениях по умолчанию — ваш config.local.json на них не влияет.
export QA_LINT_CONFIG=/nonexistent/qa-report-lint-config.json
L="$PY tools/qa-report-lint/lint.py"
T="tools/qa-report-lint/tests"
fail=0
n=$($L $T/bad.md.fixture | grep -cE ' E[1-6] ')
[ "$n" -ge 8 ] || { echo "REGRESS: bad.md.fixture дал $n ошибок, ожидалось >=8"; fail=1; }
for f in good allgreen redgreen; do
  out=$($L "$T/$f.md.fixture")
  case "$out" in *OK) ;; *) echo "REGRESS: $f.md.fixture не чистый: $out"; fail=1;; esac
done

# codes <fixture> -> коды правил, которые линтер напечатал по фикстуре (отсортированы, через пробел)
codes() {
  $L "$T/$1.md.fixture" | tr -d '\r' | sed -nE 's/^.*:[0-9]+: ([EW][0-9]+) .*$/\1/p' | sort | tr '\n' ' ' | sed 's/ $//'
}
# expect <fixture> "<коды>" — точное совпадение набора срабатываний
expect() {
  got=$(codes "$1")
  [ "$got" = "$2" ] || { echo "REGRESS: $1.md.fixture дал [$got], ожидалось [$2]"; fail=1; }
}
# expect_clean <fixture> — ни одного срабатывания (E и W), линтер печатает OK
expect_clean() {
  out=$($L "$T/$1.md.fixture" | tr -d '\r')
  case "$out" in *OK) ;; *) echo "REGRESS: $1.md.fixture не чистый: $out"; fail=1;; esac
}

# bad: E8 добавился к прежним нарушениям (c5 — FAIL только с [R], без [C])
expect bad "E1 E2 E2 E2 E4 E4 E5 E6 E8 E9"

# E7: находка описана в тексте (жирный абзац, заголовок, пункт списка), строки в таблице нет
expect e7_bad "E7 E7 E7"
expect_clean e7_good

# E8: FAIL без [C] и без [?] с пояснением; FAIL_EXPECTED и итоговый вердикт не проверяются
expect e8_bad "E4 E8 E8 E8 E8 E8 E8 E9 E9 E9 E9 E9 E9 E9"
expect_clean e8_good

# E9: FAIL без происхождения (ветка / легаси / легаси+ветка / не установлено), с чужим значением,
# без основания сравнения с базой, «не установлено» без [A], ссылка на находку без происхождения
expect e9_bad "E9 E9 E9 E9 E9 E9 E9 E9 E9"
expect_clean e9_good

# W2: утверждения о причине без основания (обе группы, вне раздела «Предположения»)
expect w2_bad "W2 W2 W2 W2 W2 W2 W2 W2 W2 W2 W2 W2 W2"
expect_clean w2_good

# длинные строки с несколькими маркерами и `where[id][in]` в code span — не срабатывать
expect_clean longline_ok

# Тест-план (2026-10-13). План: контекст со строкой env (E13), кейсы с источником (E14),
# покрытие девяти измерений (E12). Отчёт с отчётным именем: ссылка на план или раздел
# `## Тест-план` (E10), каждый кейс плана с вердиктом (E11); файл багов E11 не проверяет.
expect_clean testplan_good
expect testplan_bad "E12 E12 E12 E13 E14"
expect qa_noplan "E10"
expect qa_plan_na "E10"
expect qa_plan_nofile "E10"
expect qa_plan_missing "E11"
expect_clean qa_plan_ok
expect_clean qa_plan_inline
expect_clean bugs_plan

# Маркеры для разработчиков (2026-10-13): буквы — только с расшифровкой в файле (E15), либо основание
# словами — «Воспроизведено на qa2 13.10:», «В коде:», «Требование:», «Предположение:», «Не проверено:»;
# подписи засчитываются как маркеры во всех правилах (E1–E4, E8, E9, E14, W2). Старые фикстуры несут
# строку «Обозначения: …» в шапке.
expect legend_bad "E15"
expect_clean legend_table_ok
expect_clean qa_words
# c1 — только требование (E2), c2 — стенд без даты (E3), c3 — «В коде:» без path:line (E4),
# c4 — «Воспроизведено:» без стенда и даты прогоном не считается (E1 + E2)
expect qa_words_bad "E1 E2 E2 E3 E4"

# Происхождение для разработчиков (2026-10-14): метка «ветка / легаси / легаси+ветка» у FAIL — только
# с расшифровкой в файле (строка «легаси — …» вне таблиц проверок или таблица `| легаси | … |`) или с
# пояснением словами в той же строке; можно и только словами («внесён веткой», «есть и без ветки»).
expect origin_legend_bad "E16"
expect_clean origin_table_ok

# предупреждения не меняют exit code: w2_bad только с W2 -> exit 0
$L "$T/w2_bad.md.fixture" >/dev/null 2>&1 || { echo "REGRESS: w2_bad.md.fixture дал ненулевой exit code, W2 — только предупреждения"; fail=1; }

[ "$fail" = 0 ] && echo "qa-report-lint: регресс OK"
exit $fail
