#!/usr/bin/env bash
#
# Локальная проверка перед отправкой в GitHub — то, что GitHub Actions
# не может сделать за вас: сверку ДВУХ копий проекта (эта папка и вторая
# копия на этом же компьютере).
#
#     bash ci/check_local.sh
#
# Проверяется:
#   1. все тесты (python run_tests.py);
#   2. вторая копия проекта не разошлась с текущей;
#   3. репозиторий чист: секреты не отслеживаются, дерево = коммит.
#
# Вторая копия ищется автоматически (compare_copies.KNOWN_COPIES). Задать
# путь вручную:  COPY_B=/путь/к/копии bash ci/check_local.sh
#
# Код возврата: 0 — всё хорошо; 1 — есть проблемы (выход первого упавшего).
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root" || exit 2

rc=0

echo "════════ 1/3. Тесты ════════"
if ! python run_tests.py; then
    echo "ПРОВАЛ: тесты не прошли"
    rc=1
fi

echo
echo "════════ 2/3. Сверка копий проекта ════════"
if ! python - "$root" "${COPY_B:-}" <<'PY'
import os
import sys

repo = os.path.abspath(sys.argv[1])
override = sys.argv[2]
sys.path.insert(0, repo)
import compare_copies as C

if override:
    other = os.path.abspath(override)
else:
    other = None
    for p in C.KNOWN_COPIES:
        cand = os.path.abspath(p)
        if cand != repo and os.path.isdir(cand):
            other = cand
            break

if other is None:
    print("ВНИМАНИЕ: вторая копия проекта не найдена — сверка пропущена")
    print("         (укажите путь в COPY_B, чтобы сверка выполнялась)")
    sys.exit(0)

print(f"копия A: {repo}")
print(f"копия B: {other}")
sys.exit(C.main([repo, other]))
PY
then
    echo "ПРОВАЛ: копии проекта разошлись — синхронизируйте их перед отправкой"
    rc=1
fi

echo
echo "════════ 3/3. Проверки репозитория ════════"
if ! bash ci/check_repo.sh; then
    echo "ПРОВАЛ: проверки репозитория не прошли"
    rc=1
fi

echo
if [ "$rc" -ne 0 ]; then
    echo "ИТОГ: есть проблемы — отправка не рекомендуется"
    exit 1
fi
echo "ИТОГ: всё чисто"
exit 0
