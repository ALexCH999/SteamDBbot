# bot_redis.py
from dotenv import load_dotenv
load_dotenv()
import os
import sys
import logging
import re
import time
import json
import asyncio
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# async redis client
try:
    from redis.asyncio import Redis
except Exception:
    Redis = None

# ================== CONFIG ==================
TOKEN = os.environ.get("BOT_TOKEN")  # <-- MUST be set in env or .env
REDIS_URL = os.environ.get("REDIS_URL") or "redis://localhost:6379/0"
CACHE_TTL = int(os.environ.get("CACHE_TTL", "180"))  # seconds
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "5"))

# ADMIN_IDS: comma separated list of Telegram user IDs (admins who can call /stats)
# Example: ADMIN_IDS="2045900240,123456789"
#ADMIN_IDS = [int(x) for x in (os.environ.get("ADMIN_IDS") or "").split(",") if x.strip().isdigit()]
ADMIN_IDS = {2045900240}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ================== HTTP session (sync) ==================
session = requests.Session()
retries = Retry(total=4, backoff_factor=0.4, status_forcelist=[429, 500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retries))
session.headers.update({"User-Agent": "Mozilla/5.0 (SteamInfoBot)"})

# ================== In-memory fallback stores (if Redis not available) ==================
_local_cache = {}   # key -> {"time": ts, "value": any}
_local_user_settings = {}  # chat_id -> {"lang": "ru"}
_local_users = set()  # fallback set of unique users

# ================== Redis client (global) ==================
redis_client = None  # will be initialized in init_redis()


def init_redis():
    """
    Синхронная инициализация клиента Redis (без await/ping).
    Создаёт объект клиента; реальное соединение будет открыто при первом обращении в event loop.
    """
    global redis_client, REDIS_URL
    if Redis is None:
        logger.warning("redis.asyncio (redis package) not available. Running without Redis.")
        redis_client = None
        return

    env = (os.environ.get("REDIS_URL") or REDIS_URL or "").strip()
    if not env or env.lower() in ("none", "disable", "disabled"):
        logger.info("REDIS_URL not set or disabled — running with in-memory cache.")
        redis_client = None
        return

    try:
        redis_client = Redis.from_url(env, decode_responses=True)
        logger.info("Initialized Redis client at %s (connection will be opened on first use)", env)
    except Exception:
        logger.exception("Failed to create Redis client. Falling back to in-memory cache.")
        redis_client = None


# ================== Cache helpers (async) ==================
async def cache_get(key: str):
    if redis_client:
        try:
            val = await redis_client.get(key)
            if val is None:
                return None
            return json.loads(val)
        except Exception:
            logger.exception("Redis GET failed for %s — falling back to local cache", key)
            return None
    # fallback
    ent = _local_cache.get(key)
    if not ent:
        return None
    if time.time() - ent["time"] > CACHE_TTL:
        del _local_cache[key]
        return None
    return ent["value"]


async def cache_set(key: str, value, ttl: int = CACHE_TTL):
    if redis_client:
        try:
            await redis_client.setex(key, ttl, json.dumps(value))
            return
        except Exception:
            logger.exception("Redis SET failed for %s — storing in local cache instead", key)
    # fallback
    _local_cache[key] = {"time": time.time(), "value": value}


# ================== User settings helpers (async) ==================
async def get_user_lang(chat_id: int) -> str:
    if redis_client:
        try:
            v = await redis_client.hget("user_settings", str(chat_id))
            return v or "ru"
        except Exception:
            logger.exception("Redis HGET failed — using local fallback for user lang")
            return _local_user_settings.get(chat_id, {}).get("lang", "ru")
    return _local_user_settings.get(chat_id, {}).get("lang", "ru")


async def set_user_lang(chat_id: int, lang: str):
    if redis_client:
        try:
            await redis_client.hset("user_settings", str(chat_id), lang)
            return
        except Exception:
            logger.exception("Redis HSET failed — storing in local fallback")
            _local_user_settings.setdefault(chat_id, {})["lang"] = lang
            return
    _local_user_settings.setdefault(chat_id, {})["lang"] = lang


