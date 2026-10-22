# 🚀 Запуск бота на VPS (Ubuntu) — инструкция

Бот будет работать **24/7 сам**, переживать обрывы сети и перезагрузки.
Ноутбук можно выключать.

Понадобится: IP сервера, доступ по SSH, ~20 минут.

---

## Шаг 1. Подключиться

```bash
ssh admin@ТВОЙ_IP
```

Приглашение вида `admin@vm:~$` — вы внутри сервера. Все команды ниже вводятся
**там**, в этом окне. Где нужны права root, перед командой стойт `sudo`;
постоянная root-сессия — `sudo -i` (приглашение станет `root@vm:~#`).

> Первичная настройка свежего сервера (когда пользователя `admin` ещё нет)
> делается от root через консоль хостера или `ssh root@IP` — пока root-логин
> не выключен. Дальше всё управление идёт через `admin`.

### Ключ вместо пароля (рекомендуется сразу)

SSH-ключ **не нужно нигде искать** — он создаётся на вашем ноутбуке, а
публичная половина один раз кладётся на сервер. Пароль от сервера после
этого можно отключить (сервера ломают перебором тысячи раз в день).

**1. Сгенерировать ключ (на ноутбуке, один раз):**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_bybit -C "bybit-funding-vps"
```

Пустая passphrase (Enter на оба вопроса) — чтобы деплой работал без
интерактива. Создаются два файла:

| файл | что это | можно ли показывать |
|---|---|---|
| `~/.ssh/id_ed25519_bybit` | **приватный** ключ | **никогда** — это вход в сервер |
| `~/.ssh/id_ed25519_bybit.pub` | публичный ключ | можно — его и надо на сервер |

> Если ключ уже создан (проверка: `ls ~/.ssh/id_ed25519_bybit*`) — шаг
> пропускайте, генерировать второй не нужно. Скрипт `deploy/install_key.py`
> по умолчанию берёт именно `~/.ssh/id_ed25519_bybit`; другой путь —
> через переменную `VPS_KEY`.

**2. Положить публичный ключ на сервер** (нужен пароль, один раз):

```bash
# из папки проекта — скрипт сам добавит ключ в authorized_keys
python deploy/install_key.py --ask          # спросит пароль в терминале
# или, если так удобнее (пароль попадёт в историю команд!):
VPS_PASS='root-пароль' python deploy/install_key.py
```

Пароль берётся из переменной окружения и **не печатается** — в коде, в
истории команд и в выводе его нет. Вариант `--ask` вообще не пишет пароль
в историю: скрипт спрашивает его в терминале (ввод не отображается).
Альтернатива без пароля (если есть веб-консоль хостера) — вставить строку из
`~/.ssh/id_ed25519_bybit.pub` в `/root/.ssh/authorized_keys` вручную.

> Скрипт `deploy/install_key.py` работает только до ужесточения входа:
> после шага 5 root-логин и пароль выключены, и он уже не подключится.

**3. Завести пользователя `admin`** (сразу после того, как ключ уже работает):

```bash
sudo useradd -m -s /bin/bash -g admin admin
sudo install -d -m 700 -o admin -g admin /home/admin/.ssh
sudo cp /root/.ssh/authorized_keys /home/admin/.ssh/authorized_keys
sudo chown admin:admin /home/admin/.ssh/authorized_keys
sudo chmod 600 /home/admin/.ssh/authorized_keys
sudo usermod -p '*' admin                 # пароль заблокирован: вход только по ключу
# эскалация без пароля — нужна deploy-скриптам (ssh не спрашивает пароль)
echo 'admin ALL=(ALL:ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/90-admin-nopasswd
sudo chmod 440 /etc/sudoers.d/90-admin-nopasswd
sudo visudo -cf /etc/sudoers.d/90-admin-nopasswd   # должно быть: parsed OK
```

**4. Проверить вход новым пользователем и зафиксировать ключ в `~/.ssh/config`:**

```bash
ssh -o BatchMode=yes admin@ТВОЙ_IP 'echo ДОСТУП_ЕСТЬ'
```

```
Host ТВОЙ_IP
    HostName ТВОЙ_IP
    User admin
    IdentityFile ~/.ssh/id_ed25519_bybit
    IdentitiesOnly yes
