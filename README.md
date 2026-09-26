# HORIZON Telegram bridge

Пишеш на бота в Telegram → агентът OpenHands получава съобщението и работи.

## Инсталация на Oracle (копирай 3 реда)

```bash
git clone https://github.com/tarifberatung24-blip/horizon-telegram.git
cd horizon-telegram
bash setup.sh
```

Това е. Скриптът ще те пита за 2 неща (скрито, никой не ги вижда):
1. **Telegram bot token** — ключът от BotFather
2. **OpenHands API key** — от `app.all-hands.dev` → Settings → API keys

После сам пуска тестовете и стартира.

Ако видиш `bridge: up in cloud mode` — работи. Отвори Telegram, пиши на бота.

## Спри го

`Ctrl+C`

## Пусни го да работи постоянно (затваряш терминала, ботът остава)

```bash
sudo tee /etc/systemd/system/horizon-telegram.service >/dev/null <<EOF
[Unit]
Description=HORIZON Telegram bridge
After=network-online.target

[Service]
User=$USER
WorkingDirectory=$PWD
EnvironmentFile=$PWD/.env
ExecStart=/usr/bin/python3 $PWD/bridge.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now horizon-telegram
```

Виж какво прави: `journalctl -u horizon-telegram -f`

## Команди в Telegram

```
/help    списък с командите
/status  какво прави агентът
/new     нов разговор от нулата
```

Всичко друго отива при агента.

## Два режима

- **cloud** (по подразбиране) — агентът работи в OpenHands облак, вижда само репото. Безопасно.
- **local** — агентът работи на Oracle машината и може да пипа файлове. Иска още настройки. Смени `OPENHANDS_MODE=cloud` на `local`, когато си готов.

## Сигурност

- Ботът отговаря само на `2065255514`. Другите получават „Not authorized."
- Токенът е само в `.env` (само ти можеш да го четеш, `.gitignore` го пази от commit). Никога не влиза в логове.
- Не иска и не пази пароли, PIN, TAN, OTP.

## Ако не тръгне

| Съобщение | Какво значи |
|---|---|
| `refusing to start: TELEGRAM_BOT_TOKEN is required` | не си дал токен — пусни `bash setup.sh` пак |
| `HTTP Error 401: Unauthorized` | грешен Telegram токен |
| `HTTP Error 401` от OpenHands | грешен OpenHands ключ |
| Ботът мълчи | виж `journalctl -u horizon-telegram -f` |

## Тестове

```bash
python3 -m unittest discover -s tests
```

30 теста, без интернет.