# ================== Unique users tracking ==================
async def track_user(chat_id: int):
    """Добавляет chat_id в множество уникальных пользователей (Redis set 'users')."""
    if redis_client:
        try:
            # SADD возвращает 1 если добавлено, 0 если уже был
            await redis_client.sadd("users", str(chat_id))
            return
        except Exception:
            logger.exception("Redis SADD failed for users — using local fallback")
            _local_users.add(chat_id)
            return
    _local_users.add(chat_id)


# ================== Blocking helpers (sync) ==================
def _search_apps_sync(query: str, limit: int = MAX_SEARCH_RESULTS):
    try:
        url = "https://store.steampowered.com/api/storesearch/"
        r = session.get(url, params={"term": query, "cc": "US", "l": "english"}, timeout=8)
        r.raise_for_status()
        items = r.json().get("items") or r.json().get("results") or []
        out = []
        for it in items[:limit]:
            appid = it.get("id") or it.get("appid")
            name = it.get("name") or it.get("title") or ""
            if appid:
                out.append({"appid": int(appid), "name": name})
        if out:
            return out
    except Exception:
        logger.debug("storesearch JSON failed, fallback to HTML", exc_info=True)
    # fallback HTML
    try:
        url = "https://store.steampowered.com/search/"
        r = session.get(url, params={"term": query}, timeout=8)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("a.search_result_row")
        out = []
        for a in rows[:limit]:
            href = a.get("href", "")
            m = re.search(r"/app/(\d+)", href)
            title_el = a.select_one(".search_name .title") or a.select_one(".title") or a
            title = title_el.get_text(strip=True)
            if m:
                out.append({"appid": int(m.group(1)), "name": title})
        return out
    except Exception:
        logger.exception("HTML search fallback failed")
        return []


def _get_appdetails_sync(appid: int, lang: str):
    cc = "ru" if lang == "ru" else "us"
    lc = "russian" if lang == "ru" else "english"
    try:
        r = session.get("https://store.steampowered.com/api/appdetails", params={"appids": appid, "cc": cc, "l": lc}, timeout=10)
        r.raise_for_status()
        payload = r.json().get(str(appid), {})
        data = payload.get("data", {}) if payload else {}
        return data
    except Exception:
        logger.exception("appdetails failed for %s", appid)
        return {}


def _get_current_players_sync(appid: int):
    try:
        r = session.get("https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/", params={"appid": appid}, timeout=6)
        r.raise_for_status()
        return r.json().get("response", {}).get("player_count", "N/A")
    except Exception:
        return "N/A"


# ================== peak parsing functions ==================
def _normalize_number(s: str) -> str:
    if not s:
        return "N/A"
    s = str(s).strip()
    s = s.replace("\xa0", "").replace(" ", "").replace(",", "")
    s2 = re.sub(r"[^\d]", "", s)
    if not s2:
        return "N/A"
    return f"{int(s2):,}"


def _parse_steamcharts_sync(appid: int):
    url = f"https://steamcharts.com/app/{appid}"
    try:
        r = session.get(url, timeout=8)
        if r.status_code == 403:
            return None
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        heading = soup.find(id="app-heading")
        if not heading:
            return None
        stats = heading.find_all("div", class_="app-stat")
        if len(stats) >= 3:
            def read_stat(div):
                num = div.find("span", class_="num")
                return _normalize_number(num.get_text(strip=True)) if num else None
            p24 = read_stat(stats[1])
            allp = read_stat(stats[2])
            return (p24 or "N/A", allp or "N/A")
        text = soup.get_text(" ", strip=True)
        m24 = re.search(r"24-?hour peak\s*([\d,]+)", text, re.I)
        mall = re.search(r"all-?time peak\s*([\d,]+)", text, re.I)
        if m24 or mall:
            return (_normalize_number(m24.group(1)) if m24 else "N/A", _normalize_number(mall.group(1)) if mall else "N/A")
        return None
    except Exception:
        logger.exception("steamcharts parse error for %s", appid)
        return None


