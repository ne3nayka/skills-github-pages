import asyncio
import logging
import sys
import os
from datetime import datetime, timedelta
import json
from typing import Dict, Any, Optional

import aiosqlite
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.web_app import check_webapp_signature
from quart import Quart, jsonify, request, send_from_directory, Response
from quart_cors import cors
import requests
from redis.asyncio import ConnectionError
import backoff
from logging.handlers import RotatingFileHandler
import pytz
import hmac
import hashlib
import urllib.parse
import hypercorn
from hypercorn.config import Config
from hypercorn.asyncio import serve

from config import (
    BOT_TOKEN, ROLES, ADMIN_ROLE, LOG_LEVEL, ADMIN_IDS,
    ROLE_MAPPING, WEBAPP_URL, DB_NAME, DB_TIMEOUT, PORT,
    WEBHOOK_PATH, CATEGORIES
)
from database import init_db
from products import check_expired_products_without_final_price
from registration import router as registration_router, Registration
from expiration import router as expiration_router, check_expired_items
from profile import register_handlers as register_profile_handlers
from user_requests import register_handlers as register_request_handlers
from common import register_handlers as register_common_handlers
from admin import register_handlers as register_admin_handlers, AdminStates
from utils import (
    check_role, make_keyboard, MONTHS_UZ, check_subscription,
    format_uz_datetime, parse_uz_datetime,
    get_main_menu, get_admin_menu, notify_admin, invalidate_cache
)

WEBHOOK_URL = f"{WEBAPP_URL}{WEBHOOK_PATH}"

# Глобальные переменные
bot: Bot = None
dp: Dispatcher = None

# Настройка логирования
class UzbekDateFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=pytz.UTC)
        eng_date = dt.strftime("%d %B %Y йил %H:%M:%S")
        for eng, uz in MONTHS_UZ.items():
            eng_date = eng_date.replace(eng, uz)
        return eng_date

