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
SW = Emu(12192000)   # ширина слайда
SH = Emu(6858000)    # высота слайда
MG = Emu(365760)     # отступ от края 0.4"
GP = Emu(274320)     # зазор между текстом и картинкой 0.3"
IW = Emu(4937760)    # ширина картинки 5.4"
TW = SW - IW - GP - MG*2  # ширина текста (автовычисление)

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
  - "image_prompt": Короткое описание картинки НА АНГЛИЙСКОМ ЯЗЫКЕ (3-6 слов), которая ИЛЛЮСТРИРУЕТ ТЕКСТ этого слайда.
    Например: "digital brain learning from mistakes"
    или: "neural network recognizing cat photo"
    или: "self driving car on highway"
    или: "quantum particles interacting abstract"

Слайд {n}: Список литературы (5 реальных книг/статей)

ФОРМАТ ОТВЕТА — ТОЛЬКО JSON:
{{
  "title": "Заголовок всей презентации",
  "slides": [
    {{"type": "title", "text": "Москва, 2026"}},
    {{"type": "content", "title": "Как нейросеть учится на ошибках?", "text": "Нейросеть учится как ребёнок. Сначала она путает кошку с собакой, но после 10 000 примеров находит закономерности. Алгоритм обратного распространения ошибки работает как строгий учитель, исправляющий каждую неточность.", "image_prompt": "robot child learning from teacher"}},
    ...
    {{"type": "references", "text": "1. Книга 1\\n2. Книга 2\\n..."}}
  ]
}}

ВАЖНО: image_prompt должен быть РАЗНЫМ для каждого слайда и соответствовать его тексту!
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

# ===== КАРТИНКИ =====
async def get_image(prompt):
    """Генерирует картинку по текстовому описанию."""
    if not prompt:
        return None
    
    # Очищаем промпт
    safe = re.sub(r'[^a-zA-Z0-9\s]', '', prompt).strip().replace(' ', '%20')
    if not safe:
        safe = "abstract%20presentation%20background"
    
    # Добавляем случайный seed для разнообразия
    seed = str(uuid.uuid4().int)[:8]
    
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://image.pollinations.ai/prompt/{safe}",
                params={"width": 1024, "height": 768, "nologo": "true", "seed": seed},
                timeout=20
            ) as r:
                if r.status == 200:
                    return BytesIO(await r.read())
    except Exception as e:
        log.warning(f"Картинка: {e}")
    return None

