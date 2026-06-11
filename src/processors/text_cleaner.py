import re
import html
from typing import Optional
from bs4 import BeautifulSoup


class TextCleaner:
    """
    Утиліта для розумної очистки тексту та HTML-описів.
    Зберігає логічну структуру (абзаци, списки), але безжально видаляє
    системний мусор, нерозривні пробіли та зайві атрибути тегів.
    """

    @staticmethod
    def clean_text(text: Optional[str]) -> Optional[str]:
        """
        Очищає звичайний текст, строго зберігаючи одинарні переноси рядків (\n)
        для збереження читабельності списків та абзаців.
        Автоматично вирізає всі зовнішні посилання (URL).
        """
        if not text:
            return None

        text = html.unescape(text)

        text = re.sub(r'https?://\S+|www\.\S+', '', text)

        text = text.replace('\xa0', ' ').replace('\u200b', '')

        text = text.replace('\r\n', '\n')

        text = re.sub(r'[ \t]+', ' ', text)

        text = re.sub(r' \n', '\n', text)
        text = re.sub(r'\n ', '\n', text)

        text = re.sub(r'\n{3,}', '\n\n', text)

        return text.strip()

    @staticmethod
    def clean_html(html_content: Optional[str]) -> Optional[str]:
        """
        Очищає HTML-опис для експорту на маркетплейси (Prom, Rozetka).
        Залишає структуру (<p>, <ul>, <li>, <strong>), але видаляє класи,
        inline-стилі та скрипти.
        """
        if not html_content:
            return None

        html_content = html.unescape(html_content)

        soup = BeautifulSoup(html_content, 'html.parser')

        for tag in soup.find_all(['script', 'style', 'meta', 'link', 'noscript', 'iframe']):
            tag.decompose()

        allowed_attributes = ['href', 'src', 'alt', 'title']

        for tag in soup.find_all(True):
            attrs_to_keep = {}
            for attr, value in tag.attrs.items():
                if attr in allowed_attributes:
                    attrs_to_keep[attr] = value
            tag.attrs = attrs_to_keep

        for tag in soup.find_all(['div', 'p', 'span']):
            if not tag.contents and not tag.get_text(strip=True):
                tag.decompose()

        clean_str = str(soup)

        clean_str = re.sub(r'\n\s*\n', '\n', clean_str)

        return clean_str.strip()
