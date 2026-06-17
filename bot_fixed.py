import asyncio
import re
import json
import os
import logging
import math
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI
import requests
from fuzzywuzzy import fuzz

#  НАЛАШТУВАННЯ 
import os

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ЗАВАНТАЖЕННЯ БАЗИ З JSON 
LOCATIONS_FILE = "locations.json"

def load_locations():
    if not os.path.exists(LOCATIONS_FILE):
        raise FileNotFoundError(f"Файл {LOCATIONS_FILE} не знайдено.")
    with open(LOCATIONS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        for loc in data:
            #  наявність ключів
            loc.setdefault("indoor", False)
            loc.setdefault("description", "")
            loc.setdefault("hours", "")
            loc.setdefault("price", "")
            loc.setdefault("title_en", "")
            loc.setdefault("description_en", "")
            loc.setdefault("hours_en", "")
            loc.setdefault("price_en", "")
        return data

locations = load_locations()
logger.info(f"✅ Завантажено {len(locations)} локацій.")

TYPE_ICON = {
    "historical": "🏛",
    "park": "🌳",
    "church": "⛪",
    "museum": "🏺",
    "other": "📍"
}

translations = {
    "uk": {
        "start": "👋 Привіт! Я бот для планування маршрутів Рівним.\nНапиши, що хочеш побачити (наприклад: історичні місця та парки).",
        "ask_location": "📍 Надішліть геолокацію або напишіть «ні» / «пропустити».",
        "ask_time": "Оберіть час з клавіатури або введіть вручну (2 години).",
        "thanks_location": "📍 Дякую! Тепер оберіть час.",
        "analyzing": "🤖 Аналізую запит...",
        "error_openai": "Помилка аналізу. Спробуйте простіше.",
        "not_found": "На жаль, нічого не знайдено.",
        "route_variants": "Знайдено варіанти маршрутів:",
        "route_ready": "✅ **Ваш маршрут готовий!**",
        "points_list": "🗺 **Список точок:**",
        "open_map": "🔗 [Відкрити карту з маршрутом]({url})",
        "travel_mode": "Оберіть спосіб пересування:",
        "choose_route": "Оберіть маршрут за допомогою кнопки.",
        "new_route_prompt": "Бажаєте спланувати ще один маршрут? Надішліть /start або напишіть новий запит.",
        "cancel": "✅ Діалог скасовано.",
        "popular_routes": "Популярні маршрути:",
        "route_historical": "Історичний центр (2 год)",
        "route_parks": "Парки та відпочинок (3 год)",
        "route_museums": "Музеї та культура (4 год)"
    },
    "en": {
        "start": "👋 Hi! Plan your route in Rivne.\nTell me what you'd like to see.",
        "ask_location": "📍 Send your location or type 'skip'.",
        "ask_time": "Choose time from keyboard or enter manually.",
        "thanks_location": "📍 Thanks! Now choose time.",
        "analyzing": "🤖 Analyzing...",
        "error_openai": "Analysis error. Try a simpler query.",
        "not_found": "Sorry, nothing found.",
        "route_variants": "Route variants found:",
        "route_ready": "✅ **Your route is ready!**",
        "points_list": "🗺 **Points:**",
        "open_map": "🔗 [Open route on Google Maps]({url})",
        "travel_mode": "Choose travel mode:",
        "choose_route": "Select a route.",
        "new_route_prompt": "Plan another route? Send /start or type new query.",
        "cancel": "✅ Dialog cancelled.",
        "popular_routes": "Popular routes:",
        "route_historical": "Historical center (2h)",
        "route_parks": "Parks & relaxation (3h)",
        "route_museums": "Museums & culture (4h)"
    }
}

def detect_language(text):
    cyrillic = sum(1 for ch in text if 'а' <= ch.lower() <= 'я' or ch.lower() == 'ё')
    latin = sum(1 for ch in text if 'a' <= ch.lower() <= 'z')
    return 'en' if latin > cyrillic else 'uk'

class RouteStates(StatesGroup):
    waiting_query = State()
    waiting_location = State()
    waiting_time = State()
    waiting_travel_mode = State()
    waiting_choice = State()

user_lang = {}

def get_text(user_id, key):
    lang = user_lang.get(user_id, 'uk')
    return translations[lang].get(key, key)

def get_localized_field(loc, field, lang):
    """Повертає локалізоване поле об'єкта (назва, опис, години, ціна)."""
    field_en = f"{field}_en"
    if lang == 'en' and loc.get(field_en):
        return loc[field_en]
    return loc.get(field, "")

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def travel_time_min(distance_km, travel_mode):
    speeds = {'walking': 5, 'driving': 40}
    speed = speeds.get(travel_mode, 5)
    return (distance_km / speed) * 60

def total_time_with_travel(route, travel_mode, start_lat=None, start_lon=None):
    if not route:
        return 0
    total = sum(loc['duration'] for loc in route)
    coords = []
    if start_lat is not None and start_lon is not None:
        coords.append((start_lat, start_lon))
    for loc in route:
        coords.append((loc['lat'], loc['lng']))
    for i in range(len(coords)-1):
        d = haversine(coords[i][0], coords[i][1], coords[i+1][0], coords[i+1][1])
        total += travel_time_min(d, travel_mode)
    return total

def optimize_order(points, start_lat=None, start_lon=None):
    if not points or len(points) <= 1:
        return points
    pts = points.copy()
    if start_lat is not None and start_lon is not None:
        idx = min(range(len(pts)), key=lambda i: haversine(start_lat, start_lon, pts[i]['lat'], pts[i]['lng']))
        route = [pts.pop(idx)]
    else:
        route = [pts.pop(0)]
    while pts:
        last = route[-1]
        idx = min(range(len(pts)), key=lambda i: haversine(last['lat'], last['lng'], pts[i]['lat'], pts[i]['lng']))
        route.append(pts.pop(idx))
    return route

def filter_by_types(locations, types):
    if 'all' in types:
        return locations
    return [loc for loc in locations if loc['type'] in types]

def find_by_name_fuzzy(query, locations, th=70):
    query_low = query.lower()
    scores = []
    for loc in locations:
        ratio = fuzz.partial_ratio(query_low, loc['title'].lower())
        if ratio >= th:
            scores.append((ratio, loc))
    words = query_low.split()
    for loc in locations:
        for w in words:
            if len(w) > 2 and w in loc['title'].lower():
                scores.append((90, loc))
    seen = set()
    res = []
    for _, loc in sorted(scores, key=lambda x: -x[0]):
        if loc['id'] not in seen:
            seen.add(loc['id'])
            res.append(loc)
    return res[:5]

def generate_variants(filtered, types_req, limit_min, travel_mode, user_lat=None, user_lon=None):
    if not filtered:
        return []
    if not types_req or 'all' in types_req:
        short = sorted(filtered, key=lambda x: x['duration'])[:5]
        opt = optimize_order(short, user_lat, user_lon)
        return [opt] if opt else []

    by_type = {}
    for loc in filtered:
        by_type.setdefault(loc['type'], []).append(loc)
    for t in by_type:
        by_type[t].sort(key=lambda x: x['duration'])

    var1 = []
    for t in types_req:
        if t in by_type and by_type[t]:
            var1.append(by_type[t][0])
    var2 = []
    for t in types_req:
        if t in by_type:
            if len(by_type[t]) > 1:
                var2.append(by_type[t][1])
            else:
                var2.append(by_type[t][0])
    var3 = []
    if len(types_req) >= 2:
        t1, t2 = types_req[0], types_req[1]
        if t1 in by_type and by_type[t1] and t2 in by_type and by_type[t2]:
            var3 = [by_type[t1][0], by_type[t2][0]]
    if not var3 and filtered:
        var3 = [min(filtered, key=lambda x: x['duration'])]

    for v in [var1, var2, var3]:
        if len(v) > 5:
            v[:] = v[:5]

    results = []
    for v in [var1, var2, var3]:
        if not v:
            continue
        v_opt = optimize_order(v, user_lat, user_lon)
        total = total_time_with_travel(v_opt, travel_mode, user_lat, user_lon)
        if limit_min < 9999 and total > limit_min * 1.2:
            v_sorted = sorted(v_opt, key=lambda x: x['duration'], reverse=True)
            while len(v_sorted) > 1 and total_time_with_travel(v_sorted, travel_mode, user_lat, user_lon) > limit_min * 1.2:
                v_sorted.pop()
            v_opt = optimize_order(v_sorted, user_lat, user_lon)
        results.append(v_opt)

    unique = []
    for r in results:
        key = tuple(loc['id'] for loc in r)
        if key not in [tuple(loc['id'] for loc in u) for u in unique]:
            unique.append(r)
    return unique[:3]

def build_google_maps_url(route, user_lat=None, user_lon=None, travel="walking"):
    if not route:
        return None
    mode = travel if travel in ["walking","driving"] else "walking"
    if user_lat is not None and user_lon is not None:
        origin = f"{user_lat},{user_lon}"
        dest = f"{route[-1]['lat']},{route[-1]['lng']}"
        waypoints = "|".join([f"{loc['lat']},{loc['lng']}" for loc in route])
        return f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={dest}&waypoints={waypoints}&travelmode={mode}"
    else:
        if len(route) == 1:
            return f"https://www.google.com/maps/search/?api=1&query={route[0]['lat']},{route[0]['lng']}"
        origin = f"{route[0]['lat']},{route[0]['lng']}"
        dest = f"{route[-1]['lat']},{route[-1]['lng']}"
        waypoints = "|".join([f"{loc['lat']},{loc['lng']}" for loc in route[1:-1]])
        return f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={dest}&waypoints={waypoints}&travelmode={mode}"

def get_weather():
    if not WEATHER_API_KEY or WEATHER_API_KEY == "ваш_ключ_від_OpenWeatherMap":
        return None, None
    url = f"http://api.openweathermap.org/data/2.5/weather?q=Rivne,ua&appid={WEATHER_API_KEY}&units=metric&lang=ua"
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if resp.status_code == 200:
            return data['weather'][0]['description'], data['main']['temp']
    except:
        pass
    return None, None

def recommend_indoor(locations, types=None):
    indoor = [loc for loc in locations if loc.get('indoor', False)]
    if types:
        indoor = [loc for loc in indoor if loc['type'] in types]
    return indoor[:3]

#  ОБРОБНИКИ 
bot = Bot(token=TELEGRAM_TOKEN, request_timeout=60)
dp = Dispatcher()

time_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="30 хв"), KeyboardButton(text="1 год")],
        [KeyboardButton(text="2 год"), KeyboardButton(text="3 год")],
        [KeyboardButton(text="Необмежено")]
    ],
    resize_keyboard=True, one_time_keyboard=True
)

