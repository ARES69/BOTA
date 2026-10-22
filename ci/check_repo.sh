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
# -z: пути без кавычек и без восьмеричного экранирования (иначе git
# превращает кириллицу в "\320\217..." и пути не сойдутся с диском).
tracked="$(git ls-files -z 2>/dev/null | tr '\0' '\n')"

for f in .env config.json position.json snapshot.json bot.log memory.json \
         dashboard.html watch.out health_state.json backtest_data.json \
         scalp_state.json drift_report.txt drift_report.json; do
    if printf '%s\n' "$tracked" | grep -qxF "$f"; then
        error "файл '$f' отслеживается git — он не должен попадать в репозиторий"
    fi
done
while IFS= read -r f; do
    [ -n "$f" ] && error "файл '$f' (лог прогона) отслеживается git"
done <<< "$(printf '%s\n' "$tracked" | grep -E '^bot\.log\.' || true)"

note "=== 2. В отслеживаемых файлах нет настоящих секретов ==="
# Сканирование на Python: у git grep регулярка, начинающаяся с "-----",
# трактуется как опция, а значения секретов нельзя печатать в лог.
python - <<'PY' || fail=1
import os
import re
import subprocess
import sys

# Консоль Windows по умолчанию cp1252 — печать кириллицы (пути, отчёт)
# роняла скрипт с UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# Строгие маркеры: однозначно секрет, заглушками не отменяются.
STRICT = [
    ("приватный ключ", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("токен GitHub", re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("ключ AWS", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("токен Slack", re.compile(rb"xox[abprs]-[A-Za-z0-9-]{10,}")),
]

# Мягкое правило: секрету присвоено значение из 20+ символов.
SOFT = re.compile(
    rb"(?i)(api_key|api_secret|secret_key|access_key|token|password|passwd)"
    rb"\s*[=:]\s*[\"']?([A-Za-z0-9_-]{20,})")

# Значение-заглушка: «ваш-пароль», your_key_here, $VAR, <...> и т.п.
PLACEHOLDER = re.compile(
    rb"(?i)(your|here|example|placeholder|changeme|xxx|\.\.\.|<[^>]*>|\$[A-Za-z_{])")

raw = subprocess.run(["git", "ls-files", "-z"], capture_output=True).stdout
files = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]

problems = []
for path in files:
    try:
        data = open(path, "rb").read()
    except OSError:
        continue
    for num, line in enumerate(data.splitlines(), 1):
        for label, rx in STRICT:
            if rx.search(line):
                problems.append((path, num, label))
        for m in SOFT.finditer(line):
            value = m.group(2)
            if PLACEHOLDER.search(value):
                continue          # заглушка, не настоящий секрет
            problems.append((path, num,
                             "значение секрета (%d символов)" % len(value)))

if problems:
    for path, num, label in problems:
        print("ОШИБКА: %s:%d — %s (значение не показываем)" % (path, num, label))
    sys.exit(1)

print("проверено файлов: %d, секретов не найдено" % len(files))
sys.exit(0)
PY

note "=== 3. Рабочее дерево совпадает с закоммиченным ==="
# Сверка идёт через штатный compare_copies.py: файлы проекта на диске против
# списка отслеживаемых git'ом. Файлы данных прогона и служебные каталоги
# (compare_copies их игнорирует) в сравнение не попадают.
python - <<'PY' || fail=1
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, ".")
import compare_copies as C

# encoding="utf-8" обязателен: вывод git содержит кириллицу в путях, а
# консоль Windows по умолчанию cp1252 — без явной кодировки декод падает.
out = subprocess.run(["git", "ls-files", "-z"], capture_output=True,
                     encoding="utf-8", errors="replace")
tracked = {p for p in out.stdout.split("\0") if p}
project, state = C.scan(".")
on_disk = set(project)

extra = sorted(on_disk - tracked)      # есть на диске, но не закоммичено
# Отсутствующими считаем только файлы проекта: данные прогона (.env,
# config.json и т.п.) compare_copies выносит в отдельный класс, и о них
# заботится проверка №1 выше.
missing = sorted((tracked - on_disk) - set(state))

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

print("файлов проекта на диске: %d, отслеживается: %d" % (len(on_disk), len(tracked)))
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
