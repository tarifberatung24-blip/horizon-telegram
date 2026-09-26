# HORIZON Telegram bridge

Пишеш на бота в Telegram → агентът OpenHands получава съобщението и работи.

## Инсталация на Oracle (копирай ред по ред)

```bash
# 1. Изтегли
git clone https://github.com/tarifberatung24-blip/horizon-telegram.git
cd horizon-telegram

# 2. Направи конфигурация. Смени TOKEN-A-ТУК с твоя ключ от BotFather.
cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=TOKEN-A-ТУК
TELEGRAM_ALLOWED_USER_IDS=2065255514
OPENHANDS_MODE=cloud
OPENHANDS_CLOUD_API_KEY=ОЩЕ-ЕДИН-КЛЮЧ
OPENHANDS_REPOSITORY=tarifberatung24-blip/VZGplattform
OPENHANDS_BRANCH=main
EOF
chmod 600 .env

# 3. Пусни
python3 bridge.py
```

Ако каже `bridge: up in cloud mode` — работи. Отвори Telegram, пиши на бота.

## Спри го

`Ctrl+C`

## Пусни го като услуга (за да работи и след затваряне на терминала)

```bash
sudo tee /etc/systemd/system/horizon-telegram.service >/dev/null <<'EOF'
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

Проверка: `journalctl -u horizon-telegram -f`

## Команди в Telegram

```
/help    списък с командите
/status  какво прави агентът
/new     нов разговор от нулата
```

Всичко друго отива при агента.

## Два режима

- **cloud** — агентът работи в OpenHands облак, вижда само репото. Безопасно. **Започни с това.**
- **local** — агентът работи на Oracle машината и може да пипа файлове. Иска `OPENHANDS_SESSION_API_KEY` и `OPENHANDS_AGENT_CONFIG`.

Смени `OPENHANDS_MODE=cloud` на `local`, когато си готов.

## Тестове

```bash
python3 -m unittest discover -s tests
```

30 теста, без интернет. Работят офлайн.

## Сигурност

- Ботът отговаря само на `2065255514`. Другите получават „Not authorized."
- Токенът е само в `.env` (chmod 600). Никога не в логове.
- Не иска и не пази пароли, PIN, TAN, OTP.

## Ако не тръгне

- `refusing to start: TELEGRAM_BOT_TOKEN is required` → забравил си стъпка 2.
- `Unauthorized` от Telegram → грешен токен.
- Ботът мълчи → провери `journalctl -u horizon-telegram -f`.
