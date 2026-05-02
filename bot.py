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
).decode() if os.getenv('GIGACHAT_CLIENT_ID') else ""
YOOKASSA_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_KEY = os.getenv("YOOKASSA_SECRET_KEY")
UNSPLASH_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_CX = os.getenv("GOOGLE_CX", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PRICE = 100

if not all([BOT_TOKEN, YOOKASSA_ID, YOOKASSA_KEY]):
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

bot = Bot(token=BOT_TOKEN, session=AiohttpSession(timeout=120))
dp = Dispatcher(storage=MemoryStorage())

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

# ===== ТОКЕН GIGACHAT =====
_token = None
_token_exp = 0
_token_lock = asyncio.Lock()

async def get_giga_token():
    global _token, _token_exp
    if not GIGA_AUTH:
        return None
    async with _token_lock:
        if _token and time.time() < _token_exp - 300:
            return _token
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as s:
                async with s.post(
                    "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                    headers={
                        "Authorization": f"Basic {GIGA_AUTH}",
                        "RqUID": str(uuid.uuid4()),
                        "Content-Type": "application/x-www-form-urlencoded"
                    },
                    data={"scope": "GIGACHAT_API_PERS"},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        _token = data["access_token"]
                        _token_exp = time.time() + 3600
                        return _token
        except:
            pass
        return None

# ===== ИСТОЧНИКИ ИИ =====

async def ask_gigachat(prompt):
    if not GIGA_AUTH:
        return None
    token = await get_giga_token()
    if not token:
        return None
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as s:
            async with s.post(
                "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "model": "GigaChat",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.6, "max_tokens": 3500
                },
                timeout=aiohttp.ClientTimeout(total=60)
            ) as r:
                if r.status == 200:
                    return (await r.json())["choices"][0]["message"]["content"]
    except:
        pass
    return None

async def ask_duckduckgo(prompt):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://duckduckgo.com/duckchat/v1/status",
                           headers={"x-vqd-accept": "1"}, timeout=10) as r:
                if r.status != 200:
                    return None
                vqd = r.headers.get("x-vqd-4")
            async with s.post("https://duckduckgo.com/duckchat/v1/chat",
                            headers={"x-vqd-4": vqd, "Content-Type": "application/json"},
                            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}]},
                            timeout=60) as r:
                if r.status == 200:
                    text = await r.text()
                    message = ""
                    for line in text.split('\n'):
                        if line.startswith('data: '):
                            try:
                                d = json.loads(line[6:])
                                if d.get("message"):
                                    message += d["message"]
                            except:
                                pass
                    if message:
                        return message.strip()
    except:
        pass
    return None

async def ask_ai(prompt):
    # GigaChat
    resp = await ask_gigachat(prompt)
    if resp:
        return resp
    # DuckDuckGo
    resp = await ask_duckduckgo(prompt)
    if resp:
        return resp
    return None

# ===== ШАГ 1: ГЕНЕРАЦИЯ ТЕКСТА =====
async def get_content(topic, n):
    prompt = f"""Create an educational presentation about "{topic}". Exactly {n} slides.

IMPORTANT: Generate ONLY the slide content. Do NOT generate search queries.
For each content slide provide:
- "title": Engaging title
- "text": 3-4 sentences with facts, dates, names, specific details

Return ONLY JSON:
{{"title":"Presentation Title","slides":[
  {{"type":"title","text":"Moscow, 2026"}},
  {{"type":"content","title":"...","text":"..."}},
  {{"type":"references","text":"1. ...\\n2. ...\\n3. ...\\n4. ...\\n5. ..."}}
]}}"""
    
    resp = await ask_ai(prompt)
    if not resp:
        return None
    data = extract_json(resp)
    if not data:
        resp2 = await ask_ai("Return ONLY the JSON. " + prompt)
        if resp2:
            data = extract_json(resp2)
    return data if data and "slides" in data else None

# ===== ШАГ 2: АНАЛИЗ ТЕКСТА → КОНКРЕТНЫЕ ОБЪЕКТЫ =====
async def extract_visual_objects(slide_text: str, slide_title: str) -> List[str]:
    """
    Анализирует текст слайда и извлекает КОНКРЕТНЫЕ визуальные объекты.
    Возвращает список поисковых запросов (от конкретного к общему).
    """
    prompt = f"""Analyze this slide text and extract SPECIFIC visual subjects that can be photographed.

Title: {slide_title}
Text: {slide_text}

Rules:
1. Extract proper nouns: people, places, buildings, specific objects
2. For each, create a search query: "Subject Name + type + context"
3. Order from most specific to most general
4. ONLY list things that EXIST in the real world and can be photographed

Examples:
- Text about "Ivan the Terrible" → ["Ivan the Terrible 16th century portrait painting", "Tsar Ivan IV sculpture monument"]
- Text about "DNA" → ["DNA double helix molecular model", "DNA structure microscope"]
- Text about "Kremlin" → ["Moscow Kremlin red brick walls", "Kremlin cathedrals architecture"]

Return ONLY a JSON array of strings:
["most specific query", "second query", "third query"]"""

    resp = await ask_ai(prompt)
    if not resp:
        return []
    
    try:
        # Извлекаем массив из ответа
        start = resp.find('[')
        end = resp.rfind(']')
        if start != -1 and end != -1:
            queries = json.loads(resp[start:end+1])
            if isinstance(queries, list):
                log.info(f"Визуальные объекты: {queries[:3]}")
                return queries
    except:
        pass
    return []