logger = logging.getLogger(__name__)
log_level = getattr(logging, LOG_LEVEL.upper(), logging.DEBUG)
file_handler = RotatingFileHandler(
    '/home/mbozor/Mbozor/webhook.log',
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
    encoding='utf-8'
)
file_handler.setLevel(log_level)
file_handler.setFormatter(UzbekDateFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(log_level)
console_handler.setFormatter(UzbekDateFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=log_level, handlers=[file_handler, console_handler])

logging.Formatter.converter = lambda *args: datetime.now(pytz.timezone('Asia/Tashkent')).timetuple()

# Инициализация Quart приложения
app = Quart(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # 1 MB
app = cors(app, allow_origin=["http://159.65.7.40", "https://web.telegram.org"], allow_methods=["GET", "POST"],
           allow_headers=["X-Telegram-Init-Data"], allow_credentials=False)
logger.info(f"CORS настроен для: http://159.65.7.40, https://web.telegram.org")

# Проверка наличия необходимых файлов
def check_required_files():
    required_files = [
        '/home/mbozor/Mbozor/webapp.html'
    ]
    for path in required_files:
        if not os.path.exists(path):
            logger.critical(f"Отсутствует файл: {path}")
            raise FileNotFoundError(f"Required file not found: {path}")

# Функция для ручной проверки подписи initData
def manual_check_webapp_signature(token: str, init_data: str) -> bool:
    if not init_data:
        logger.error("Пустой initData")
        return False
    try:
        parsed_data = urllib.parse.parse_qs(init_data)
        if not parsed_data.get('hash'):
            logger.error("initData не содержит hash")
            return False
        received_hash = parsed_data['hash'][0]
        data_check_string = '\n'.join(
            f"{key}={value[0]}" for key, value in sorted(parsed_data.items()) if key != 'hash'
        )
        secret_key = hmac.new(
            b"WebAppData",
            token.encode('utf-8'),
            hashlib.sha256
        ).digest()
        computed_hash = hmac.new(
            secret_key,
            data_check_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        logger.debug(f"manual_check: data_check_string='{data_check_string}', computed_hash='{computed_hash}', received_hash='{received_hash}', init_data='{init_data}'")
        return computed_hash == received_hash
    except Exception as e:
        logger.error(f"Ошибка ручной проверки подписи: {e}, init_data='{init_data}'")
        return False

# Обработчик маршрута для Web App
@app.route('/', methods=['GET', 'HEAD'])
async def serve_webapp():
    logger.info("Запрос on /")
    headers = dict(request.headers)
    init_data = request.headers.get("X-Telegram-Init-Data", "Not provided")
    logger.debug(f"serve_webapp: remote_addr={request.remote_addr}, User-Agent={request.user_agent}, Headers={headers}, Init-Data={init_data}")
    try:
        response = await send_from_directory('/home/mbozor/Mbozor', 'webapp.html')
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        response.headers['Cache-Control'] = 'public, max-age=3600'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' https://telegram.org; "
            "style-src 'self'; "
            "img-src 'self' data: https://api.telegram.org; "
            "connect-src 'self' http://159.65.7.40 https://api.telegram.org"
        )
        logger.debug("Файл webapp.html успешно отправлен")
        return response
    except FileNotFoundError:
        logger.error("Файл webapp.html не найден")
        await notify_admin("Файл webapp.html не найден", bot=bot)
        return jsonify({"error": "Webapp file not found"}), 404
    except Exception as e:
        logger.error(f"Ошибка при отправке webapp.html: {e}", exc_info=True)
        await notify_admin(f"Ошибка при отправке webapp.html: {str(e)}", bot=bot)
        return jsonify({"error": "Server error", "details": str(e)}), 500

# Обработчик маршрута для статических файлов
@app.route('/static/<path:filename>', methods=['GET', 'HEAD'])
async def serve_static(filename):
    safe_filename = filename
    if request.method == 'HEAD':
        response = Response(status=200)
        response.headers['Cache-Control'] = 'public, max-age=86400'
        logger.debug(f"HEAD запрос на статический файл {safe_filename} обработан")
        return response
    logger.info(f"GET запрос на статический файл: {safe_filename}")
    try:
        response = await send_from_directory('/home/mbozor/Mbozor/static', safe_filename)
        response.headers['Cache-Control'] = 'public, max-age=86400'
        logger.debug(f"Статический файл {safe_filename} успешно отправлен")
        return response
    except FileNotFoundError:
        logger.error(f"Статический файл {safe_filename} не найден")
        await notify_admin(f"Статический файл {safe_filename} не найден", bot=bot)
        return jsonify({"error": "File not found"}), 404
    except Exception as e:
        logger.error(f"Ошибка при отправке статического файла {safe_filename}: {e}", exc_info=True)
        await notify_admin(f"Ошибка при отправке статического файла {safe_filename}: {str(e)}", bot=bot)
        return jsonify({"error": "Server error", "details": str(e)}), 500

# Подключение к Redis
@backoff.on_exception(backoff.expo, ConnectionError, max_tries=5)
async def connect_redis():
    logger.info("Попытка подключения к Redis")
    try:
        storage = RedisStorage.from_url("redis://localhost:6379/0")
        await storage.redis.ping()
        logger.info("Успешно подключено к RedisStorage")
        return storage
    except ConnectionError as e:
        logger.warning(f"Не удалось подключиться к Redis: {e}. Используется MemoryStorage.")
        await notify_admin(f"Не удалось подключиться к Redis: {str(e)}", bot=bot)
        return MemoryStorage()

# Middleware для проверки подписки
class SubscriptionCheckMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.Message | types.CallbackQuery, data: dict) -> None:
        user_id = event.from_user.id
        state = data["state"]
        current_state = await state.get_state()
        event_text = event.text if isinstance(event, types.Message) else event.data
        logger.info(f"Middleware processing: user_id={user_id}, chat_id={event.chat.id if hasattr(event, 'chat') else 'unknown'}, event_text={event_text}, state={current_state}, handler={handler.__name__}")

        data["dp"] = self.dp  # Передаём Dispatcher в data

        if user_id in ADMIN_IDS:
            logger.debug(f"Админ {user_id} пропущен")
            await handler(event, data)
            return

        if isinstance(event, types.Message) and event.text == "/start":
            logger.debug(f"Пропуск проверки для команды /start от user_id={user_id}, вызов обработчика {handler.__name__}")
            await state.clear()
            await handler(event, data)
            return

        protected_states = [
            "Registration:start", "Registration:phone", "Registration:role", "Registration:region",
            "Registration:district", "Registration:company_name", "Registration:subscription",
            "DeleteRequest:delete_request", "CloseRequest:select_request",
            "CloseRequest:awaiting_final_price", "ExpiredItem:choice", "ExpiredItem:final_price",
            "RequestsMenu:menu", "ProfileStates:main", "EditProfile:region", "EditProfile:district",
            "EditProfile:company_name",
            "CloseProduct:select_item", "CloseProduct:final_price"  # Разрешаем состояния для закрытия объявления
        ]
        if isinstance(event, types.CallbackQuery) or current_state in protected_states:
            logger.debug(f"Пропуск проверки для callback или состояния {current_state}")
            await handler(event, data)
            return

        allowed_commands = [
            "/myid", "/subscribe", "/reset", "Обуна", "Рўйхатдан ўтиш",
            "Эълонни ёпиш"  # Разрешаем команду для закрытия объявления
        ]
        if isinstance(event, types.Message) and event.text in allowed_commands:
            logger.debug(f"Пропуск проверки для команды '{event_text}'")
            await handler(event, data)
            return

        allowed, role = await check_role(event, allow_unregistered=True)
        if not allowed or not role:
            logger.info(f"Незарегистрированный пользователь {user_id} перенаправлен на регистрацию")
            try:
                await event.answer(
                    "Илтимос, рўйхатдан ўтинг:",
                    reply_markup=make_keyboard(["Рўйхатдан ўтиш"], one_time=True)
                )
                await state.set_state(Registration.start)
            except Exception as e:
                logger.error(f"Ошибка отправки ответа для user_id={user_id}: {e}")
                await notify_admin(f"Ошибка отправки ответа для user_id={user_id}: {str(e)}", bot=data["bot"])
            return

        if role != ADMIN_ROLE:
            # Проверка истёкших объявлений без final_price
            try:
                has_expired_products = await check_expired_products_without_final_price(data["bot"], user_id)
                if has_expired_products:
                    logger.info(f"User {user_id} has expired products without final_price, restricting access")
                    try:
                        await event.answer(
                            "Сизда якуний нарх киритилмаган эълонлар мавжуд! Илтимос, 'Эълонни ёпиш' тугмасини босинг:",
                            reply_markup=make_keyboard(["Эълонни ёпиш"], one_time=True)
                        )
                        await state.set_state("CloseProduct:select_item")
                        return
                    except Exception as e:
                        logger.error(f"Ошибка отправки ответа о необходимости final_price для user_id={user_id}: {e}")
                        await notify_admin(f"Ошибка отправки ответа о необходимости final_price для user_id={user_id}: {str(e)}", bot=data["bot"])
                        return
            except Exception as e:
                logger.error(f"Ошибка проверки истёкших объявлений для user_id={user_id}: {e}")
                await notify_admin(f"Ошибка проверки истёкших объявлений для user_id={user_id}: {str(e)}", bot=data["bot"])

            # Проверка подписки
            try:
                _, bot_active, is_subscribed = await check_subscription(data["bot"], user_id, data["dp"].storage)
                logger.debug(f"Подписка: user_id={user_id}, bot_active={bot_active}, is_subscribed={is_subscribed}")
                if not is_subscribed:
                    try:
                        await event.answer(
                            "Сизда фаол обуна мавжуд эмас. 'Обуна' тугмасини босинг:",
                            reply_markup=make_keyboard(["Обуна", "Орқага"], columns=2, one_time=True)
                        )
                        await state.set_state(Registration.subscription)
                        logger.info(f"Пользователь {user_id} перенаправлен на подписку")
                        return
                    except Exception as e:
                        logger.error(f"Ошибка отправки ответа о подписке для user_id={user_id}: {e}")
                        await notify_admin(f"Ошибка отправки ответа о подписке для user_id={user_id}: {str(e)}", bot=data["bot"])
                        return
            except Exception as e:
                logger.error(f"Ошибка проверки подписки для user_id={user_id}: {e}", exc_info=True)
                await notify_admin(f"Ошибка проверки подписки для user_id={user_id}: {str(e)}", bot=data["bot"])
                try:
                    await event.answer("Хатолик юз берди. Админ билан боғланинг (@ad_mbozor).")
                except Exception as e2:
                    logger.error(f"Ошибка отправки сообщения об ошибке для user_id={user_id}: {e2}")
                return

        await handler(event, data)

async def open_webapp(message: types.Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    current_state = await state.get_state()
    logger.info(f"open_webapp: user_id={user_id}, text='{message.text}', state={current_state}, WEBAPP_URL={WEBAPP_URL}")
    allowed, role = await check_role(message, allow_unregistered=True)
    if not allowed or not role:
        await message.answer(
            "Илтимос, рўйхатдан ўтинг:",
            reply_markup=make_keyboard(["Рўйхатдан ўтиш"], one_time=True)
        )
        await state.set_state(Registration.start)
        logger.info(f"Незарегистрированный пользователь {user_id} пытался открыть Web App")
        return
    display_role = {v: k for k, v in ROLE_MAPPING.items()}.get(role, role)
    # Отправляем подтверждение с главным меню
    reply_markup = get_main_menu(display_role)
    await message.answer(
        "Эълонлар доскаси очиляпти...",
        reply_markup=reply_markup
    )
    logger.info(f"Пользователь {user_id} (роль: {display_role}) открыл Web App по URL: {WEBAPP_URL}")

async def handle_back(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    allowed, role = await check_role(callback, allow_unregistered=True)
    display_role = {v: k for k, v in ROLE_MAPPING.items()}.get(role, role) if role else None
    await callback.message.edit_text("Асосий меню:", reply_markup=get_main_menu(display_role))
    await state.clear()
    logger.info(f"Пользователь {user_id} вернулся в главное меню")

async def handle_webapp_close(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    web_app_data = message.web_app_data.data
    current_state = await state.get_state()
    logger.info(f"handle_webapp_close: user_id={user_id}, data='{web_app_data}', state={current_state}")

    if not web_app_data:
        logger.warning(f"Пустые данные Web App от user_id={user_id}")
        return

    try:
        data = json.loads(web_app_data)
    except json.JSONDecodeError:
        logger.warning(f"Неверный формат JSON от user_id={user_id}: '{web_app_data}'")
        await message.answer("Неверный формат данных.", reply_markup=get_main_menu(None))
        return

    allowed, role = await check_role(message, allow_unregistered=True)
    if not allowed or not role:
        await message.answer(
            "Илтимос, рўйхатдан ўтинг:",
            reply_markup=make_keyboard(["Рўйхатдан ўтиш"], one_time=True)
        )
        await state.set_state(Registration.start)
        return

    if data.get("closed"):
        await message.answer("Асосий меню:", reply_markup=get_main_menu(role))
        await state.clear()
        logger.info(f"Пользователь {user_id} возвращен в главное меню из Web App")
    else:
        logger.warning(f"Неизвестные данные Web App от user_id={user_id}: '{web_app_data}'")
        await message.answer("Неверный формат данных.", reply_markup=get_main_menu(role))

async def get_active_data(table: str, bot: Optional[object] = None) -> Dict[str, any]:
    """
    Jadvaldan (products yoki requests) status='active' bo‘lgan ma’lumotlarni oladi, сортируя по created_at DESC.
    """
    if table not in ["products", "requests"]:
        logger.error(f"Недопустимая таблица: {table}")
        return {"items": [], "total": 0}

    try:
        async with aiosqlite.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
            query = f"""
                SELECT p.*, u.region, u.phone_number
                FROM {table} p
                JOIN users u ON p.user_id = u.id
                WHERE p.status = 'active'
                ORDER BY p.created_at DESC
            """
            count_query = query.replace("SELECT p.*, u.region", "SELECT COUNT(*)").replace("ORDER BY p.created_at DESC", "")

            async with conn.execute(count_query) as cursor:
                total = (await cursor.fetchone())[0]

            async with conn.execute(query) as cursor:
                items = await cursor.fetchall()
                columns = [desc[0] for desc in cursor.description]
                result = [dict(zip(columns, item)) for item in items]

            for item in result:
                if "created_at" in item and item["created_at"]:
                    created_at_dt = parse_uz_datetime(item["created_at"])
                    if created_at_dt:
                        item["created_at"] = format_uz_datetime(created_at_dt)
                    else:
                        logger.error(f"Ошибка парсинга created_at: {item['created_at']}")
                        item["created_at"] = "Не указано"
                if "archived_at" in item and item["archived_at"]:
                    archived_at_dt = parse_uz_datetime(item["archived_at"])
                    if archived_at_dt:
                        item["archived_at"] = format_uz_datetime(archived_at_dt)
                    else:
                        logger.error(f"Ошибка парсинга archived_at: {item['archived_at']}")
                        item["archived_at"] = "Не указано"
                if "notified" in item:
                    item["notified"] = bool(item["notified"])
                if "photos" in item and item["photos"]:
                    item["photos"] = item["photos"].split(",") if item["photos"] else []

            logger.debug(f"Fetched {len(result)} items from {table}, total: {total}")
            return {"items": result, "total": total}

    except aiosqlite.Error as e:
        logger.error(f"Ошибка загрузки данных из {table}: {e}", exc_info=True)
        if bot:
            await notify_admin(f"Ошибка загрузки данных из {table}: {str(e)}", bot=bot)
        return {"items": [], "total": 0}

async def get_all_data(
        table: str,
        storage: Optional[BaseStorage] = None,
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 20,
        category: Optional[str] = None,
        region: Optional[str] = None,
        search: Optional[str] = None,
        bot: Optional[Bot] = None
) -> Dict[str, Any]:
    if table not in ["products", "requests"]:
        logger.error(f"Недопустимая таблица: {table}")
        return {"items": [], "total": 0}
    offset = (page - 1) * per_page
    cache_key = f"cache:{table}:{status}:{page}:{per_page}:{category}:{region}:{search}"
    try:
        if storage and hasattr(storage, 'redis'):
            await invalidate_cache(storage, table)
            cached = await storage.redis.get(cache_key)
            if cached:
                logger.debug(f"Данные из кэша для {cache_key}")
                return json.loads(cached)
    except Exception as e:
        logger.warning(f"Redis недоступен для {cache_key}: {e}")

    try:
        async with aiosqlite.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
            params = [(datetime.now(pytz.UTC) - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')]
            query = f"""
                SELECT p.*, u.region, u.phone_number
                FROM {table} p
                JOIN users u ON p.user_id = u.id
                WHERE p.status != 'hidden' AND p.created_at >= ?
            """
            if status:
                query += " AND p.status = ?"
                params.append(status)
            if category and category in CATEGORIES:
                query += " AND p.category = ?"
                params.append(category)
            if region:
                query += " AND u.region = ?"
                params.append(region)
            if search:
                query += " AND (p.sort LIKE ? OR p.category LIKE ?)"
                search_term = f"%{search}%"
                params.extend([search_term, search_term])
            count_query = query.replace("SELECT p.*, u.region", "SELECT COUNT(*)")
            query += " ORDER BY p.created_at DESC LIMIT ? OFFSET ?"
            params.extend([per_page, offset])

            async with conn.execute(count_query, params[:-2]) as cursor:
                total = (await cursor.fetchone())[0]
            async with conn.execute(query, params) as cursor:
                items = await cursor.fetchall()
                columns = [desc[0] for desc in cursor.description]
                result = [dict(zip(columns, item)) for item in items]

            for item in result:
                if "created_at" in item and item["created_at"]:
                    created_at_dt = parse_uz_datetime(item["created_at"])
                    item["created_at"] = format_uz_datetime(created_at_dt) if created_at_dt else "Не указано"
                if "archived_at" in item and item["archived_at"]:
                    archived_at_dt = parse_uz_datetime(item["archived_at"])
                    item["archived_at"] = format_uz_datetime(archived_at_dt) if archived_at_dt else "Не указано"
                if "notified" in item:
                    item["notified"] = bool(item["notified"])
                if "photos" in item and item["photos"]:
                    item["photos"] = item["photos"].split(",") if item["photos"] else []

            response = {"items": result, "total": total}
            try:
                if storage and hasattr(storage, 'redis'):
                    await storage.redis.setex(cache_key, 300, json.dumps(response))
            except Exception as e:
                logger.warning(f"Redis недоступен для кэширования {cache_key}: {e}")
            return response
    except aiosqlite.Error as e:
        logger.error(f"Ошибка загрузки данных из {table}: {e}", exc_info=True)
        await notify_admin(f"Ошибка загрузки данных из {table}: {str(e)}", bot=bot)
        return {"items": [], "total": 0}

@app.route('/api/all_active_products')
async def get_all_active_products():
    logger.info(f"Запрос на /api/all_active_products, User-Agent: {request.headers.get('User-Agent')}, IP: {request.remote_addr}")
    try:
        products = await get_active_data("products")
        logger.info(f"Возвращено {len(products['items'])} продуктов, всего: {products['total']}")
        return jsonify(products), 200
    except Exception as e:
        logger.error(f"Ошибка обработки /api/all_active_products: {e}", exc_info=True)
        if bot:
            await notify_admin(f"Ошибка обработки /api/all_active_products: {str(e)}", bot=bot)
        return jsonify({"error": "Server error", "details": str(e)}), 500

@app.route('/api/all_active_requests')
async def get_all_active_requests():
    logger.info(f"Запрос на /api/all_active_requests, User-Agent: {request.headers.get('User-Agent')}, IP: {request.remote_addr}")
    try:
        requests_data = await get_active_data("requests")
        logger.info(f"Возвращено {len(requests_data['items'])} запросов, всего: {requests_data['total']}")
        return jsonify(requests_data), 200
    except Exception as e:
        logger.error(f"Ошибка обработки /api/all_active_requests: {e}", exc_info=True)
        if bot:
            await notify_admin(f"Ошибка обработки /api/all_active_requests: {str(e)}", bot=bot)
        return jsonify({"error": "Server error", "details": str(e)}), 500
@app.route('/api/all_products')
async def get_all_products():
    logger.info(f"Запрос на /api/all_products, args={request.args}")
    init_data = request.headers.get("X-Telegram-Init-Data", "Not provided")
    headers = dict(request.headers)
    logger.debug(f"get_all_products: init_data='{init_data}', length={len(init_data) if init_data != 'Not provided' else 0}, remote_addr={request.remote_addr}, Headers={headers}")
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))
        category = request.args.get('category')
        region = request.args.get('region')
        search = request.args.get('search')
        products = await get_all_data("products", dp.storage if dp else None, "active", page, per_page, category, region, search, bot=bot)
        logger.info(f"Возвращено {len(products['items'])} продуктов, всего: {products['total']}")
        return jsonify(products), 200
    except Exception as e:
        logger.error(f"Ошибка обработки /api/all_products: {e}", exc_info=True)
        await notify_admin(f"Ошибка обработки /api/all_products: {str(e)}", bot=bot)
        return jsonify({"error": "Server error", "details": str(e)}), 500

@app.route('/api/all_requests')
async def get_all_requests():
    logger.info(f"Запрос на /api/all_requests, args={request.args}")
    init_data = request.headers.get("X-Telegram-Init-Data", "Not provided")
    headers = dict(request.headers)
    logger.debug(f"get_all_requests: init_data='{init_data}', length={len(init_data) if init_data != 'Not provided' else 0}, remote_addr={request.remote_addr}, Headers={headers}")
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))
        category = request.args.get('category')
        region = request.args.get('region')
        search = request.args.get('search')
        requests_data = await get_all_data("requests", dp.storage if dp else None, "active", page, per_page, category, region, search, bot=bot)
        logger.info(f"Возвращено {len(requests_data['items'])} запросов, всего: {requests_data['total']}")
        return jsonify(requests_data), 200
    except Exception as e:
        logger.error(f"Ошибка обработки /api/all_requests: {e}", exc_info=True)
        await notify_admin(f"Ошибка обработки /api/all_requests: {str(e)}", bot=bot)
        return jsonify({"error": "Server error", "details": str(e)}), 500

@app.route('/api/archive')
async def get_archive():
    logger.info(f"Запрос на /api/archive, args={request.args}")
    init_data = request.headers.get("X-Telegram-Init-Data", "Not provided")
    headers = dict(request.headers)
    logger.debug(f"get_archive: init_data='{init_data}', length={len(init_data) if init_data != 'Not provided' else 0}, remote_addr={request.remote_addr}, Headers={headers}")
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))
        category = request.args.get('category')
        region = request.args.get('region')
        search = request.args.get('search')
        archived_products = await get_all_data("products", dp.storage if dp else None, "archived", page, per_page, category, region, search, bot=bot)
        archived_requests = await get_all_data("requests", dp.storage if dp else None, "archived", page, per_page, category, region, search, bot=bot)
        archived = {"items": archived_products["items"] + archived_requests["items"],
                    "total": archived_products["total"] + archived_requests["total"]}
        logger.info(f"Возвращено {len(archived['items'])} архивных записей, всего: {archived['total']}")
        return jsonify(archived), 200
    except Exception as e:
        logger.error(f"Ошибка обработки /api/archive: {e}", exc_info=True)
        await notify_admin(f"Ошибка обработки /api/archive: {str(e)}", bot=bot)
        return jsonify({"error": "Server error", "details": str(e)}), 500



