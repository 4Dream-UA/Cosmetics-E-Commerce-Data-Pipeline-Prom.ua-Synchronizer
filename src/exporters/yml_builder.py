import os
import sys
import html
import json
from datetime import datetime
from typing import Dict, List, Any

# Додаємо шлях для імпорту, якщо запускаємо файл напряму
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from processors.text_cleaner import TextCleaner
except ImportError:
    # Fallback для прямого запуску
    from text_cleaner import TextCleaner


class PromYmlBuilder:
    """
    Відмовостійкий генератор YML/XML фіду для Prom.ua.
    Приймає сирі дані від парсерів, самостійно проганяє описи через TextCleaner
    та формує ідеальну структуру для маркетплейсу.
    """

    def __init__(self, shop_name: str, company_name: str, shop_url: str):
        self.shop_name = shop_name
        self.company_name = company_name
        self.shop_url = shop_url
        self.categories: Dict[str, int] = {}
        self.offers: List[str] = []
        self._category_counter = 1

    def _get_category_id(self, category_name: str) -> int:
        """Повертає ID категорії. Якщо її ще немає — створює нову."""
        if not category_name:
            category_name = "Різне"

        if category_name not in self.categories:
            self.categories[category_name] = self._category_counter
            self._category_counter += 1

        return self.categories[category_name]

    def _prepare_description(self, product: Dict[str, Any]) -> str:
        """
        Смарт-вибір та очищення опису.
        Пріоритет віддається HTML-опису. Якщо його немає — беремо звичайний текст.
        """
        raw_html = product.get("description_html")
        raw_text = product.get("description_text")

        if raw_html:
            clean_desc = TextCleaner.clean_html(raw_html) or ""
        elif raw_text:
            clean_desc = TextCleaner.clean_text(raw_text) or ""
        else:
            clean_desc = ""

        # Захист від пошкодження блоку CDATA
        return clean_desc.replace("]]>", "]]&gt;")

    def add_product(self, product: Dict[str, Any]):
        """
        Перетворює словник товару на набір YML-оферів (варіацій).
        """
        # Базові дані, спільні для всіх варіацій
        base_sku = product.get("sku") or "no-sku"
        category_name = product.get("category")
        category_id = self._get_category_id(category_name)

        url = product.get("url", "")
        vendor = product.get("source", "")
        title = product.get("title", "Товар без назви")

        # Очищаємо опис через TextCleaner
        description_cdata = self._prepare_description(product)

        for var in product.get("variations", []):
            var_sku = var.get("sku") or base_sku
            price = var.get("price")

            # Якщо ціни немає або вона нульова, пропускаємо
            if not price or float(price) <= 0:
                continue

            in_stock = "true" if var.get("in_stock") else "false"

            # Формуємо назву (додаємо об'єм для унікальності, якщо є)
            var_value = var.get("value", "")
            if var_value and var_value.lower() != "default":
                full_name = f"{title}, {var_value}"
            else:
                full_name = title

            offer_xml = [
                f'    <offer id="{html.escape(str(var_sku))}" available="{in_stock}" group_id="{html.escape(str(base_sku))}">',
                f'        <url>{html.escape(url)}</url>',
                f'        <price>{price}</price>',
                f'        <currencyId>UAH</currencyId>',
                f'        <categoryId>{category_id}</categoryId>',
            ]

            # Додаємо картинки
            images = var.get("images", [])
            for img in images:
                offer_xml.append(f'        <picture>{html.escape(img)}</picture>')

            # Додаємо артикул для Prom.ua та назву
            offer_xml.extend([
                f'        <vendorCode>{html.escape(str(var_sku))}</vendorCode>',
                f'        <vendor>{html.escape(vendor)}</vendor>',
                f'        <name>{html.escape(full_name)}</name>',
                f'        <description><![CDATA[{description_cdata}]]></description>'
            ])

            # Додаємо параметри для створення випадаючого списку на Prom
            var_name = var.get("name", "")
            if ":" in var_name:
                param_name, param_val = map(str.strip, var_name.split(":", 1))
                offer_xml.append(f'        <param name="{html.escape(param_name)}">{html.escape(param_val)}</param>')
            elif var_name and var_name.lower() != "default":
                # Якщо просто рядок "1000 мл", назвемо параметр "Об'єм" (або "Варіант")
                offer_xml.append(f'        <param name="Характеристика">{html.escape(var_name)}</param>')

            offer_xml.append('    </offer>')

            self.offers.append("\n".join(offer_xml))

    def generate_xml(self) -> str:
        """Збирає всі компоненти в єдиний валідний XML/YML документ."""
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

        # Категорії
        for cat_name, cat_id in self.categories.items():
            lines.append(f'      <category id="{cat_id}">{html.escape(cat_name)}</category>')

        lines.append('    </categories>')
        lines.append('    <offers>')

        # Товари
        lines.extend(self.offers)

        lines.append('    </offers>')
        lines.append('  </shop>')
        lines.append('</yml_catalog>')

        return "\n".join(lines)

    def save(self, filepath: str):
        """Зберігає готовий YML фід у файл."""
        xml_content = self.generate_xml()

        # Створюємо директорії, якщо їх немає
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(xml_content)
        print(f"[+] Фід успішно згенеровано та збережено у {filepath}")


