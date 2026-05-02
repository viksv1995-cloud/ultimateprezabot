import asyncio, os, re, json, uuid, base64, time, logging, sys
from io import BytesIO
from datetime import datetime
from typing import Optional, Dict
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
)
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramRetryAfter
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.enum.text import PP_ALIGN
from yookassa import Configuration, Payment

# ===== НАСТРОЙКА ЛОГОВ =====
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("bot")

# ===== ЗАГРУЗКА .env =====
if not os.getenv("RAILWAY_ENVIRONMENT"):
    env_file = Path(__file__).parent / '.env'
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ[k.strip()] = v.strip()

# ===== ПРОВЕРКА КЛЮЧЕЙ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
GIGA_AUTH = base64.b64encode(
    f"{os.getenv('GIGACHAT_CLIENT_ID')}:{os.getenv('GIGACHAT_SECRET')}".encode()
).decode()
YOOKASSA_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_KEY = os.getenv("YOOKASSA_SECRET_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PRICE = 100  # рублей

if not all([BOT_TOKEN, GIGA_AUTH, YOOKASSA_ID, YOOKASSA_KEY]):
    raise SystemExit("❌ Не все ключи в .env файле!")

Configuration.account_id = YOOKASSA_ID
Configuration.secret_key = YOOKASSA_KEY

# ===== РАЗМЕРЫ СЛАЙДА =====
SW = Emu(12192000)  # ширина 13.333"
SH = Emu(6858000)   # высота 7.5"
IW = Emu(5029200)   # ширина картинки 5.5"
MG = Emu(274320)    # отступ 0.3"
GP = Emu(365760)    # зазор 0.4"

# ===== СОСТОЯНИЯ =====
class State(StatesGroup):
    topic = State()
    payment = State()

# ===== ТОКЕН GIGACHAT =====
_token = None
_token_exp = 0
_token_lock = asyncio.Lock()

async def get_token():
    global _token, _token_exp
    async with _token_lock:
        if _token and time.time() < _token_exp - 300:
            return _token
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                    headers={"Authorization": f"Basic {GIGA_AUTH}", "RqUID": str(uuid.uuid4())},
                    data={"scope": "GIGACHAT_API_PERS"}, ssl=False, timeout=30
                ) as r:
                    if r.status == 200:
                        _token = (await r.json())["access_token"]
                        _token_exp = time.time() + 3600
                        return _token
        except Exception as e:
            log.error(f"Токен: {e}")
        return None

# ===== БОТ =====
bot = Bot(token=BOT_TOKEN, session=AiohttpSession(timeout=120))
dp = Dispatcher(storage=MemoryStorage())

# ===== КНОПКИ =====
def menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎨 Создать презентацию")],
        [KeyboardButton(text="ℹ️ Помощь"), KeyboardButton(text="💰 Цена")]
    ], resize_keyboard=True)

def pay_kb(url):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить 100₽", url=url)],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data="paid")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])

# ===== GIGACHAT =====
async def ask_ai(text):
    token = await get_token()
    if not token: return None
    
    for try_n in range(3):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "model": "GigaChat",
                        "messages": [
                            {"role": "system", "content": "Ты - профессор. Объясняешь сложное на примерах."},
                            {"role": "user", "content": text}
                        ],
                        "temperature": 0.75, "max_tokens": 3500
                    }, ssl=False, timeout=90
                ) as r:
                    if r.status == 200:
                        return (await r.json())["choices"][0]["message"]["content"]
                    if r.status == 429:
                        await asyncio.sleep(2 ** try_n)
        except: await asyncio.sleep(1)
    return None

async def get_content(topic, n):
    prompt = f"""Сделай презентацию на тему "{topic}". Ровно {n} слайдов.

Слайд 1: титульный (тема, "Москва, 2026")
Слайды 2-{n-1}: с текстом (3-4 предложения С ПРИМЕРАМИ)
Слайд {n}: список литературы (5 книг/статей)

Ответ - ТОЛЬКО JSON:
{{
  "title": "Название презентации",
  "slides": [
    {{"type": "title", "text": "Москва, 2026"}},
    {{"type": "content", "title": "Заголовок", "text": "Текст с примерами", "caption": "Подпись к картинке"}},
    {{"type": "references", "text": "1. Книга\\\\n2. Статья"}}
  ]
}}

ПРИМЕР СЛАЙДА:
{{"type":"content","title":"Как учится нейросеть?","text":"Нейросеть учится как ребёнок: сначала делает ошибки, потом исправляется. Например, чтобы отличать кошек от собак, ей показывают 10 000 фото. Алгоритм обратного распространения ошибки работает как строгий учитель.","caption":"Цифровой мозг учится на ошибках"}}
"""
    resp = await ask_ai(prompt)
    if not resp: return None
    
    s = resp.find('{')
    e = resp.rfind('}')
    if s == -1 or e == -1:
        return None
    
    try:
        data = json.loads(resp[s:e+1])
    except:
        return None
    
    if "slides" not in data:
        return None
    
    return data

