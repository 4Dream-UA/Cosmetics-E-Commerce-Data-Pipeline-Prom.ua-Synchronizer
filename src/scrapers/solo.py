import re
import json
import logging
import asyncio
from decimal import Decimal, InvalidOperation
from typing import Optional, List
from bs4 import BeautifulSoup
from dataclasses import dataclass, field

from scrapers.base_scraper import AsyncBaseScraper


# ==========================================
# МОДЕЛІ ДАНИХ
# ==========================================
@dataclass
class Variation:
    name: str
    value: str
    price: Optional[Decimal] = None
    images: List[str] = field(default_factory=list)


@dataclass
class Product:
    url: str
    source: str
    title: Optional[str] = None
    sku: Optional[str] = None
    price: Optional[Decimal] = None
    in_stock: bool = False
    images: List[str] = field(default_factory=list)
    category: Optional[str] = None
    description_text: Optional[str] = None
    description_html: Optional[str] = None
    variations: List[Variation] = field(default_factory=list)


# ==========================================
# ГОЛОВНИЙ КЛАС ПАРСЕРА
# ==========================================
class AsyncSoloScraper(AsyncBaseScraper):
    """
    Асинхронний екстрактор даних для карток товарів з solo.ua.
    """

    def __init__(self, concurrency_limit: int = 15):
        super().__init__(concurrency_limit=concurrency_limit)
        self.domain = "https://solo.ua"

    async def parse_product(self, url: str) -> Optional[Product]:
        """Асинхронний метод парсингу сторінки товару."""
        self.logger.info("Аналіз картки товару: %s", url)

        soup = await self.get_soup(url)

        if not soup:
            self.logger.error("Не вдалося отримати HTML для %s", url)
            return None

        product = Product(url=url, source="solo.ua")

        try:
            # 1. Назва товару
            h1 = soup.find('h1')
            if h1:
                product.title = h1.get_text(separator=' ', strip=True)

            if not product.title:
                name_elem = soup.find(class_=re.compile(r'product-name'))
                if name_elem:
                    product.title = name_elem.get_text(strip=True)
                else:
                    title_meta = soup.find(itemprop="name")
                    product.title = title_meta.get('content') or title_meta.get_text(strip=True) if title_meta else None

            # 2. Артикул (SKU)
            sku_block = soup.find(class_=re.compile(r'sku|product-code'))
            if sku_block:
                value_span = sku_block.find('span', class_='value') or sku_block.find('span', itemprop='sku')
                if value_span:
                    product.sku = value_span.get_text(strip=True)
                else:
                    raw_sku_text = sku_block.get_text(strip=True)
                    match = re.search(r'(\d+)$', raw_sku_text)
                    if match:
                        product.sku = match.group(1)
                    else:
                        product.sku = re.sub(r'(?i)(код|артикул|наявність.*?:.*?\n)', '', raw_sku_text).strip()

            if not product.sku:
                sku_meta = soup.find(itemprop="sku")
                if sku_meta:
                    product.sku = sku_meta.get('content') or sku_meta.get_text(strip=True)

            # 3. Базова Ціна
            price_str = None
            price_elem = soup.find(class_=re.compile(r'price'))
            if price_elem:
                price_str = price_elem.get_text()

            if price_str:
                match = re.search(r'\d+[\s\d]*[.,]?\d*', price_str)
                if match:
                    try:
                        clean_price = match.group().replace(' ', '').replace(',', '.')
                        clean_price = clean_price.rstrip('.')
                        product.price = Decimal(clean_price)
                    except InvalidOperation:
                        self.logger.warning("Помилка конвертації ціни: %s", price_str)

            # 4. Наявність
            avail_meta = soup.find(itemprop="availability")
            if avail_meta:
                product.in_stock = "InStock" in avail_meta.get('href', '')
            else:
                buy_btn = soup.select_one('.btn-buy, .add-to-cart, button[type="submit"]')
                stock_elem = soup.find(class_=re.compile(r'stock|availability'))

                if stock_elem:
                    product.in_stock = "в наявності" in stock_elem.get_text(strip=True).lower()
                else:
                    product.in_stock = bool(buy_btn)

            # 5. Хлібні крихти (Категорія)
            bc_container = soup.select_one('.breadcrumbs, .breadcrumb, ul.b-crumbs, [itemprop="breadcrumb"]')
            if bc_container:
                links = bc_container.find_all('a')
                if len(links) > 1:
                    product.category = links[-2].get_text(strip=True)
                elif len(links) == 1:
                    product.category = links[0].get_text(strip=True)

            # 6. ЗОБРАЖЕННЯ (ГАЛЕРЕЯ)
            gallery_scripts = soup.find_all('script', type='text/x-magento-init')
            for script in gallery_scripts:
                if script.string and '[data-gallery-role=gallery-placeholder]' in script.string:
                    try:
                        g_data = json.loads(script.string)
                        gallery = g_data.get('[data-gallery-role=gallery-placeholder]', {}).get('mage/gallery/gallery',
                                                                                                {}).get('data', [])
                        for img in gallery:
                            img_url = img.get('full') or img.get('img')
                            if img_url and img_url not in product.images:
                                product.images.append(img_url)
                    except json.JSONDecodeError:
                        pass

            if not product.images:
                og_img = soup.find("meta", property="og:image")
                if og_img and og_img.get('content'):
                    product.images.append(og_img.get('content'))
                else:
                    img_elem = soup.select_one('.product-image img, .gallery img, img.main-image')
                    if img_elem:
                        img_url = img_elem.get('src') or img_elem.get('data-src')
                        if img_url:
                            product.images.append(self.domain + img_url if img_url.startswith('/') else img_url)

            # 7. Опис
            desc_elem = soup.select_one('div.product.attribute.description, div[itemprop="description"], #description')
            if not desc_elem:
                desc_elem = soup.select_one('.product.data.items .data.item.content, #description_tab')

            if desc_elem:
                desc_text_check = desc_elem.get_text(strip=True)
                if "Бренди" not in desc_text_check[:100] and "Увійти" not in desc_text_check:
                    for header in desc_elem.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
                        header.decompose()

                    product.description_html = str(desc_elem)
                    cleaned_lines = []

                    for line in desc_elem.get_text("\n").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        if not re.search(r'[а-яА-ЯєЄіІїЇґҐ]', line) and len(line) > 10:
                            continue
                        if line.startswith("Опис"):
                            continue
                        cleaned_lines.append(line)

                    product.description_text = "\n".join(cleaned_lines)
                else:
                    self.logger.warning("Захоплено невірний блок опису (мега-меню).")

            # 8. РІЗНОВИДИ
            product.variations = self._extract_variations(soup)

            return product

        except Exception as e:
            self.logger.error("Помилка парсингу %s: %s", url, e, exc_info=True)
            return None

    def _extract_variations(self, soup: BeautifulSoup) -> List[Variation]:
        variations = []
        scripts = soup.find_all('script', type='text/x-magento-init')
        for script in scripts:
            if not script.string:
                continue

            if 'spConfig' in script.string or 'jsonConfig' in script.string:
                try:
                    data = json.loads(script.string)

                    def find_config_root(obj):
                        if isinstance(obj, dict) and 'attributes' in obj and 'optionPrices' in obj:
                            return obj
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                res = find_config_root(v)
                                if res: return res
                        return None

                    sp_config = find_config_root(data)

                    if sp_config:
                        attributes = sp_config.get('attributes', {})
                        option_prices = sp_config.get('optionPrices', {})
                        images_data = sp_config.get('images', {})

                        for attr_id, attr_data in attributes.items():
                            attr_label = attr_data.get('label', '')
                            options = attr_data.get('options', [])

                            for opt in options:
                                label = opt.get('label')
                                val = str(opt.get('id'))
                                products = opt.get('products', [])

                                if label and val and products:
                                    p_id = str(products[0])
                                    var_price = None
                                    if p_id in option_prices:
                                        amt = option_prices[p_id].get('finalPrice', {}).get('amount')
                                        if amt is not None:
                                            var_price = Decimal(str(amt))

                                    var_images = []
                                    if p_id in images_data:
                                        for img in images_data[p_id]:
                                            img_url = img.get('full') or img.get('img')
                                            if img_url:
                                                var_images.append(img_url)

                                    if not re.search(r'(?i)(оберіть|виберіть|choose|select)', label):
                                        variations.append(Variation(
                                            name=f"{attr_label}: {label}" if attr_label else label,
                                            value=label,
                                            price=var_price,
                                            images=var_images
                                        ))

                        if variations:
                            return self._deduplicate_variations(variations)
                except json.JSONDecodeError:
                    continue

        container = soup.find('form', id='product_addtocart_form') or soup.select_one('.product-info-main')
        if container:
            selects = container.find_all('select')
            for select in selects:
                for opt in select.find_all('option'):
                    val = opt.get('value')
                    text = opt.get_text(strip=True)
                    if val and val.strip() and text:
                        if not re.search(r'(?i)(оберіть|виберіть|choose|select)', text):
                            variations.append(Variation(name=text, value=val))

            if not variations:
                swatches = container.select('.swatch-option')
                for swatch in swatches:
                    val = swatch.get('option-id') or swatch.get('data-option-id')
                    text = swatch.get_text(strip=True) or swatch.get('option-label')
                    if val and text:
                        variations.append(Variation(name=text, value=val))

        return self._deduplicate_variations(variations)

    def _deduplicate_variations(self, variations: List[Variation]) -> List[Variation]:
        unique_variations = []
        seen = set()
        for v in variations:
            identifier = f"{v.name}-{v.value}"
            if identifier not in seen:
                seen.add(identifier)
                unique_variations.append(v)
        return unique_variations