def _parse_steamdb_meta_sync(appid: int, charts_page: bool = True):
    url = f"https://steamdb.info/app/{appid}/charts/" if charts_page else f"https://steamdb.info/app/{appid}/"
    try:
        r = session.get(url, timeout=8)
        if r.status_code == 403:
            return None
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        meta = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
        content = meta.get("content") if meta and meta.get("content") else soup.get_text(" ", strip=True)
        m24 = re.search(r"24-?hour peak(?:.*?)([\d,]+)", content, re.I)
        mall = re.search(r"all-?time peak(?:.*?)([\d,]+)", content, re.I)
        if not (m24 or mall):
            m24 = re.search(r"([\d,]+)\s+24-?hour peak", content, re.I)
            mall = re.search(r"([\d,]+)\s+all-?time peak", content, re.I)
        if m24 or mall:
            return (_normalize_number(m24.group(1)) if m24 else "N/A", _normalize_number(mall.group(1)) if mall else "N/A")
        return None
    except Exception:
        logger.exception("steamdb parse error for %s (charts_page=%s)", appid, charts_page)
        return None


# ================== Async wrappers around sync functions ==================
async def search_apps(query: str):
    return await asyncio.to_thread(_search_apps_sync, query)


async def get_appdetails(appid: int, lang: str):
    cache_key = f"details:{appid}:{lang}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached
    data = await asyncio.to_thread(_get_appdetails_sync, appid, lang)
    await cache_set(cache_key, data)
    return data


async def get_current_players(appid: int):
    return await asyncio.to_thread(_get_current_players_sync, appid)


async def get_peaks(appid: int):
    cache_key = f"peaks:{appid}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached
    sc = await asyncio.to_thread(_parse_steamcharts_sync, appid)
    if sc:
        res = {"24h": sc[0], "all": sc[1]}
        await cache_set(cache_key, res)
        return res
    sd1 = await asyncio.to_thread(_parse_steamdb_meta_sync, appid, True)
    if sd1:
        res = {"24h": sd1[0], "all": sd1[1]}
        await cache_set(cache_key, res)
        return res
    sd2 = await asyncio.to_thread(_parse_steamdb_meta_sync, appid, False)
    if sd2:
        res = {"24h": sd2[0], "all": sd2[1]}
        await cache_set(cache_key, res)
        return res
    res = {"24h": "N/A", "all": "N/A"}
    await cache_set(cache_key, res)
    return res


# ================== Message builders ==================
def _clean_html(text: str) -> str:
    return BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)


