#!/usr/bin/env bash
#
# Ставит git-хук pre-push: перед отправкой в GitHub прогоняется локальная
# проверка (тесты + сверка двух копий + чистота репозитория). Смысл: CI
# умеет сверять репозиторий сам с собой, но ДВЕ копии проекта на этом
# компьютере он не видит — только хук.
#
#     bash ci/install_hooks.sh
#
# Убрать хук: rm .git/hooks/pre-push
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

git_dir="$(git rev-parse --git-dir)"
hooks="$git_dir/hooks"
mkdir -p "$hooks"

cat > "$hooks/pre-push" <<EOF
#!/usr/bin/env bash
# Установлен ci/install_hooks.sh. Локальная проверка перед отправкой.
# Отключить: rm "$hooks/pre-push"
set -uo pipefail
exec bash "$root/ci/check_local.sh"
EOF
chmod +x "$hooks/pre-push"

echo "Установлен хук: $hooks/pre-push"
echo "Теперь каждый 'git push' сначала прогонит тесты и сверку копий."