travel_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚶 Пішки"), KeyboardButton(text="🚗 Авто")]
    ],
    resize_keyboard=True, one_time_keyboard=True
)

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    await message.answer(get_text(uid, "start"))
    await state.set_state(RouteStates.waiting_query)

@dp.message(Command("cancel"))
async def cancel_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    await state.clear()
    await message.answer(get_text(uid, "cancel"), reply_markup=ReplyKeyboardRemove())

@dp.message(Command("lang"))
async def lang_cmd(message: types.Message):
    uid = message.from_user.id
    args = message.text.split()
    if len(args) > 1:
        if args[1] == 'en':
            user_lang[uid] = 'en'
            await message.answer("Language set to English")
        else:
            user_lang[uid] = 'uk'
            await message.answer("Мову встановлено: українська")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🇺🇦 Українська", callback_data="lang_uk")],
            [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")]
        ])
        await message.answer("Оберіть мову / Choose language:", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith('lang_'))
async def lang_cb(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if callback.data == 'lang_uk':
        user_lang[uid] = 'uk'
        await callback.message.edit_text("Мову встановлено: українська")
    else:
        user_lang[uid] = 'en'
        await callback.message.edit_text("Language set to English")
    await callback.answer()

@dp.message(Command("tours"))
async def tours_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    lang = detect_language(message.text)
    user_lang[uid] = lang
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=get_text(uid, "route_historical"))],
            [KeyboardButton(text=get_text(uid, "route_parks"))],
            [KeyboardButton(text=get_text(uid, "route_museums"))]
        ],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer(get_text(uid, "popular_routes"), reply_markup=kb)
    await state.set_state(RouteStates.waiting_query)