def build_game_text(details: dict, appid: int, players, peaks: dict, lang: str, query_name: str | None = None) -> str:
    name = details.get("name") or query_name or "Unknown"
    price = details.get("price_overview", {}).get("final_formatted") if details.get("price_overview") else ("Free to Play" if lang == "en" else "Бесплатно")
    release = (details.get("release_date") or {}).get("date", "—")
    developers = ", ".join(details.get("developers") or []) or "—"
    publishers = ", ".join(details.get("publishers") or []) or "—"
    platforms = ", ".join([k.capitalize() for k, v in (details.get("platforms") or {}).items() if v]) or "—"
    metacritic = (details.get("metacritic") or {}).get("score")
    recommendations = (details.get("recommendations") or {}).get("total")
    short_desc = _clean_html(details.get("short_description", "") or "")
    reqs_list = []
    pc = details.get("pc_requirements") or {}
    for key in ("minimum", "recommended"):
        text = pc.get(key)
        if text:
            clean = _clean_html(text)
            if lang == "ru":
                label = "Минимум" if key == "minimum" else "Рекомендуемые"
            else:
                label = "Minimum" if key == "minimum" else "Recommended"
            reqs_list.append(f"{label}: {clean}")

    lines = []
    lines.append(f"🎮 {name} (AppID: {appid})")
    if query_name:
        lines.append(f"Запрос: {query_name}")
    lines.append(f"👥 Онлайн сейчас: {players}")
    lines.append(f"💰 Цена: {price}")
    lines.append(f"📅 Релиз: {release}")
    lines.append(f"🧩 Разработчик: {developers}")
    lines.append(f"🏷️ Издатель: {publishers}")
    lines.append(f"🖥️ Платформы: {platforms}")
    if metacritic:
        lines.append(f"📝 Metacritic: {metacritic}")
    if recommendations:
        lines.append(f"👍 Рекомендаций (примерно): {recommendations}")
    lines.append(f"📈 Пик 24ч: {peaks.get('24h','N/A')}")
    lines.append(f"🏆 All-time peak: {peaks.get('all','N/A')}")
    if short_desc:
        sd = short_desc if len(short_desc) <= 500 else short_desc[:497] + "..."
        lines.append("")
        lines.append(sd)
    if reqs_list:
        lines.append("")
        lines.append("*Системные требования:*")
        for r in reqs_list:
            lines.append(f"– {r}")

    return "\n".join(lines)


# ================== Handlers (async) ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await track_user(chat_id)
    # ensure default lang saved in redis/local
    await set_user_lang(chat_id, await get_user_lang(chat_id))

    # set bot commands visible in client
    try:
        await context.bot.set_my_commands([
            BotCommand("start", "Start and choose language"),
            BotCommand("help", "Show help"),
            BotCommand("lang", "Set language: /lang ru or /lang en"),
            BotCommand("cacheinfo", "Show cache status"),
            BotCommand("clearcache", "Clear cache (dev)"),
            BotCommand("stats", "Admin: show basic stats"),
        ])
    except Exception:
        logger.exception("Failed to set bot commands")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Русский 🇷🇺", callback_data="lang:ru"),
        InlineKeyboardButton("English 🇬🇧", callback_data="lang:en"),
    ]])
    await update.message.reply_text("Выберите язык / Choose language:", reply_markup=kb)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await track_user(chat_id)
    lang = await get_user_lang(chat_id)
    text = (
        "/start — Перезапустить бота и выбрать язык\n"
        "/help — Показать это сообщение\n"
        "/lang <ru|en> — Установить язык\n"
        "Просто отправьте название игры, например: Dota 2"
    )
    if lang != "ru":
        text = (
            "/start — Restart bot and choose language\n"
            "/help — Show this message\n"
            "/lang <ru|en> — Set language\n"
            "Just send a game name, e.g. Dota 2"
        )
    await update.message.reply_text(text)


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await track_user(chat_id)
    args = context.args
    if not args or args[0] not in ("ru", "en"):
        await update.message.reply_text("Usage: /lang ru or /lang en")
        return
    await set_user_lang(chat_id, args[0])
    await update.message.reply_text("Язык установлен: Русский." if args[0] == "ru" else "Language set: English.")


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        logger.exception("query.answer failed")

    data = query.data or ""
    chat_id = query.message.chat.id if query.message else query.from_user.id
    await track_user(chat_id)
    logger.info("Callback received: %s from %s", data, query.from_user.id)

    if data.startswith("lang:"):
        lang = data.split(":", 1)[1]
        await set_user_lang(chat_id, lang)
        try:
            if lang == "ru":
                await query.edit_message_text("Язык установлен: Русский 🇷🇺")
            else:
                await query.edit_message_text("Language set: English 🇬🇧")
        except Exception:
            await query.message.reply_text("Язык установлен." if lang == "ru" else "Language set.")
        return

    if data.startswith("game:"):
        try:
            appid = int(data.split(":", 1)[1])
        except Exception:
            await query.edit_message_text("Неверный выбор.")
            return

        lang = await get_user_lang(chat_id)
        details = await get_appdetails(appid, lang)
        players = await get_current_players(appid)
        peaks = await get_peaks(appid)

        image = details.get("header_image")
        text = build_game_text(details, appid, players, peaks, lang)
        try:
            if image:
                await query.message.reply_photo(image, caption=f"{details.get('name','')} — AppID: {appid}")
            await query.message.reply_text(text)
        except Exception:
            logger.exception("Failed to send game message")
            await query.message.reply_text(text)
        return


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await track_user(chat_id)
    lang = await get_user_lang(chat_id)
    query_text = (update.message.text or "").strip()
    if not query_text:
        return

    logger.info("Received search query from %s: %s", chat_id, query_text)
    results = await search_apps(query_text)
    if not results:
        await update.message.reply_text("Игра не найдена на Steam Store." if lang == "ru" else "Game not found on Steam Store.")
        return

    if len(results) > 1:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(r["name"], callback_data=f"game:{r['appid']}")] for r in results[:3]])
        await update.message.reply_text("Найдено несколько вариантов. Выберите:" if lang == "ru" else "Multiple matches found. Choose:", reply_markup=kb)
        return

    chosen = results[0]
    appid = chosen["appid"]
    details = await get_appdetails(appid, lang)
    players = await get_current_players(appid)
    peaks = await get_peaks(appid)
    text = build_game_text(details, appid, players, peaks, lang, query_name=query_text)

    try:
        if details.get("header_image"):
            await update.message.reply_photo(details["header_image"], caption=f"{details.get('name','')} — AppID: {appid}")
            await update.message.reply_text(text)
        else:
            await update.message.reply_text(text)
    except Exception:
        logger.exception("Failed to send message")
        await update.message.reply_text(text)


