import asyncio, os, re, json, uuid, base64, time, logging, sys, random
from io import BytesIO
from datetime import datetime
from typing import Optional, Dict, List
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
UNSPLASH_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PRICE = 100

if not all([BOT_TOKEN, GIGA_AUTH, YOOKASSA_ID, YOOKASSA_KEY]):
    raise SystemExit("❌ Нет обязательных ключей!")

Configuration.account_id = YOOKASSA_ID
Configuration.secret_key = YOOKASSA_KEY

# ===== РАЗМЕРЫ СЛАЙДА =====
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

# ===== ПАРСИНГ JSON =====
def extract_json(text):
    if not text:
        return None
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return None
    json_str = text[start:end+1]
    in_string = False
    cleaned = []
    for c in json_str:
        if c == '"' and (not cleaned or cleaned[-1] != '\\'):
            in_string = not in_string
        if c == '\n' and in_string:
            cleaned.append('\\n')
        elif c == '\r' and in_string:
            continue
        elif c == '\t' and in_string:
            cleaned.append(' ')
        else:
            cleaned.append(c)
    json_str = ''.join(cleaned)
    json_str = re.sub(r',\s*}', '}', json_str)
    json_str = re.sub(r',\s*]', ']', json_str)
    try:
        return json.loads(json_str)
    except:
        return None

# ===== GIGACHAT =====
async def ask_ai(text, temp=0.75):
    token = await get_token()
    if not token:
        return None
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "model": "GigaChat",
                        "messages": [
                            {"role": "system", "content": "You are a professor. You respond ONLY with valid JSON. No comments."},
                            {"role": "user", "content": text}
                        ],
                        "temperature": temp, "max_tokens": 3500
                    }, ssl=False, timeout=90
                ) as r:
                    if r.status == 200:
                        return (await r.json())["choices"][0]["message"]["content"]
                    if r.status == 429:
                        await asyncio.sleep(2 ** attempt)
        except:
            await asyncio.sleep(1)
    return None

# ===== НОВЫЙ ПРОМПТ (ДВА ШАГА) =====
async def get_content(topic, n):
    # Шаг 1: Получаем структуру презентации
    prompt1 = f"""Create an educational presentation about "{topic}". Exactly {n} slides.

Slide 1: Title slide
Slides 2-{n-1}: Content slides. For EACH slide provide:
  - "title": Engaging title (5-9 words)
  - "text": 3-4 sentences with FACTS, numbers, examples
  - "image_topic": WHAT THIS SLIDE IS ABOUT (1 sentence in English describing the visual subject)
    Example: "Russian Tsar Ivan the Terrible portrait 16th century"
    Example: "ancient monastery Trinity Lavra Sergiyev Posad architecture"
    Example: "DNA double helix structure molecular biology"

Slide {n}: References (5 real books/articles)

Return ONLY JSON:
{{"title":"...","slides":[
  {{"type":"title","text":"Moscow, 2026"}},
  {{"type":"content","title":"...","text":"...","image_topic":"english description of visual subject"}},
  {{"type":"references","text":"1. ...\\n2. ..."}}
]}}"""

    resp1 = await ask_ai(prompt1, temp=0.7)
    if not resp1:
        return None
    
    data = extract_json(resp1)
    if not data:
        resp1 = await ask_ai(prompt1, temp=0.5)
        if resp1:
            data = extract_json(resp1)
    
    if not data or "slides" not in data:
        return None

    # Шаг 2: Для каждого слайда генерируем КОНКРЕТНЫЙ поисковый запрос
    slides = data["slides"]
    for i, s in enumerate(slides):
        if s.get("type") == "content":
            image_topic = s.get("image_topic", "")
            if image_topic:
                search_prompt = f"""Convert this description into a SHORT image search query (2-5 words, English only).
Description: "{image_topic}"

Rules:
- Use ONLY concrete nouns: people, buildings, objects, animals, nature
- Add type words: portrait, painting, photo, monument, cathedral, manuscript
- Example: "Tsar Ivan portrait painting"
- Example: "Trinity Lavra monastery Russia"
- Example: "DNA helix structure"

Return ONLY the search query, nothing else."""

                search_query = await ask_ai(search_prompt, temp=0.3)
                if search_query:
                    # Очищаем ответ от кавычек и лишнего
                    sq = search_query.strip().strip('"').strip("'")
                    # Ограничиваем длину
                    sq = ' '.join(sq.split()[:6])
                    s["search_query"] = sq
                    log.info(f"Слайд {i}: '{s['title']}' → '{sq}'")
                else:
                    # Fallback — используем image_topic
                    s["search_query"] = ' '.join(image_topic.split()[:5])

    return data

# ===== ПОИСК КАРТИНОК =====

async def _download(url: str) -> Optional[BytesIO]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                if r.status == 200:
                    data = await r.read()
                    if len(data) > 2000:
                        return BytesIO(data)
    except:
        pass
    return None

