import re
import json
import logging
import asyncio
import html as html_modifier
from decimal import Decimal, InvalidOperation
from typing import Optional, List
from bs4 import BeautifulSoup
from dataclasses import dataclass, field

from src.scrapers.base_scraper import AsyncBaseScraper


# ==========================================
# МОДЕЛІ ДАНИХ (Уніфікована Архітектура)
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
    description_text: Optional[str] = None  # Повернули роздільне поле
    description_html: Optional[str] = None  # Повернули роздільне поле
    variations: List[Variation] = field(default_factory=list)


# ==========================================
# ГОЛОВНИЙ КЛАС ПАРСЕРА
# ==========================================
class AsyncSoloScraper(AsyncBaseScraper):
    """
    Асинхронний екстрактор даних для карток товарів з solo.ua (Magento).
    """

    def __init__(self, concurrency_limit: int = 15):
        super().__init__(concurrency_limit=concurrency_limit)
        self.domain = "https://solo.ua"

    # --- СМАРТ-ЧИСТКА ДЛЯ DESCRIPTION_HTML ---
    def _clean_description_html(self, raw_html_node) -> Optional[str]:
        if not raw_html_node:
            return None

        soup = BeautifulSoup(str(raw_html_node), 'html.parser')

        # Видаляємо дублюючі заголовки
        for header in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
            header.decompose()

        for trash in soup.find_all(['script', 'style', 'iframe']):
            trash.decompose()

        allowed_attrs = ['href', 'title', 'alt']
        for tag in soup.find_all(True):
            clean_attrs = {}
            for attr, value in tag.attrs.items():
                if attr in allowed_attrs:
                    clean_attrs[attr] = value
            tag.attrs = clean_attrs

        for empty_tag in soup.find_all(['div', 'p', 'span']):
            if not empty_tag.contents and not empty_tag.get_text(strip=True):
                empty_tag.decompose()

        clean_html_str = html_modifier.unescape(str(soup))
        clean_html_str = re.sub(r'\n\s*\n', '\n', clean_html_str)
        return clean_html_str.strip()

    # --- ОСНОВНИЙ МЕТОД ПАРСИНГУ ---
    async def parse_product(self, url: str) -> Optional[Product]:
        self.logger.info("Аналіз картки товару: %s", url)

        soup = await self.get_soup(url)

        if not soup:
            self.logger.error("Не вдалося отримати HTML для %s", url)
            return None

        product = Product(url=url, source="solo.ua")

        try:
            # === 1. ЗАГАЛЬНІ ДАНІ (Тайтл) ===
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

            # === 2. КАТЕГОРІЯ ===
            bc_container = soup.select_one('.breadcrumbs, .breadcrumb, ul.b-crumbs, [itemprop="breadcrumb"]')
            if bc_container:
                links = bc_container.find_all('a')
                if len(links) > 1:
                    product.category = links[-2].get_text(strip=True)
                elif len(links) == 1:
                    product.category = links[0].get_text(strip=True)

            # === 3. ОПИС (Розділяємо на TEXT та HTML) ===
            desc_node = soup.select_one('div.product.attribute.description, div[itemprop="description"], #description')
            if not desc_node:
                desc_node = soup.select_one('.product.data.items .data.item.content, #description_tab')

            if desc_node:
                # Очищений та акуратний HTML
                product.description_html = self._clean_description_html(desc_node)

                # Структурований текстовий опис
                cleaned_lines = []
                for line in desc_node.get_text("\n").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if not re.search(r'[а-яА-ЯєЄіІїЇґҐa-zA-Z]', line) and len(line) > 10:
                        continue
                    if line.startswith("Опис") or line.startswith("Description"):
                        continue
                    cleaned_lines.append(line)
                product.description_text = "\n".join(cleaned_lines)

            # === 4. БАЗОВІ ДАНІ (Для дефолтного різновиду) ===
            base_sku = None
            sku_block = soup.find(class_=re.compile(r'sku|product-code'))
            if sku_block:
                value_span = sku_block.find('span', class_='value') or sku_block.find('span', itemprop='sku')
                if value_span:
                    base_sku = value_span.get_text(strip=True)
                else:
                    raw_sku_text = sku_block.get_text(strip=True)
                    match = re.search(r'(\d+)$', raw_sku_text)
                    if match:
                        base_sku = match.group(1)
                    else:
                        base_sku = re.sub(r'(?i)(код|артикул|наявність.*?:.*?\n)', '', raw_sku_text).strip()

            if not base_sku:
                sku_meta = soup.find(itemprop="sku")
                if sku_meta:
                    base_sku = sku_meta.get('content') or sku_meta.get_text(strip=True)

            base_price = None
            price_elem = soup.find(class_=re.compile(r'price'))
            if price_elem:
                price_str = price_elem.get_text()
                match = re.search(r'\d+[\s\d]*[.,]?\d*', price_str)
                if match:
                    try:
                        clean_price = match.group().replace(' ', '').replace(',', '.').rstrip('.')
                        base_price = Decimal(clean_price)
                    except InvalidOperation:
                        pass

            base_in_stock = False
            avail_meta = soup.find(itemprop="availability")
            if avail_meta:
                base_in_stock = "InStock" in avail_meta.get('href', '')
            else:
                buy_btn = soup.select_one('.btn-buy, .add-to-cart, button[type="submit"]')
                stock_elem = soup.find(class_=re.compile(r'stock|availability'))
                if stock_elem:
                    base_in_stock = "в наявності" in stock_elem.get_text(strip=True).lower()
                else:
                    base_in_stock = bool(buy_btn)

            base_images = []
            gallery_scripts = soup.find_all('script', type='text/x-magento-init')
            for script in gallery_scripts:
                if script.string and '[data-gallery-role=gallery-placeholder]' in script.string:
                    try:
                        g_data = json.loads(script.string)
                        gallery = g_data.get('[data-gallery-role=gallery-placeholder]', {}).get('mage/gallery/gallery',
                                                                                                {}).get('data', [])
                        for img in gallery:
                            img_url = img.get('full') or img.get('img')
                            if img_url and img_url not in base_images:
                                base_images.append(img_url)
                    except json.JSONDecodeError:
                        pass

            if not base_images:
                og_img = soup.find("meta", property="og:image")
                if og_img and og_img.get('content'):
                    base_images.append(og_img.get('content'))

            # === 5. ПАРСИНГ РІЗНОВИДІВ (З точними мінливими SKU) ===
            product.variations = self._extract_variations(soup, base_sku, base_price, base_in_stock, base_images)

            return product

        except Exception as e:
            self.logger.error("Помилка парсингу %s: %s", url, e, exc_info=True)
            return None

    def _extract_variations(self, soup: BeautifulSoup, base_sku: str, base_price: Decimal, base_in_stock: bool,
                            base_images: List[str]) -> List[Variation]:
        variations = []

        # Резервний пошук SKU через Google Tag Manager об'єкт
        gtm_skus = {}
        mst_script = soup.find(string=re.compile(r'window\.mstGtmProductVariants\s*='))
        if mst_script:
            gtm_match = re.search(r'window\.mstGtmProductVariants\s*=\s*(\{.*?\});', mst_script, re.DOTALL)
            if gtm_match:
                try:
                    gtm_data = json.loads(gtm_match.group(1))
                    for p_id, events in gtm_data.items():
                        if events and isinstance(events, list):
                            items = events[0].get('2', {}).get('items', [])
                            if items:
                                gtm_skus[str(p_id)] = items[0].get('item_variant')
                except json.JSONDecodeError:
                    pass

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

                        skus_data = sp_config.get('sku', {})
                        if not skus_data:
                            skus_data = sp_config.get('skus', {})
                        if not skus_data and 'dynamic' in sp_config:
                            skus_data = sp_config['dynamic'].get('sku', {})

                        for attr_id, attr_data in attributes.items():
                            attr_label = attr_data.get('label', '')
                            options = attr_data.get('options', [])

                            for opt in options:
                                label = opt.get('label')
                                val = str(opt.get('id'))
                                products = opt.get('products', [])

                                if label and val and products:
                                    p_id = str(products[0])

                                    var_price = base_price
                                    if p_id in option_prices:
                                        amt = option_prices[p_id].get('finalPrice', {}).get('amount')
                                        if amt is not None:
                                            var_price = Decimal(str(amt))

                                    # Застосовуємо точний артикул варіації
                                    var_sku = base_sku
                                    if p_id in skus_data:
                                        var_sku = str(skus_data[p_id])
                                    elif p_id in gtm_skus:
                                        var_sku = str(gtm_skus[p_id])

                                    var_images = base_images.copy()
                                    if p_id in images_data:
                                        fetched_images = []
                                        for img in images_data[p_id]:
                                            img_url = img.get('full') or img.get('img')
                                            if img_url:
                                                fetched_images.append(img_url)
                                        if fetched_images:
                                            var_images = fetched_images

                                    var_in_stock = True

                                    if not re.search(r'(?i)(оберіть|виберіть|choose|select)', label):
                                        variations.append(Variation(
                                            name=f"{attr_label}: {label}" if attr_label else label,
                                            value=label,
                                            price=var_price,
                                            sku=var_sku,
                                            in_stock=var_in_stock,
                                            images=var_images
                                        ))

                        if variations:
                            return self._deduplicate_variations(variations)
                except json.JSONDecodeError:
                    continue

        if not variations:
            variations.append(Variation(
                name="Default",
                value="Default",
                price=base_price,
                sku=base_sku,
                in_stock=base_in_stock,
                images=base_images.copy()
            ))

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