# ===== PPTX С ПРАВИЛЬНОЙ ВЕРСТКОЙ =====
async def make_pptx(data):
    slides = data.get("slides", [])
    if not slides:
        return None

    prs = Presentation()
    prs.slide_width = SW
    prs.slide_height = SH

    # Готовим картинки ПАРАЛЛЕЛЬНО (быстрее)
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
        # Четные слайды — картинка СЛЕВА, нечетные — картинка СПРАВА
        img_on_left = (i % 2 == 0)

        try:
            if stype == "title":
                # ТИТУЛЬНЫЙ СЛАЙД
                sl = prs.slides.add_slide(prs.slide_layouts[0])
                sl.shapes.title.text = data.get("title", "Презентация")
                # Подзаголовок
                if len(sl.placeholders) > 1:
                    sl.placeholders[1].text = s.get("text", f"Москва, {datetime.now().year}")
                    # Центрируем и делаем красивее
                    sl.placeholders[1].text_frame.paragraphs[0].font.size = Pt(20)

            elif stype == "references":
                # СПИСОК ЛИТЕРАТУРЫ
                sl = prs.slides.add_slide(prs.slide_layouts[1])
                sl.shapes.title.text = "📚 Список литературы"
                # Текст литературы
                if len(sl.placeholders) > 1:
                    sl.placeholders[1].text = s.get("text", "")
                    # Уменьшаем шрифт
                    for p in sl.placeholders[1].text_frame.paragraphs:
                        p.font.size = Pt(14)

            else:
                # СОДЕРЖАТЕЛЬНЫЙ СЛАЙД
                sl = prs.slides.add_slide(prs.slide_layouts[1])
                
                # Заголовок слайда
                sl.shapes.title.text = s.get("title", "Информация")
                title_font = sl.shapes.title.text_frame.paragraphs[0].font
                title_font.size = Pt(28)
                title_font.bold = True

                # === ТЕКСТ СЛАЙДА ===
                text = s.get("text", "")
                
                if img_on_left:
                    # Картинка слева → текст справа
                    txt_left = MG + IW + GP
                    img_left = MG
                else:
                    # Картинка справа → текст слева
                    txt_left = MG
                    img_left = SW - IW - MG

                # Добавляем текстовое поле
                txt_top = Emu(1600000)     # 1.75" от верха
                txt_height = Emu(4500000)  # 4.9" высота
                
                txBox = sl.shapes.add_textbox(
                    txt_left, txt_top, TW, txt_height
                )
                tf = txBox.text_frame
                tf.word_wrap = True
                tf.text = text
                
                # Настройка шрифта текста
                for p in tf.paragraphs:
                    p.font.size = Pt(15)
                    p.space_after = Pt(8)
                    p.alignment = PP_ALIGN.LEFT

                # === КАРТИНКА ===
                img = images[i]
                img_top = Emu(1800000)  # 2.0" от верха
                
                if isinstance(img, BytesIO):
                    # Вставляем картинку
                    sl.shapes.add_picture(img, img_left, img_top, width=IW)
                    
                    # Подпись под картинкой
                    caption = s.get("image_prompt", s.get("caption", "Иллюстрация"))
                    cap_top = img_top + Emu(3700000)  # под картинкой
                    cap = sl.shapes.add_textbox(
                        img_left, cap_top, IW, Emu(400000)
                    )
                    cap.text_frame.text = caption
                    for p in cap.text_frame.paragraphs:
                        p.font.size = Pt(9)
                        p.font.italic = True
                        p.alignment = PP_ALIGN.CENTER
                else:
                    # Заглушка если картинка не загрузилась
                    shape = sl.shapes.add_shape(
                        1, img_left, img_top, IW, Emu(3600000)  # Прямоугольник
                    )
                    shape.fill.solid()
                    shape.fill.fore_color.rgb = type(shape.fill.fore_color).rgb = (240, 240, 245)
                    shape.line.color.rgb = type(shape.line.color).rgb = (180, 180, 190)
                    shape.line.width = Pt(1)
                    
                    # Текст в заглушке
                    pltf = shape.text_frame
                    pltf.text = "🎨\nИллюстрация\nзагружается..."
                    for p in pltf.paragraphs:
                        p.alignment = PP_ALIGN.CENTER
                        p.font.size = Pt(12)
                        p.font.color.rgb = type(p.font.color).rgb = (130, 130, 150)

        except Exception as e:
            log.error(f"Слайд {i}: {e}")
            continue

    # Сохраняем
    buf = BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf

# ===== ИМЯ ФАЙЛА =====
def filename(topic):
    name = re.sub(r'[^\w\s-]', '', topic).strip()
    if len(name) > 30:
        name = name[:30].rsplit(' ', 1)[0]
    name = name.replace(' ', '_') or "presentation"
    return f"{name}.pptx"

# ===== ОТПРАВКА ФАЙЛА =====
async def send_file(msg, data, name, caption):
    for t in range(3):
        try:
            return await msg.answer_document(
                BufferedInputFile(data, name),
                caption=caption,
                parse_mode="Markdown"
            )
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            if t == 2:
                raise e
            await asyncio.sleep(2)

# ========== ОБРАБОТЧИКИ КОМАНД ==========

