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
from pptx.oxml.ns import qn
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
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PRICE = 100

if not all([BOT_TOKEN, GIGA_AUTH, YOOKASSA_ID, YOOKASSA_KEY]):
    raise SystemExit("❌ Не все ключи!")

Configuration.account_id = YOOKASSA_ID
Configuration.secret_key = YOOKASSA_KEY

# ===== КОНСТАНТЫ СЛАЙДА =====
SW = Emu(12192000)
SH = Emu(6858000)
MG = Emu(365760)
GP = Emu(274320)
IW = Emu(4937760)
TW = SW - IW - GP - MG*2

# ===== ЦВЕТОВЫЕ ТЕМЫ ДЛЯ ФОНА =====
THEMES = [
    {"bg": "F5F0E8", "accent": "3D5A80", "name": "Теплый пергамент"},
    {"bg": "E8F4F8", "accent": "2C3E50", "name": "Небесный"},
    {"bg": "F0F4E8", "accent": "4A6741", "name": "Зеленый чай"},
    {"bg": "F8F0F5", "accent": "6B3A5B", "name": "Розовый туман"},
    {"bg": "F0F0F5", "accent": "2B3A67", "name": "Сумеречный"},
    {"bg": "FFF8F0", "accent": "C0392B", "name": "Теплый рассвет"},
    {"bg": "F5F5F0", "accent": "1A5276", "name": "Морской бриз"},
    {"bg": "FDFEFE", "accent": "34495E", "name": "Облачный"},
]

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
            cleaned.append(c)
        elif c == '\n' and in_string:
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
    except json.JSONDecodeError as e:
        log.error(f"JSON error: {e}")
        log.error(f"Near: ...{json_str[max(0,e.pos-30):e.pos+30]}...")
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
                            {"role": "system", "content": "Ты — профессор-методист. Отвечаешь ТОЛЬКО валидным JSON. Строго соблюдаешь формат."},
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

# ===== УЛУЧШЕННЫЙ ПРОМПТ ДЛЯ КОНТЕНТА =====
async def get_content(topic, n):
    prompt = f"""Ты — эксперт по теме "{topic}". Создай учебную презентацию из РОВНО {n} слайдов.

СТРУКТУРА:
Слайд 1: Титульный ("{topic}", "Москва, 2026")
Слайды 2-{n-1}: Содержательные. Для каждого напиши:
  - "title": точный исторический/научный заголовок (5-9 слов), строго по теме
  - "text": 3-4 предложения с КОНКРЕТНЫМИ фактами, датами, именами, цифрами
  - "search_query": ВАЖНО! Это запрос для поиска фото. Пиши 2-4 КОНКРЕТНЫХ слова на АНГЛИЙСКОМ. 
    Правила для search_query:
    * ОПИСЫВАЙ ТО, ЧТО МОЖНО СФОТОГРАФИРОВАТЬ: людей, здания, предметы, природу
    * ПРИМЕРЫ ПРАВИЛЬНЫХ запросов:
      - "Ivan the Terrible portrait" (а не "Ivan Grozny")
      - "Kremlin Moscow 16th century" (а не "Russia history")
      - "ancient printing press book" (а не "culture education")
      - "Trinity Lavra monastery Russia" (а не "Троице-Сергиева лавра")
      - "medieval battle armor warriors" (а не "борьба с врагами")
      - "Stroganov merchant salt trade" (а не "купцы Строгановы")
    * НЕ ИСПОЛЬЗУЙ абстрактные понятия, которые нельзя сфоткать
    * Всегда добавляй уточняющие слова: portrait, building, painting, monument, manuscript, map

Слайд {n}: Список литературы (реальные книги/статьи по теме, 5 шт.)

ФОРМАТ ОТВЕТА — СТРОГО JSON:
{{
  "title": "Заголовок презентации",
  "slides": [
    {{"type": "title", "text": "Москва, 2026"}},
    {{"type": "content", "title": "Заголовок слайда", "text": "Текст с фактами...", "search_query": "english specific searchable words"}},
    ...
    {{"type": "references", "text": "1. ...\\\\n2. ...\\\\n3. ...\\\\n4. ...\\\\n5. ..."}}
  ]
}}

ВАЖНО:
- Ровно {n} слайдов в массиве slides
- Каждый search_query УНИКАЛЕН и соответствует тексту слайда
- Слова в search_query только на английском
- ОПИСЫВАЙ КОНКРЕТНЫЕ ВИЗУАЛЬНЫЕ ОБЪЕКТЫ
"""
    
    resp = await ask_ai(prompt, temp=0.7)
    if not resp:
        log.error("GigaChat не ответил")
        return None
    
    data = extract_json(resp)
    if not data:
        log.warning("Первая попытка не удалась, пробую с другой температурой...")
        resp2 = await ask_ai(prompt, temp=0.5)
        if resp2:
            data = extract_json(resp2)
    
    if not data or "slides" not in data:
        log.error("Нет slides в ответе")
        return None
    
    # Проверка search_query
    for i, s in enumerate(data.get("slides", [])):
        if s.get("type") == "content":
            sq = s.get("search_query", "")
            log.info(f"Слайд {i}: search_query='{sq}'")
    
    log.info(f"OK: {len(data['slides'])} слайдов")
    return data

