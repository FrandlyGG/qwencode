import os
from pathlib import Path
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# Конфигурация
WB_API_TOKEN = os.getenv("WB_API_TOKEN", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# Интервал проверки в секундах
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

# ID складов для мониторинга (через запятую в env)
MONITOR_WAREHOUSE_IDS_RAW = os.getenv("MONITOR_WAREHOUSE_IDS", "")
MONITOR_WAREHOUSE_IDS: list[int] = (
    [int(x.strip()) for x in MONITOR_WAREHOUSE_IDS_RAW.split(",") if x.strip()]
    if MONITOR_WAREHOUSE_IDS_RAW
    else []
)

# Настройки логирования
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "wb_monitor.log")