# ===== КАРТИНКИ =====
async def get_image(prompt):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://image.pollinations.ai/prompt/{prompt}",
                params={"width": 1024, "height": 768, "nologo": "true"},
                timeout=15
            ) as r:
                if r.status == 200:
                    return BytesIO(await r.read())
    except: pass
    return None

# ===== PPTX =====
async def make_pptx(data):
    slides = data.get("slides", [])
    if not slides: return None
    
    prs = Presentation()
    prs.slide_width = SW
    prs.slide_height = SH
    
    tasks = []
    for s in slides:
        if s.get("type") == "content":
            tasks.append(get_image(s.get("caption", "")))
        else:
            tasks.append(asyncio.sleep(0))
    images = await asyncio.gather(*tasks)
    
    for i, s in enumerate(slides):
        left_side = (i % 2 == 0)
        if left_side:
            img_x = MG
            txt_x = MG + IW + GP
        else:
            img_x = SW - IW - MG
            txt_x = MG
        txt_w = SW - IW - GP - MG*2
        
        try:
            if s.get("type") == "title":
                sl = prs.slides.add_slide(prs.slide_layouts[0])
                sl.shapes.title.text = data.get("title", "Презентация")
                if len(sl.placeholders) > 1:
                    sl.placeholders[1].text = s.get("text", f"Москва, {datetime.now().year}")
                    
            elif s.get("type") == "references":
                sl = prs.slides.add_slide(prs.slide_layouts[1])
                sl.shapes.title.text = "📚 Список литературы"
                if len(sl.placeholders) > 1:
                    sl.placeholders[1].text = s.get("text", "")
                    
            else:
                sl = prs.slides.add_slide(prs.slide_layouts[1])
                sl.shapes.title.text = s.get("title", "Слайд")
                
                if len(sl.placeholders) > 1:
                    tf = sl.placeholders[1]
                    tf.text = s.get("text", "")
                    tf.left = txt_x
                    tf.top = Emu(1371600)
                    tf.width = txt_w
                
                img = images[i]
                if isinstance(img, BytesIO):
                    sl.shapes.add_picture(img, img_x, Emu(1371600), width=IW)
                    cap = sl.shapes.add_textbox(img_x, Emu(5300000), IW, Emu(548640))
                    cap.text_frame.text = s.get("caption", "")
                    cap.text_frame.paragraphs[0].font.size = Pt(10)
                    cap.text_frame.paragraphs[0].font.italic = True
                    cap.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        except Exception as e:
            log.error(f"Слайд {i}: {e}")
    
    buf = BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf

# ===== ИМЯ ФАЙЛА =====
def filename(topic):
    name = re.sub(r'[^\w\s-]', '', topic).strip()[:30]
    name = name.rsplit(' ', 1)[0] if ' ' in name else name
    name = name.replace(' ', '_') or "presentation"
    return f"{name}.pptx"

# ===== ОТПРАВКА =====
async def send_file(msg, data, name, caption):
    for t in range(3):
        try:
            return await msg.answer_document(
                BufferedInputFile(data, name), caption=caption, parse_mode="Markdown"
            )
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except: await asyncio.sleep(2)

# ===== КОМАНДЫ =====
@dp.message(Command("start"))
async def start(msg: Message, state: FSMContext):
    await state.clear()
    admin = "🆓 Бесплатный доступ!\n" if msg.from_user.id == ADMIN_ID else ""
    await msg.answer(f"🎓 Привет! Я делаю презентации с ИИ.\n💰 Цена: {PRICE}₽\n{admin}\n👇 Кнопки внизу!",
                     reply_markup=menu())

@dp.message(F.text == "ℹ️ Помощь")
async def help_cmd(msg: Message):
    await msg.answer("📌 *Как работать:*\n\n1. Нажми «Создать»\n2. Напиши тему и число (от 4 до 12)\n3. Оплати 100₽\n4. Получи файл\n\nПример: `Нейросети 6`",
                     parse_mode="Markdown")

@dp.message(F.text == "💰 Цена")
async def price_cmd(msg: Message):
    await msg.answer(f"💎 {PRICE}₽ за презентацию\n\n✅ Текст ИИ\n✅ Картинки\n✅ Слайды 5-12\n✅ Литература",
                     parse_mode="Markdown")