# ================== Debug / Admin commands ==================
async def cacheinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text("Access denied.")
        return

    keys = []
    if redis_client:
        try:
            # Warning: KEYS * is expensive on large DBs; used for dev/debug only
            keys = await redis_client.keys("*")
            await update.message.reply_text(f"Redis keys: {len(keys)}")
            return
        except Exception:
            logger.exception("Redis keys failed")
    await update.message.reply_text(f"In-memory cache keys: {len(_local_cache)}")


async def clearcache_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text("Access denied.")
        return

    if redis_client:
        try:
            await redis_client.flushdb()
            await update.message.reply_text("Redis flushed.")
            return
        except Exception:
            logger.exception("Redis flush failed")
    _local_cache.clear()
    _local_users.clear()
    await update.message.reply_text("Local cache cleared.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text("Access denied.")
        return

    # unique users
    count = 0
    if redis_client:
        try:
            count = await redis_client.scard("users")
        except Exception:
            logger.exception("Redis SCARD failed")
            count = len(_local_users)
    else:
        count = len(_local_users)

    cache_keys = "N/A"
    if redis_client:
        try:
            cache_keys = await redis_client.dbsize()
        except Exception:
            logger.exception("Redis DBSIZE failed")
            cache_keys = "N/A"
    else:
        cache_keys = len(_local_cache)

    text = f"Unique users: {count}\nCache keys (approx): {cache_keys}"
    await update.message.reply_text(text)


# ================== MAIN ==================
def main():
    if not TOKEN:
        logger.error("BOT_TOKEN is not set. Please set BOT_TOKEN environment variable and restart.")
        sys.exit(1)

    init_redis()

    async def _post_init(app_obj):
        if redis_client:
            try:
                await redis_client.ping()
                logger.info("Connected to Redis at %s", (os.environ.get("REDIS_URL") or REDIS_URL))
            except Exception:
                logger.exception("Redis ping failed — will continue with fallback caches")
        else:
            logger.info("REDIS disabled or not configured — running with in-memory cache.")

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(CommandHandler("cacheinfo", cacheinfo_cmd))
    app.add_handler(CommandHandler("clearcache", clearcache_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