@app.route('/api/get_user_phone')
async def get_user_phone():
    logger.info(f"Запрос на /api/get_user_phone, args={request.args}")
    init_data = request.headers.get("X-Telegram-Init-Data", "Not provided")
    headers = dict(request.headers)
    logger.debug(f"get_user_phone: init_data='{init_data}', length={len(init_data) if init_data != 'Not provided' else 0}, remote_addr={request.remote_addr}, Headers={headers}")
    user_id = request.args.get('user_id')
    if not user_id:
        logger.error("Отсутствует user_id")
        return jsonify({"error": "User ID is required"}), 400
    try:
        user_id_int = int(user_id)
        async with aiosqlite.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
            async with conn.execute(
                    "SELECT phone_number, region FROM users WHERE id = ?", (user_id_int,)
            ) as cursor:
                result = await cursor.fetchone()
        if result:
            logger.info(f"Возвращен номер телефона для user_id={user_id}")
            return jsonify({"phone_number": result[0], "region": result[1] or "Не указан"}), 200
        logger.warning(f"Пользователь user_id={user_id} не найден")
        return jsonify({"error": "User not found"}), 404
    except ValueError:
        logger.error(f"Некорректный user_id: {user_id}")
        return jsonify({"error": "Invalid user_id format"}), 400
    except aiosqlite.Error as e:
        logger.error(f"Ошибка базы данных для user_id={user_id}: {e}")
        await notify_admin(f"Ошибка базы данных для user_id={user_id}: {str(e)}", bot=bot)
        return jsonify({"error": "Database error"}), 500

