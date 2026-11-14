# Group Cleaner + Auto Avatar Bot

## O'rnatish
1. @BotFather → /newbot → token oling.
2. `cp .env.example .env` va `BOT_TOKEN` ni yozing.
3. `python -m venv venv && source venv/bin/activate` (Windows: `venv\Scripts\activate`)
4. `pip install -r requirements.txt`
5. `python bot.py`

## Ishlatish
- Botga private chatda /start bosing (faqat OWNER_ID uchun panel ochiladi).
- Botni guruhga qo'shib, **admin** qiling. Huquqlar:
  - Xabarlarni o'chirish
  - Guruh ma'lumotlarini o'zgartirish (avatar uchun)
- Bot guruhda hech narsa yozmaydi: xizmat xabarlarini o'chiradi va 1 marta avatar qo'yadi.
- Sozlamalar: panel → Guruhlar → guruhni tanlang.

## Server (systemd) — ixtiyoriy
`/etc/systemd/system/tgbot.service`:
[Service]
WorkingDirectory=/path/to/tgbot
ExecStart=/path/to/tgbot/venv/bin/python bot.py
Restart=always
