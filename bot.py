"""Telegram bot for monitoring fuel availability on nearby gas stations.

Uses gdebenz.ru API to check for fuel and notifies users when their
desired fuel type becomes available at nearby stations.

Notification strategy:
- Every 5 minutes, fetch all matching stations for each active user.
- Compare with previously sent messages (stored in DB with message_id).
- New stations → send new message with "Open in maps" button.
- Stations that lost fuel → delete the old message.
- Stations that still have fuel → update the existing message (edit).
"""

import asyncio
import logging
import os
import re
import sys
import time
import traceback

import aiohttp
import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import cache
import checker
import db

# --- Configuration ---
TOKEN = os.environ.get("BOT_TOKEN", "")
CHECK_INTERVAL_SECONDS = 300  # Check every 5 minutes
MAX_STATIONS_PER_NOTIFICATION = 5
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(__file__))
SOCKS_PROXY = os.environ.get("SOCKS_PROXY", "")  # e.g. socks5://127.0.0.1:1080

# httpx only supports socks5:// (not socks5h://), normalize if needed
if SOCKS_PROXY.startswith("socks5h://"):
    SOCKS_PROXY = SOCKS_PROXY.replace("socks5h://", "socks5://", 1)

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.DEBUG,
)
# Keep httpx/apscheduler at INFO to reduce noise
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.INFO)
logging.getLogger("aiosqlite").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Keyboards ---

FUEL_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("АИ-92", callback_data="fuel:92"),
            InlineKeyboardButton("АИ-95", callback_data="fuel:95"),
        ],
        [
            InlineKeyboardButton("АИ-98", callback_data="fuel:98"),
            InlineKeyboardButton("АИ-100", callback_data="fuel:100"),
        ],
        [
            InlineKeyboardButton("ДТ", callback_data="fuel:ДТ"),
            InlineKeyboardButton("Газ", callback_data="fuel:Газ"),
        ],
        [InlineKeyboardButton("Любое топливо", callback_data="fuel:any")],
    ]
)

LOCATION_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📍 Отправить геопозицию", request_location=True)],
        [KeyboardButton("⌨️ Ввести координаты вручную")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📍 Изменить локацию", "⛽ Изменить топливо"],
        ["📊 Статус", "⏹ Остановить"],
    ],
    resize_keyboard=True,
)

STOPPED_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["▶️ Запустить мониторинг"],
        ["📍 Изменить локацию", "⛽ Изменить топливо"],
    ],
    resize_keyboard=True,
)


def _maps_button(lat: float, lon: float) -> InlineKeyboardMarkup:
    """Create an inline keyboard with 'Open in maps' button."""
    url = checker.build_maps_url(lat, lon)
    return InlineKeyboardMarkup([[InlineKeyboardButton("🗺 Открыть на карте", url=url)]])


# --- Command Handlers ---


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user = update.effective_user
    logger.info(f"/start from {user.id} (@{user.username})")

    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Я помогу отслеживать наличие топлива на ближайших заправках.\n\n"
        "Вот как это работает:\n"
        "1️⃣ Вы указываете свою локацию\n"
        "2️⃣ Выбираете нужное топливо\n"
        "3️⃣ Я каждые 5 минут проверяю заправки в радиусе 10 км\n"
        "4️⃣ Присылаю уведомление, когда топливо появляется\n\n"
        "📍 <b>Отправьте геопозицию</b> (на телефоне) или\n"
        "⌨️ <b>Введите координаты вручную</b> (на компьютере)\n"
        "   в формате: <code>54.72, 55.99</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=LOCATION_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await update.message.reply_text(
        "🔍 <b>Как пользоваться ботом</b>\n\n"
        "<b>Команды:</b>\n"
        "/start — Начать сначала\n"
        "/setlocation — Изменить локацию\n"
        "/setfuel — Изменить тип топлива\n"
        "/status — Текущие настройки\n"
        "/stop — Остановить мониторинг\n"
        "/startmon — Запустить мониторинг\n\n"
        "<b>Как это работает:</b>\n"
        "Бот проверяет заправки каждые 5 минут через сервис gdebenz.ru.\n"
        "Когда нужное топливо появляется в радиусе 10 км — вы получаете уведомление.\n"
        "Когда топливо заканчивается — сообщение удаляется.\n\n"
        "Данные предоставлены сервисом <a href='https://gdebenz.ru'>gdebenz.ru</a>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current user settings."""
    chat_id = update.effective_chat.id
    user_data = await db.get_user(chat_id)

    if not user_data:
        await update.message.reply_text(
            "❌ Вы ещё не настроили мониторинг.\n"
            "Отправьте свою геопозицию или введите координаты, чтобы начать.",
            reply_markup=LOCATION_KEYBOARD,
        )
        return

    fuel_label = checker.FUEL_LABELS.get(user_data["fuel_type"], user_data["fuel_type"])
    active_text = "✅ Активен" if user_data["is_active"] else "⏸ Остановлен"

    await update.message.reply_text(
        f"📊 <b>Ваши настройки</b>\n\n"
        f"📍 Координаты: <code>{user_data['lat']:.4f}, {user_data['lon']:.4f}</code>\n"
        f"⛽ Топливо: <b>{fuel_label}</b>\n"
        f"🔄 Статус: {active_text}\n"
        f"📏 Радиус поиска: 10 км\n"
        f"⏱ Интервал проверки: 5 мин",
        parse_mode=ParseMode.HTML,
    )


