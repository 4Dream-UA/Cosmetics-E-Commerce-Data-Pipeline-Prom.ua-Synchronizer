import os
import time
import logging
from typing import Optional
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Завантажуємо змінні оточення з файлу .env
load_dotenv()

# Налаштування логування (формат, який буде зручно читати в логах Docker)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


class BaseScraper:
    """
    Базовий клас для всіх парсерів проєкту.
    Реалізує надійну логіку HTTP-запитів, ретраїв (повторних спроб),
    затримок між запитами та управління сесіями.
    """

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)

        # Зчитуємо налаштування з .env, використовуючи безпечні дефолтні значення
        self.delay = float(os.getenv("REQUEST_DELAY", 2))
        self.max_retries = int(os.getenv("MAX_RETRIES", 3))
        self.user_agent = os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

        # Ініціалізація сесії для прискорення мережевих запитів
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
            "Connection": "keep-alive"
        })

    def fetch_html(self, url: str, **kwargs) -> Optional[str]:
        """
        Виконує GET-запит до вказаного URL з урахуванням затримки та ретраїв.
        Повертає сирий HTML-текст або None у разі критичної помилки.
        """
        global response
        for attempt in range(1, self.max_retries + 1):
            try:
                if attempt == 1:
                    time.sleep(self.delay)
                else:
                    time.sleep(self.delay * attempt)

                self.logger.info(f"Завантаження [Спроба {attempt}/{self.max_retries}]: {url}")
                response = self.session.get(url, timeout=15, **kwargs)

                # Перевіряємо наявність HTTP-помилок (наприклад, 404 Not Found або 503 Service Unavailable)
                response.raise_for_status()

                return response.text

            except requests.exceptions.HTTPError as http_err:
                # Якщо сторінки не існує, немає сенсу робити ретрай
                if response.status_code == 404:
                    self.logger.warning(f"Сторінка не знайдена (404): {url}")
                    return None
                self.logger.error(f"HTTP помилка: {http_err}")

            except requests.exceptions.RequestException as req_err:
                self.logger.warning(f"Помилка з'єднання з {url}: {req_err}")

            if attempt == self.max_retries:
                self.logger.error(f"Критичний збій: не вдалося завантажити {url} після {self.max_retries} спроб.")

        return None

    def get_soup(self, url: str, **kwargs) -> Optional[BeautifulSoup]:
        """
        Отримує HTML-код сторінки та перетворює його на об'єкт BeautifulSoup.
        """
        html = self.fetch_html(url, **kwargs)
        if html:
            # Використовуємо lxml парсер, оскільки він найшвидший і добре працює з помилками верстки
            return BeautifulSoup(html, "lxml")
        return None

    def close(self):
        """Коректно закриває HTTP-сесію при завершенні роботи парсера."""
        self.session.close()