# ===== PIXABAY =====
async def pixabay_search(query: str, count: int = 5) -> List[str]:
    if not PIXABAY_KEY:
        log.warning("Нет Pixabay API ключа!")
        return []
    
    # Если запрос на русском — пробуем и так, но лучше английский
    params = {
        "key": PIXABAY_KEY,
        "q": query,
        "per_page": min(count, 20),
        "orientation": "horizontal",
        "min_width": 1024,
        "min_height": 768,
        "safesearch": "true",
        "image_type": "photo",
        "order": "popular"
    }
    
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://pixabay.com/api/", params=params, timeout=15) as r:
                if r.status == 200:
                    data = await r.json()
                    hits = data.get("hits", [])
                    urls = [h["largeImageURL"] for h in hits if "largeImageURL" in h]
                    log.info(f"Pixabay: {len(urls)} фото по '{query}'")
                    if urls:
                        return urls
                    
        # Если фото нет — пробуем с векторами/иллюстрациями
        params["image_type"] = "illustration"
        async with aiohttp.ClientSession() as s:
            async with s.get("https://pixabay.com/api/", params=params, timeout=15) as r:
                if r.status == 200:
                    data = await r.json()
                    hits = data.get("hits", [])
                    urls = [h["largeImageURL"] for h in hits if "largeImageURL" in h]
                    log.info(f"Pixabay (illustration): {len(urls)} по '{query}'")
                    return urls
    except Exception as e:
        log.warning(f"Pixabay: {e}")
    
    return []

async def download_image(url: str) -> Optional[BytesIO]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                if r.status == 200:
                    data = await r.read()
                    if len(data) > 1000:
                        return BytesIO(data)
    except: pass
    return None

async def get_image(query: str) -> Optional[BytesIO]:
    # 1. Pixabay
    if PIXABAY_KEY and query:
        urls = await pixabay_search(query)
        if urls:
            url = random.choice(urls)
            img = await download_image(url)
            if img:
                return img
    
    # 2. Запасной — упрощённый запрос
    if query:
        simple = re.sub(r'[^a-zA-Z\s]', '', query).strip()
        words = simple.split()
        if len(words) > 3:
            simple = ' '.join(words[:3])
        
        if simple:
            safe = simple.replace(' ', '%20')
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        f"https://image.pollinations.ai/prompt/{safe}",
                        params={"width": 1024, "height": 768, "nologo": "true", "seed": str(uuid.uuid4().int)[:8]},
                        timeout=25
                    ) as r:
                        if r.status == 200:
                            data = await r.read()
                            if len(data) > 1000:
                                return BytesIO(data)
            except: pass
    
    return None

# ===== PPTX С ДИЗАЙНОМ =====
def apply_theme(slide, theme: dict, is_title=False):
    """Применяет цветовую тему к фону слайда."""
    bg_color = theme["bg"]
    accent = theme["accent"]
    
    # Цвет фона
    background = slide.background
    fill = background.fill
    fill.solid()
    fill.fore_color.rgb = type(fill.fore_color).rgb = _hex_to_rgb(bg_color)
    
    # Цвет заголовка
    if slide.shapes.title:
        title = slide.shapes.title
        title.text_frame.paragraphs[0].font.color.rgb = type(title.text_frame.paragraphs[0].font.color).rgb = _hex_to_rgb(accent)

def _hex_to_rgb(hex_color: str):
    """Переводит HEX в tuple для python-pptx."""
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