```

**5. Только после этого** ужесточить вход (файл `deploy/sshd-hardening.conf`):

```bash
# ВАЖНО: имя 00-hardening.conf выбрано не случайно. sshd берёт ПЕРВОЕ
# значение параметра, а /etc/ssh/sshd_config подключает drop-in'ы самой
# верхней строкой (Include ...). Файл 50-cloud-init.conf с
# "PasswordAuthentication yes" проигрывает первому; файл 99-*.conf, наоборот,
# ничего не переопределит.
sudo install -m 644 deploy/sshd-hardening.conf /etc/ssh/sshd_config.d/00-hardening.conf
sudo rm -f /etc/ssh/sshd_config.d/00-disable-password-auth.conf   # его содержимое вошло в новый файл
sudo sshd -t                    # проверить синтаксис ДО reload — иначе sshd не поднимется
sudo systemctl reload ssh       # reload, а не restart: текущие сессии не оборвутся
sudo sshd -T | grep -Ei '^(permitrootlogin|allowusers)'   # permitrootlogin no / allowusers admin
```

Проверьте **новыми** сессиями: `admin` входит, `root` — нет:

```bash
ssh -o BatchMode=yes admin@ТВОЙ_IP 'echo ДОСТУП_ЕСТЬ'   # должно напечатать ДОСТУП_ЕСТЬ
ssh -o BatchMode=yes root@ТВОЙ_IP  'echo ДОСТУП_ЕСТЬ'   # должно быть Permission denied
```

> ⚠️ Не закрывайте текущую сессию, пока `admin` не подтвердил вход. Если
> что-то пошло не так, откат из **веб-консоли хостера**:
> `rm /etc/ssh/sshd_config.d/00-hardening.conf && systemctl reload ssh`.

**6. fail2ban** — баны за перебор ключей (`/etc/fail2ban/jail.local`):

```bash
sudo apt-get install -y fail2ban
sudo tee /etc/fail2ban/jail.local >/dev/null <<'EOF'
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 37.78.70.143 85.172.100.38 62.183.17.163 5.178.109.237
bantime  = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
backend = systemd
maxretry = 4
bantime  = 2h
EOF
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sshd      # Currently banned — сколько адресов уже отбито
```

IP в `ignoreip` — ваши собственные адреса: если промахнуться ключом с них,
бан не запрёт вас самого. Список адресов в `AllowUsers` НЕ указываем
сознательно: домашний IP меняется, и жёсткий allowlist по адресу запирает
доступ при смене провайдера.

---

## Шаг 2. Проверить Python и часы

```bash
python3 --version
timedatectl status | head -5
```

Должно быть Python 3.10+ (весь код на чистом stdlib, `pip install` не нужен).

**Часы критичны.** Bybit отклоняет подписанные запросы, если локальное время
отличается больше чем на 5 секунд (ошибка `10002`) — тогда бот не сможет
торговать вообще. Если `System clock synchronized: no`:

```bash
timedatectl set-ntp true
sleep 3 && timedatectl status | grep -i synchronized
```

(Бот также поднимет алерт «ЧАСЫ СЕРВЕРА РАСХОДЯТСЯ С БИРЖЕЙ», если это
всё-таки случится.)

---

## Шаг 3. Загрузить файлы

### Способ А — автоматически (рекомендую)

С **ноутбука**, из папки проекта:

```bash
# Linux/macOS:
VPS=admin@ТВОЙ_IP bash deploy/deploy_vps.sh

