import os
import time
import logging
from typing import Optional
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


class BaseScraper:
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.delay = float(os.getenv("REQUEST_DELAY", 2))
        self.max_retries = int(os.getenv("MAX_RETRIES", 3))
        self.timeout = int(os.getenv("REQUEST_TIMEOUT", 15))
        self.user_agent = os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
            "Connection": "keep-alive"
        })

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def fetch_html(self, url: str, **kwargs) -> Optional[str]:
        for attempt in range(1, self.max_retries + 1):
            try:
                time.sleep(self.delay if attempt == 1 else self.delay * attempt)

                self.logger.info("Завантаження [Спроба %s/%s]: %s", attempt, self.max_retries, url)
                response = self.session.get(url, timeout=self.timeout, **kwargs)
                response.raise_for_status()
                return response.text

            except requests.exceptions.HTTPError as http_err:
                if http_err.response.status_code == 404:
                    self.logger.warning("Сторінка не знайдена (404): %s", url)
                    return None
                self.logger.error("HTTP помилка: %s", http_err)

            except requests.exceptions.RequestException as req_err:
                self.logger.warning("Помилка з'єднання з %s: %s", url, req_err)

            if attempt == self.max_retries:
                self.logger.error("Критичний збій: не вдалося завантажити %s", url)

        return None

    def get_soup(self, url: str, **kwargs) -> Optional[BeautifulSoup]:
        html = self.fetch_html(url, **kwargs)
        return BeautifulSoup(html, "lxml") if html else None

    def close(self):
        self.session.close()