@dp.message(F.text == "🎨 Создать презентацию")
async def start_create(msg: Message, state: FSMContext):
    await state.clear()
    await state.set_state(State.topic)
    await msg.answer("✏️ Напиши тему и количество:\n\nПримеры:\n`Нейросети 6`\n`История 5`\n`Квантовая физика 4`",
                     parse_mode="Markdown")

@dp.message(StateFilter(State.topic))
async def got_topic(msg: Message, state: FSMContext):
    parts = msg.text.strip().split()
    if len(parts) < 2:
        return await msg.answer("❌ Надо: Тема Число. Например: `История 6`")
    
    try:
        n = int(parts[-1])
        topic = " ".join(parts[:-1])
    except:
        return await msg.answer("❌ Последнее слово - число. Например: `История 6`")
    
    if n < 4 or n > 12:
        return await msg.answer("❌ От 4 до 12 слайдов")
    
    await state.update_data(topic=topic, num=n)
    
    if msg.from_user.id == ADMIN_ID:
        # Бесплатно для админа
        await state.clear()
        msg2 = await msg.answer(f"🔄 Создаю «{topic}», {n} слайдов...")
        try:
            data = await asyncio.wait_for(get_content(topic, n), timeout=120)
            if not data:
                return await msg2.edit_text("❌ GigaChat не ответил")
            await msg2.edit_text("🎨 Рисую картинки...")
            pptx = await asyncio.wait_for(make_pptx(data), timeout=120)
            if not pptx:
                return await msg2.edit_text("❌ Ошибка сборки")
            await send_file(msg, pptx.getvalue(), filename(topic),
                          f"✅ Готово!\n📌 {topic}\n📊 {n} слайдов")
            await msg2.delete()
        except asyncio.TimeoutError:
            await msg2.edit_text("⏰ Слишком долго. Попробуй ещё раз.")
        except Exception as e:
            log.error(f"Ошибка: {e}")
            await msg2.edit_text("❌ Ошибка. /start")
    else:
        # Платно
        try:
            payment = Payment.create({
                "amount": {"value": f"{PRICE}.00", "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "return_url": f"https://t.me/{(await bot.get_me()).username}"
                },
                "description": f"Презентация «{topic[:50]}», {n} слайдов",
                "metadata": {"uid": msg.from_user.id, "topic": topic, "n": n},
                "capture": True
            })
            await state.update_data(pid=payment.id)
            await state.set_state(State.payment)
            await msg.answer(f"💎 Заказ:\n📌 {topic}\n📊 {n} слайдов\n💰 {PRICE}₽\n\n👇 Оплати:",
                           reply_markup=pay_kb(payment.confirmation.confirmation_url))
        except Exception as e:
            log.error(f"Платёж: {e}")
            await msg.answer("❌ Ошибка. Попробуй позже.")
            await state.clear()

@dp.callback_query(F.data == "paid")
async def check_pay(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    pid = d.get("pid")
    if not pid:
        await cb.answer("Платёж не найден")
        return
    
    try:
        p = Payment.find_one(pid)
    except:
        await cb.answer("Ошибка проверки")
        return
    
    if p.status == "succeeded":
        topic = d.get("topic") or p.metadata.get("topic")
        n = d.get("num") or p.metadata.get("n")
        
        await cb.message.edit_text(f"✅ Оплачено! Создаю «{topic}»...")
        try:
            data = await asyncio.wait_for(get_content(topic, n), timeout=120)
            if not data:
                return await cb.message.edit_text("❌ Ошибка. Деньги вернутся.")
            await cb.message.edit_text("🎨 Собираю слайды...")
            pptx = await asyncio.wait_for(make_pptx(data), timeout=120)
            if not pptx:
                return await cb.message.edit_text("❌ Ошибка сборки.")
            await send_file(cb.message, pptx.getvalue(), filename(topic),
                          f"✅ Готово!\n📌 {topic}\n📊 {n} слайдов\n💰 {PRICE}₽ оплачено")
            await cb.message.delete()
        except asyncio.TimeoutError:
            await cb.message.edit_text("⏰ Слишком долго. Напиши в поддержку.")
        except Exception as e:
            log.error(f"Ошибка: {e}")
            await cb.message.edit_text("❌ Ошибка. Поддержка: @ultimatepreza")
        await state.clear()
    elif p.status == "pending":
        await cb.answer("⏳ Жди 30 секунд и нажми ещё раз", show_alert=True)
    else:
        await cb.answer(f"Статус: {p.status}. /start для нового заказа", show_alert=True)
        await state.clear()

@dp.callback_query(F.data == "cancel")
async def cancel_pay(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Отменено. /start для нового заказа")

@dp.message()
async def other(msg: Message):
    await msg.answer("🤔 Используй кнопки или /start", reply_markup=menu())

# ===== ЗАПУСК =====
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())