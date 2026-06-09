import re
import json
import logging
import asyncio
from decimal import Decimal, InvalidOperation
from typing import Optional, List
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from urllib.parse import urljoin
from playwright.async_api import async_playwright, Page

from scrapers.base_scraper import AsyncBaseScraper


# ==========================================
# МОДЕЛІ ДАНИХ (E-commerce Архітектура)
# ==========================================
@dataclass
class Variation:
    name: str
    value: str
    price: Optional[Decimal] = None
    sku: Optional[str] = None
    in_stock: bool = False
    images: List[str] = field(default_factory=list)


@dataclass
class Product:
    url: str
    source: str
    title: Optional[str] = None
    category: Optional[str] = None
    description_text: Optional[str] = None
    description_html: Optional[str] = None
    variations: List[Variation] = field(default_factory=list)


# ==========================================
# ПАРСЕР НА БАЗІ PLAYWRIGHT (ФІНАЛ)
# ==========================================
class PlaywrightChernomorScraper(AsyncBaseScraper):
    """
    Екстрактор даних з використанням реального браузера (Chromium).
    Ідеально вирішує проблему JS-капч та AJAX-підвантаження модифікацій.
    """

    def __init__(self, concurrency_limit: int = 5):
        super().__init__(concurrency_limit=concurrency_limit)
        self.domain = "https://chernomor-cosmetics.ua"
        self.playwright = None
        self.browser = None

    async def __aenter__(self):
        await super().__aenter__()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        await super().__aexit__(exc_type, exc_val, exc_tb)

    # --- ХЕЛПЕРИ ДЛЯ ВИТЯГУВАННЯ ДАНИХ ---
    def _extract_sku(self, soup: BeautifulSoup) -> Optional[str]:
        sku_meta = soup.select_one('meta[itemprop="sku"]')
        if sku_meta and sku_meta.get('content'):
            return sku_meta.get('content')
        sku_elem = soup.select_one('.product-header__code, .product__code')
        if sku_elem:
            return re.sub(r'(?i)(артикул|:|\s)', '', sku_elem.get_text(strip=True))
        return None

    def _extract_price(self, soup: BeautifulSoup) -> Optional[Decimal]:
        price_str = None
        price_elem = soup.select_one('.product-price__item, .price')
        if price_elem:
            price_str = price_elem.get('content') or price_elem.get_text(strip=True)
        else:
            price_meta = soup.select_one('meta[property="product:price:amount"], meta[itemprop="price"]')
            if price_meta and price_meta.get('content'):
                price_str = price_meta.get('content')

        if price_str:
            match = re.search(r'\d+[\s\d]*[.,]?\d*', str(price_str))
            if match:
                try:
                    clean_price = match.group().replace(' ', '').replace(',', '.')
                    return Decimal(clean_price.rstrip('.'))
                except InvalidOperation:
                    pass
        return None

    def _extract_stock(self, soup: BeautifulSoup) -> bool:
        avail_meta = soup.select_one('meta[itemprop="availability"]')
        if avail_meta:
            content = (avail_meta.get('href') or avail_meta.get('content') or '').lower()
            if 'instock' in content:
                return True
        status_text = soup.select_one('.product-header__availability, .product__status')
        if status_text:
            text = status_text.get_text(strip=True).lower()
            if 'немає' in text or 'нет' in text or 'out' in text:
                return False
            if 'наявн' in text or 'є' in text:
                return True
        return False

    def _extract_images(self, soup: BeautifulSoup) -> List[str]:
        images = []
        for img_link in soup.select('.gallery__link[data-href], .gallery__thumb-link[data-href]'):
            href = img_link.get('data-href')
            if href:
                full_url = urljoin(self.domain, href)
                if full_url not in images:
                    images.append(full_url)
        if not images:
            og_img = soup.select_one('meta[property="og:image"]')
            if og_img and og_img.get('content'):
                images.append(og_img.get('content'))
        return images

    # --- ОСНОВНА ЛОГІКА ПАРСИНГУ ---
    async def parse_product(self, url: str) -> Optional[Product]:
        self.logger.info("Відкриваємо сторінку: %s", url)

        if not self.browser:
            self.logger.error("Браузер не ініціалізовано. Використовуйте 'async with PlaywrightChernomorScraper()'.")
            return None

        context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector('.product-price', timeout=10000)
            except Exception:
                self.logger.warning("Не вдалося знайти ціну. Можливо, спрацювала капча.")

            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            product = Product(url=url, source="chernomor-cosmetics.ua")

            title_meta = soup.select_one('meta[property="og:title"]')
            product.title = title_meta.get('content') if title_meta else (
                soup.find('h1').get_text(strip=True) if soup.find('h1') else None)

            cat_meta = soup.select_one('meta[property="product:category"]')
            if cat_meta:
                product.category = cat_meta.get('content')
            else:
                bc = soup.select('.breadcrumbs-i span[itemprop="name"]')
                if len(bc) > 2:
                    product.category = bc[-2].get_text(strip=True)

            desc_elem = soup.select_one('.product-description[itemprop="description"]')
            if desc_elem:
                product.description_html = str(desc_elem)
                product.description_text = desc_elem.get_text(separator='\n', strip=True)


            product.variations = await self._extract_variations_with_browser(page, soup)

            return product
        except Exception as e:
            self.logger.error("Помилка Playwright для %s: %s", url, e, exc_info=True)
            return None
        finally:
            await context.close()

    async def _extract_variations_with_browser(self, page: Page, initial_soup: BeautifulSoup) -> List[Variation]:
        variations = []

        form = initial_soup.select_one('form[data-action*="load-modification"]')
        if not form:
            variations.append(Variation(
                name="Default",
                value="Default",
                sku=self._extract_sku(initial_soup),
                price=self._extract_price(initial_soup),
                in_stock=self._extract_stock(initial_soup),
                images=self._extract_images(initial_soup)
            ))
            return variations

        select_elem = form.find('select')
        if not select_elem:
            return variations

        select_name = select_elem.get('name')

        attr_label = ""
        mod_block = select_elem.find_parent('div', class_=re.compile(r'modification'))
        if mod_block:
            title_elem = mod_block.select_one('.modification__title')
            if title_elem:
                attr_label = title_elem.get_text(strip=True).strip(':')

        options = select_elem.find_all('option')

        for opt in options:
            val_id = opt.get('value')
            text = opt.get('data-selectedtext') or opt.get_text(strip=True)

            if not val_id or "оберіть" in text.lower():
                continue

            var_name = f"{attr_label}: {text}" if attr_label else text

            if opt.has_attr('selected'):
                variations.append(Variation(
                    name=var_name,
                    value=text,
                    sku=self._extract_sku(initial_soup),
                    price=self._extract_price(initial_soup),
                    in_stock=self._extract_stock(initial_soup),
                    images=self._extract_images(initial_soup)
                ))
            else:
                try:
                    js_code = f"""
                        if (typeof jQuery !== 'undefined') {{
                            jQuery('select[name="{select_name}"]').val("{val_id}").trigger('change');
                        }} else {{
                            const el = document.querySelector('select[name="{select_name}"]');
                            if(el) {{
                                el.value = "{val_id}";
                                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            }}
                        }}
                    """

                    async with page.expect_response(
                            lambda response: "load-modification" in response.url and response.status == 200,
                            timeout=10000):
                        await page.evaluate(js_code)

                    await page.wait_for_timeout(300)

                    updated_html = await page.content()
                    updated_soup = BeautifulSoup(updated_html, "lxml")

                    variations.append(Variation(
                        name=var_name,
                        value=text,
                        sku=self._extract_sku(updated_soup),
                        price=self._extract_price(updated_soup),
                        in_stock=self._extract_stock(updated_soup),
                        images=self._extract_images(updated_soup)
                    ))
                except Exception as e:
                    self.logger.warning(f"Не вдалося переключити на '{text}': {e}")
                    variations.append(Variation(
                        name=var_name, value=text, sku=self._extract_sku(initial_soup),
                        price=self._extract_price(initial_soup),
                        in_stock=self._extract_stock(initial_soup), images=self._extract_images(initial_soup)
                    ))

        unique_vars = []
        seen = set()
        for v in variations:
            if v.value not in seen:
                seen.add(v.value)
                unique_vars.append(v)

        return unique_vars
