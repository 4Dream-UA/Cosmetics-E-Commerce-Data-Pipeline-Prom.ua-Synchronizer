import asyncio
import aiohttp
import logging
from typing import Dict, List, Optional
from urllib.parse import urljoin, quote_plus
from bs4 import BeautifulSoup
import re
import json

from src.scrapers.base_scraper import AsyncBaseScraper


class AsyncSoloCrawler:
    """
    Асинхронний краулер для solo.ua.
    Використовує надійну логіку переходу по сторінках (while True + Next button),
    але працює на базі швидких асинхронних сесій aiohttp.
    """

    def __init__(self):
        self.domain = "https://solo.ua"
        self.logger = logging.getLogger(self.__class__.__name__)
        self.semaphore = asyncio.Semaphore(15)
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
        }

    async def fetch_soup(self, session: aiohttp.ClientSession, url: str) -> Optional[BeautifulSoup]:
        """Асинхронно завантажує сторінку та повертає BeautifulSoup об'єкт."""
        async with self.semaphore:
            for attempt in range(1, 4):
                try:
                    async with session.get(url, headers=self.headers, timeout=20) as response:
                        response.raise_for_status()
                        html = await response.text()
                        return BeautifulSoup(html, 'lxml')
                except Exception as e:
                    if attempt == 3:
                        self.logger.error("Помилка завантаження %s: %s", url, e)
                        return None
                    await asyncio.sleep(2)

    async def get_product_links(self, target_brands: List[str]) -> Dict[str, List[str]]:
        self.logger.info("Розпочинаю асинхронний пошук для: %s", target_brands)
        result_links = {brand: [] for brand in target_brands}

        connector = aiohttp.TCPConnector(limit=20)
        async with aiohttp.ClientSession(connector=connector) as session:

            brand_urls = await self._find_brand_category_urls(session, target_brands)

            tasks = []
            for brand, category_url in brand_urls.items():
                task = asyncio.create_task(self._scrape_pagination(session, brand, category_url))
                tasks.append(task)

            results = await asyncio.gather(*tasks)

            for brand, links in results:
                result_links[brand] = links
                self.logger.info("==> ФІНАЛ: Зібрано %d унікальних посилань для %s", len(links), brand)

        return result_links

    async def _find_brand_category_urls(self, session: aiohttp.ClientSession, target_brands: List[str]) -> Dict[
        str, str]:
        brand_urls = {}
        soup = await self.fetch_soup(session, self.domain)

        if not soup:
            self.logger.error("Не вдалося завантажити головну сторінку.")
            return brand_urls

        menu_links = soup.select('.menu-brands-category a, .horizontal-menu a')
        for link in menu_links:
            text = link.get_text(strip=True).lower()
            href = link.get('href')
            if href and text:
                matched_brand = next((b for b in target_brands if b.lower() in text or text in b.lower()), None)
                if matched_brand and matched_brand not in brand_urls:
                    brand_urls[matched_brand] = urljoin(self.domain, href)

        return brand_urls

    async def _scrape_pagination(self, session: aiohttp.ClientSession, brand: str, category_url: str) -> tuple:
        """
        Класичний і найнадійніший прохід по пагінації (через перевірку кнопки "Next").
        """
        product_links = []
        page = 1

        while True:
            join_char = '&' if '?' in category_url else '?'
            url = f"{category_url}{join_char}p={page}" if page > 1 else category_url

            self.logger.info("[%s] Сканування сторінки %d: %s", brand, page, url)

            soup = await self.fetch_soup(session, url)
            if not soup:
                break

            page_links = self._extract_links_from_soup(soup)

            if not page_links:
                self.logger.info("[%s] Товарів на сторінці %d не знайдено. Зупинка.", brand, page)
                break

            product_links.extend(page_links)

            next_btn = soup.select_one('.pages-item-next a, .next')
            if not next_btn:
                self.logger.info("[%s] Кнопку 'Наступна' не знайдено на сторінці %d. Зупинка.", brand, page)
                break

            page += 1

        unique_links = list(set(product_links))
        return brand, unique_links

    def _extract_links_from_soup(self, soup: BeautifulSoup) -> List[str]:
        links = []
        items = soup.select('a.product-item-link, h2.product-name a')

        for item in items:
            href = item.get('href')
            if href and ('/product/' in href or '.html' in href):
                links.append(href)

        if not links:
            images = soup.select('.product-item-photo')
            for img_link in images:
                href = img_link.get('href')
                if href:
                    links.append(href)

        return links


