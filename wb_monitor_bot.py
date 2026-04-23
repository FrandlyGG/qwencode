import asyncio
import aiohttp
from datetime import datetime
from typing import Optional

# Конфигурация
WB_API_TOKEN = ""  # Вставьте ваш токен для категории "Поставки"
TG_BOT_TOKEN = "8658224553:AAGE9O-wMIgeIcMaL9YH3IJsKL6fwMYgT1Y"
TG_CHAT_ID = ""  # Вставьте ваш chat_id (можно узнать через @userinfobot)

# Интервал проверки в секундах (минимум 10 сек из-за лимитов API)
CHECK_INTERVAL = 10

# ID складов для мониторинга (по умолчанию все активные)
MONITOR_WAREHOUSE_IDS: list[int] = []  # Оставьте пустым для всех складов или укажите конкретные [507, 123456]


class WildberriesMonitor:
    def __init__(self):
        self.base_url = "https://supplies-api.wildberries.ru/api/v1"
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_notified_slots: set = set()

    async def start(self):
        """Инициализация сессии"""
        self.session = aiohttp.ClientSession(
            headers={"Authorization": self._get_api_token()}
        )

    async def stop(self):
        """Закрытие сессии"""
        if self.session:
            await self.session.close()

    def _get_api_token(self) -> str:
        if not WB_API_TOKEN:
            raise ValueError("WB_API_TOKEN не установлен!")
        return WB_API_TOKEN

    async def get_warehouses(self) -> list[dict]:
        """Получение списка складов"""
        url = f"{self.base_url}/warehouses"
        async with self.session.get(url) as response:
            if response.status == 200:
                return await response.json()
            elif response.status == 429:
                print(f"⚠️ Слишком много запросов (429). Ждём...")
                await asyncio.sleep(5)
                return await self.get_warehouses()
            else:
                print(f"Ошибка получения складов: {response.status}")
                return []

    async def get_acceptance_options(self, warehouse_id: Optional[int] = None, 
                                      barcode: str = "test", quantity: int = 1) -> dict:
        """Получение опций приёмки для склада"""
        url = f"{self.base_url}/acceptance/options"
        
        params = {}
        if warehouse_id:
            params["warehouseID"] = warehouse_id

        payload = [{"barcode": barcode, "quantity": quantity}]
        
        async with self.session.post(url, params=params, json=payload) as response:
            if response.status == 200:
                return await response.json()
            elif response.status == 429:
                print(f"⚠️ Слишком много запросов (429). Ждём...")
                await asyncio.sleep(5)
                return await self.get_acceptance_options(warehouse_id, barcode, quantity)
            else:
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

            # Проверяем опции приёмки для каждого склада
            options = await self.get_acceptance_options(warehouse_id=warehouse["ID"])
            
            if options and "result" in options:
                for result in options["result"]:
                    # Проверяем, есть ли доступные слоты
                    # Структура ответа может варьироваться, адаптируйте под реальный ответ
                    if result:
                        slot_info = {
                            "warehouse_id": warehouse["ID"],
                            "warehouse_name": warehouse["name"],
                            "address": warehouse["address"],
                            "work_time": warehouse["workTime"],
                            "details": result
                        }
                        available_slots.append(slot_info)

        return available_slots

    def _generate_slot_key(self, slot: dict) -> str:
        """Генерация уникального ключа для слота"""
        return f"{slot['warehouse_id']}_{slot.get('warehouse_name', '')}"

    async def send_telegram_message(self, message: str):
        """Отправка сообщения в Telegram"""
        if not TG_CHAT_ID:
            print(f"📩 Сообщение: {message}")
            return

        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    if response.status != 200:
                        print(f"Ошибка отправки в Telegram: {response.status}")
        except Exception as e:
            print(f"Ошибка Telegram: {e}")

    async def monitor(self):
        """Основной цикл мониторинга"""
        print("🚀 Запуск мониторинга складов WB...")
        await self.start()

        try:
            while True:
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
                                print(f"✅ Найдено: {slot['warehouse_name']}")
                        
                        self.last_notified_slots = current_slots
                    else:
                        now = datetime.now().strftime("%H:%M:%S")
                        print(f"[{now}] Доступных слотов нет")
                        self.last_notified_slots.clear()

                except Exception as e:
                    print(f"❌ Ошибка в цикле мониторинга: {e}")

                await asyncio.sleep(CHECK_INTERVAL)

        finally:
            await self.stop()

    def _format_notification(self, slot: dict) -> str:
        """Форматирование уведомления"""
        return f"""
🔔 <b>Доступен слот для отгрузки!</b>

📦 <b>Склад:</b> {slot['warehouse_name']}
🆔 <b>ID:</b> {slot['warehouse_id']}
📍 <b>Адрес:</b> {slot['address']}
⏰ <b>Режим работы:</b> {slot['work_time']}

⚡️ Успейте записаться!
        """.strip()


async def main():
    monitor = WildberriesMonitor()
    await monitor.monitor()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Мониторинг остановлен пользователем")
