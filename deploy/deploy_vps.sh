#!/usr/bin/env bash
# Развёртывание бота на Ubuntu-VPS (запускать с ноутбука, откуда идёт ssh).
# Использование: VPS=user@IP bash deploy/deploy_vps.sh
set -euo pipefail

# Цель задаётся явно — деплой на боевой сервер не должен стартовать от
# случайного запуска без аргументов. Вход — пользователем admin (root-логин
# по SSH выключен); sudo у него без пароля, поэтому установка юнитов идёт
# без вопросов. Работает и алиас из ~/.ssh/config: VPS=bybit-vps.
VPS="${VPS:?Укажите VPS=admin@IP или VPS=имя-из-ssh-config, например VPS=admin@1.2.3.4 bash deploy/deploy_vps.sh}"
# Полный набор модулей бота: без bybit_memory.py/bybit_ws.py исполнитель
# работает, но без памяти самообучения и без WS-ленты.
FILES="funding_monitor.py funding_executor.py bybit_memory.py bybit_ws.py
       trade_sanity.py dashboard.py dashboard_metrics.py backtest.py
       healthcheck.py drift_check.py compare_copies.py smotrim.py diag_entry.py
       README.md"
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
    deploy/dashboard-public.conf \
    deploy/funding-health.service deploy/funding-health.timer \
    deploy/drift-check.service deploy/drift-check.timer \
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

echo "==> [$VPS] настраиваю панель"
# По умолчанию панель слушает ТОЛЬКО 127.0.0.1, и доступ к ней — через
# SSH-туннель (ssh -L 8050:127.0.0.1:8050 admin@IP). Публичный bind
# (0.0.0.0) открывает всему интернету позицию, PnL и кнопки управления,
# поэтому включается только по явному запросу: DASHBOARD_PUBLIC=1.
#
# Пароль и токен панели можно задать прямо здесь (переменные окружения
# запускающего скрипта) — тогда они попадут в .env на сервере и .env
# не придётся править руками. Значения в .env перезаписываются, чтобы
# старый пароль не остался рядом с новым.
PUBLIC="${DASHBOARD_PUBLIC:-0}"
DASH_PW="${DASHBOARD_PASSWORD:-}"
DASH_TOK="${DASHBOARD_TOKEN:-}"
if [ -n "$DASH_PW" ] || [ -n "$DASH_TOK" ]; then
    if [ "$PUBLIC" = "1" ]; then
        echo "==> [$VPS] задаю DASHBOARD_PASSWORD и DASHBOARD_TOKEN в .env"
    else
        echo "==> [$VPS] задаю DASHBOARD_TOKEN в .env (панель локальная)"
    fi
    # Значения кодируем в base64: в пароле могут быть кавычки, пробелы и
    # спецсимволы, которые ломают и shell-квотирование, и python-литерал.
    PW_B64=$(printf '%s' "$DASH_PW" | base64 | tr -d '\n')
    TOK_B64=$(printf '%s' "$DASH_TOK" | base64 | tr -d '\n')
    ssh "$VPS" "PW_B64='$PW_B64' TOK_B64='$TOK_B64' PUBLIC='$PUBLIC' python3 - <<'PY'
import base64
import os

def dec(b64):
    if not b64:
        return ''
    return base64.b64decode(b64).decode('utf-8')

p = os.path.expanduser('~/bybit-funding/.env')
public = os.environ.get('PUBLIC') == '1'

want = {}
tok = dec(os.environ.get('TOK_B64', ''))
if tok:
    want['DASHBOARD_TOKEN'] = tok
if public:
    want['DASHBOARD_PASSWORD'] = dec(os.environ.get('PW_B64', ''))
    want['DASHBOARD_PUBLIC'] = '1'
else:
    # локальный режим: флаг публичности из .env убираем
    want['DASHBOARD_PUBLIC'] = ''
lines = open(p, encoding='utf-8').read().splitlines() if os.path.exists(p) else []
out = []
seen = set()
for ln in lines:
    k = ln.split('=', 1)[0].strip()
    if k in want:
        # старую строку заменяем, а не дублируем: иначе в .env останется
        # прежний пароль, и systemd возьмёт не тот
        if k not in seen:
            if want[k]:
                out.append(f'{k}={want[k]}')
            seen.add(k)
        continue
    out.append(ln)
for k, v in want.items():
    if k not in seen and v:
        out.append(f'{k}={v}')
