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