# ==========================================
# ТЕСТУВАННЯ НА РЕАЛЬНИХ ДАНИХ (SOLO.UA)
# ==========================================
if __name__ == "__main__":
    # РЕАЛЬНИЙ ТОВАР 1: Шампунь Contempora
    test_product_1 = {
        "url": "https://solo.ua/product/shampun_restrukturuvalniy_z_roslinnim_keratinom_ta_oli_yu_opuntsii/",
        "source": "solo.ua",
        "title": "Шампунь реструктурувальний з рослинним кератином та олією опунції",
        "category": "Шампуні",
        "description_text": "CONTEMPORA Hair Superfood For Brittle Hair Vegan Restructuring Shampoo\nШампунь для відновлення...",
        "description_html": "<div>\n    CONTEMPORA Hair Superfood For Brittle Hair Vegan Restructuring Shampoo<br/>\nШампунь для відновлення ламкого і тьмяного волосся. Збільшує кількість кератину в структурі волосся, захищаючи його від подальших пошкоджень. Надає зволожувальну й регенерувальну дію. Рослинний кератин – натуральний пептидний комплекс, отриманий з гороху, рису та сої, зміцнює волосся зсередини, підвищує його стійкість до ламкості та додає об'єму. Екологічно чиста олія опунції має антиейдж-ефект і відновлює блиск волосся. Шампунь підходить як для щільного, так і для тонкого волосся.<br/>\nБЕЗ SLES – БЕЗ CDEA – ДЕРМАТОЛОГІЧНО ТЕСТОВАНО                </div>",
        "sku": "13093",  # Базовий артикул
        "variations": [
            {
                "name": "Об'єм: 1000мл",
                "value": "1000мл",
                "price": 888.0,
                "sku": "13093",
                "in_stock": True,
                "images": [
                    "https://solo.ua/media/catalog/product/cache/3c952c8ec233623ccda15f16a1dc59b0/2/a/2a9314f5-7697-11f0-9b8c-3cecef8ff173.png",
                    "https://solo.ua/media/catalog/product/cache/3c952c8ec233623ccda15f16a1dc59b0/2/a/2a9314f6-7697-11f0-9b8c-3cecef8ff173.png"
                ]
            },
            {
                "name": "Об'єм: 500мл",
                "value": "500мл",
                "price": 624.0,
                "sku": "10931",
                "in_stock": True,
                "images": [
                    "https://solo.ua/media/catalog/product/cache/3c952c8ec233623ccda15f16a1dc59b0/c/a/ca665738-17a8-11f0-9b8b-3cecef8ff173.png",
                    "https://solo.ua/media/catalog/product/cache/3c952c8ec233623ccda15f16a1dc59b0/c/a/ca665739-17a8-11f0-9b8b-3cecef8ff173.png"
                ]
            },
            {
                "name": "Об'єм: 10мл",
                "value": "10мл",
                "price": 39.0,
                "sku": "11058",
                "in_stock": True,
                "images": [
                    "https://solo.ua/media/catalog/product/cache/3c952c8ec233623ccda15f16a1dc59b0/c/a/ca66573a-17a8-11f0-9b8b-3cecef8ff173.png",
                    "https://solo.ua/media/catalog/product/cache/3c952c8ec233623ccda15f16a1dc59b0/c/a/ca66573b-17a8-11f0-9b8b-3cecef8ff173.png"
                ]
            }
        ]
    }

    # РЕАЛЬНИЙ ТОВАР 2: Ампули JOC CURE
    test_product_2 = {
        "url": "https://solo.ua/product/joc_cure_intensivna_terapiya_proti_vipadinnya_volossya_1ampula_8ml_v_up_9_sht/",
        "source": "solo.ua",
        "title": "JOC CURE Інтенсивна терапія проти випадіння волосся 1АМПУЛА 8мл",
        "category": "Лікування",
        "description_text": "Barex JOC CURE Re-Power Intensive Treatment\nМістить екстракт листя...",
        "description_html": "<div>\n    Barex JOC CURE Re-Power Intensive Treatment<br/>\nМістить екстракт листя лісового горіха, який тонізує та надає життєвої сили. Виконує потужну стимулювальну дію. Завдяки поєднанню амінокислот, вітамінів та інгредієнтів рослинного походження покращує закріплення волосяної цибулини у глибоких шарах шкіри, запобігаючи випадінню, та сприяє росту волосся. Заспокоює шкіру голови, покращуючи її стан, допомагає зробити волосся сильнішим і щільнішим.<br/>\n<br/>\nОсновний інгредієнт - екстракт листя лісового горіха, яке має тонізувальні та циркуляторні властивості: надає енергію клітинам шкіри голови, стимулюючи ріст волосся, захищає від окисного стресу, забезпечує ущільнювальний ефект і підтримує тонус шкіри, підвищує еластичність, допомагає зберегти цілісність шкіри і сприяє регенерації тканин.<br/>\n<br/>\nБЕЗ ПАРАБЕНІВ - БЕЗ ШТУЧНИХ БАРВНИКІВ - ДЕРМАТОЛОГІЧНО ТЕСТОВАНО                </div>",
        "sku": "7937",  # Базовий артикул
        "variations": [
            {
                "name": "Об'єм: 8мл",
                "value": "8мл",
                "price": 154.0,
                "sku": "7937",
                "in_stock": True,
                "images": [
                    "https://solo.ua/media/catalog/product/cache/3c952c8ec233623ccda15f16a1dc59b0/3/5/3534f176-1719-11f0-9b8b-3cecef8ff173.png"
                ]
            }
        ]
    }

    # Ініціалізуємо білдер
    builder = PromYmlBuilder(
        shop_name="Solo Beauty",
        company_name="Solo Beauty LLC",
        shop_url="https://solo.ua"
    )

    # Завантажуємо товари. Білдер САМ викличе TextCleaner для описів.
    builder.add_product(test_product_1)
    builder.add_product(test_product_2)

    # Зберігаємо готовий фід
    output_path = os.path.join(os.getcwd(), "data", "output", "prom_feed_test.xml")
    builder.save(output_path)

    print("\n[+] УРИВОК ЗГЕНЕРОВАНОГО XML-ФІДУ:\n")
    # Виведемо перші 4000 символів, щоб переконатися, що все гарно лягло
    print(builder.generate_xml()[:4000] + "\n\n... (документ обрізано для прев'ю)")