@dp.message(StateFilter(None))
async def auto_start(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    lang = detect_language(message.text)
    user_lang[uid] = lang
    await state.set_state(RouteStates.waiting_query)
    await get_query(message, state)

@dp.message(RouteStates.waiting_query)
async def get_query(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    lang = detect_language(message.text)
    user_lang[uid] = lang
    txt = message.text

    if txt == get_text(uid, "route_historical"):
        sel = [loc for loc in locations if loc['id'] in [4,5,10,14,20]]
        await state.update_data(variants=[sel])
        await message.answer("✅ Готовий маршрут 'Історичний центр'", reply_markup=ReplyKeyboardRemove())
        await show_route(message, state, sel)
        return
    elif txt == get_text(uid, "route_parks"):
        sel = [loc for loc in locations if loc['id'] in [3,15,16]]
        await state.update_data(variants=[sel])
        await message.answer("✅ Готовий маршрут 'Парки та відпочинок'", reply_markup=ReplyKeyboardRemove())
        await show_route(message, state, sel)
        return
    elif txt == get_text(uid, "route_museums"):
        sel = [loc for loc in locations if loc['id'] in [6,26,27]]
        await state.update_data(variants=[sel])
        await message.answer("✅ Готовий маршрут 'Музеї та культура'", reply_markup=ReplyKeyboardRemove())
        await show_route(message, state, sel)
        return

    await state.update_data(query=txt)
    loc_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Надіслати геолокацію", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer(get_text(uid, "ask_location"), reply_markup=loc_kb)
    await state.set_state(RouteStates.waiting_location)

@dp.message(RouteStates.waiting_location)
async def location_or_skip(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if message.location:
        await state.update_data(user_lat=message.location.latitude, user_lon=message.location.longitude)
        await message.answer(get_text(uid, "thanks_location"), reply_markup=time_kb)
        await state.set_state(RouteStates.waiting_time)
    else:
        skip = message.text.lower()
        if any(w in skip for w in ["ні", "пропустити", "skip", "no"]):
            await state.update_data(user_lat=None, user_lon=None)
            await message.answer("⏩ Пропускаємо геолокацію.", reply_markup=ReplyKeyboardRemove())
            await message.answer(get_text(uid, "ask_time"), reply_markup=time_kb)
            await state.set_state(RouteStates.waiting_time)
        else:
            kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="📍 Надіслати геолокацію", request_location=True)]],
                resize_keyboard=True, one_time_keyboard=True
            )
            await message.answer("⚠️ Надішліть геолокацію або напишіть «ні» / «пропустити».", reply_markup=kb)

