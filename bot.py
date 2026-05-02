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

# ===== ЛОГИ =====
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

# ===== КЛЮЧИ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
GIGA_AUTH = base64.b64encode(
    f"{os.getenv('GIGACHAT_CLIENT_ID')}:{os.getenv('GIGACHAT_SECRET')}".encode()
).decode()
YOOKASSA_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_KEY = os.getenv("YOOKASSA_SECRET_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PRICE = 100

if not all([BOT_TOKEN, GIGA_AUTH, YOOKASSA_ID, YOOKASSA_KEY]):
    raise SystemExit("❌ Не все ключи!")

Configuration.account_id = YOOKASSA_ID
Configuration.secret_key = YOOKASSA_KEY

# ===== РАЗМЕРЫ СЛАЙДА (13.333" x 7.5") =====
SW = Emu(12192000)
SH = Emu(6858000)
MG = Emu(365760)
GP = Emu(274320)
IW = Emu(4937760)
TW = SW - IW - GP - MG*2

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

# ===== КЛАВИАТУРЫ =====
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
async def ask_ai(text, temp=0.75):
    token = await get_token()
    if not token: return None

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "model": "GigaChat",
                        "messages": [
                            {"role": "system", "content": "Ты — профессор. Объясняешь сложные темы на пальцах, с яркими примерами и метафорами."},
                            {"role": "user", "content": text}
                        ],
                        "temperature": temp, "max_tokens": 3500
                    }, ssl=False, timeout=90
                ) as r:
                    if r.status == 200:
                        return (await r.json())["choices"][0]["message"]["content"]
                    if r.status == 429:
                        await asyncio.sleep(2 ** attempt)
        except: await asyncio.sleep(1)
    return None

async def get_content(topic, n):
    prompt = f"""Создай учебную презентацию на тему "{topic}". Ровно {n} слайдов.

СТРУКТУРА:
Слайд 1: Титульный (название темы, "Москва, 2026")
Слайды 2-{n-1}: Содержательные слайды. Для КАЖДОГО напиши:
  - "title": Яркий заголовок (5-9 слов), вызывающий интерес
  - "text": 3-4 развёрнутых предложения с КОНКРЕТНЫМИ ПРИМЕРАМИ ИЗ ЖИЗНИ, цифрами, фактами
  - "image_prompt": Короткое описание картинки НА АНГЛИЙСКОМ ЯЗЫКЕ (3-6 слов).
    ВАЖНО: Каждый image_prompt должен быть УНИКАЛЬНЫМ и соответствовать тексту слайда.
    Примеры:
    "colorful DNA double helix structure"
    "microscope view of human cell"
    "friendly bacteria in human gut"
    "quantum wave function visualization"
    "cartoon characters learning together"

Слайд {n}: Список литературы (5 реальных книг/статей)

ФОРМАТ ОТВЕТА — ТОЛЬКО JSON:
{{
  "title": "Заголовок всей презентации",
  "slides": [
    {{"type": "title", "text": "Москва, 2026"}},
    {{"type": "content", "title": "Заголовок слайда", "text": "Текст с примерами...", "image_prompt": "english description of image"}},
    ...
    {{"type": "references", "text": "1. Книга 1\\n2. Книга 2\\n..."}}
  ]
}}
"""
    resp = await ask_ai(prompt, temp=0.8)
    if not resp: return None

    s = resp.find('{')
    e = resp.rfind('}')
    if s == -1 or e == -1:
        log.error("JSON не найден")
        return None

    try:
        data = json.loads(resp[s:e+1])
    except json.JSONDecodeError:
        log.error("JSON decode error")
        return None

    if "slides" not in data:
        return None
    
    return data

