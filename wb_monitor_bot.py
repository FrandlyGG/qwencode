import asyncio
import logging
import sys
import ssl
from datetime import datetime
from typing import Optional, Set

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiohttp import ClientSession, TCPConnector
import urllib3

# Отключаем предупреждения о SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Проверка токенов
try:
    from config import WB_API_TOKEN, TG_BOT_TOKEN, LOG_LEVEL
except ImportError:
    WB_API_TOKEN = ""
    TG_BOT_TOKEN = ""
    LOG_LEVEL = "DEBUG"

if not WB_API_TOKEN or WB_API_TOKEN == "YOUR_WB_TOKEN" or WB_API_TOKEN == "your_wb_api_token_here":
    print("❌ ОШИБКА: Не указан токен Wildberries API!")
    print("   Получите токен в личном кабинете селлера WB (категория 'Поставки')")
    print("   и добавьте в файл .env: WB_API_TOKEN=ваш_токен")
    sys.exit(1)

if not TG_BOT_TOKEN or TG_BOT_TOKEN == "YOUR_BOT_TOKEN" or TG_BOT_TOKEN == "your_bot_token_here":
    print("❌ ОШИБКА: Не указан токен Telegram бота!")
    print("   Получите токен у @BotFather и добавьте в файл .env: TG_BOT_TOKEN=ваш_токен")
    sys.exit(1)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.DEBUG),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("wb_monitor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class CityState(StatesGroup):
    waiting_for_city = State()

is_monitoring = False
monitor_task: Optional[asyncio.Task] = None
found_slots = []
check_count = 0
error_count = 0
notification_count = 0
start_time = None
api_session: Optional[ClientSession] = None
user_city: Optional[str] = None
active_users: Set[int] = set()
current_bot: Optional[Bot] = None

router = Router()

def get_main_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="▶️ Запустить мониторинг")],
        [KeyboardButton(text="⏸️ Остановить мониторинг")],
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="🌍 Выбрать город")],
        [KeyboardButton(text="❓ Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, input_field_placeholder="Выберите действие...")

def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

def get_status_text() -> str:
    uptime = ""
    if start_time:
        delta = datetime.now() - start_time
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        uptime = f"\n⏳ Время работы: {days}д {hours}ч {minutes}м"
    city_filter = f"\n🌍 Фильтр города: <b>{user_city}</b>" if user_city else "\n🌍 Фильтр города: <b>Нет</b>"
    return (f"📊 <b>Статистика</b>\n\n"
            f"🔄 Проверок: <code>{check_count}</code>\n"
            f"✅ Слотов: <code>{len(found_slots)}</code>\n"
            f"🔔 Уведомлений: <code>{notification_count}</code>\n"
            f"❌ Ошибок: <code>{error_count}</code>"
            f"{city_filter}{uptime}")

async def init_api_session():
    global api_session
    if api_session is None or api_session.closed:
        # Создаем SSL контекст для обхода проблем с сертификатами
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        connector = TCPConnector(limit=10, ssl=ssl_context)
        api_session = ClientSession(connector=connector, headers={"Authorization": WB_API_TOKEN})
        logger.info("API сессия создана (SSL проверки отключены для отладки)")

async def close_api_session():
    global api_session
    if api_session and not api_session.closed:
        await api_session.close()
        api_session = None

async def get_warehouses() -> list:
    if not api_session:
        await init_api_session()
    try:
        # Правильный URL согласно документации WB API
        async with api_session.get("https://supplies-api.wildberries.ru/api/v1/warehouses") as resp:
            if resp.status == 200:
                data = await resp.json()
                logger.info(f"Получено {len(data)} складов")
                return data
            logger.error(f"HTTP {resp.status}")
            return []
    except Exception as e:
        logger.error(f"Ошибка API: {e}", exc_info=True)
        return []

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    active_users.add(message.from_user.id)
    await message.answer("👋 <b>Привет!</b>\nВыберите действие:", parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

@router.message(Command("help"))
async def cmd_help(message: Message):
    text = ("ℹ️ <b>Справка</b>\n\n"
            "/start - Меню\n/start_monitoring - Старт\n/stop_monitoring - Стоп\n"
            "/status - Статистика\n/city - Город\n/help - Справка\n\n"
            "🌍 Введите город после нажатия кнопки или /city")
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())

@router.message(Command("city"))
async def cmd_city(message: Message, state: FSMContext):
    cur = user_city if user_city else "не задан"
    await message.answer(f"🌍 Текущий: <b>{cur}</b>\nВведите город или пусто для сброса:", 
                        parse_mode=ParseMode.HTML, reply_markup=get_cancel_keyboard())
    await state.set_state(CityState.waiting_for_city)

@router.message(CityState.waiting_for_city, F.text.casefold() == "❌ отмена".casefold())
async def cancel_city(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено", reply_markup=get_main_keyboard())

@router.message(CityState.waiting_for_city)
async def process_city(message: Message, state: FSMContext):
    global user_city
    city = message.text.strip()
    user_city = city if city else None
    await message.answer(f"✅ Город: <b>{user_city or 'сброшен'}</b>", 
                        parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())
    await state.clear()
    logger.info(f"Город: {user_city}")

@router.message(F.text == "🌍 Выбрать город")
async def btn_city(message: Message, state: FSMContext):
    await cmd_city(message, state)

@router.message(F.text == "❓ Помощь")
async def btn_help(message: Message):
    await cmd_help(message)

@router.message(F.text == "📊 Статус")
async def btn_status(message: Message):
    await message.answer(get_status_text(), parse_mode=ParseMode.HTML)

@router.message(Command("start_monitoring"))
@router.message(F.text == "▶️ Запустить мониторинг")
async def start_mon(message: Message):
    global is_monitoring, monitor_task, start_time
    if is_monitoring:
        await message.answer("Уже запущен", reply_markup=get_main_keyboard())
        return
    is_monitoring = True
    start_time = datetime.now()
    active_users.add(message.from_user.id)
    await message.answer("🚀 <b>Запущено!</b>", parse_mode=ParseMode.HTML)
    monitor_task = asyncio.create_task(monitor_loop())
    logger.info("Мониторинг старт")

@router.message(Command("stop_monitoring"))
@router.message(F.text == "⏸️ Остановить мониторинг")
async def stop_mon(message: Message):
    global is_monitoring, monitor_task
    if not is_monitoring:
        await message.answer("Не запущен", reply_markup=get_main_keyboard())
        return
    is_monitoring = False
    if monitor_task:
        monitor_task.cancel()
        try: await monitor_task
        except asyncio.CancelledError: pass
        monitor_task = None
    await message.answer("⏸️ <b>Остановлено</b>", parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())
    logger.info("Мониторинг стоп")

@router.message(Command("status"))
async def cmd_status(message: Message):
    await message.answer(get_status_text(), parse_mode=ParseMode.HTML)

async def notify_users(text: str):
    if not current_bot: return
    for uid in list(active_users):
        try:
            await current_bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Не удалось отправить {uid}: {e}")
            active_users.discard(uid)

async def monitor_loop():
    global check_count, found_slots, notification_count, error_count
    logger.info("Цикл старт")
    await notify_users("🚀 <b>Мониторинг запущен!</b>")
    while is_monitoring:
        try:
            check_count += 1
            logger.debug(f"Проверка #{check_count}, город: {user_city}")
            warehouses = await get_warehouses()
            if not warehouses:
                logger.warning("Список складов пуст")
                await asyncio.sleep(10)
                continue
            logger.info(f"Получено {len(warehouses)} складов всего")
            current = []
            for wh in warehouses:
                name = wh.get('name', '')
                addr = wh.get('address', '')
                if user_city:
                    if user_city.lower() not in name.lower() and user_city.lower() not in addr.lower():
                        continue
                current.append(wh)
            logger.info(f"Найдено {len(current)} складов в городе {user_city or 'без фильтра'}")
            new_slots = current if not found_slots or len(current) != len(found_slots) else []
            if new_slots:
                found_slots = current
                notification_count += 1
                msg = f"🎉 <b>Слоты!</b>\nВсего: {len(new_slots)}\n\n"
                for i, s in enumerate(new_slots[:5], 1):
                    msg += f"{i}. {s.get('name')}\n{s.get('address')}\n\n"
                if len(new_slots) > 5: msg += f"... ещё {len(new_slots)-5}\n"
                await notify_users(msg)
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            logger.info("Цикл отменен")
            break
        except Exception as e:
            error_count += 1
            logger.error(f"Ошибка: {e}", exc_info=True)
            await asyncio.sleep(10)

async def main():
    global current_bot
    # Токены уже проверены при старте скрипта
    current_bot = Bot(token=TG_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    try:
        logger.info("Бот старт")
        await dp.start_polling(current_bot)
    except KeyboardInterrupt:
        logger.info("Стоп")
    finally:
        await close_api_session()
        await current_bot.session.close()
        await current_bot.close()

if __name__ == "__main__":
    asyncio.run(main())