@app.route(WEBHOOK_PATH, methods=['POST'])
@backoff.on_exception(backoff.expo, Exception, max_tries=3)
async def webhook_handler():
    logger.debug(f"Получен вебхук-запрос, headers={request.headers}, remote_addr={request.remote_addr}, content_length={request.content_length}")
    if dp is None:
        logger.error("Dispatcher не инициализирован")
        await notify_admin("Критическая ошибка: Dispatcher не инициализирован", bot=bot)
        return jsonify({"ok": False, "error": "Dispatcher not initialized"}), 500

    try:
        raw_data = await request.get_data(as_text=True)
        logger.debug(f"Webhook raw_data={raw_data}")
        update_data = await request.get_json()
        logger.debug(f"Webhook update_data={update_data}")
        if not update_data:
            logger.warning(f"Пустой вебхук-запрос, raw_data={raw_data}, remote_addr={request.remote_addr}, headers={request.headers}")
            return jsonify({"ok": True}), 200

        update_id = update_data.get('update_id')
        logger.debug(f"Webhook update_id={update_id}")
        if not update_id:
            logger.warning(f"Отсутствует update_id в запросе: {update_data}")
            return jsonify({"ok": True}), 200

        async with aiosqlite.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
            async with conn.execute(
                    "SELECT update_id FROM processed_updates WHERE update_id = ?", (update_id,)
            ) as cursor:
                if await cursor.fetchone():
                    logger.debug(f"Повторный update_id={update_id}, пропущен")
                    return jsonify({"ok": True}), 200
            await conn.execute("INSERT INTO processed_updates (update_id) VALUES (?)", (update_id,))
            await conn.commit()

        logger.debug(f"Передача обновления в Dispatcher: update_id={update_id}")
        await dp.feed_raw_update(bot, update_data)
        logger.info(f"Обработан update_id={update_id}")
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}, raw_data={raw_data}", exc_info=True)
        await notify_admin(f"Ошибка обработки вебхука: {str(e)}", bot=bot)
        return jsonify({"ok": False, "error": str(e)}), 500