async def make_pptx(data):
    slides = data.get("slides", [])
    if not slides:
        return None

    prs = Presentation()
    prs.slide_width = SW
    prs.slide_height = SH
    
    # Выбираем случайную тему для всей презентации
    theme = random.choice(THEMES)
    log.info(f"Тема: {theme['name']} (bg: #{theme['bg']}, accent: #{theme['accent']})")
    
    tasks = []
    for s in slides:
        if s.get("type") == "content":
            query = s.get("search_query", s.get("image_prompt", ""))
            tasks.append(get_image(query))
        else:
            tasks.append(asyncio.sleep(0))
    images = await asyncio.gather(*tasks, return_exceptions=True)

    for i, s in enumerate(slides):
        stype = s.get("type", "content")
        img_on_left = (i % 2 == 0)

        try:
            if stype == "title":
                sl = prs.slides.add_slide(prs.slide_layouts[0])
                apply_theme(sl, theme, is_title=True)
                sl.shapes.title.text = data.get("title", "Презентация")
                sl.shapes.title.text_frame.paragraphs[0].font.size = Pt(36)
                sl.shapes.title.text_frame.paragraphs[0].font.bold = True
                
                if len(sl.placeholders) > 1:
                    sl.placeholders[1].text = s.get("text", f"Москва, {datetime.now().year}")
                    sl.placeholders[1].text_frame.paragraphs[0].font.size = Pt(20)
                    sl.placeholders[1].text_frame.paragraphs[0].font.color.rgb = type(sl.placeholders[1].text_frame.paragraphs[0].font.color).rgb = _hex_to_rgb(theme["accent"])

            elif stype == "references":
                sl = prs.slides.add_slide(prs.slide_layouts[1])
                apply_theme(sl, theme)
                sl.shapes.title.text = "📚 Список литературы"
                if len(sl.placeholders) > 1:
                    sl.placeholders[1].text = s.get("text", "")
                    for p in sl.placeholders[1].text_frame.paragraphs:
                        p.font.size = Pt(14)

            else:
                sl = prs.slides.add_slide(prs.slide_layouts[1])
                apply_theme(sl, theme)
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

                img_top = Emu(1800000)
                img = images[i]
                
                if isinstance(img, BytesIO):
                    try:
                        sl.shapes.add_picture(img, img_left, img_top, width=IW)
                        caption = s.get("search_query", "Иллюстрация")
                        cap_top = img_top + Emu(3700000)
                        cap = sl.shapes.add_textbox(img_left, cap_top, IW, Emu(400000))
                        cap.text_frame.text = caption
                        for p in cap.text_frame.paragraphs:
                            p.font.size = Pt(9)
                            p.font.italic = True
                            p.alignment = PP_ALIGN.CENTER
                            p.font.color.rgb = type(p.font.color).rgb = _hex_to_rgb(theme["accent"])
                    except Exception as e:
                        log.error(f"Вставка {i}: {e}")
                        _add_placeholder(sl, img_left, img_top, IW, theme)
                else:
                    _add_placeholder(sl, img_left, img_top, IW, theme)

        except Exception as e:
            log.error(f"Слайд {i}: {e}")
            continue

    buf = BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf

def _add_placeholder(slide, left, top, width, theme: dict):
    shape = slide.shapes.add_shape(1, left, top, width, Emu(3600000))
    shape.fill.solid()
    shape.fill.fore_color.rgb = type(shape.fill.fore_color).rgb = (250, 250, 255)
    shape.line.color.rgb = type(shape.line.color).rgb = _hex_to_rgb(theme["accent"])
    shape.line.width = Pt(1)
    shape.text_frame.text = "🖼️\nФото не найдено"
    for p in shape.text_frame.paragraphs:
        p.alignment = PP_ALIGN.CENTER
        p.font.size = Pt(13)
        p.font.color.rgb = type(p.font.color).rgb = _hex_to_rgb(theme["accent"])

# ===== ОСТАЛЬНОЕ (без изменений) =====

def filename(topic):
    name = re.sub(r'[^\w\s-]', '', topic).strip()
    if len(name) > 30:
        name = name[:30].rsplit(' ', 1)[0]
    name = name.replace(' ', '_') or "presentation"
    return f"{name}.pptx"

async def send_file(msg, data, name, caption):
    for t in range(3):
        try:
            return await msg.answer_document(
                BufferedInputFile(data, name), caption=caption, parse_mode="Markdown"
            )
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except: await asyncio.sleep(2)

# ===== ОБРАБОТЧИКИ =====