open(p, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
os.chmod(p, 0o600)
print('  .env обновлён:', ', '.join(sorted(k for k in want if want[k])))
PY"
fi
if [ "$PUBLIC" = "1" ] && \
   ! ssh "$VPS" "grep -q '^DASHBOARD_PASSWORD=.' ~/bybit-funding/.env 2>/dev/null"; then
    echo "!! Публичный режим: DASHBOARD_PASSWORD не задан в ~/bybit-funding/.env"
    echo "   Панель с --host 0.0.0.0 без пароля НЕ стартует (fail closed)."
    echo "   Задайте пароль и запустите скрипт снова:"
    echo "     DASHBOARD_PUBLIC=1 DASHBOARD_PASSWORD='ваш-пароль' bash deploy/deploy_vps.sh"
    exit 1
fi

echo "==> [$VPS] ставлю systemd-сервисы"
ssh "$VPS" "$SUDO cp ~/bybit-funding/deploy/funding-watch.service /etc/systemd/system/ && \
            $SUDO cp ~/bybit-funding/deploy/dashboard.service /etc/systemd/system/ && \
            $SUDO systemctl daemon-reload && \
            $SUDO systemctl enable --now funding-watch dashboard && \
            sleep 3 && systemctl is-active funding-watch dashboard"

# Публичный bind — только по запросу; drop-in переопределяет ExecStart.
# systemd читает drop-in'ы только из <юнит>.d/*.conf — файл, положенный
# просто в /etc/systemd/system/dashboard-public.conf, был бы мёртвым грузом:
# панель молча осталась бы на localhost в обоих режимах.
DROPIN_DIR=/etc/systemd/system/dashboard.service.d
if [ "$PUBLIC" = "1" ]; then
    echo "==> [$VPS] включаю публичный доступ к панели (0.0.0.0:8050)"
    ssh "$VPS" "$SUDO mkdir -p $DROPIN_DIR && \
                $SUDO cp ~/bybit-funding/deploy/dashboard-public.conf \
                         $DROPIN_DIR/public.conf && \
                $SUDO systemctl daemon-reload && \
                $SUDO systemctl restart dashboard"
else
    echo "==> [$VPS] панель остаётся на 127.0.0.1 (доступ — через SSH-туннель)"
    ssh "$VPS" "$SUDO rm -f $DROPIN_DIR/public.conf && \
                $SUDO rmdir $DROPIN_DIR 2>/dev/null; \
                $SUDO systemctl daemon-reload && \
                $SUDO systemctl restart dashboard"
fi

echo "==> [$VPS] ставлю ежедневный health-check (таймер)"
ssh "$VPS" "$SUDO cp ~/bybit-funding/deploy/funding-health.service ~/bybit-funding/deploy/funding-health.timer /etc/systemd/system/ && \
            $SUDO systemctl daemon-reload && \
            $SUDO systemctl enable --now funding-health.timer && \
            systemctl list-timers funding-health.timer --no-pager"

echo "==> [$VPS] обновляю зеркало-эталон для сверки кода"
# Зеркало — срез того, что развёрнуто сейчас. Без него drift-check сравнивал
# бы сервер со старым срезом и врал бы «всё совпадает».
if ! MIRROR=bybit-drift/mirror.git VPS="$VPS" bash "$(dirname "$0")/../ci/publish_mirror.sh"; then
    echo "!! зеркало не обновилось — drift-check будет сравнивать со старым"
fi

echo "==> [$VPS] ставлю ежедневную сверку кода с эталоном (таймер)"
ssh "$VPS" "$SUDO cp ~/bybit-funding/deploy/drift-check.service ~/bybit-funding/deploy/drift-check.timer /etc/systemd/system/ && \
            $SUDO systemctl daemon-reload && \
            $SUDO systemctl enable --now drift-check.timer && \
            systemctl list-timers drift-check.timer --no-pager"

if [ "$PUBLIC" = "1" ]; then
    echo "==> [$VPS] открываю порт панели (вход — только по паролю)"
    # ufw может не быть — тогда порт открыт по умолчанию, падать нельзя
    ssh "$VPS" "command -v ufw >/dev/null 2>&1 && { $SUDO ufw allow ssh >/dev/null 2>&1; \
                $SUDO ufw allow 8050/tcp >/dev/null 2>&1; $SUDO ufw status | head -8; } \
                || echo '  ufw не установлен — порт 8050 слушает все интерфейсы по умолчанию'"
else
    echo "==> [$VPS] порт 8050 наружу НЕ открываю — панель через SSH-туннель"
    # Если панель раньше открывали наружу, убираем правило: иначе порт
    # останется открытым, а слушать там уже нечего.
    ssh "$VPS" "command -v ufw >/dev/null 2>&1 && { $SUDO ufw delete allow 8050/tcp >/dev/null 2>&1 || true; } ; true"
fi

echo "==> Готово. Панель:"
if [ "$PUBLIC" = "1" ]; then
    echo "    http://<IP сервера>:8050  →  вход по паролю из DASHBOARD_PASSWORD"
else
    echo "    http://127.0.0.1:8050  →  через SSH-туннель:"
    echo "      ssh -L 8050:127.0.0.1:8050 $VPS"
    echo "    (панель слушает только 127.0.0.1, порт наружу не открыт)"
fi
echo "    Кнопки управления живут с токеном из DASHBOARD_TOKEN в ~/bybit-funding/.env"
echo "    Лог бота:  ssh $VPS 'tail -20 ~/bybit-funding/watch.log'"
echo "    Статус:    ssh $VPS 'systemctl status funding-watch dashboard --no-pager'"
echo "    Дрейф:     ssh $VPS 'systemctl status drift-check --no-pager; cat ~/bybit-funding/drift_report.txt'"
echo "    Позиция:   ssh $VPS 'cd ~/bybit-funding && python3 funding_executor.py status'"
echo "    Закрыть:   ssh $VPS 'cd ~/bybit-funding && python3 funding_executor.py close'"