# ===== КАРТИНКИ (ИСПРАВЛЕНО!) =====
async def get_image(prompt):
    """Генерирует картинку через Pollinations AI с повторными попытками."""
    if not prompt:
        return None
    
    # Очищаем промпт
    safe = re.sub(r'[^a-zA-Z0-9\s]', '', prompt).strip()
    if not safe or len(safe) < 3:
        safe = "abstract presentation background"
    
    # Кодируем для URL
    encoded = safe.replace(' ', '%20')
    seed = str(uuid.uuid4().int)[:8]
    
    # Пробуем ДВА разных URL для надёжности
    urls = [
        f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=768&nologo=true&seed={seed}",
        f"https://pollinations.ai/p/{encoded}?width=1024&height=768&nologo=true&seed={seed}"
    ]
    
    for url in urls:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=25) as r:
                    if r.status == 200:
                        content_type = r.headers.get('Content-Type', '')
                        if 'image' in content_type or len(await r.read()) > 1000:
                            return BytesIO(await r.read() if hasattr(r, '_body') else (await r.read()))
                        
                        # Второй вариант чтения
                        data = await r.read()
                        if len(data) > 1000:
                            return BytesIO(data)
        except Exception as e:
            log.warning(f"URL {url[:50]}... не сработал: {e}")
            continue
    
    # Если не сработало — пробуем Unsplash (заглушка)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://source.unsplash.com/1024x768/?{encoded}",
                timeout=15
            ) as r:
                if r.status == 200:
                    return BytesIO(await r.read())
    except:
        pass
    
    log.warning(f"Все сервисы картинок не ответили для: {safe[:50]}")
    return None

# ===== PPTX =====
async def make_pptx(data):
    slides = data.get("slides", [])
    if not slides:
        return None

    prs = Presentation()
    prs.slide_width = SW
    prs.slide_height = SH

    # Готовим картинки параллельно
    tasks = []
    for s in slides:
        if s.get("type") == "content":
            prompt = s.get("image_prompt", s.get("caption", ""))
            tasks.append(get_image(prompt))
        else:
            tasks.append(asyncio.sleep(0))
    images = await asyncio.gather(*tasks, return_exceptions=True)

    for i, s in enumerate(slides):
        stype = s.get("type", "content")
        img_on_left = (i % 2 == 0)

        try:
            if stype == "title":
                sl = prs.slides.add_slide(prs.slide_layouts[0])
                sl.shapes.title.text = data.get("title", "Презентация")
                if len(sl.placeholders) > 1:
                    sl.placeholders[1].text = s.get("text", f"Москва, {datetime.now().year}")
                    sl.placeholders[1].text_frame.paragraphs[0].font.size = Pt(20)

            elif stype == "references":
                sl = prs.slides.add_slide(prs.slide_layouts[1])
                sl.shapes.title.text = "📚 Список литературы"
                if len(sl.placeholders) > 1:
                    sl.placeholders[1].text = s.get("text", "")
                    for p in sl.placeholders[1].text_frame.paragraphs:
                        p.font.size = Pt(14)

            else:
                sl = prs.slides.add_slide(prs.slide_layouts[1])
                sl.shapes.title.text = s.get("title", "Информация")
                sl.shapes.title.text_frame.paragraphs[0].font.size = Pt(28)
                sl.shapes.title.text_frame.paragraphs[0].font.bold = True

                text = s.get("text", "")
                
                if img_on_left:
                    txt_left = MG + IW + GP
                    img_left = MG
                else:
                    txt_left = MG
                    img_left = SW - IW - MG

                txt_top = Emu(1600000)
                txt_height = Emu(4500000)
                
                txBox = sl.shapes.add_textbox(txt_left, txt_top, TW, txt_height)
                tf = txBox.text_frame
                tf.word_wrap = True
                tf.text = text
                
                for p in tf.paragraphs:
                    p.font.size = Pt(15)
                    p.space_after = Pt(8)
                    p.alignment = PP_ALIGN.LEFT

                # Картинка
                img_top = Emu(1800000)
                img = images[i]
                
                if isinstance(img, BytesIO):
                    try:
                        sl.shapes.add_picture(img, img_left, img_top, width=IW)
                        
                        # Подпись
                        caption = s.get("image_prompt", s.get("caption", "Иллюстрация"))
                        cap_top = img_top + Emu(3700000)
                        cap = sl.shapes.add_textbox(img_left, cap_top, IW, Emu(400000))
                        cap.text_frame.text = caption
                        for p in cap.text_frame.paragraphs:
                            p.font.size = Pt(9)
                            p.font.italic = True
                            p.alignment = PP_ALIGN.CENTER
                    except Exception as e:
                        log.error(f"Вставка картинки {i}: {e}")
                        _add_image_placeholder(sl, img_left, img_top, IW)
                else:
                    _add_image_placeholder(sl, img_left, img_top, IW)

        except Exception as e:
            log.error(f"Слайд {i}: {e}")
            continue

    buf = BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf

def _add_image_placeholder(slide, left, top, width):
    """Красивая заглушка если картинка не загрузилась."""
    shape = slide.shapes.add_shape(1, left, top, width, Emu(3600000))
    shape.fill.solid()
    shape.fill.fore_color.rgb = type(shape.fill.fore_color).rgb = (245, 245, 250)
    shape.line.color.rgb = type(shape.line.color).rgb = (180, 180, 200)
    shape.line.width = Pt(1)
    
    tf = shape.text_frame
    tf.text = "🖼️\nИллюстрация\nк слайду"
    for p in tf.paragraphs:
        p.alignment = PP_ALIGN.CENTER
        p.font.size = Pt(13)
        p.font.color.rgb = type(p.font.color).rgb = (140, 140, 160)

# ===== ИМЯ ФАЙЛА =====
def filename(topic):
    name = re.sub(r'[^\w\s-]', '', topic).strip()
    if len(name) > 30:
        name = name[:30].rsplit(' ', 1)[0]
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

# ========== ОБРАБОТЧИКИ ==========

@dp.message(Command("start"))
async def start(msg: Message, state: FSMContext):
    await state.clear()
    admin = "🆓 Бесплатный доступ!\n" if msg.from_user.id == ADMIN_ID else ""
    await msg.answer(
        f"🎓 *Привет! Я создаю презентации с ИИ!*\n\n"
        f"✨ Умный текст\n🎨 Уникальные картинки\n📊 PowerPoint\n\n"
        f"💰 Цена: {PRICE}₽\n{admin}\n👇 Кнопка:",
        parse_mode="Markdown", reply_markup=menu()
    )

@dp.message(F.text == "ℹ️ Помощь")
async def help_cmd(msg: Message):
    await msg.answer(
        "📌 *Как создать:*\n\n1. Нажми «Создать»\n2. Напиши тему и число (4-12)\n"
        "3. Оплати 100₽\n4. Получи файл\n\nПример: `Нейросети 8`",
        parse_mode="Markdown"
    )

@dp.message(F.text == "💰 Цена")
async def price_cmd(msg: Message):
    await msg.answer(
        f"💎 *{PRICE}₽*\n\n✅ Текст GigaChat\n✅ AI-картинки\n✅ 5-12 слайдов\n✅ Литература\n💳 Оплата: карты, СБП",
        parse_mode="Markdown"
    )

@dp.message(F.text == "🎨 Создать презентацию")
async def start_create(msg: Message, state: FSMContext):
    await state.clear()
    await state.set_state(State.topic)
    await msg.answer(
        "✏️ *Тема и количество:*\n\n`Нейросети 8`\n`История 6`\n`Квантовая физика 5`",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(State.topic))