# Windows (PowerShell):
$env:VPS="admin@ТВОЙ_IP"; bash deploy/deploy_vps.sh
```

Скрипт сам: создаст папки, скопирует все модули (`funding_executor.py`,
`bybit_memory.py`, `bybit_ws.py`, …), проверит синтаксис, прогоняет офлайн-тесты,
проверит часы, поставит systemd-сервисы и включит часовой PnL-отчёт
(`report-every=60`).

**Панель по умолчанию слушает только `127.0.0.1`**, и доступ к ней — через
SSH-туннель. Порт 8050 наружу скрипт НЕ открывает:

```bash
ssh -L 8050:127.0.0.1:8050 admin@ТВОЙ_IP     # → http://127.0.0.1:8050
```

Открыть панель всему интернету — только осознанно и обязательно с паролем:

```bash
DASHBOARD_PUBLIC=1 DASHBOARD_PASSWORD='ваш-пароль' bash deploy/deploy_vps.sh
```

Тогда скрипт поставит drop-in `dashboard-public.conf` (bind `0.0.0.0`),
запишет `DASHBOARD_PASSWORD` и `DASHBOARD_PUBLIC=1` в `.env` на сервере и
откроет порт (`ufw allow 8050/tcp`). Без пароля панель с публичным bind
не стартует (fail closed).

**Важно:** `.env` с ключами копируется **только если его ещё нет** на сервере —
рабочие ключи на VPS не затираются. Форсировать: `ENVFILE=другой_файл bash ...`.

### Способ Б — вручную

С **ноутбука** (в отдельном окне, не в том, где ssh):

```bash
ssh admin@ТВОЙ_IP "mkdir -p ~/bybit-funding/deploy ~/bybit-funding/snapshots"
scp funding_monitor.py funding_executor.py bybit_memory.py bybit_ws.py \
    trade_sanity.py dashboard.py dashboard_metrics.py backtest.py \
    healthcheck.py smotrim.py README.md config.example.json \
    admin@ТВОЙ_IP:~/bybit-funding/