async def set_bot_commands(bot: Bot) -> None:
    logger.info("Установка команд бота")
    try:
        commands = [
            BotCommand(command="/start", description="Ботни бошлаш"),
            BotCommand(command="/myid", description="Сизнинг ID'ингизни кўрсатиш"),
            BotCommand(command="/subscribe", description="Обуна бўлиш"),
            BotCommand(command="/reset", description="Состояние бота скинуть")
        ]
        await bot.set_my_commands(commands)
        logger.info("Команды бота успешно установлены")
    except Exception as e:
        logger.error(f"Ошибка установки команд бота: {e}", exc_info=True)
        await notify_admin(f"Ошибка установки команд бота: {str(e)}", bot=bot)
        raise

async def on_startup(bot: Bot) -> None:
    logger.info("Запуск on_startup")
    try:
        logger.info("Инициализация базы данных")
        await init_db(bot=bot)
        logger.info("База данных успешно инициализирована")
    except Exception as e:
        logger.critical(f"Ошибка инициализации базы данных: {e}", exc_info=True)
        await notify_admin(f"Критическая ошибка: Ошибка инициализации базы данных: {str(e)}", bot=bot)
        raise

    try:
        webhook_info = await bot.get_webhook_info()
        logger.debug(f"Текущий вебхук: {webhook_info.url}")
        if webhook_info.url != WEBHOOK_URL:
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_webhook(
                url=WEBHOOK_URL,
                drop_pending_updates=True
            )
            logger.info(f"Вебхук установлен: {WEBHOOK_URL}")
        else:
            logger.info(f"Вебхук уже установлен: {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"Ошибка установки вебхука: {e}", exc_info=True)
        await notify_admin(f"Ошибка установки вебхука: {str(e)}", bot=bot)
        raise

async def on_shutdown(bot: Bot, dp: Dispatcher, expired_items_task: Optional[asyncio.Task] = None) -> None:
    logger.info("Запуск on_shutdown")
    try:
        if expired_items_task and not expired_items_task.done():
            expired_items_task.cancel()
            try:
                await expired_items_task
            except asyncio.CancelledError:
                logger.info("Фоновая задача check_expired_items отменена")

        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and task is not expired_items_task]
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Вебхук удалён")

        await dp.storage.close()
        logger.info("Хранилище закрыто")

        await bot.session.close()
        logger.info("Сессия бота закрыта")
    except Exception as e:
        logger.error(f"Ошибка при завершении работы: {e}", exc_info=True)
        await notify_admin(f"Ошибка при завершении работы: {str(e)}", bot=bot)