@dp.message(RouteStates.waiting_time)
async def get_time(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    txt = message.text.lower()
    minutes = None
    if txt in ["30 хв", "1 год", "2 год", "3 год"]:
        if "30 хв" in txt: minutes = 30
        elif "1 год" in txt: minutes = 60
        elif "2 год" in txt: minutes = 120
        elif "3 год" in txt: minutes = 180
    elif txt == "необмежено":
        minutes = 9999
    else:
        m = re.search(r'(\d+)\s*год', txt)
        if m: minutes = int(m.group(1))*60
        else:
            m = re.search(r'(\d+)\s*хв', txt)
            if m: minutes = int(m.group(1))
    if not minutes:
        await message.answer(get_text(uid, "ask_time"))
        return

    await state.update_data(max_minutes=minutes)

    cond, temp = get_weather()
    if cond:
        await message.answer(f"🌡 Погода в Рівному: {cond}, {temp}°C")
        if any(w in cond.lower() for w in ['дощ','сніг','злива']):
            ind = recommend_indoor(locations)
            if ind:
                await message.answer(f"⚠️ Рекомендуємо криті локації: {', '.join(l['title'] for l in ind)}")

    await message.answer(get_text(uid, "travel_mode"), reply_markup=travel_kb)
    await state.set_state(RouteStates.waiting_travel_mode)

@dp.message(RouteStates.waiting_travel_mode)
async def get_travel_mode(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    txt = message.text.lower()
    mode_map = {"🚶 пішки": "walking", "🚗 авто": "driving"}
    travel_mode = mode_map.get(txt, "walking")
    await state.update_data(travel_mode=travel_mode)

    data = await state.get_data()
    query = data.get('query')
    if not query:
        await message.answer(get_text(uid, "error_openai"))
        await state.clear()
        return

    minutes = data.get('max_minutes')
    user_lat = data.get('user_lat')
    user_lon = data.get('user_lon')
    await message.answer(get_text(uid, "analyzing"), reply_markup=ReplyKeyboardRemove())
    logger.info(f"Запит: {query}, час: {minutes} хв")

    prompt = f"""Ти — помічник для планування маршрутів.
    Отримай текст запиту: '{query}'.
    Визнач, які типи місць (historical, park, church, museum, other) згадуються в запиті.
    Поверни ТІЛЬКИ ті типи, які явно або за змістом присутні в запиті.
    Якщо жоден тип не згадано, поверни ['all'].
    Відповідай тільки JSON з полем 'types'.
    Приклади:
    - "хочу історичні місця" → {{"types": ["historical"]}}
    - "парк та музей" → {{"types": ["park", "museum"]}}
    - "щось цікаве" → {{"types": ["all"]}}
    """
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        ai = resp.choices[0].message.content
        types = json.loads(ai).get('types', ['all'])
        logger.info(f"OpenAI типи: {types}")
    except Exception as e:
        logger.error(f"OpenAI помилка: {e}")
        await message.answer(get_text(uid, "error_openai"))
        await state.clear()
        return

    filtered_by_type = filter_by_types(locations, types)
    named = find_by_name_fuzzy(query, locations)
    combined = []
    seen = set()
    for loc in named + filtered_by_type:
        if loc['id'] not in seen:
            combined.append(loc)
            seen.add(loc['id'])

    need_acc = any(w in query.lower() for w in ['інвалід','коляск','доступн','accessible'])
    if need_acc:
        combined = [loc for loc in combined if loc.get('accessibility', False)]

    if not combined:
        await message.answer(get_text(uid, "not_found"))
        await state.clear()
        return

    variants = generate_variants(combined, types, minutes, travel_mode, user_lat, user_lon)
    if not variants:
        await message.answer(get_text(uid, "not_found"))
        await state.clear()
        return

    await state.update_data(variants=variants)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=f"Маршрут {i+1}")] for i in range(len(variants))],
        resize_keyboard=True, one_time_keyboard=True
    )
    msg = get_text(uid, "route_variants") + "\n"
    for i, r in enumerate(variants):
        total_dur = sum(l['duration'] for l in r)
        total_est = int(total_time_with_travel(r, travel_mode, user_lat, user_lon))
        exceed = total_est - minutes if minutes != 9999 else 0
        msg += f"{i+1}. {len(r)} місць, огляд {total_dur} хв, загалом {total_est} хв"
        if exceed > 0:
            msg += f" (⚠️ на {exceed} хв більше ліміту)"
        msg += "\n" + "\n".join(f"  {TYPE_ICON.get(l['type'],'📍')} {get_localized_field(l, 'title', user_lang.get(uid, 'uk'))} ({l['duration']} хв)" for l in r) + "\n"
    await message.answer(msg, reply_markup=kb)
    await state.set_state(RouteStates.waiting_choice)

