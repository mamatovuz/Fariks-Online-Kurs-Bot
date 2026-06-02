# FARIKS LMS Platformasi

Bu workspace ichida ikki asosiy qism bor:

- `bot/` - Telegram bot, SQLite baza, test API va admin endpointlar.
- `client/` - test sahifasi va oddiy admin panel interfeysi.

## Ishga tushirish

1. `bot/.env.example` faylini `bot/.env` qilib nusxalang.
2. `BOT_TOKEN` qiymatini Telegram BotFather tokeni bilan to'ldiring.
3. Terminalda ishga tushiring:

```powershell
cd bot
python app.py
```

Server client saytni ham o'zi beradi:

- Test sayt: `http://localhost:8080/test/lesson-1?token=...`
- Admin panel: `http://localhost:8080/admin`
- API health: `http://localhost:8080/api/health`

Admin panel tokeni `.env` ichidagi `ADMIN_TOKEN` qiymati bilan kiriladi.

## Railway deploy

Railway servisni GitHub repodan deploy qiling. Repo ichidagi `railway.json` serverni
`python app.py` bilan ishga tushiradi va `/api/health` orqali tekshiradi.

Railway Settings ichida:

```text
Root Directory: bo'sh qoldiring
Start Command: python app.py
```

Root Directory `bot` bo'lsa frontend deployga kirmaydi.

Railway Variables bo'limiga quyidagilarni qo'shing:

```text
BOT_TOKEN=Telegram BotFather tokeni
PUBLIC_CLIENT_URL=https://farikstest.up.railway.app
ADMIN_TOKEN=7903688837
DB_PATH=/data/fariks_lms.sqlite3
```

SQLite ma'lumotlari saqlanishi uchun Railway servisga Volume qo'shing va uni
`/data` ga mount qiling. Public Networking yoqilgandan keyin Railway domeni
test linklarda avtomatik ishlatiladi. Agar custom domen ishlatsangiz:

```text
PUBLIC_CLIENT_URL=https://sizning-domeningiz.uz
```

## Bot oqimi

Bot quyidagilarni bajaradi:

- `/start` orqali ro'yxatdan o'tkazadi.
- Kurslar ro'yxatini ko'rsatadi.
- To'lov usulini tanlatadi va MVP rejimida to'lovni tasdiqlaydi.
- Kursga qo'shadi.
- Modul va darslarni bosqichma-bosqich ochadi.
- Dars uchun test link yuboradi.
- Test natijasi 80% yoki undan yuqori bo'lsa keyingi darsni ochadi.
- Natijani Telegramga yuboradi.

## Admin panel

Admin panel orqali boshlang'ich MVP rejimida quyidagilar bor:

- Kurs yaratish.
- Modul yaratish.
- Dars qo'shish.
- Test savoli qo'shish.
- O'quvchilar, to'lovlar va natijalarni ko'rish.

Real to'lov integratsiyalari uchun Click, Payme, Uzum webhooklarini keyingi bosqichda shu API tuzilmasiga ulash mumkin.