async def search_unsplash(query: str) -> Optional[BytesIO]:
    if not UNSPLASH_KEY or not query:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.unsplash.com/search/photos",
                params={"query": query, "per_page": 10, "orientation": "landscape", "client_id": UNSPLASH_KEY},
                timeout=15
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    results = data.get("results", [])
                    if results:
                        photo = random.choice(results)
                        url = photo["urls"].get("regular") or photo["urls"].get("small")
                        if url:
                            img = await _download(url)
                            if img:
                                log.info(f"✅ Unsplash: {query[:50]}")
                                return img
    except:
        pass
    return None

async def search_pixabay(query: str) -> Optional[BytesIO]:
    if not PIXABAY_KEY or not query:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            for img_type in ["photo", "illustration"]:
                async with s.get(
                    "https://pixabay.com/api/",
                    params={"key": PIXABAY_KEY, "q": query, "per_page": 10,
                            "orientation": "horizontal", "min_width": 1024,
                            "safesearch": "true", "image_type": img_type},
                    timeout=15
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        hits = data.get("hits", [])
                        if hits:
                            url = random.choice(hits)["largeImageURL"]
                            img = await _download(url)
                            if img:
                                log.info(f"✅ Pixabay: {query[:50]}")
                                return img
    except:
        pass
    return None

async def get_image(query: str) -> Optional[BytesIO]:
    if not query:
        return None
    
    # 1. Unsplash
    img = await search_unsplash(query)
    if img: return img
    
    # 2. Pixabay
    img = await search_pixabay(query)
    if img: return img
    
    # 3. Pollinations
    safe = re.sub(r'[^a-zA-Z0-9\s]', '', query).strip()
    if safe:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"https://image.pollinations.ai/prompt/{safe.replace(' ', '%20')}",
                    params={"width": 1024, "height": 768, "nologo": "true", "seed": str(random.randint(1, 99999))},
                    timeout=25
                ) as r:
                    if r.status == 200:
                        data = await r.read()
                        if len(data) > 2000:
                            log.info(f"✅ Pollinations: {safe[:50]}")
                            return BytesIO(data)
        except:
            pass
    
    log.warning(f"❌ Нет фото: {query[:60]}")
    return None

# ===== PPTX =====
async def make_pptx(data):
    slides = data.get("slides", [])
    if not slides:
        return None

    prs = Presentation()
    prs.slide_width = SW
    prs.slide_height = SH

    tasks = []
    for s in slides:
        if s.get("type") == "content":
            tasks.append(get_image(s.get("search_query", "")))
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
                
                txBox = sl.shapes.add_textbox(txt_left, txt_top, TW, Emu(4500000))
                tf = txBox.text_frame
                tf.word_wrap = True
                tf.text = text
                for p in tf.paragraphs:
                    p.font.size = Pt(15)
                    p.space_after = Pt(8)

                img_top = Emu(1800000)
                img = images[i]
                
                if isinstance(img, BytesIO):
                    try:
                        sl.shapes.add_picture(img, img_left, img_top, width=IW)
                    except:
                        pass

        except Exception as e:
            log.error(f"Слайд {i}: {e}")

    buf = BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf

# ===== ИМЯ ФАЙЛА =====
def filename(topic):
    name = re.sub(r'[^\w\s-]', '', topic).strip()
    if len(name) > 30:
        name = name[:30].rsplit(' ', 1)[0]
    return f"{name.replace(' ', '_') or 'presentation'}.pptx"

async def send_file(msg, data, name, caption):
    for t in range(3):
        try:
            return await msg.answer_document(
                BufferedInputFile(data, name), caption=caption, parse_mode="Markdown"
            )
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except:
            await asyncio.sleep(2)

# ========== ОБРАБОТЧИКИ (без изменений) ==========

@dp.message(Command("start"))
async def start(msg: Message, state: FSMContext):
    await state.clear()
    admin = "🆓 Бесплатный доступ!\n" if msg.from_user.id == ADMIN_ID else ""
    await msg.answer(f"🎓 *PrezaBot — презентации с ИИ!*\n\n✨ Текст: GigaChat\n🖼️ Фото: Unsplash + Pixabay\n📊 PowerPoint\n\n💰 Цена: {PRICE}₽\n{admin}\n👇 Кнопка:", parse_mode="Markdown", reply_markup=menu())

@dp.message(F.text == "ℹ️ Помощь")
async def help_cmd(msg: Message):
    await msg.answer("📌 *Как:*\n1. «Создать»\n2. Тема Число (4-12)\n3. Оплатить\n4. Файл!\n\nПример: `Нейросети 8`", parse_mode="Markdown")

@dp.message(F.text == "💰 Цена")
async def price_cmd(msg: Message):
    await msg.answer(f"💎 *{PRICE}₽*\n✅ Текст GigaChat\n✅ Фото Unsplash/Pixabay\n✅ Слайды 5-12\n✅ Литература", parse_mode="Markdown")