# ===== ШАГ 3: ПОИСК КАРТИНОК ВО ВСЕХ ИСТОЧНИКАХ =====

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
                params={"query": query, "per_page": 5, "orientation": "landscape", "client_id": UNSPLASH_KEY},
                timeout=15
            ) as r:
                if r.status == 200:
                    results = (await r.json()).get("results", [])
                    if results:
                        url = random.choice(results)["urls"].get("regular")
                        if url:
                            img = await _download(url)
                            if img:
                                log.info(f"✅ Unsplash: {query[:60]}")
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
                    params={"key": PIXABAY_KEY, "q": query, "per_page": 5,
                            "orientation": "horizontal", "min_width": 1024,
                            "safesearch": "true", "image_type": img_type},
                    timeout=15
                ) as r:
                    if r.status == 200:
                        hits = (await r.json()).get("hits", [])
                        if hits:
                            url = random.choice(hits)["largeImageURL"]
                            img = await _download(url)
                            if img:
                                log.info(f"✅ Pixabay: {query[:60]}")
                                return img
    except:
        pass
    return None

async def search_google(query: str) -> Optional[BytesIO]:
    """Поиск картинок через Google Custom Search API."""
    if not GOOGLE_API_KEY or not GOOGLE_CX or not query:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": GOOGLE_API_KEY,
                    "cx": GOOGLE_CX,
                    "q": query,
                    "searchType": "image",
                    "num": 5,
                    "imgSize": "large",
                    "safe": "active"
                },
                timeout=15
            ) as r:
                if r.status == 200:
                    items = (await r.json()).get("items", [])
                    if items:
                        url = random.choice(items)["link"]
                        img = await _download(url)
                        if img:
                            log.info(f"✅ Google: {query[:60]}")
                            return img
    except:
        pass
    return None

