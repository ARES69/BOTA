#!/usr/bin/env bash
#
# Проверки репозитория — одинаково работают в CI (GitHub Actions, Linux) и
# локально (Git Bash на Windows). Запуск:
#
#     bash ci/check_repo.sh          # из корня проекта
#
# Что проверяется:
#   1. Файлы с секретами и данные прогона не отслеживаются git'ом.
#   2. В отслеживаемых файлах нет приватных ключей, токенов и API-ключей.
#   3. Рабочее дерево совпадает с тем, что закоммичено (нет «забытых»
#      файлов проекта) — логика сверки берётся из штатного compare_copies.py.
#
# Код возврата: 0 — чисто; 1 — есть нарушения.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

fail=0
note()  { printf '%s\n' "$*"; }
error() { printf 'ОШИБКА: %s\n' "$*"; fail=1; }

note "=== 1. Секреты и данные прогона не отслеживаются ==="
tracked="$(git ls-files 2>/dev/null)"

for f in .env config.json position.json snapshot.json bot.log memory.json \
         dashboard.html watch.out health_state.json backtest_data.json \
         scalp_state.json; do
    if printf '%s\n' "$tracked" | grep -qxF "$f"; then
        error "файл '$f' отслеживается git — он не должен попадать в репозиторий"
    fi
done
while IFS= read -r f; do
    [ -n "$f" ] && error "файл '$f' (лог прогона) отслеживается git"
done <<< "$(printf '%s\n' "$tracked" | grep -E '^bot\.log\.' || true)"

note "=== 2. В отслеживаемых файлах нет настоящих секретов ==="
# Строгие шаблоны: однозначные маркеры ключей и токенов.
STRICT='-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}'
# Мягкий шаблон: присваивание секрету значения длиной 20+ символов.
# Заглушки (`...`, `ваш-пароль`, `$VAR`) отфильтровываются отдельной строкой.
SOFT='(api_key|api_secret|secret_key|access_key|token|password|passwd)\s*[=:]\s*.?[A-Za-z0-9_-]{20,}'
PLACEHOLDER='ваш|your|here|example|placeholder|changeme|xxx|\.\.\.|<[^>]*>|\$\{|\$[A-Z_]'

hits="$(git grep -nEI "$STRICT" -- . 2>/dev/null || true)"
hits="$(printf '%s\n' "$hits" | grep -vE "$PLACEHOLDER" || true)"
if [ -n "$hits" ]; then
    printf '%s\n' "$hits" | while IFS= read -r line; do
        [ -n "$line" ] && error "похоже на секрет: $line"
    done
    fail=1
fi

hits="$(git grep -nEI "$SOFT" -- . 2>/dev/null || true)"
hits="$(printf '%s\n' "$hits" | grep -vE "$PLACEHOLDER" || true)"
if [ -n "$hits" ]; then
    printf '%s\n' "$hits" | while IFS= read -r line; do
        [ -n "$line" ] && error "похоже на секрет: $line"
    done
    fail=1
fi

note "=== 3. Рабочее дерево совпадает с закоммиченным ==="
# Сверка идёт через штатный compare_copies.py: файлы проекта на диске против
# списка отслеживаемых git'ом. Файлы данных прогона и служебные каталоги
# (compare_copies их игнорирует) в сравнение не попадают.
python - <<'PY' || fail=1
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath("ci/check_repo.sh")))
sys.path.insert(0, ".")
import compare_copies as C

out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
tracked = {p for p in out.stdout.splitlines() if p}
project, _state = C.scan(".")
on_disk = set(project)

extra = sorted(on_disk - tracked)      # есть на диске, но не закоммичено
missing = sorted(tracked - on_disk)    # закоммичено, но нет на диске

if missing:
    print("ОШИБКА: отслеживаются, но отсутствуют на диске: "
          + ", ".join(missing))
    sys.exit(1)

if extra:
    if os.environ.get("CI", "").lower() == "true":
        print("ОШИБКА: файлы проекта не закоммичены: " + ", ".join(extra))
        sys.exit(1)
    print("ВНИМАНИЕ: файлы проекта есть на диске, но не закоммичены: "
          + ", ".join(extra))
    print("         (в CI это ошибка, локально — предупреждение)")

print(f"файлов проекта на диске: {len(on_disk)}, отслеживается: {len(tracked)}")
sys.exit(0)
PY

if [ "$fail" -ne 0 ]; then
    note ""
    note "ИТОГ: есть нарушения"
    exit 1
fi
note ""
note "ИТОГ: репозиторий чист"
exit 0