async def main():
    global bot, dp
    logger.info("Запуск бота")
    try:
        check_required_files()
        logger.debug("Все необходимые файлы найдены")
    except Exception as e:
        logger.critical(f"Ошибка проверки файлов: {e}", exc_info=True)
        raise

    try:
        logger.info("Инициализация бота")
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        logger.info("Бот успешно инициализирован")
    except Exception as e:
        logger.critical(f"Ошибка инициализации бота: {e}", exc_info=True)
        raise

    try:
        storage = await connect_redis()
        dp = Dispatcher(bot=bot, storage=storage)
        logger.info(f"Dispatcher инициализирован: storage={type(storage).__name__}")
    except Exception as e:
        logger.critical(f"Ошибка инициализации диспетчера или Redis: {e}", exc_info=True)
        raise

    try:
        middleware = SubscriptionCheckMiddleware()
        middleware.dp = dp
        dp.message.middleware(middleware)
        dp.callback_query.middleware(middleware)
        logger.debug("Middleware зарегистрированы для сообщений и callback-запросов")

        dp.include_router(registration_router)
        logger.debug("Зарегистрирован роутер registration_router")
        dp.include_router(expiration_router)
        logger.debug("Зарегистрирован роутер expiration_router")
        from products import register_handlers as products_register_handlers
        products_register_handlers(dp)
        logger.debug("Зарегистрированы обработчики products")
        register_profile_handlers(dp)
        logger.debug("Зарегистрированы обработчики profile")
        register_request_handlers(dp)
        logger.debug("Зарегистрированы обработчики user_requests")
        register_common_handlers(dp)
        logger.debug("Зарегистрированы обработчики common")
        register_admin_handlers(dp)
        logger.debug("Зарегистрированы обработчики admin")

        dp.message.register(open_webapp, F.text == "Эълонлар доскаси")
        logger.debug("Зарегистрирован обработчик для команды 'Эълонлар доскаси'")
        dp.message.register(handle_webapp_close, F.web_app_data)
        logger.debug("Зарегистрирован обработчик для закрытия Web App")
        dp.callback_query.register(handle_back, F.data == "back")
        logger.debug("Зарегистрирован обработчик для callback 'back'")
    except Exception as e:
        logger.critical(f"Ошибка регистрации middleware или роутеров: {e}", exc_info=True)
        raise

    try:
        expired_items_task = asyncio.create_task(check_expired_items(bot, storage))
        logger.info("Запуск проверки истёкших элементов")
    except Exception as e:
        logger.critical(f"Ошибка запуска фоновой задачи check_expired_items: {e}", exc_info=True)
        raise

    try:
        await set_bot_commands(bot)
    except Exception as e:
        logger.critical(f"Ошибка установки команд бота: {e}", exc_info=True)
        raise

    try:
        await on_startup(bot)
    except Exception as e:
        logger.critical(f"Ошибка в on_startup: {e}", exc_info=True)
        raise

    try:
        config = Config()
        config.bind = [f"127.0.0.1:{PORT}"]
        config.reuse_port = True
        config.graceful_timeout = 5
        config.accesslog = "/home/mbozor/Mbozor/webhook.log"
        config.errorlog = "/home/mbozor/Mbozor/webhook.log"
        config.loglevel = "DEBUG"
        logger.info(f"Конфигурация Hypercorn: bind={config.bind}, loglevel={config.loglevel}")
        await serve(app, config)
    except Exception as e:
        logger.critical(f"Ошибка запуска сервера на порту {PORT}: {e}", exc_info=True)
        await notify_admin(f"Ошибка запуска сервера на порту {PORT}: {str(e)}", bot=bot)
        raise
    finally:
        await on_shutdown(bot, dp, expired_items_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.critical(f"Критическая ошибка в main: {e}", exc_info=True)
        raise