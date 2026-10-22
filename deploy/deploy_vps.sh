#!/usr/bin/env bash
# Развёртывание бота на Ubuntu-VPS (запускать с ноутбука, откуда идёт ssh).
# Использование: VPS=user@IP bash deploy/deploy_vps.sh
set -euo pipefail

VPS="${VPS:?Укажите VPS=user@IP, например VPS=root@1.2.3.4 bash deploy/deploy_vps.sh}"
# Полный набор модулей бота: без bybit_memory.py/bybit_ws.py исполнитель
# работает, но без памяти самообучения и без WS-ленты.
FILES="funding_monitor.py funding_executor.py bybit_memory.py bybit_ws.py
       trade_sanity.py dashboard.py dashboard_metrics.py backtest.py
       healthcheck.py smotrim.py diag_entry.py README.md"
ENVFILE="${ENVFILE:-.env}"

# sudo нужен не всегда: при входе как root на минимальном Ubuntu пакета sudo
# может не быть вообще, и `sudo ...` упал бы. Определяем на сервере один раз.
SUDO=""
if [ "$(ssh "$VPS" 'id -u')" != "0" ]; then
    SUDO="sudo"
fi

echo "==> [$VPS] создаю папки"
ssh "$VPS" "mkdir -p ~/bybit-funding/deploy ~/bybit-funding/snapshots"

echo "==> [$VPS] копирую модули"
# bash не склеивает переводы строк в список — $FILES разворачивается в слова
# (без кавычек), и scp получает файлы списком
# shellcheck disable=SC2086
scp $FILES config.example.json "$VPS":~/bybit-funding/
scp deploy/funding-watch.service deploy/dashboard.service \
    deploy/funding-health.service deploy/funding-health.timer \
    "$VPS":~/bybit-funding/deploy/

# тесты едут вместе с кодом: иначе шаг «прогоняю офлайн-тесты» ниже проверял
# бы версии тестов, оставшиеся на сервере с прошлого раза
echo "==> [$VPS] копирую тесты и раннер"
scp test_*.py run_tests.py "$VPS":~/bybit-funding/

# .env с ключами: копируем только если его ещё нет на сервере, чтобы
# не затереть рабочие ключи на VPS пустым файлом с ноутбука
if ssh "$VPS" "test -s ~/bybit-funding/$ENVFILE" 2>/dev/null; then
    echo "==> [$VPS] .env уже есть — НЕ перезаписываю (ключи на сервере)"
else
    echo "==> [$VPS] копирую .env с ключами"
    scp "$ENVFILE" "$VPS":~/bybit-funding/
fi
ssh "$VPS" "chmod 600 ~/bybit-funding/$ENVFILE 2>/dev/null || true"

echo "==> [$VPS] проверяю синтаксис и импорты"
ssh "$VPS" "cd ~/bybit-funding && python3 --version && \
            python3 -m py_compile funding_executor.py bybit_memory.py bybit_ws.py \
                                    trade_sanity.py dashboard.py dashboard_metrics.py \
                                    backtest.py healthcheck.py funding_monitor.py && \
            echo 'синтаксис OK' && python3 -c 'import funding_executor' && echo 'импорты OK'"

echo "==> [$VPS] создаю config.json из примера (если его ещё нет)"
ssh "$VPS" "cd ~/bybit-funding && \
            if [ -f config.json ]; then echo '  config.json уже есть — не трогаю'; \
            else cp config.example.json config.json && echo '  создан из примера'; fi"

# Отчёты в Telegram: часовой и ежедневная сводка. report-every ставим принудительно
# (час), часовой пояс и время сводки — только если ключа ещё нет, чтобы не затирать
# выбранное пользователем.
REPORT_TZ="${REPORT_TZ:-+03:00}"
DAILY_AT="${DAILY_AT:-00:00}"
echo "==> [$VPS] включаю отчёты: часовой + сводка в $DAILY_AT ($REPORT_TZ)"
ssh "$VPS" "cd ~/bybit-funding && python3 -c \"import json; p='config.json'; \
            d=json.load(open(p, encoding='utf-8')); d['report-every']=60; \
            d.setdefault('daily-report-at', '$DAILY_AT'); \
            d.setdefault('report-tz', '$REPORT_TZ'); \
            json.dump(d, open(p,'w',encoding='utf-8'), ensure_ascii=False, indent=2); \
            print('  config.json:', d['report-every'], d['daily-report-at'], repr(d['report-tz']))\""

echo "==> [$VPS] тестовый запуск монитора (живые данные)"
ssh "$VPS" "cd ~/bybit-funding && timeout 60 python3 funding_monitor.py --top 5 | tail -6"

echo "==> [$VPS] прогоняю офлайн-тесты (единый раннер; живой тест — SKIP)"
ssh "$VPS" "cd ~/bybit-funding && PYTHONIOENCODING=utf-8 python3 run_tests.py"

echo "==> [$VPS] собираю панель (проверка рендера на реальных данных)"
ssh "$VPS" "cd ~/bybit-funding && python3 dashboard.py --no-live --refresh 0 --out /tmp/_dash_check.html | tail -1 && rm -f /tmp/_dash_check.html"

echo "==> [$VPS] проверяю часы (10002 = подпись не пройдёт)"
ssh "$VPS" "timedatectl status | head -4"