async def got_topic(msg: Message, state: FSMContext):
    text = msg.text.strip()
    if text.startswith('/'):
        await state.clear()
        return await start(msg, state)
    
    parts = text.split()
    if len(parts) < 2:
        return await msg.answer("❌ Нужно: Тема Число\nПример: `Нейросети 6`")
    
    try:
        n = int(parts[-1])
        topic = " ".join(parts[:-1])
    except ValueError:
        return await msg.answer("❌ Последнее слово — число!\nПример: `История 6`")
    
    if n < 4 or n > 12:
        return await msg.answer("❌ От 4 до 12 слайдов")
    
    await state.update_data(topic=topic, num=n)
    
    # АДМИН — БЕСПЛАТНО
    if msg.from_user.id == ADMIN_ID:
        await state.clear()
        status = await msg.answer(f"🔄 Генерирую «{topic}», {n} слайдов...")
        try:
            data = await asyncio.wait_for(get_content(topic, n), timeout=120)
            if not data:
                return await status.edit_text("❌ GigaChat не ответил")
            await status.edit_text("🎨 Создаю слайды...")
            pptx = await asyncio.wait_for(make_pptx(data), timeout=120)
            if not pptx:
                return await status.edit_text("❌ Ошибка сборки")
            await send_file(msg, pptx.getvalue(), filename(topic),
                          f"✅ *Готово!*\n📌 {topic}\n📊 {n} слайдов")
            await status.delete()
        except asyncio.TimeoutError:
            await status.edit_text("⏰ Долго. Упрости тему.")
        except Exception as e:
            log.error(f"Ошибка: {e}")
            await status.edit_text("❌ Ошибка. /start")
    else:
        # ПЛАТНО
        try:
            payment = Payment.create({
                "amount": {"value": f"{PRICE}.00", "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "return_url": f"https://t.me/{(await bot.get_me()).username}"
                },
                "description": f"Презентация «{topic[:50]}», {n} слайдов",
                "metadata": {"uid": msg.from_user.id, "topic": topic, "n": n},
                "capture": True,
                "receipt": {
                    "customer": {"email": f"{msg.from_user.id}@t.me"},
                    "items": [{
                        "description": f"Презентация «{topic[:30]}»",
                        "quantity": "1",
                        "amount": {"value": f"{PRICE}.00", "currency": "RUB"},
                        "vat_code": "1",
                        "payment_mode": "full_prepayment",
                        "payment_subject": "service"
                    }]
                }
            })
            await state.update_data(pid=payment.id)
            await state.set_state(State.payment)
            await msg.answer(
                f"💎 *Заказ*\n📌 {topic}\n📊 {n} слайдов\n💰 *{PRICE}₽*\n\n👇 Оплатить:",
                parse_mode="Markdown", reply_markup=pay_kb(payment.confirmation.confirmation_url)
            )
        except Exception as e:
            log.error(f"Платёж: {e}")
            await msg.answer("❌ Ошибка платежа.")
            await state.clear()

@dp.callback_query(F.data == "paid")
async def check_pay(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    pid = d.get("pid")
    if not pid:
        await cb.answer("❌ Платёж не найден")
        return
    
    try:
        p = Payment.find_one(pid)
    except:
        await cb.answer("❌ Ошибка проверки")
        return
    
    if p.status == "succeeded":
        topic = d.get("topic") or (p.metadata or {}).get("topic")
        n = d.get("num") or (p.metadata or {}).get("n")
        
        await cb.message.edit_text(f"✅ Оплачено! Создаю «{topic}»...")
        try:
            data = await asyncio.wait_for(get_content(topic, n), timeout=120)
            if not data:
                return await cb.message.edit_text("❌ Ошибка. Деньги вернутся.")
            await cb.message.edit_text("🎨 Собираю...")
            pptx = await asyncio.wait_for(make_pptx(data), timeout=120)
            if not pptx:
                return await cb.message.edit_text("❌ Ошибка сборки.")
            await send_file(cb.message, pptx.getvalue(), filename(topic),
                          f"✅ *Готово!*\n📌 {topic}\n📊 {n} слайдов\n💰 {PRICE}₽ оплачено")
            await cb.message.delete()
        except asyncio.TimeoutError:
            await cb.message.edit_text("⏰ Долго. Поддержка: @ultimatepreza")
        except Exception as e:
            log.error(f"Ошибка: {e}")
            await cb.message.edit_text("❌ Ошибка. Поддержка: @ultimatepreza")
        await state.clear()
    elif p.status == "pending":
        await cb.answer("⏳ Жди 30 сек", show_alert=True)
    else:
        await cb.answer(f"❌ Статус: {p.status}", show_alert=True)
        await state.clear()

@dp.callback_query(F.data == "cancel")
async def cancel_pay(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Отменено. /start")

@dp.message()
async def fallback(msg: Message):
    await msg.answer("🤔 Кнопки меню или /start", reply_markup=menu())

# ===== ЗАПУСК =====
async def main():
    log.info("🚀 Запуск...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())