@dp.message(Command("start"))
async def start(msg: Message, state: FSMContext):
    await state.clear()
    admin = "🆓 Бесплатный доступ!\n" if msg.from_user.id == ADMIN_ID else ""
    await msg.answer(
        f"🎓 *Привет! Я создаю презентации с ИИ!*\n\n"
        f"✨ Умный текст\n🖼️ Фото Pixabay\n🎨 Красивый дизайн\n\n"
        f"💰 Цена: {PRICE}₽\n{admin}\n👇 Кнопка:",
        parse_mode="Markdown", reply_markup=menu()
    )

@dp.message(F.text == "ℹ️ Помощь")
async def help_cmd(msg: Message):
    await msg.answer(
        "📌 *Как создать:*\n\n1. Нажми «Создать»\n2. Тема и число (4-12)\n"
        "3. Оплати 100₽\n4. Получи файл!\n\n*Примеры:*\n`Нейросети 8`\n`История России 16 век 7`\n`Биология 6`",
        parse_mode="Markdown"
    )

@dp.message(F.text == "💰 Цена")
async def price_cmd(msg: Message):
    await msg.answer(f"💎 *{PRICE}₽*\n\n✅ Текст GigaChat\n✅ Фото Pixabay\n✅ Цветной дизайн\n✅ Литература",
                     parse_mode="Markdown")

@dp.message(F.text == "🎨 Создать презентацию")
async def start_create(msg: Message, state: FSMContext):
    await state.clear()
    await state.set_state(State.topic)
    await msg.answer("✏️ *Тема и количество:*\n\n`Нейросети 8`\n`История России 16 век 7`\n`Квантовая физика 5`",
                     parse_mode="Markdown")

@dp.message(StateFilter(State.topic))
async def got_topic(msg: Message, state: FSMContext):
    text = msg.text.strip()
    if text.startswith('/'):
        await state.clear()
        return await start(msg, state)
    
    parts = text.split()
    if len(parts) < 2:
        return await msg.answer("❌ Формат: Тема Число\nПример: `Нейросети 6`")
    
    try:
        n = int(parts[-1])
        topic = " ".join(parts[:-1])
    except ValueError:
        return await msg.answer("❌ Число в конце!\nПример: `История 6`")
    
    if n < 4 or n > 12:
        return await msg.answer("❌ От 4 до 12 слайдов")
    
    await state.update_data(topic=topic, num=n)
    
    if msg.from_user.id == ADMIN_ID:
        await state.clear()
        status = await msg.answer(f"🔄 Генерирую «{topic}», {n} слайдов...")
        try:
            data = await asyncio.wait_for(get_content(topic, n), timeout=120)
            if not data:
                return await status.edit_text("❌ GigaChat не ответил")
            await status.edit_text("🖼️ Подбираю фото с Pixabay...")
            pptx = await asyncio.wait_for(make_pptx(data), timeout=180)
            if not pptx:
                return await status.edit_text("❌ Ошибка сборки")
            await send_file(msg, pptx.getvalue(), filename(topic),
                          f"✅ *Готово!*\n📌 {topic}\n📊 {n} слайдов\n🎨 Цветной дизайн\n🖼️ Фото Pixabay")
            await status.delete()
        except asyncio.TimeoutError:
            await status.edit_text("⏰ Долго. Упрости тему.")
        except Exception as e:
            log.error(f"Ошибка: {e}")
            await status.edit_text("❌ Ошибка. /start")
    else:
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
        await cb.message.edit_text(f"✅ Оплачено! Генерирую «{topic}»...")
        try:
            data = await asyncio.wait_for(get_content(topic, n), timeout=120)
            if not data:
                return await cb.message.edit_text("❌ Ошибка. Деньги вернутся.")
            await cb.message.edit_text("🖼️ Подбираю фото...")
            pptx = await asyncio.wait_for(make_pptx(data), timeout=180)
            if not pptx:
                return await cb.message.edit_text("❌ Ошибка сборки.")
            await send_file(cb.message, pptx.getvalue(), filename(topic),
                          f"✅ *Готово!*\n📌 {topic}\n📊 {n} слайдов\n💰 {PRICE}₽ оплачено\n🎨 Цветной дизайн")
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

async def main():
    log.info("🚀 Запуск...")
    if PIXABAY_KEY:
        log.info("✅ Pixabay API подключён")
    else:
        log.warning("⚠️ Pixabay API не подключён")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())