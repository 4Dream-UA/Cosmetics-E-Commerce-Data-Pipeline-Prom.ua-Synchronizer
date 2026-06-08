import asyncio
import aiohttp
import logging
from typing import Dict, List, Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup


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