scp test_*.py run_tests.py admin@ТВОЙ_IP:~/bybit-funding/
scp deploy/*.service deploy/*.timer admin@ТВОЙ_IP:~/bybit-funding/deploy/
scp .env admin@ТВОЙ_IP:~/bybit-funding/
```

На сервере:

```bash
chmod 600 ~/bybit-funding/.env
```

`.env` — файл с ключами (создаётся вручную, в git не коммитится):

```
BYBIT_TESTNET_API_KEY=...
BYBIT_TESTNET_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
# токен кнопок управления (генерируется или задаётся вручную)
DASHBOARD_TOKEN=...
# Публичный доступ к панели по умолчанию НЕ включается: панель слушает
# только 127.0.0.1, доступ — через SSH-туннель. Если включаете — пароль
# обязателен, без него сервис не стартует:
# DASHBOARD_PASSWORD=ваш-пароль
# DASHBOARD_PUBLIC=1
```

Для **mainnet** добавить `BYBIT_API_KEY` / `BYBIT_API_SECRET` — ключи
**без права вывода** и с IP-whitelist этого сервера.

---

## Шаг 4. Проверить на сервере

```bash
cd ~/bybit-funding

# синтаксис и импорты
python3 -m py_compile funding_executor.py bybit_memory.py bybit_ws.py \
    trade_sanity.py dashboard.py dashboard_metrics.py && echo OK

# офлайн-тесты (сеть не нужна; живой тест печатает SKIP)
PYTHONIOENCODING=utf-8 python3 run_tests.py

# живой отчёт
python3 funding_monitor.py --top 5
```

---

## Шаг 5. Запустить 24/7 (systemd)

```bash
sudo cp ~/bybit-funding/deploy/*.service ~/bybit-funding/deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now funding-watch dashboard funding-health.timer drift-check.timer
systemctl status funding-watch dashboard --no-pager
```

Что поднялось:

| сервис | что делает |
|---|---|
| `funding-watch` | сам бот (`watch`), перезапуск при падении |
| `dashboard` | панель на `127.0.0.1:8050` — доступ через SSH-туннель (публичный bind — только по запросу `DASHBOARD_PUBLIC=1`): закрытие позиции, управление сервисом бота (`systemctl`) и ручной вход |
| `funding-health.timer` | ежедневная проверка (09:00 UTC) + авто-восстановление |
| `drift-check.timer` | ежедневная сверка кода с эталоном (10:15 UTC): отчёт `~/bybit-funding/drift_report.txt` + алерт в Telegram |

---

## Настройка торговли: config.json

Параметры торговли лежат в `~/bybit-funding/config.json` (деплой создаёт его
из примера, если файла ещё нет). Правьте его вместо `ExecStart` в unit-файле:

```bash
nano ~/bybit-funding/config.json
sudo systemctl restart funding-watch
```

```json
{
  "mode": "testnet",
  "capital": 1000,
  "max-positions": 1,
  "leverage": 2.0,
  "preset": "hold",
  "hold-payouts": 2,
  "exit-neg": 3,
  "max-loss": 5.0,
  "report-every": 60,
  "daily-report-at": "00:00",
  "report-tz": "+03:00"
}
```

- `report-every` — часовой отчёт в Telegram (баланс счёта, PnL за день,
  позиция, закрытые сделки); `daily-report-at` — ежедневная сводка (баланс,
  итог дня и недели) в это время; `report-tz` — часовой пояс отчётов: имя
  (`Europe/Moscow`) или сдвиг (`+03:00`). **Важно:** на VPS время обычно UTC,
  а день считается по `report-tz` — без него сводка в «00:00» придёт в 03:00 МСК;
- ключи, начинающиеся с `_`, — комментарии (игнорируются);
- **несколько пар**: `"max-positions": 3` — бот держит до 3 пар одновременно;
  каждая новая пар получает `min(capital, свободная маржа / 1.1)`; закрытие
  конкретной пары — `close --symbol BTCUSDT`, всех сразу — `close --all`
  (в панели у каждой пары свои кнопки);
- флаг в командной строке **перекрывает** конфиг:
  `python3 funding_executor.py watch --capital 500`;
- опечатка в имени параметра — понятная ошибка при старте, а не тихий промах;
- секреты (`BYBIT_API_KEY` и т.д.) остаются в `.env` — в конфиге их быть
  не должно.

Перед сменой капитала на живых деньгах проверь, что новые значения проходят
бэктест (ниже) — иначе легко выставить непроходимый порог, и бот молча
перестанет торговать.

---

## Бэктест перед запуском денег

```bash
python3 backtest.py --preset hold --sweep
```

Показывает, что стратегия дала бы на истории и какой порог лучший.
Подробности и честные ограничения — в README, раздел «Бэктест».

**Важно:** на данных осени 2026 бэктест показал, что фандинг на мейджерах
не отбивает комиссию круга (покрытие ~0.02×), а базис перп/спот стабильно
отрицательный (−0.05%). Значит пресет `hold` в это окно **не торгует** — и
это правильно. Не «чините» это ослаблением порогов: бэктест покажет, что
при ослаблении стратегия уходит в минус на комиссиях.

---

Управление:

```bash
sudo systemctl restart funding-watch     # перезапустить после правок кода
sudo systemctl stop funding-watch
systemctl status funding-watch --no-pager
journalctl -u funding-watch -n 100       # последние 100 строк
journalctl -u funding-watch -f           # смотреть вживую (Ctrl+C — выйти)
tail -f ~/bybit-funding/bot.log          # журнал событий бота
```

---

## Шпаргалка: торговля

```bash
cd ~/bybit-funding
python3 funding_executor.py status       # позиция и PnL
python3 funding_executor.py scan        # кто проходит фильтр
python3 funding_executor.py check        # решение по выходу
python3 funding_executor.py close       # закрыть позицию
python3 funding_executor.py stats        # статистика сделок
python3 funding_executor.py memory       # выводы памяти + пороги
python3 funding_executor.py memory --relearn   # пересобрать память из bot.log
```

⚠️ `close` при фантомной позиции (на бирже её уже нет, а в `position.json`
она ещё числится) **опасен** — докупит перп-ногу и оставит голый лонг.
Тогда:

```bash
python3 funding_executor.py void --force
```

---

## Переход на реальные деньги (mainnet)

Только после нескольких недель нормальной работы на testnet.

1. Создать ключи на bybit.com → API Management: **без права вывода**,
   с **IP-whitelist** этого VPS.
2. Добавить в `~/.env`:
   ```
   BYBIT_API_KEY=...
   BYBIT_API_SECRET=...
   ```
   и `chmod 600 ~/.env`.
3. Прогнать монитор с запасом по комиссиям:
   ```bash
   python3 funding_monitor.py --preset mainnet
   ```
   (в пресете `mainnet` пороги выше: `min_apy30 ≥ 15%`, `OI ≥ $30M`).
4. Запустить с явным подтверждением (без флага бот не отправит ордера):
   ```bash
   python3 funding_executor.py watch --mode mainnet --confirm-live \
       --preset mainnet --ws
   ```
5. И обновить сервис на mainnet:
   ```bash
   sudo nano /etc/systemd/system/funding-watch.service
   # в ExecStart заменить --mode testnet на --mode mainnet --confirm-live --preset mainnet
   sudo systemctl daemon-reload && sudo systemctl restart funding-watch
   ```

**Что страхует бота на mainnet:**
- биржевой стоп-лосс на перп-ногу (срабатывает без нас, если VPS лежит);
- проверка баланса = спот-нотион + маржа перпа до входа;
- сверка расчётного funding с фактическим при каждом закрытии;
- блокировка автОВхода при потере связи с биржей (30 минут);
- атомарная запись `position.json` (падение не оставит «слепого» бота);
- ежедневный health-check с алертом и авто-восстановлением.

---

## Безопасность сервера

```bash
# 1. Ключ вместо пароля (сервера с ssh ломают тысячами попыток в день)
#    подробно — в начале инструкции, раздел «Ключ вместо пароля»
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_bybit
python deploy/install_key.py --ask
ssh -o BatchMode=yes admin@ТВОЙ_IP 'echo ДОСТУП_ЕСТЬ'   # проверить!
# ⚠️ сначала убедись, что вход по ключу работает, ПОТОМ отключай пароль.
# Правка самого sshd_config НЕ сработает — нужен drop-in 00-*.conf
# (см. «Ключ вместо пароля», шаг 5): sshd берёт первое значение параметра,
# а 50-cloud-init.conf уже содержит "PasswordAuthentication yes".

# 2. Firewall
sudo ufw allow ssh && sudo ufw enable
# Панель: СНАЧАЛА посмотрите, как она слушает порт:
#   ss -ltn | grep 8050
#   127.0.0.1:8050  → панель видна ТОЛЬКО с самого сервера: снаружи не нужна,
#                     открывать порт НЕЛЬЗЯ, заходите через SSH-туннель
#   0.0.0.0:8050    → панель видна всему интернету: нужен пароль
#                     (DASHBOARD_PASSWORD) и тогда открываем порт:
#   sudo ufw allow 8050/tcp
# SSH-туннель (без открытого порта) — всегда безопасный вариант:
#   ssh -L 8050:127.0.0.1:8050 root@IP   →   http://127.0.0.1:8050
# Кнопки управления на панели работают с токеном DASHBOARD_TOKEN из
# ~/bybit-funding/.env; без токена кнопок нет.

# 3. Обновления раз в пару недель
sudo apt update && sudo apt upgrade -y
```

Ключи Bybit — **никогда** без IP-whitelist и **никогда** с правом вывода.

---

## Если что-то пошло не так

| Симптом | Что делать |
|---|---|
| `systemctl status` = failed | `journalctl -u funding-watch -n 50` — смотреть ошибку |
| Бот не торгует, в логе `10002` | часы: `timedatectl set-ntp true` |
| Бот не торгует, алерт «СТАТЕ ПОВРЕЖДЕНО» | `position.json` битый → проверь позицию на бирже; если её нет, `rm ~/bybit-funding/position.json.bak` и перезапуск |
| Алерт «расхождение позиции» | позиция на бирже ≠ `position.json`: `close` (если ноги есть) или `void --force` (если позиции нет) |
| Алерт «расхождение funding» | формулы разошлись с биржой — скинь мне цифры из лога |
| Панель не открывается по IP | так и должно быть: панель слушает `127.0.0.1`, а доступ — через туннель `ssh -L 8050:127.0.0.1:8050 root@IP`. Проверить: `systemctl status dashboard`, `ss -ltn | grep 8050` |
| Нужен публичный доступ к панели | `DASHBOARD_PUBLIC=1 DASHBOARD_PASSWORD='...' bash deploy/deploy_vps.sh` — поставит drop-in `dashboard-public.conf`, запишет пароль в `.env` и откроет порт. Без пароля панель не стартует |
| Панель просит пароль — не помню | пароль нужен только в публичном режиме: новый пароль в `.env` (`DASHBOARD_PASSWORD=...`, старую строку удалить), `systemctl restart dashboard` |
| Панель показывает зелёный +$0.00 без сети | так и должно быть без `--no-live`; проверь `dashboard` в логе |