async def set_location_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /setlocation command."""
    await update.message.reply_text(
        "📍 <b>Отправьте геопозицию</b> (на телефоне) или\n"
        "⌨️ <b>Введите координаты вручную</b> (на компьютере)\n"
        "   в формате: <code>54.72, 55.99</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=LOCATION_KEYBOARD,
    )


async def set_fuel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /setfuel command."""
    await update.message.reply_text(
        "⛽ Выберите тип топлива, который вас интересует:",
        reply_markup=FUEL_KEYBOARD,
    )


async def stop_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stop command — stop monitoring and delete all notification messages."""
    chat_id = update.effective_chat.id
    await db.set_user_active(chat_id, False)

    # Delete all existing notification messages for this user
    message_ids = await db.delete_all_notifications_for_user(chat_id)
    for mid in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass  # Message may already be deleted

    await update.message.reply_text(
        "⏹ Мониторинг остановлен. Все уведомления удалены.\n\n"
        "Чтобы возобновить, нажмите «Запустить мониторинг».",
        reply_markup=STOPPED_KEYBOARD,
    )


async def start_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /startmon command."""
    chat_id = update.effective_chat.id
    user_data = await db.get_user(chat_id)

    if not user_data or not user_data["lat"] or not user_data["lon"]:
        await update.message.reply_text(
            "❌ Сначала укажите локацию и тип топлива.\n"
            "Отправьте геопозицию или введите координаты 👇",
            reply_markup=LOCATION_KEYBOARD,
        )
        return

    await db.set_user_active(chat_id, True)
    fuel_label = checker.FUEL_LABELS.get(user_data["fuel_type"], user_data["fuel_type"])
    await update.message.reply_text(
        f"▶️ Мониторинг запущен!\n\n"
        f"📍 Координаты: <code>{user_data['lat']:.4f}, {user_data['lon']:.4f}</code>\n"
        f"⛽ Топливо: <b>{fuel_label}</b>\n"
        f"📏 Радиус: 10 км\n"
        f"⏱ Проверка: каждые 5 минут\n\n"
        f"Я пришлю уведомление, когда топливо появится поблизости!",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )


