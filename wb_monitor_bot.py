import asyncio
import aiohttp
import logging
from datetime import datetime
from typing import Optional, Set, List
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    WebAppInfo
)
from aiogram.enums import ParseMode
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from config import (
    WB_API_TOKEN,
    TG_BOT_TOKEN,
    TG_CHAT_ID,
    CHECK_INTERVAL,
    MONITOR_WAREHOUSE_IDS,
    LOG_LEVEL,
    LOG_FILE
)

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class CitySelection(StatesGroup):
    waiting_for_city = State()


class WildberriesMonitor:
    def __init__(self):
        self.base_url = "https://supplies-api.wildberries.ru/api/v1"
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_notified_slots: Set[str] = set()
        self.is_monitoring = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.bot: Optional[Bot] = None
        self.warehouses_cache: list[dict] = []
        self.cache_timestamp: float = 0
        self.cache_ttl = 300  # Кэш складов на 5 минут
        self.target_city: str = ""  # Город для фильтрации складов
        self.stats = {
            "total_checks": 0,
            "slots_found": 0,
            "notifications_sent": 0,
            "errors": 0,
            "start_time": None
        }

    async def start(self):
        """Инициализация сессии"""
        logger.info("Инициализация сессии API Wildberries")
        self.session = aiohttp.ClientSession(
            headers={"Authorization": self._get_api_token()}
        )
        self.stats["start_time"] = datetime.now()

    async def stop(self):
        """Закрытие сессии"""
        logger.info("Закрытие сессии API Wildberries")
        if self.session:
            await self.session.close()

    def _get_api_token(self) -> str:
        if not WB_API_TOKEN:
            logger.error("WB_API_TOKEN не установлен!")
            raise ValueError("WB_API_TOKEN не установлен!")
        return WB_API_TOKEN

    async def get_warehouses(self, force_refresh: bool = False) -> list[dict]:
        """Получение списка складов с кэшированием"""
        now = asyncio.get_event_loop().time()
        
        # Возвращаем кэш если он ещё актуален и не запрошено обновление
        if self.warehouses_cache and (now - self.cache_timestamp) < self.cache_ttl and not force_refresh:
            logger.debug(f"Используем кэш складов (возраст: {int(now - self.cache_timestamp)} сек.)")
            return self.warehouses_cache

        url = f"{self.base_url}/warehouses"
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                logger.debug(f"Запрос складов (попытка {attempt + 1}/{max_retries})")
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        self.warehouses_cache = data
                        self.cache_timestamp = now
                        logger.info(f"Получено {len(data)} складов")
                        return data
                    elif response.status == 429:
                        wait_time = 60 * (attempt + 1)
                        logger.warning(f"Rate limit, ждём {wait_time} сек.")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"Ошибка API WB: статус {response.status}")
                        return []
            except Exception as e:
                logger.error(f"Ошибка при получении складов: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5)
                else:
                    return []
        
        return []

    async def get_acceptance_options(self, warehouse_id: Optional[int] = None, 
                                      barcode: str = "test", quantity: int = 1) -> dict:
        """Получение опций приёмки для склада"""
        url = f"{self.base_url}/acceptance/options"
        
        params = {}
        if warehouse_id:
            params["warehouseID"] = warehouse_id

        payload = [{"barcode": barcode, "quantity": quantity}]
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                async with self.session.post(url, params=params, json=payload) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 429:
                        wait_time = 60 * (attempt + 1)
                        logger.warning(f"Rate limit на acceptance/options, ждём {wait_time} сек.")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.debug(f"Ошибка API при получении опций: статус {response.status}")
                        return {}
            except Exception as e:
                logger.error(f"Ошибка при получении опций приёмки: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5)
                else:
                    return {}
        
        return {}

    async def check_available_slots(self) -> list[dict]:
        """Проверка доступных слотов для отгрузки"""
        warehouses = await self.get_warehouses()
        available_slots = []

        for warehouse in warehouses:
            # Пропускаем неактивные склады
            if not warehouse.get("isActive", False):
                continue

            # Если указаны конкретные склады для мониторинга
            if MONITOR_WAREHOUSE_IDS and warehouse["ID"] not in MONITOR_WAREHOUSE_IDS:
                continue

            # Фильтрация по городу если указан
            if self.target_city:
                warehouse_city = warehouse.get("city", "") or warehouse.get("address", "")
                if self.target_city.lower() not in warehouse_city.lower():
                    continue

            # Проверяем опции приёмки для каждого склада
            options = await self.get_acceptance_options(warehouse_id=warehouse["ID"])
            
            if options and "result" in options:
                for result in options["result"]:
                    # Проверяем, есть ли доступные слоты
                    if result:
                        slot_info = {
                            "warehouse_id": warehouse["ID"],
                            "warehouse_name": warehouse["name"],
                            "address": warehouse["address"],
                            "work_time": warehouse["workTime"],
                            "details": result
                        }
                        available_slots.append(slot_info)

        self.stats["total_checks"] += 1
        self.stats["slots_found"] += len(available_slots)
        logger.info(f"Найдено {len(available_slots)} доступных слотов" + 
                   (f" в городе '{self.target_city}'" if self.target_city else ""))
        return available_slots

    def _generate_slot_key(self, slot: dict) -> str:
        """Генерация уникального ключа для слота"""
        details = slot.get('details', {})
        slot_date = details.get('date', '') if isinstance(details, dict) else ''
        return f"{slot['warehouse_id']}_{slot['warehouse_name']}_{slot_date}"

    async def send_telegram_message(self, message: str, parse_mode: str = "HTML"):
        """Отправка сообщения в Telegram"""
        if not TG_CHAT_ID:
            logger.warning("TG_CHAT_ID не установлен, сообщение не отправлено")
            return

        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": parse_mode
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    if response.status != 200:
                        logger.error(f"Ошибка отправки сообщения: {response.status}")
                    else:
                        self.stats["notifications_sent"] += 1
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения: {e}")

    async def monitor_loop(self):
        """Основной цикл мониторинга"""
        logger.info("Запуск цикла мониторинга")
        while self.is_monitoring:
            try:
                slots = await self.check_available_slots()
                
                if slots:
                    current_slots = set()
                    
                    for slot in slots:
                        slot_key = self._generate_slot_key(slot)
                        current_slots.add(slot_key)
                        
                        # Отправляем уведомление только о новых слотах
                        if slot_key not in self.last_notified_slots:
                            message = self._format_notification(slot)
                            await self.send_telegram_message(message)
                            if self.bot:
                                try:
                                    await self.bot.send_message(
                                        TG_CHAT_ID, 
                                        f"✅ Найдено: {slot['warehouse_name']}"
                                    )
                                except Exception as e:
                                    logger.error(f"Ошибка отправки уведомления: {e}")
                            logger.info(f"Уведомление отправлено: {slot['warehouse_name']}")
                    
                    self.last_notified_slots = current_slots
                else:
                    if self.last_notified_slots:
                        logger.info("Слоты исчезли, очистка истории")
                    self.last_notified_slots.clear()

            except asyncio.CancelledError:
                logger.info("Цикл мониторинга остановлен")
                break
            except Exception as e:
                self.stats["errors"] += 1
                logger.error(f"Ошибка в цикле мониторинга: {e}")
                if self.bot:
                    await self.bot.send_message(TG_CHAT_ID, f"❌ Ошибка: {e}")

            # Ждём следующий интервал проверки
            await asyncio.sleep(CHECK_INTERVAL)

    def _format_notification(self, slot: dict) -> str:
        """Форматирование уведомления"""
        details = slot.get('details', {})
        date_info = ""
        if isinstance(details, dict) and details.get('date'):
            date_info = f"\n📅 <b>Дата:</b> {details.get('date', '')}"
        
        return f"""
🔔 <b>Доступен слот для отгрузки!</b>

📦 <b>Склад:</b> {slot['warehouse_name']}
🆔 <b>ID:</b> {slot['warehouse_id']}
📍 <b>Адрес:</b> {slot['address']}
⏰ <b>Режим работы:</b> {slot['work_time']}{date_info}

⚡️ <b>Успейте записаться!</b>
        """.strip()

    async def start_monitoring(self, bot: Bot, chat_id: Optional[str] = None):
        """Запуск мониторинга"""
        if self.is_monitoring:
            await bot.send_message(
                chat_id or TG_CHAT_ID, 
                "⚠️ Мониторинг уже запущен!"
            )
            return
        
        self.is_monitoring = True
        self.bot = bot
        await self.start()
        self.monitor_task = asyncio.create_task(self.monitor_loop())
        logger.info("Мониторинг запущен пользователем")
        
        city_info = f" для города '{self.target_city}'" if self.target_city else ""
        await bot.send_message(
            chat_id or TG_CHAT_ID, 
            f"🚀 <b>Мониторинг запущен{city_info}!</b>\n\nТеперь я проверяю склады WB и сообщу о свободных слотах.",
            parse_mode=ParseMode.HTML
        )

    async def stop_monitoring(self, bot: Bot, chat_id: Optional[str] = None):
        """Остановка мониторинга"""
        if not self.is_monitoring:
            await bot.send_message(
                chat_id or TG_CHAT_ID, 
                "⚠️ Мониторинг не запущен!"
            )
            return
        
        self.is_monitoring = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        
        await self.stop()
        self.monitor_task = None
        self.bot = None
        logger.info("Мониторинг остановлен пользователем")
        await bot.send_message(
            chat_id or TG_CHAT_ID, 
            "⏸️ <b>Мониторинг остановлен.</b>", 
            parse_mode=ParseMode.HTML
        )

    async def get_status(self, bot: Bot, chat_id: Optional[str] = None):
        """Получение статуса мониторинга"""
        status = "🟢 Активен" if self.is_monitoring else "🔴 Остановлен"
        warehouses_count = len(self.warehouses_cache)
        cache_age = int(asyncio.get_event_loop().time() - self.cache_timestamp) if self.cache_timestamp > 0 else 0
        
        city_info = f"\nГород: {self.target_city}" if self.target_city else "\nГород: Все города"
        
        message = f"""
📊 <b>Статус мониторинга</b>

Статус: {status}{city_info}
Складов в кэше: {warehouses_count}
Возраст кэша: {cache_age} сек.
Интервал проверки: {CHECK_INTERVAL} сек.
        """.strip()
        
        await bot.send_message(chat_id or TG_CHAT_ID, message, parse_mode="HTML")

    async def set_city(self, bot: Bot, chat_id: str, city: str):
        """Установка города для мониторинга"""
        self.target_city = city.strip()
        # Очищаем кэш складов чтобы применить фильтр города
        self.warehouses_cache = []
        self.cache_timestamp = 0
        
        if self.target_city:
            message = f"✅ <b>Город установлен: {self.target_city}</b>\n\nТеперь будут показываться только склады в этом городе."
        else:
            message = "✅ <b>Фильтр города снят</b>\n\nТеперь будут показываться все склады."
        
        await bot.send_message(chat_id, message, parse_mode=ParseMode.HTML)
        logger.info(f"Город установлен: {self.target_city if self.target_city else 'Все города'}")


async def main():
    monitor = WildberriesMonitor()
    
    # Создаём бота
    bot = Bot(token=TG_BOT_TOKEN)
    dp = Dispatcher()
    
    # Кнопки управления
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Запустить"), KeyboardButton(text="⏸️ Остановить")],
            [KeyboardButton(text="📊 Статус"), KeyboardButton(text="🌍 Выбрать город")],
            [KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True
    )
    
    @dp.message(Command("start"))
    async def cmd_start(message: types.Message):
        await message.answer(
            "👋 Привет! Я бот для мониторинга свободных слотов WB.\n\n"
            "Используйте кнопки ниже или команды:\n"
            "/start_monitoring - Запустить мониторинг\n"
            "/stop_monitoring - Остановить мониторинг\n"
            "/status - Показать статус\n"
            "/city - Установить город для поиска\n"
            "/help - Помощь",
            reply_markup=kb
        )
    
    @dp.message(Command("start_monitoring"))
    async def cmd_start_monitoring(message: types.Message):
        await message.answer("⏳ Запускаю мониторинг...")
        await monitor.start_monitoring(bot, message.chat.id)
    
    @dp.message(Command("stop_monitoring"))
    async def cmd_stop_monitoring(message: types.Message):
        await message.answer("⏳ Останавливаю мониторинг...")
        await monitor.stop_monitoring(bot, message.chat.id)
    
    @dp.message(Command("status"))
    async def cmd_status(message: types.Message):
        await monitor.get_status(bot, message.chat.id)
    
    @dp.message(Command("city"))
    async def cmd_city(message: types.Message):
        await message.answer(
            "🌍 <b>Выбор города</b>\n\n"
            "Введите название города для фильтрации складов.\n"
            "Например: <i>Москва</i>, <i>Санкт-Петербург</i>, <i>Казань</i>\n\n"
            "Отправьте пустое сообщение чтобы снять фильтр.",
            parse_mode=ParseMode.HTML
        )
        await dp.storage.set_state(message.from_user.id, CitySelection.waiting_for_city)
    
    @dp.message(Command("help"))
    async def cmd_help(message: types.Message):
        help_text = """
📚 <b>Помощь по боту</b>

<b>Основные команды:</b>
/start - Главное меню
/start_monitoring - Запустить мониторинг
/stop_monitoring - Остановить мониторинг
/status - Показать текущий статус
/city - Установить город для поиска
/help - Эта справка

<b>Как это работает:</b>
1. Бот периодически проверяет API Wildberries
2. Ищет доступные слоты для отгрузки
3. Отправляет уведомления о новых слотах

<b>Фильтр по городу:</b>
Используйте команду /city чтобы указать город.
Бот будет показывать только склады в этом городе.

<b>Кнопки:</b>
▶️ Запустить - старт мониторинга
⏸️ Остановить - остановка мониторинга
📊 Статус - информация о работе
🌍 Выбрать город - установка фильтра
❓ Помощь - эта справка
        """.strip()
        await message.answer(help_text, parse_mode=ParseMode.HTML)
    
    # Обработчик ввода города
    @dp.message(StateFilter(CitySelection.waiting_for_city))
    async def process_city_input(message: types.Message, state: FSMContext):
        city = message.text.strip() if message.text else ""
        await monitor.set_city(bot, message.chat.id, city)
        await state.clear()
    
    @dp.message(F.text == "▶️ Запустить")
    async def btn_start(message: types.Message):
        await message.answer("⏳ Запускаю мониторинг...")
        await monitor.start_monitoring(bot, message.chat.id)
    
    @dp.message(F.text == "⏸️ Остановить")
    async def btn_stop(message: types.Message):
        await message.answer("⏳ Останавливаю мониторинг...")
        await monitor.stop_monitoring(bot, message.chat.id)
    
    @dp.message(F.text == "📊 Статус")
    async def btn_status(message: types.Message):
        await monitor.get_status(bot, message.chat.id)
    
    @dp.message(F.text == "🌍 Выбрать город")
    async def btn_city(message: types.Message):
        await message.answer(
            "🌍 <b>Выбор города</b>\n\n"
            "Введите название города для фильтрации складов.\n"
            "Например: <i>Москва</i>, <i>Санкт-Петербург</i>, <i>Казань</i>\n\n"
            "Отправьте пустое сообщение чтобы снять фильтр.",
            parse_mode=ParseMode.HTML
        )
        await dp.storage.set_state(message.from_user.id, CitySelection.waiting_for_city)
    
    @dp.message(F.text == "❓ Помощь")
    async def btn_help(message: types.Message):
        help_text = """
📚 <b>Помощь по боту</b>

<b>Основные команды:</b>
/start - Главное меню
/start_monitoring - Запустить мониторинг
/stop_monitoring - Остановить мониторинг
/status - Показать текущий статус
/city - Установить город для поиска
/help - Эта справка

<b>Как это работает:</b>
1. Бот периодически проверяет API Wildberries
2. Ищет доступные слоты для отгрузки
3. Отправляет уведомления о новых слотах

<b>Фильтр по городу:</b>
Используйте команду /city чтобы указать город.
Бот будет показывать только склады в этом городе.

<b>Кнопки:</b>
▶️ Запустить - старт мониторинга
⏸️ Остановить - остановка мониторинга
📊 Статус - информация о работе
🌍 Выбрать город - установка фильтра
❓ Помощь - эта справка
        """.strip()
        await message.answer(help_text, parse_mode=ParseMode.HTML)
    
    print("🤖 Бот запущен! Используйте /start для начала работы.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен пользователем")
