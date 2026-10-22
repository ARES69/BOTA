#!/usr/bin/env bash
#
# Отправить текущую версию проекта в зеркало на VPS — это эталон для
# drift_check.py, который сверяет с ним код на сервере.
#
#     bash ci/publish_mirror.sh
#     VPS=root@1.2.3.4 bash ci/publish_mirror.sh
#
# Зачем именно так: GitHub из этой сети закрыт для чтения (деплой-ключ
# аутентифицируется, но git отвечает «Repository not found»), а SSH до
# сервера уже есть. Зеркало — обычный bare-репозиторий в ~/bybit-drift/,
# обновляется принудительно: оно обязано совпадать с этой копией, а не
# накапливать историю.
#
# Код возврата: 0 — зеркало обновлено; 1 — не вышло (сеть, ssh, git).
set -uo pipefail

VPS="${VPS:-bybit-vps}"
MIRROR="${MIRROR:-bybit-drift/mirror.git}"

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root" || exit 2

if [ ! -d .git ]; then
    echo "не git-репозиторий: $root" >&2
    exit 1
fi

# Каталог и bare-репозиторий создаются один раз; повторный init безвреден.
if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$VPS" \
        "mkdir -p ~/$(dirname "$MIRROR") && { test -d ~/$MIRROR || git init --bare -q ~/$MIRROR; }"; then
    echo "не удалось подготовить зеркало на $VPS" >&2
    exit 1
fi

if ! git push --quiet --force "$VPS:$MIRROR" HEAD:refs/heads/main; then
    echo "не удалось отправить зеркало на $VPS" >&2
    exit 1
fi

echo "зеркало обновлено: $VPS:~/$MIRROR ($(git rev-parse --short HEAD))"