# --- Message Handlers ---


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming location message (from mobile clients)."""
    chat_id = update.effective_chat.id
    location = update.message.location
    lat = location.latitude
    lon = location.longitude
    logger.info(f"Location from {chat_id}: {lat}, {lon}")
    try:
        await _save_location_and_proceed(update, chat_id, lat, lon)
    except Exception as e:
        logger.error(
            f"Error saving location for {chat_id}: {e}\n{traceback.format_exc()}"
        )
        await update.message.reply_text(
            "❌ Произошла ошибка при сохранении локации. Попробуйте ещё раз или введите координаты вручную."
        )


async def handle_fuel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle fuel selection from inline keyboard."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    fuel_type = query.data.split(":")[1]

    user_data = await db.get_user(chat_id)

    if not user_data or not user_data["lat"]:
        await query.edit_message_text("❌ Сначала отправьте свою геопозицию!")
        await query.message.reply_text(
            "📍 Используйте кнопку ниже или введите координаты:",
            reply_markup=LOCATION_KEYBOARD,
        )
        return

    # Delete old notification messages when changing fuel type
    old_message_ids = await db.delete_all_notifications_for_user(chat_id)
    for mid in old_message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass

    await db.upsert_user(chat_id, user_data["lat"], user_data["lon"], fuel_type)
    fuel_label = checker.FUEL_LABELS.get(fuel_type, fuel_type)

    await query.edit_message_text(
        f"✅ Выбрано топливо: <b>{fuel_label}</b>\n\n"
        f"Мониторинг запущен! Я буду проверять заправки каждые 5 минут.",
        parse_mode=ParseMode.HTML,
    )

    await query.message.reply_text("Главное меню:", reply_markup=MAIN_KEYBOARD)


COORDINATES_PATTERN = re.compile(
    r"^\s*([-]?\d{1,2}(?:\.\d+)?)\s*[,;\s]+\s*([-]?\d{1,3}(?:\.\d+)?)\s*$"
)


def _parse_coordinates(text: str) -> tuple[float, float] | None:
    """Try to parse coordinates from text like '54.72, 55.99' or '54.72 55.99'."""
    m = COORDINATES_PATTERN.match(text)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    return lat, lon


async def _save_location_and_proceed(
    update: Update, chat_id: int, lat: float, lon: float
) -> None:
    """Save coordinates and either ask for fuel or start monitoring."""
    user_data = await db.get_user(chat_id)
    fuel_type = user_data["fuel_type"] if user_data else None

    # Delete old notification messages when changing location
    old_message_ids = await db.delete_all_notifications_for_user(chat_id)
    for mid in old_message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass

    if fuel_type:
        await db.upsert_user(chat_id, lat, lon, fuel_type)
        fuel_label = checker.FUEL_LABELS.get(fuel_type, fuel_type)
        await update.message.reply_text(
            f"✅ Локация сохранена: <code>{lat:.4f}, {lon:.4f}</code>\n"
            f"⛽ Топливо: <b>{fuel_label}</b>\n\n"
            f"Мониторинг запущен! Я буду проверять заправки каждые 5 минут.",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await db.upsert_user(chat_id, lat, lon, "")
        await update.message.reply_text(
            f"✅ Локация сохранена: <code>{lat:.4f}, {lon:.4f}</code>\n\n"
            f"Теперь выберите тип топлива:",
            parse_mode=ParseMode.HTML,
            reply_markup=FUEL_KEYBOARD,
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages (keyboard buttons and coordinate input)."""
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    logger.debug(f"Text from {chat_id}: {text[:50]}")

    if text == "⌨️ Ввести координаты вручную":
        await update.message.reply_text(
            "📍 Введите координаты в формате: <code>54.72, 55.99</code>\n"
            "(широта и долгота через запятую или пробел)",
            parse_mode=ParseMode.HTML,
        )
        return

    coords = _parse_coordinates(text)
    if coords is not None:
        lat, lon = coords
        await _save_location_and_proceed(update, chat_id, lat, lon)
        return

    if text == "📍 Изменить локацию":
        await set_location_cmd(update, context)
    elif text == "⛽ Изменить топливо":
        await set_fuel_cmd(update, context)
    elif text == "📊 Статус":
        await status(update, context)
    elif text == "⏹ Остановить":
        await stop_monitoring(update, context)
    elif text == "▶️ Запустить мониторинг":
        await start_monitoring(update, context)
    else:
        await update.message.reply_text(
            "Используйте кнопки меню или команды.\n/help — список команд",
        )


import cache
import checker
import db

# In-memory cluster cache (persists across job runs)
_clusters_cache: list[cache.Cluster] = []


def _make_session() -> aiohttp.ClientSession:
    """Create an aiohttp session, optionally with SOCKS proxy."""
    if SOCKS_PROXY and checker.HAS_SOCKS:
        connector = checker.ProxyConnector.from_url(SOCKS_PROXY)
        return aiohttp.ClientSession(connector=connector)
    return aiohttp.ClientSession()


# --- Background Job: Check for fuel ---


