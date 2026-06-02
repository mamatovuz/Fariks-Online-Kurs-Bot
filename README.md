# FARIKS LMS Platformasi

Bu workspace ichida ikki asosiy qism bor:

- `bot/` - Telegram bot, MongoDB baza, test API va admin endpointlar.
- `client/` - test sahifasi va oddiy admin panel interfeysi.

## Ishga tushirish

1. `bot/.env.example` faylini `bot/.env` qilib nusxalang.
2. `BOT_TOKEN` va `MONGODB_URI` qiymatlarini to'ldiring.
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
PUBLIC_API_URL=https://farikstest.up.railway.app
ADMIN_TELEGRAM_ID=7903688837
ADMIN_TOKEN=mustahkam-admin-parol
APP_SECRET=uzun-maxfiy-random-string
MONGODB_URI=mongodb+srv://user:password@cluster.mongodb.net/?retryWrites=true&w=majority
MONGODB_DB=fariks_lms
```

`DB_PATH` va Railway Volume endi kerak emas. Ma'lumotlar MongoDB kolleksiyalarida
saqlanadi. Public Networking yoqilgandan keyin Railway domeni test linklarda
ishlatiladi. Agar frontend va backend alohida servis bo'lsa:

```text
PUBLIC_CLIENT_URL=https://frontend-domeningiz
PUBLIC_API_URL=https://backend-domeningiz
```

MongoDB kolleksiyalar: `users`, `courses`, `modules`, `lessons`, `questions`,
`payments`, `enrollments`, `progress`, `results`, `test_tokens`, `user_states`,
`counters`.

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

Telegramda admin `/admin` deb yozsa, `ADMIN_TELEGRAM_ID` mos kelsa bot
admin panel uchun maxsus link yuboradi. Link orqali panel token so'ramasdan
ochiladi.

Real to'lov integratsiyalari uchun Click, Payme, Uzum webhooklarini keyingi bosqichda shu API tuzilmasiga ulash mumkin.