class AsyncChernomorCrawler(AsyncBaseScraper):
    """
    Асинхронний краулер для chernomor-cosmetics.ua.
    Optimized Senior Edition:
    - Обхід JS Challenge (Cloudflare/Horoshop Anti-Bot).
    - Розумна асинхронність (Staggered starts) для швидкості без бану.
    - Спільне використання розблокованої сесії.
    """

    def __init__(self):
        super().__init__(concurrency_limit=4)
        self.domain = "https://chernomor-cosmetics.ua"
        self.logger = logging.getLogger(self.__class__.__name__)

        self.delay = 0.5
        self.max_retries = 3

    async def get_soup(self, url: str, **kwargs) -> Optional[BeautifulSoup]:
        """
        Перехоплювач відповідей. Аналізує, що віддав сервер:
        реальний HTML, AJAX-JSON або сторінку-заглушку з JS Challenge.
        """
        html = await self.fetch_html(url, **kwargs)
        if not html:
            return None

        # ==========================================
        # ХАК 1: ОБХІД ЗАХИСТУ JS CHALLENGE
        # ==========================================
        if "challenge_passed=" in html:
            self.logger.warning("⚠️ Сервер видав Anti-Bot заглушку (JS Challenge). Зламуємо...")
            match = re.search(r'const defaultHash = ["\']([^"\']+)["\']', html)
            if match:
                hash_val = match.group(1)
                self.logger.info("🔓 Хеш знайдено! Встановлюємо cookie та повторюємо запит...")

                self.session.cookie_jar.update_cookies({'challenge_passed': hash_val})
                await asyncio.sleep(0.5)

                html = await self.fetch_html(url, **kwargs)
                if not html:
                    return None
            else:
                self.logger.error("❌ Не вдалося знайти defaultHash у скрипті захисту.")

        # ==========================================
        # ХАК 2: РОЗПАКУВАННЯ AJAX ПАГІНАЦІЇ ХОРОШОПУ
        # ==========================================
        html_stripped = html.strip()
        if html_stripped.startswith('{') and html_stripped.endswith('}'):
            try:
                data = json.loads(html_stripped)
                content = data.get('content') or data.get('html') or data.get('data', '')
                if content:
                    self.logger.info("📦 Розпаковано прихований AJAX JSON-відповідь.")
                    html = content
            except json.JSONDecodeError:
                pass

        return BeautifulSoup(html, "lxml")

    async def get_product_links(self, target_brands: List[str]) -> Dict[str, List[str]]:
        self.logger.info("🚀 Запускаємо швидкісний бронебійний парсинг для Chernomor...")
        result_links = {brand: [] for brand in target_brands}

        await self.get_soup(self.domain)
        await asyncio.sleep(0.5)

        tasks = []
        for i, brand in enumerate(target_brands):
            clean_brand = re.sub(r'\d+', '', brand).strip()
            stagger_delay = i * 1.5

            task = asyncio.create_task(
                self._search_and_scrape(brand, clean_brand, stagger_delay)
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks)

        for brand, links in results:
            result_links[brand] = links
            self.logger.info("==> ФІНАЛ: Зібрано %d ЧИСТИХ посилань для %s", len(links), brand)

        return result_links

    async def _search_and_scrape(self, original_brand: str, search_brand: str, stagger_delay: float) -> tuple:
        if stagger_delay > 0:
            await asyncio.sleep(stagger_delay)

        product_links = []
        page = 1

        while True:
            if page == 1:
                current_url = f"{self.domain}/kataloh/search/?q={quote_plus(search_brand)}"
            else:
                current_url = f"{self.domain}/kataloh/search/filter/page={page}/?q={quote_plus(search_brand)}"

            self.logger.info("[%s] Парсинг сторінки %d: %s", original_brand, page, current_url)

            soup = await self.get_soup(current_url)
            if not soup:
                self.logger.warning("[%s] Сторінка недоступна. Перериваємо.", original_brand)
                break

            page_links = self._extract_links_from_soup(soup, search_brand)

            if not page_links:
                self.logger.info("[%s] Товари на сторінці %d закінчилися (пуста сітка).", original_brand, page)
                break

            product_links.extend(page_links)

            page += 1
            if page > 50:
                break

        return original_brand, list(set(product_links))

    def _extract_links_from_soup(self, soup: BeautifulSoup, search_brand: str) -> List[str]:
        links = []
        brand_lower = search_brand.lower()

        cards = soup.select('.catalogCard, .product-layout, .product-item, .item')

        for card in cards:
            card_text = card.get_text(separator=' ', strip=True).lower()
            a_tag = card.select_one('.catalogCard-title, .product-title, a.name, h3 a, h4 a, a')

            if a_tag and a_tag.get('href'):
                href = a_tag.get('href')
                full_url = urljoin(self.domain, href)

                brand_words = [w for w in brand_lower.split() if len(w) > 3]

                if brand_lower in card_text or brand_lower in href.lower() or any(w in card_text for w in brand_words):
                    if not any(skip in full_url for skip in ['cart', 'compare', 'wishlist', 'login', 'action']):
                        links.append(full_url)

        return links