async def search_bing(query: str) -> Optional[BytesIO]:
    """Свободный парсинг Bing Images."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://www.bing.com/images/search?q={query.replace(' ', '+')}&qft=+filterui:imagesize-wallpaper",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=15
            ) as r:
                if r.status == 200:
                    html = await r.text()
                    # Ищем прямые ссылки на изображения
                    urls = re.findall(r'https?://[^"\']+\.(?:jpg|jpeg|png|webp)', html)
                    urls = [u for u in urls if len(u) < 500][:10]
                    if urls:
                        url = random.choice(urls)
                        img = await _download(url)
                        if img:
                            log.info(f"✅ Bing: {query[:60]}")
                            return img
    except:
        pass
    return None

async def get_image_for_slide(slide_text: str, slide_title: str) -> Optional[BytesIO]:
    """
    Трёхступенчатый поиск картинки:
    1. Анализируем текст → извлекаем визуальные объекты
    2. Для каждого объекта пробуем ВСЕ источники
    3. Если не нашли — Pollinations AI
    """
    # Шаг 1: Извлекаем визуальные объекты
    queries = await extract_visual_objects(slide_text, slide_title)
    
    if not queries:
        # Fallback: используем заголовок как запрос
        queries = [f"{slide_title} historical photo"]
    
    # Шаг 2: Пробуем каждый запрос во всех источниках
    for query in queries[:5]:  # Максимум 5 попыток
        log.info(f"Поиск: '{query[:80]}'")
        
        # Unsplash
        img = await search_unsplash(query)
        if img: return img
        
        # Pixabay
        img = await search_pixabay(query)
        if img: return img
        
        # Google
        img = await search_google(query)
        if img: return img
        
        # Bing (свободный парсинг)
        img = await search_bing(query)
        if img: return img
    
    # Шаг 3: Запасной — Pollinations
    safe = re.sub(r'[^a-zA-Z0-9\s]', '', queries[0] if queries else slide_title).strip()
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
                            log.info(f"✅ Pollinations: {safe[:60]}")
                            return BytesIO(data)
        except:
            pass
    
    log.warning(f"❌ Картинка не найдена для: {slide_title[:50]}")
    return None

# ===== PPTX =====
async def make_pptx(data):
    slides = data.get("slides", [])
    if not slides:
        return None

    prs = Presentation()
    prs.slide_width = SW
    prs.slide_height = SH

    # Для каждого слайда запускаем полный цикл поиска
    tasks = []
    for s in slides:
        if s.get("type") == "content":
            tasks.append(get_image_for_slide(s.get("text", ""), s.get("title", "")))
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

                txBox = sl.shapes.add_textbox(txt_left, Emu(1600000), TW, Emu(4500000))
                tf = txBox.text_frame
                tf.word_wrap = True
                tf.text = text
                for p in tf.paragraphs:
                    p.font.size = Pt(15)
                    p.space_after = Pt(8)

                img = images[i]
                if isinstance(img, BytesIO):
                    try:
                        sl.shapes.add_picture(img, img_left, Emu(1800000), width=IW)
                    except:
                        pass

        except Exception as e:
            log.error(f"Слайд {i}: {e}")

    buf = BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf

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

# ========== ОБРАБОТЧИКИ ==========

@dp.message(Command("start"))
async def start(msg: Message, state: FSMContext):
    await state.clear()
    admin = "🆓 Бесплатный доступ!\n" if msg.from_user.id == ADMIN_ID else ""
    await msg.answer(
        f"🎓 *PrezaBot*\n\n✨ ИИ-текст + анализ\n🖼️ Умный поиск фото\n📊 PowerPoint\n\n💰 {PRICE}₽\n{admin}👇",
        parse_mode="Markdown", reply_markup=menu()
    )

@dp.message(F.text == "ℹ️ Помощь")
async def help_cmd(msg: Message):
    await msg.answer("📌 *Как:*\n1. «Создать»\n2. Тема Число (4-12)\n3. Оплатить\n4. Файл!\n\nПример: `Нейросети 8`, `История России 16 век 7`", parse_mode="Markdown")

@dp.message(F.text == "💰 Цена")
async def price_cmd(msg: Message):
    await msg.answer(f"💎 *{PRICE}₽*\n✅ ИИ-текст\n✅ Умный поиск фото\n✅ Слайды 5-12\n✅ Литература", parse_mode="Markdown")

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
        status = await msg.answer(f"🔄 Генерирую «{topic}», {n} слайдов...")
        try:
            data = await asyncio.wait_for(get_content(topic, n), timeout=180)
            if not data:
                return await status.edit_text("❌ ИИ не отвечает.")
            await status.edit_text("🔍 Анализирую текст и ищу идеальные фото...")
            pptx = await asyncio.wait_for(make_pptx(data), timeout=300)
            if not pptx:
                return await status.edit_text("❌ Ошибка сборки")
            await send_file(msg, pptx.getvalue(), filename(topic),
                          f"✅ *Готово!*\n📌 {topic}\n📊 {n} слайдов\n🖼️ Фото подобраны по тексту")
            await status.delete()
        except asyncio.TimeoutError:
            await status.edit_text("⏰ Слишком долго.")
        except Exception as e:
            log.error(f"{e}")
            await status.edit_text("❌ Ошибка. /start")
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
                    "items": [{"description": f"Презентация «{topic[:30]}»", "quantity": "1",
                              "amount": {"value": f"{PRICE}.00", "currency": "RUB"},
                              "vat_code": "1", "payment_mode": "full_prepayment", "payment_subject": "service"}]
                }
            })
            await state.update_data(pid=payment.id)
            await state.set_state(State.payment)
            await msg.answer(
                f"💎 *Заказ*\n📌 {topic}\n📊 {n} слайдов\n💰 *{PRICE}₽*\n👇 Оплатить:",
                parse_mode="Markdown", reply_markup=pay_kb(payment.confirmation.confirmation_url)
            )
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
        await cb.message.edit_text(f"✅ Оплачено! Генерирую «{topic}»...")
        try:
            data = await asyncio.wait_for(get_content(topic, n), timeout=180)
            if not data: return await cb.message.edit_text("❌ ИИ не ответил")
            await cb.message.edit_text("🔍 Анализирую текст, ищу фото...")
            pptx = await asyncio.wait_for(make_pptx(data), timeout=300)
            if not pptx: return await cb.message.edit_text("❌ Ошибка сборки")
            await send_file(cb.message, pptx.getvalue(), filename(topic),
                          f"✅ *Готово!*\n📌 {topic}\n📊 {n} слайдов\n💰 {PRICE}₽ оплачено")
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
    sources = []
    if GIGA_AUTH: sources.append("GigaChat")
    sources.append("DuckDuckGo")
    img_sources = []
    if UNSPLASH_KEY: img_sources.append("Unsplash")
    if PIXABAY_KEY: img_sources.append("Pixabay")
    if GOOGLE_API_KEY: img_sources.append("Google")
    img_sources.append("Bing")
    log.info(f"ИИ: {' → '.join(sources)}")
    log.info(f"Фото: {' → '.join(img_sources)}")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())