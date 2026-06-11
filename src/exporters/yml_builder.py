import os
import html
import hashlib
import re
from datetime import datetime
from typing import Dict, List, Any


# =====================================================================
# СМАРТ-ОЧИСТКА ОПИСІВ (Вбудована для надійності)
# =====================================================================
class EmbeddedTextCleaner:
    @staticmethod
    def clean_text(text: str) -> str:
        if not text: return ""
        text = html.unescape(text)
        text = re.sub(r'https?://\S+|www\.\S+', '', text)
        text = text.replace('\xa0', ' ').replace('\u200b', '')
        text = text.replace('\r\n', '\n')
        text = re.sub(r'[ \t]+', ' ', text)
        return text.strip()

    @staticmethod
    def clean_html(html_content: str) -> str:
        if not html_content: return ""
        html_content = html.unescape(html_content)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')
        for tag in soup.find_all(['script', 'style', 'meta', 'link', 'noscript', 'iframe']):
            tag.decompose()
        allowed_attributes = ['href', 'src', 'alt', 'title']
        for tag in soup.find_all(True):
            attrs_to_keep = {attr: value for attr, value in tag.attrs.items() if attr in allowed_attributes}
            tag.attrs = attrs_to_keep
        return str(soup).strip()


# =====================================================================
# ІДЕАЛЬНИЙ YML БІЛДЕР
# =====================================================================
class PromYmlBuilder:
    def __init__(self, shop_name: str, company_name: str, shop_url: str):
        self.shop_name = shop_name
        self.company_name = company_name
        self.shop_url = shop_url
        self.categories: Dict[str, int] = {}
        self.offers: List[str] = []
        self._category_counter = 1
        self.seen_offer_ids = set()

    def _get_category_id(self, category_name: str) -> int:
        if not category_name:
            category_name = "Різне"
        if category_name not in self.categories:
            self.categories[category_name] = self._category_counter
            self._category_counter += 1
        return self.categories[category_name]

    def _sanitize_id(self, raw_id: str) -> str:
        if not raw_id: return ""
        clean = re.sub(r'[^a-zA-Z0-9\-_]', '_', str(raw_id))
        return clean[:30].strip('_')

    def add_product(self, product: Dict[str, Any]):
        url = product.get("url", "")
        if not url: return

        title = str(product.get("title") or "").strip()
        if not title or title.lower() in ["none", "null"]:
            title = "Косметичний засіб"

        category_name = str(product.get("category") or "Різне").strip()
        category_id = self._get_category_id(category_name)
        vendor = str(product.get("source") or "").strip()

        raw_html = product.get("description_html") or product.get("description")
        raw_text = product.get("description_text")
        if raw_html:
            description_cdata = EmbeddedTextCleaner.clean_html(str(raw_html))
        elif raw_text:
            description_cdata = EmbeddedTextCleaner.clean_text(str(raw_text))
        else:
            description_cdata = ""
        description_cdata = description_cdata.replace("]]>", "]]&gt;")

        base_sku = str(product.get("sku") or "").strip()
        if not base_sku or base_sku.lower() in ["no-sku", "none"]:
            variations = product.get("variations", [])
            if variations and variations[0].get("sku"):
                base_sku = str(variations[0].get("sku")).strip()
            else:
                base_sku = hashlib.md5(url.encode()).hexdigest()[:10]

        safe_group_id = self._sanitize_id(base_sku)
        if not safe_group_id:
            safe_group_id = hashlib.md5(url.encode()).hexdigest()[:10]

        variations = product.get("variations", [])
        valid_variations = [v for v in variations if v.get("price") and float(v.get("price")) > 0]

        has_real_variations = len(valid_variations) > 1
        group_attr = f' group_id="{html.escape(safe_group_id)}"' if has_real_variations else ""

        for idx, var in enumerate(valid_variations):
            price = float(var.get("price", 0))

            var_sku = str(var.get("sku") or "").strip()
            if "http" in var_sku or not var_sku or var_sku.lower() in ["no-sku", "none"]:
                var_sku = f"{safe_group_id}_{idx}"

            safe_offer_id = self._sanitize_id(var_sku)
            if not safe_offer_id:
                safe_offer_id = f"it_{hashlib.md5((url + str(idx)).encode()).hexdigest()[:10]}"

            base_offer_id = safe_offer_id
            counter = 1
            while safe_offer_id in self.seen_offer_ids:
                safe_offer_id = f"{base_offer_id}_{counter}"
                counter += 1
            self.seen_offer_ids.add(safe_offer_id)

            in_stock = "true" if var.get("in_stock") else "false"

            offer_xml = [
                f'    <offer id="{html.escape(safe_offer_id)}" available="{in_stock}"{group_attr}>',
                f'        <url>{html.escape(url)}</url>',
                f'        <price>{price}</price>',
                f'        <currencyId>UAH</currencyId>',
                f'        <categoryId>{category_id}</categoryId>',
            ]

            for img in var.get("images", []):
                if img: offer_xml.append(f'        <picture>{html.escape(img)}</picture>')

            var_name = str(var.get("name") or "").strip()
            param_name, param_val = "Характеристика", var_name
            if ":" in var_name:
                parts = var_name.split(":", 1)
                param_name, param_val = parts[0].strip(), parts[1].strip()
            elif any(unit in var_name.lower() for unit in ["мл", " л", "g", "грам", "ml"]):
                param_name = "Об'єм"

            # Двомовні теги для коректного імпорту
            offer_xml.extend([
                f'        <vendorCode>{html.escape(safe_offer_id)}</vendorCode>',
                f'        <vendor>{html.escape(vendor)}</vendor>',
                f'        <name>{html.escape(title)}</name>',
                f'        <name_ua>{html.escape(title)}</name_ua>',
                f'        <description><![CDATA[{description_cdata}]]></description>',
                f'        <description_ua><![CDATA[{description_cdata}]]></description_ua>'
            ])

            if var_name and var_name.lower() != "default":
                offer_xml.append(
                    f'        <param name="{html.escape(param_name)}" name_ua="{html.escape(param_name)}">{html.escape(param_val)}</param>')

            offer_xml.append('    </offer>')
            self.offers.append("\n".join(offer_xml))

    def generate_xml(self) -> str:
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE yml_catalog SYSTEM "shops.dtd">',
            f'<yml_catalog date="{date_str}">',
            '  <shop>',
            f'    <name>{html.escape(self.shop_name)}</name>',
            f'    <company>{html.escape(self.company_name)}</company>',
            f'    <url>{html.escape(self.shop_url)}</url>',
            '    <currencies>',
            '      <currency id="UAH" rate="1"/>',
            '    </currencies>',
            '    <categories>'
        ]
        for cat_name, cat_id in self.categories.items():
            lines.append(f'      <category id="{cat_id}">{html.escape(cat_name)}</category>')
        lines.append('    </categories>')
        lines.append('    <offers>')
        lines.extend(self.offers)
        lines.append('    </offers>')
        lines.append('  </shop>')
        lines.append('</yml_catalog>')
        return "\n".join(lines)

    def save(self, filepath: str):
        """Зберігає фід у файл (використовується в main.py)"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(self.generate_xml())