async def check_fuel_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: one API call per geographic cluster, then sync per user."""
    global _clusters_cache

    users = await db.get_active_users()
    if not users:
        _clusters_cache = []
        return

    # Build clusters from current users
    clusters = cache.build_clusters(users)
    logger.info(f"Checking fuel: {len(users)} users → {len(clusters)} cluster(s)")

    async with _make_session() as session:
        for cluster in clusters:
            # Fetch API once per cluster (if cache expired)
            if not cache.is_cache_valid(cluster):
                data = await checker.fetch_nearby(
                    session,
                    cluster.center_lat,
                    cluster.center_lon,
                    radius_km=int(cluster.fetch_radius_km),
                )
                if data and "stations" in data:
                    cluster.stations = data["stations"]
                    cluster.fetched_at = time.time()
                    logger.info(
                        f"Cluster ({cluster.center_lat:.2f},{cluster.center_lon:.2f}) "
                        f"r={cluster.fetch_radius_km}km: {len(cluster.stations)} stations"
                    )
                else:
                    logger.warning(
                        f"Cluster ({cluster.center_lat:.2f},{cluster.center_lon:.2f}) "
                        f"API fetch failed, using stale cache"
                    )
            else:
                logger.info(
                    f"Cluster ({cluster.center_lat:.2f},{cluster.center_lon:.2f}) "
                    f"using cache ({len(cluster.stations)} stations)"
                )

            # Sync notifications for each user in the cluster
            user_map = {u["chat_id"]: u for u in users}
            for uid in cluster.user_ids:
                if uid not in user_map:
                    continue
                user = user_map[uid]
                try:
                    await _sync_notifications(context, cluster, user)
                except Exception as e:
                    logger.error(f"Error syncing for user {uid}: {e}")

                await asyncio.sleep(0.2)

            # Small delay between clusters
            await asyncio.sleep(0.5)

    _clusters_cache = clusters


async def _sync_notifications(
    context: ContextTypes.DEFAULT_TYPE,
    cluster: cache.Cluster,
    user: dict,
) -> None:
    """Sync notification messages for one user against cached cluster data."""
    chat_id = user["chat_id"]
    lat = user["lat"]
    lon = user["lon"]
    fuel_type = user["fuel_type"]

    # Filter cached stations for this user
    stations = cache.filter_stations_for_user(
        cluster.stations, lat, lon, fuel_type, radius_km=10.0
    )
    current_osm_ids = {s["osm_id"] for s in stations}
    current_by_osm = {s["osm_id"]: s for s in stations}

    # Get previously sent notifications
    prev_notifications = await db.get_notifications_for_user(chat_id)
    prev_osm_ids = set(prev_notifications.keys())

    # --- 1. Delete messages for stations that no longer have fuel ---
    gone_osm_ids = prev_osm_ids - current_osm_ids
    for osm_id in gone_osm_ids:
        message_id = prev_notifications[osm_id]
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            logger.info(f"Deleted msg {message_id} for {osm_id} (fuel gone)")
        except Exception as e:
            logger.warning(f"Could not delete msg {message_id}: {e}")
        await db.remove_notification(chat_id, osm_id)

    # --- 2. Update existing messages ---
    still_there = prev_osm_ids & current_osm_ids
    for osm_id in still_there:
        station = current_by_osm[osm_id]
        message_id = prev_notifications[osm_id]
        new_text = checker.format_station_message(station, fuel_type)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=new_text,
                parse_mode=ParseMode.HTML,
                reply_markup=_maps_button(station["lat"], station["lon"]),
            )
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.warning(f"Could not edit msg {message_id}: {e}")

    # --- 3. Send new messages ---
    new_osm_ids = current_osm_ids - prev_osm_ids
    new_stations = [current_by_osm[oid] for oid in new_osm_ids]
    new_stations.sort(key=lambda s: s.get("distance_km", 999))
    new_stations = new_stations[:MAX_STATIONS_PER_NOTIFICATION]

    fuel_label = checker.FUEL_LABELS.get(fuel_type, fuel_type)
    for station in new_stations:
        text = checker.format_station_message(station, fuel_type)
        header = f"🔔 <b>Найдено топливо ({fuel_label}) поблизости!</b>\n\n"
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=header + text,
                parse_mode=ParseMode.HTML,
                reply_markup=_maps_button(station["lat"], station["lon"]),
                disable_web_page_preview=True,
            )
            await db.add_notification(chat_id, station["osm_id"], msg.message_id)
            logger.info(
                f"New notification for {chat_id}: {station['osm_id']} (msg {msg.message_id})"
            )
        except Exception as e:
            logger.error(f"Failed to notify {chat_id}: {e}")

        await asyncio.sleep(0.3)


# --- Error Handler ---


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and notify user."""
    logger.error(
        f"Update {update} caused error: {context.error}\n"
        f"Traceback:\n{''.join(traceback.format_tb(context.error.__traceback__))}"
    )
    try:
        if update and hasattr(update, "effective_chat") and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Произошла внутренняя ошибка. Попробуйте ещё раз или используйте /start.",
            )
    except Exception:
        pass


# --- Main ---


def main() -> None:
    """Start the bot."""
    import asyncio as _asyncio

    if not TOKEN:
        print("Error: BOT_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    _asyncio.run(db.init_db())

    builder = ApplicationBuilder().token(TOKEN)
    if SOCKS_PROXY:
        # Build httpx client with SOCKS proxy AND long read timeout for polling
        transport = httpx.AsyncHTTPTransport(proxy=SOCKS_PROXY)
        httpx_client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=15.0),
        )
        builder = builder.proxy(SOCKS_PROXY).get_updates_request(httpx_client)
        logger.info(f"Using proxy: {SOCKS_PROXY}")
    app = builder.build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("setlocation", set_location_cmd))
    app.add_handler(CommandHandler("setfuel", set_fuel_cmd))
    app.add_handler(CommandHandler("stop", stop_monitoring))
    app.add_handler(CommandHandler("startmon", start_monitoring))

    # Message handlers
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(CallbackQueryHandler(handle_fuel_callback, pattern="^fuel:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Error handler
    app.add_error_handler(error_handler)

    # Background job
    job_queue = app.job_queue
    job_queue.run_repeating(
        check_fuel_job,
        interval=CHECK_INTERVAL_SECONDS,
        first=10,
    )

    logger.info("Bot started. Polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        pool_timeout=30,
    )


if __name__ == "__main__":
    main()
