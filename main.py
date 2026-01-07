# main.py
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
TOKEN = os.environ.get("BOT_TOKEN")
REDIS_URL = os.environ.get("REDIS_URL") or "redis://localhost:6379/0"
CACHE_TTL = int(os.environ.get("CACHE_TTL", "180"))
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "5"))

ADMIN_IDS = {2045900240}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ================== HTTP session ==================
session = requests.Session()
retries = Retry(
    total=4,
    backoff_factor=0.4,
    status_forcelist=[429, 500, 502, 503, 504],
)
session.mount("https://", HTTPAdapter(max_retries=retries))
session.headers.update({"User-Agent": "Mozilla/5.0 (SteamInfoBot)"})

# ================== Fallback stores ==================
_local_cache = {}
_local_user_settings = {}
_local_users = set()

# ================== Redis ==================
redis_client = None


def init_redis():
    global redis_client
    if Redis is None:
        logger.warning("redis.asyncio not available — fallback only")
        return

    if not REDIS_URL:
        logger.info("REDIS_URL not set — fallback only")
        return

    try:
        redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
        logger.info(
            "Initialized Redis client at %s (lazy connection)",
            REDIS_URL
        )
    except Exception:
        logger.exception("Redis init failed — fallback only")
        redis_client = None


# ================== Cache helpers ==================
async def cache_get(key):
    if redis_client:
        try:
            val = await redis_client.get(key)
            return json.loads(val) if val else None
        except Exception:
            logger.exception("Redis GET failed")
    ent = _local_cache.get(key)
    if not ent:
        return None
    if time.time() - ent["time"] > CACHE_TTL:
        _local_cache.pop(key, None)
        return None
    return ent["value"]


async def cache_set(key, value):
    if redis_client:
        try:
            await redis_client.setex(key, CACHE_TTL, json.dumps(value))
            return
        except Exception:
            logger.exception("Redis SET failed")
    _local_cache[key] = {"time": time.time(), "value": value}


# ================== User helpers ==================
async def get_user_lang(chat_id):
    if redis_client:
        try:
            return await redis_client.hget("user_settings", chat_id) or "ru"
        except Exception:
            pass
    return _local_user_settings.get(chat_id, {}).get("lang", "ru")


async def set_user_lang(chat_id, lang):
    if redis_client:
        try:
            await redis_client.hset("user_settings", chat_id, lang)
            return
        except Exception:
            pass
    _local_user_settings.setdefault(chat_id, {})["lang"] = lang


async def track_user(chat_id):
    if redis_client:
        try:
            await redis_client.sadd("users", chat_id)
            return
        except Exception:
            pass
    _local_users.add(chat_id)


# ================== MAIN ==================
import asyncio

async def main():
    if not TOKEN:
        logger.error("BOT_TOKEN is not set")
        raise RuntimeError("BOT_TOKEN is not set")

    init_redis()

    async def post_init(app):
        if redis_client:
            try:
                await redis_client.ping()
                logger.info("Redis connected")
            except Exception:
                logger.exception("Redis ping failed")
        else:
            logger.info("Redis disabled, using in-memory cache")

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # handlers — БЕЗ ИЗМЕНЕНИЙ
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(CommandHandler("cacheinfo", cacheinfo_cmd))
    app.add_handler(CommandHandler("clearcache", clearcache_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    await app.initialize()
    await app.start()

    logger.info("Bot started and polling")

    # держим процесс живым (Railway этого требует)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
