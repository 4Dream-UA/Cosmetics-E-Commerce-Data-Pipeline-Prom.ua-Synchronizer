import os
import json
import asyncio
import logging
from pathlib import Path
from decimal import Decimal
from dataclasses import asdict

# Імпорти модулів проєкту згідно з архітектурою
from src.processors.crawler import AsyncSoloCrawler, AsyncChernomorCrawler
from src.scrapers.solo import AsyncSoloScraper
from src.scrapers.chernomor import PlaywrightChernomorScraper
from src.processors.text_cleaner import TextCleaner
from src.exporters.yml_builder import PromYmlBuilder

# ==========================================
# НАЛАШТУВАННЯ ТА ДОПОМІЖНІ КЛАСИ
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("MainOrchestrator")

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "data" / "input"
OUTPUT_DIR = BASE_DIR / "data" / "output"
CONFIG_FILE = BASE_DIR / "config" / "target_brands.json"

# Створюємо директорії, якщо їх немає
INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class DecimalEncoder(json.JSONEncoder):
    """Кастомний енкодер для серіалізації Decimal у JSON."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def save_json(data, filepath: Path):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4, cls=DecimalEncoder)


def load_json(filepath: Path):
    if not filepath.exists():
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_links(links: list, filepath: Path):
    with open(filepath, 'w', encoding='utf-8') as f:
        for link in links:
            f.write(f"{link}\n")


def load_links(filepath: Path) -> list:
    if not filepath.exists():
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip()]


async def phase_1_crawling(target_brands: dict):
    """Фаза 1: Збір посилань на товари."""
    logger.info("=== ФАЗА 1: ЗБІР ПОСИЛАНЬ (CRAWLING) ===")

    # Дістаємо списки брендів для кожного сайту
    solo_brands = target_brands.get("solo", {}).get("brands", [])
    chernomor_brands = target_brands.get("chernomor", {}).get("brands", [])

    # Solo.ua
    if solo_brands:
        solo_crawler = AsyncSoloCrawler()
        solo_links_dict = await solo_crawler.get_product_links(solo_brands)
        solo_all_links = [url for links in solo_links_dict.values() for url in links]
        save_links(solo_all_links, INPUT_DIR / "solo_urls.txt")
        logger.info(f"Збережено {len(solo_all_links)} посилань Solo.")
    else:
        logger.warning("Бренди для Solo не знайдено в конфігу.")

    # Chernomor
    if chernomor_brands:
        chernomor_crawler = AsyncChernomorCrawler()
        async with chernomor_crawler:
            chernomor_links_dict = await chernomor_crawler.get_product_links(chernomor_brands)
        chernomor_all_links = [url for links in chernomor_links_dict.values() for url in links]
        save_links(chernomor_all_links, INPUT_DIR / "chernomor_urls.txt")
        logger.info(f"Збережено {len(chernomor_all_links)} посилань Chernomor.")
    else:
        logger.warning("Бренди для Chernomor не знайдено в конфігу.")



async def phase_2_scraping():
    """Фаза 2: Парсинг карток товарів."""
    logger.info("=== ФАЗА 2: ПАРСИНГ ТОВАРІВ (SCRAPING) ===")

    solo_urls = load_links(INPUT_DIR / "solo_urls.txt")
    chernomor_urls = load_links(INPUT_DIR / "chernomor_urls.txt")

    # Solo Scraping
    raw_solo_products = []
    if solo_urls:
        logger.info(f"Парсинг {len(solo_urls)} товарів Solo...")
        async with AsyncSoloScraper() as solo_scraper:
            tasks = [solo_scraper.parse_product(url) for url in solo_urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if res and not isinstance(res, Exception):
                    raw_solo_products.append(asdict(res))
        save_json(raw_solo_products, OUTPUT_DIR / "raw_solo.json")

    # Chernomor Scraping (Playwright)
    raw_chernomor_products = []
    if chernomor_urls:
        logger.info(f"Парсинг {len(chernomor_urls)} товарів Chernomor...")
        async with PlaywrightChernomorScraper() as chernomor_scraper:
            for url in chernomor_urls:  # Виконуємо послідовно або батчами для стабільності браузера
                try:
                    product = await chernomor_scraper.parse_product(url)
                    if product:
                        raw_chernomor_products.append(asdict(product))
                except Exception as e:
                    logger.error(f"Помилка при парсингу {url}: {e}")
        save_json(raw_chernomor_products, OUTPUT_DIR / "raw_chernomor.json")

    return raw_solo_products, raw_chernomor_products


def phase_3_cleaning(raw_solo: list, raw_chernomor: list):
    """Фаза 3: Очищення описів через TextCleaner та збереження."""
    logger.info("=== ФАЗА 3: ОЧИЩЕННЯ ДАНИХ (CLEANING) ===")

    all_raw_products = raw_solo + raw_chernomor
    cleaned_products = []

    for prod in all_raw_products:
        # Проганяємо через TextCleaner
        if prod.get("description_html"):
            prod["description_html"] = TextCleaner.clean_html(prod["description_html"])
        if prod.get("description_text"):
            prod["description_text"] = TextCleaner.clean_text(prod["description_text"])

        cleaned_products.append(prod)

    save_json(cleaned_products, OUTPUT_DIR / "cleaned_products.json")
    logger.info(f"Очищено та збережено {len(cleaned_products)} товарів у cleaned_products.json")
    return cleaned_products


def phase_4_building(cleaned_products: list):
    """Фаза 4: Генерація XML фіду для Prom.ua."""
    logger.info("=== ФАЗА 4: ГЕНЕРАЦІЯ ФІДУ (YML BUILDING) ===")

    builder = PromYmlBuilder(
        shop_name="Мій Магазин",
        company_name="ТОВ Компанія",
        shop_url="https://myshop.ua"
    )

    for product in cleaned_products:
        builder.add_product(product)

    feed_path = BASE_DIR / "prom_feed.xml"
    builder.save(str(feed_path))
    logger.info(f"=== УСПІХ! Фід згенеровано: {feed_path} ===")


# ==========================================
# ЗАПУСК
# ==========================================
async def main():
    logger.info("Старт пайплайну парсингу...")

    if not CONFIG_FILE.exists():
        logger.error(f"Файл конфігурації {CONFIG_FILE} не знайдено!")
        return

    target_brands = load_json(CONFIG_FILE)
    logger.info("Конфіг завантажено успішно.")

    # 1. Краулінг (пошук посилань) - передаємо весь словник
    await phase_1_crawling(target_brands)

    # 2. Парсинг товарів
    raw_solo, raw_chernomor = await phase_2_scraping()

    # 3. Очищення тексту
    cleaned_products = phase_3_cleaning(raw_solo, raw_chernomor)

    # 4. Білд фіду
    phase_4_building(cleaned_products)

if __name__ == "__main__":
    asyncio.run(main())