async def show_route(message: types.Message, state: FSMContext, selected):
    uid = message.from_user.id
    lang = user_lang.get(uid, 'uk')
    data = await state.get_data()
    travel = data.get('travel_mode', 'walking')
    lat = data.get('user_lat')
    lon = data.get('user_lon')
    url = build_google_maps_url(selected, lat, lon, travel)

    txt = get_text(uid, "points_list") + "\n"
    for loc in selected:
        icon = TYPE_ICON.get(loc['type'], '📍')
        title = get_localized_field(loc, 'title', lang)
        description = get_localized_field(loc, 'description', lang)
        hours = get_localized_field(loc, 'hours', lang)
        price = get_localized_field(loc, 'price', lang)
        txt += f"{icon} *{title}*\n"
        if description:
            txt += f"📖 {description}\n"
        if hours:
            txt += f"🕒 {hours}\n"
        if price:
            txt += f"💰 {price}\n"
        txt += f"⏱ {loc['duration']} хв\n\n"
    if url:
        txt += get_text(uid, "open_map").format(url=url)
    await message.answer(txt, parse_mode="Markdown")
    await message.answer(get_text(uid, "new_route_prompt"))
    await state.clear()

@dp.message(RouteStates.waiting_choice)
async def route_choice(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    variants = data.get('variants', [])
    try:
        idx = int(message.text.split()[-1]) - 1
        if idx < 0 or idx >= len(variants):
            raise ValueError
        sel = variants[idx]
    except:
        await message.answer(get_text(uid, "choose_route"))
        return
    await show_route(message, state, sel)

async def main():
    logger.info("Бот запускається...")
    await dp.start_polling(bot, handle_signals=False)

if __name__ == "__main__":
    asyncio.run(main())