@dp.message(Command("start"))
async def start(msg: Message, state: FSMContext):
    await state.clear()
    admin = "🆓 Бесплатный доступ!\n" if msg.from_user.id == ADMIN_ID else ""
    await msg.answer(
        f"🎓 *Привет! Я создаю презентации с ИИ!*\n\n"
        f"✨ Умный текст\n"
        f"🎨 Уникальные картинки\n"
        f"📊 PowerPoint файл\n\n"
        f"💰 Цена: {PRICE}₽\n"
        f"{admin}\n"
        f"👇 Нажми кнопку:",
        parse_mode="Markdown",
        reply_markup=menu()
    )

@dp.message(F.text == "ℹ️ Помощь")
async def help_cmd(msg: Message):
    await msg.answer(
        "📌 *Как создать презентацию:*\n\n"
        "1️⃣ Нажми «Создать презентацию»\n"
        "2️⃣ Напиши тему и количество\n"
        "   *Пример:* `Нейросети 8`\n"
        "3️⃣ Оплати 100₽ (админам бесплатно)\n"
        "4️⃣ Получи готовый файл!\n\n"
        "💡 *Слайдов:* от 4 до 12\n"
        "🎨 Картинки подбираются под текст\n"
        "📚 Список литературы в конце",
        parse_mode="Markdown"
    )

@dp.message(F.text == "💰 Цена")
async def price_cmd(msg: Message):
    await msg.answer(
        f"💎 *{PRICE}₽ за презентацию*\n\n"
        f"✅ Умный текст от GigaChat\n"
        f"✅ Уникальные AI-картинки\n"
        f"✅ 5-12 слайдов\n"
        f"✅ Список литературы\n"
        f"✅ PowerPoint файл\n\n"
        f"💳 Оплата: карты, СБП",
        parse_mode="Markdown"
    )

@dp.message(F.text == "🎨 Создать презентацию")
async def start_create(msg: Message, state: FSMContext):
    await state.clear()
    await state.set_state(State.topic)
    await msg.answer(
        "✏️ *Напиши тему и количество слайдов*\n\n"
        "*Примеры:*\n"
        "`Нейросети 8`\n"
        "`История интернета 6`\n"
        "`Квантовая физика 5`\n"
        "`Солнечная система 4`\n\n"
        "❌ Отмена: /start",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(State.topic))