@dp.message(F.text == "🎨 Создать презентацию")
async def start_create(msg: Message, state: FSMContext):
    await state.clear()
    await state.set_state(State.topic)
    await msg.answer("✏️ *Тема и количество:*\n\n`Нейросети 8`\n`История России 16 век 7`\n`Биология 6`", parse_mode="Markdown")

@dp.message(StateFilter(State.topic))
async def got_topic(msg: Message, state: FSMContext):
    text = msg.text.strip()
    if text.startswith('/'):
        await state.clear()
        return await start(msg, state)
    parts = text.split()
    if len(parts) < 2:
        return await msg.answer("❌ Формат: Тема Число")
    try:
        n = int(parts[-1])
        topic = " ".join(parts[:-1])
    except ValueError:
        return await msg.answer("❌ Число в конце!")
    if n < 4 or n > 12:
        return await msg.answer("❌ 4-12 слайдов")
    
    await state.update_data(topic=topic, num=n)
    
    if msg.from_user.id == ADMIN_ID:
        await state.clear()
        status = await msg.answer(f"🔄 «{topic}», {n} слайдов...")
        try:
            data = await asyncio.wait_for(get_content(topic, n), timeout=150)
            if not data:
                return await status.edit_text("❌ GigaChat не ответил")
            await status.edit_text("🖼️ Подбираю фото...")
            pptx = await asyncio.wait_for(make_pptx(data), timeout=180)
            if not pptx:
                return await status.edit_text("❌ Ошибка сборки")
            await send_file(msg, pptx.getvalue(), filename(topic), f"✅ *Готово!*\n📌 {topic}\n📊 {n} слайдов")
            await status.delete()
        except asyncio.TimeoutError:
            await status.edit_text("⏰ Долго.")
        except Exception as e:
            log.error(f"{e}")
            await status.edit_text("❌ Ошибка")
    else:
        try:
            payment = Payment.create({
                "amount": {"value": f"{PRICE}.00", "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": f"https://t.me/{(await bot.get_me()).username}"},
                "description": f"Презентация «{topic[:50]}», {n} слайдов",
                "metadata": {"uid": msg.from_user.id, "topic": topic, "n": n},
                "capture": True,
                "receipt": {
                    "customer": {"email": f"{msg.from_user.id}@t.me"},
                    "items": [{"description": f"Презентация «{topic[:30]}»", "quantity": "1", "amount": {"value": f"{PRICE}.00", "currency": "RUB"}, "vat_code": "1", "payment_mode": "full_prepayment", "payment_subject": "service"}]
                }
            })
            await state.update_data(pid=payment.id)
            await state.set_state(State.payment)
            await msg.answer(f"💎 *Заказ*\n📌 {topic}\n📊 {n} слайдов\n💰 *{PRICE}₽*\n👇 Оплатить:", parse_mode="Markdown", reply_markup=pay_kb(payment.confirmation.confirmation_url))
        except Exception as e:
            log.error(f"Платёж: {e}")
            await msg.answer("❌ Ошибка")
            await state.clear()

@dp.callback_query(F.data == "paid")
async def check_pay(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    pid = d.get("pid")
    if not pid: return await cb.answer("❌ Нет платежа")
    try:
        p = Payment.find_one(pid)
    except:
        return await cb.answer("❌ Ошибка")
    if p.status == "succeeded":
        topic = d.get("topic") or (p.metadata or {}).get("topic")
        n = d.get("num") or (p.metadata or {}).get("n")
        await cb.message.edit_text(f"✅ «{topic}»...")
        try:
            data = await asyncio.wait_for(get_content(topic, n), timeout=150)
            if not data: return await cb.message.edit_text("❌ Ошибка генерации")
            await cb.message.edit_text("🖼️ Фото...")
            pptx = await asyncio.wait_for(make_pptx(data), timeout=180)
            if not pptx: return await cb.message.edit_text("❌ Ошибка сборки")
            await send_file(cb.message, pptx.getvalue(), filename(topic), f"✅ *Готово!*\n📌 {topic}\n📊 {n} слайдов\n💰 {PRICE}₽")
            await cb.message.delete()
        except asyncio.TimeoutError:
            await cb.message.edit_text("⏰ Долго")
        except Exception as e:
            log.error(f"{e}")
            await cb.message.edit_text("❌ Ошибка")
        await state.clear()
    elif p.status == "pending":
        await cb.answer("⏳ Жди", show_alert=True)
    else:
        await cb.answer(f"❌ {p.status}", show_alert=True)
        await state.clear()

@dp.callback_query(F.data == "cancel")
async def cancel_pay(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Отменено. /start")

@dp.message()
async def fallback(msg: Message):
    await msg.answer("🤔 Меню или /start", reply_markup=menu())

async def main():
    log.info("🚀 Запуск")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())