echo "==> [$VPS] панель по IP:8050 — проверяю пароль входа"
# Пароль и токен панели можно задать прямо здесь (переменные окружения
# запускающего скрипта) — тогда они попадут в .env на сервере и .env
# не придётся править руками. Значения в .env перезаписываются, чтобы
# старый пароль не остался рядом с новым.
DASH_PW="${DASHBOARD_PASSWORD:-}"
DASH_TOK="${DASHBOARD_TOKEN:-}"
if [ -n "$DASH_PW" ]; then
    echo "==> [$VPS] задаю DASHBOARD_PASSWORD и DASHBOARD_TOKEN в .env"
    # Значения кодируем в base64: в пароле могут быть кавычки, пробелы и
    # спецсимволы, которые ломают и shell-квотирование, и python-литерал.
    PW_B64=$(printf '%s' "$DASH_PW" | base64 | tr -d '\n')
    TOK_B64=$(printf '%s' "$DASH_TOK" | base64 | tr -d '\n')
    ssh "$VPS" "PW_B64='$PW_B64' TOK_B64='$TOK_B64' python3 - <<'PY'
import base64
import os

def dec(b64):
    if not b64:
        return ''
    return base64.b64decode(b64).decode('utf-8')

p = os.path.expanduser('~/bybit-funding/.env')
want = {'DASHBOARD_PASSWORD': dec(os.environ.get('PW_B64', '')),
        'DASHBOARD_PUBLIC': '1'}
tok = dec(os.environ.get('TOK_B64', ''))
if tok:
    want['DASHBOARD_TOKEN'] = tok
lines = open(p, encoding='utf-8').read().splitlines() if os.path.exists(p) else []
out = []
seen = set()
for ln in lines:
    k = ln.split('=', 1)[0].strip()
    if k in want:
        # старую строку заменяем, а не дублируем: иначе в .env останется
        # прежний пароль, и systemd возьмёт не тот
        if k not in seen:
            out.append(f'{k}={want[k]}')
            seen.add(k)
        continue
    out.append(ln)
for k, v in want.items():
    if k not in seen:
        out.append(f'{k}={v}')
open(p, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
os.chmod(p, 0o600)
print('  .env обновлён:', ', '.join(sorted(want)))
PY"
fi
if ! ssh "$VPS" "grep -q '^DASHBOARD_PASSWORD=.' ~/bybit-funding/.env 2>/dev/null"; then
    echo "!! DASHBOARD_PASSWORD не задан в ~/bybit-funding/.env"
    echo "   Панель с --host 0.0.0.0 без пароля НЕ стартует (fail closed)."
    echo "   Добавь пароль и запусти скрипт снова:"
    echo "     DASHBOARD_PASSWORD='ваш-пароль' bash deploy/deploy_vps.sh"
    exit 1
fi
ssh "$VPS" "grep -q '^DASHBOARD_PUBLIC=' ~/bybit-funding/.env \
                || echo 'DASHBOARD_PUBLIC=1' >> ~/bybit-funding/.env; \
            chmod 600 ~/bybit-funding/.env; echo '  пароль задан, DASHBOARD_PUBLIC=1'"

echo "==> [$VPS] ставлю systemd-сервисы"
ssh "$VPS" "$SUDO cp ~/bybit-funding/deploy/funding-watch.service /etc/systemd/system/ && \
            $SUDO cp ~/bybit-funding/deploy/dashboard.service /etc/systemd/system/ && \
            $SUDO systemctl daemon-reload && \
            $SUDO systemctl enable --now funding-watch dashboard && \
            sleep 3 && systemctl is-active funding-watch dashboard"

echo "==> [$VPS] ставлю ежедневный health-check (таймер)"
ssh "$VPS" "$SUDO cp ~/bybit-funding/deploy/funding-health.service ~/bybit-funding/deploy/funding-health.timer /etc/systemd/system/ && \
            $SUDO systemctl daemon-reload && \
            $SUDO systemctl enable --now funding-health.timer && \
            systemctl list-timers funding-health.timer --no-pager"

echo "==> [$VPS] открываю порт панели (вход — только по паролю)"
# ufw может не быть — тогда порт открыт по умолчанию, падать нельзя
ssh "$VPS" "command -v ufw >/dev/null 2>&1 && { $SUDO ufw allow ssh >/dev/null 2>&1; \
            $SUDO ufw allow 8050/tcp >/dev/null 2>&1; $SUDO ufw status | head -8; } \
            || echo '  ufw не установлен — порт 8050 слушает все интерфейсы по умолчанию'"

echo "==> Готово. Панель:"
echo "    http://<IP сервера>:8050  →  вход по паролю из DASHBOARD_PASSWORD"
echo "    Локально без открытого порта: ssh -L 8050:127.0.0.1:8050 $VPS"
echo "    Кнопки управления живут с токеном из DASHBOARD_TOKEN в ~/bybit-funding/.env"
echo "    Лог бота:  ssh $VPS 'tail -20 ~/bybit-funding/watch.log'"
echo "    Статус:    ssh $VPS 'systemctl status funding-watch dashboard --no-pager'"
echo "    Позиция:   ssh $VPS 'cd ~/bybit-funding && python3 funding_executor.py status'"
echo "    Закрыть:   ssh $VPS 'cd ~/bybit-funding && python3 funding_executor.py close'"