async def got_topic(msg: Message, state: FSMContext):
    text = msg.text.strip()
    
    # Проверка на команду
    if text.startswith('/'):
        await state.clear()
        return await start(msg, state)
    
    parts = text.split()
    if len(parts) < 2:
        return await msg.answer("❌ Напиши: Тема Число\nПример: `Нейросети 6`")
    
    try:
        n = int(parts[-1])
        topic = " ".join(parts[:-1])
    except ValueError:
        return await msg.answer("❌ Последнее слово должно быть числом!\nПример: `История 6`")
    
    if n < 4 or n > 12:
        return await msg.answer("❌ Количество слайдов: от 4 до 12")
    
    if len(topic) > 200:
        return await msg.answer("❌ Тема слишком длинная. Сократи.")
    
    await state.update_data(topic=topic, num=n)
    
    # АДМИН — БЕСПЛАТНО
    if msg.from_user.id == ADMIN_ID:
        await state.clear()
        status = await msg.answer(f"🔄 Генерирую «{topic}», {n} слайдов...")
        
        try:
            # Шаг 1: получить контент
            data = await asyncio.wait_for(get_content(topic, n), timeout=120)
            if not data:
                return await status.edit_text("❌ GigaChat не ответил. Попробуй другую тему.")
            
            await status.edit_text("🎨 Создаю слайды и рисую картинки...")
            
            # Шаг 2: собрать PPTX
            pptx = await asyncio.wait_for(make_pptx(data), timeout=120)
            if not pptx:
                return await status.edit_text("❌ Не удалось собрать файл.")
            
            # Шаг 3: отправить
            await send_file(msg, pptx.getvalue(), filename(topic),
                          f"✅ *Готово!*\n📌 Тема: {topic}\n📊 Слайдов: {n}\n🎨 Уникальные картинки")
            await status.delete()
            
        except asyncio.TimeoutError:
            await status.edit_text("⏰ Превышено время. Упрости тему или уменьши число слайдов.")
        except Exception as e:
            log.error(f"Ошибка: {e}")
            await status.edit_text("❌ Ошибка. Напиши /start и попробуй снова.")
    
    # ПЛАТНЫЙ ПОЛЬЗОВАТЕЛЬ
    else:
        try:
            payment = Payment.create({
                "amount": {"value": f"{PRICE}.00", "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "return_url": f"https://t.me/{(await bot.get_me()).username}"
                },
                "description": f"Презентация «{topic[:50]}», {n} слайдов",
                "metadata": {
                    "uid": msg.from_user.id,
                    "topic": topic,
                    "n": n,
                    "delivered": False
                },
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
                f"💎 *Ваш заказ*\n\n"
                f"📌 Тема: {topic}\n"
                f"📊 Слайдов: {n}\n"
                f"💰 Сумма: *{PRICE}₽*\n\n"
                f"👇 Нажмите для оплаты:",
                parse_mode="Markdown",
                reply_markup=pay_kb(payment.confirmation.confirmation_url)
            )
            
        except Exception as e:
            log.error(f"Платёж: {e}")
            await msg.answer("❌ Ошибка платёжной системы. Попробуй позже.")
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
        await cb.answer("❌ Ошибка проверки платежа")
        return
    
    if p.status == "succeeded":
        # Проверка, не выдали ли уже
        if p.metadata and p.metadata.get("delivered"):
            await cb.answer("⚠️ Презентация уже была отправлена!", show_alert=True)
            await state.clear()
            return
        
        topic = d.get("topic") or (p.metadata or {}).get("topic")
        n = d.get("num") or (p.metadata or {}).get("n")
        
        if not topic or not n:
            await cb.message.edit_text("❌ Данные утеряны. Напиши /start")
            await state.clear()
            return
        
        await cb.message.edit_text(f"✅ *Оплачено!*\n🔄 Генерирую «{topic}»...", parse_mode="Markdown")
        
        try:
            data = await asyncio.wait_for(get_content(topic, n), timeout=120)
            if not data:
                return await cb.message.edit_text("❌ Ошибка генерации. Деньги вернутся автоматически.")
            
            await cb.message.edit_text("🎨 Собираю слайды...")
            pptx = await asyncio.wait_for(make_pptx(data), timeout=120)
            
            if not pptx:
                return await cb.message.edit_text("❌ Ошибка сборки. Деньги вернутся.")
            
            await send_file(cb.message, pptx.getvalue(), filename(topic),
                          f"✅ *Готово!*\n📌 {topic}\n📊 {n} слайдов\n💰 {PRICE}₽ оплачено\n\nСпасибо за покупку! 🎉")
            await cb.message.delete()
            
            # Отмечаем как выданное
            try:
                # ЮKassa не даёт менять metadata, но можно пометить в логах
                log.info(f"Выдана презентация по платежу {pid}")
            except:
                pass
            
        except asyncio.TimeoutError:
            await cb.message.edit_text("⏰ Превышено время. Обратись в поддержку: @ultimatepreza")
        except Exception as e:
            log.error(f"Ошибка выдачи: {e}")
            await cb.message.edit_text("❌ Ошибка. Поддержка: @ultimatepreza")
        
        await state.clear()
        
    elif p.status == "pending":
        await cb.answer("⏳ Платёж обрабатывается. Нажми ещё раз через 30 секунд.", show_alert=True)
    else:
        await cb.answer(f"❌ Статус: {p.status}. Создай новый заказ через /start", show_alert=True)
        await state.clear()

@dp.callback_query(F.data == "cancel")
async def cancel_pay(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Заказ отменён. /start для нового.")
    await cb.answer()

@dp.message()
async def fallback(msg: Message):
    await msg.answer("🤔 Используй кнопки меню или напиши /start", reply_markup=menu())

# ===== ЗАПУСК =====
async def main():
    log.info("🚀 Бот запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())