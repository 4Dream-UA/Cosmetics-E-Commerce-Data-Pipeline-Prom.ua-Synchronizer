import os
import asyncio
import logging
from typing import Optional
import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Завантажуємо змінні оточення з файлу .env
load_dotenv()

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


class AsyncBaseScraper:
    """
    Асинхронний базовий клас для всіх парсерів проєкту.
    Реалізує логіку HTTP-запитів через aiohttp, ретраї, затримки
    та контроль паралелізму (семафор).
    """

    def __init__(self, concurrency_limit: int = 10):
        self.logger = logging.getLogger(self.__class__.__name__)

        # Налаштування з .env
        self.delay = float(os.getenv("REQUEST_DELAY", 2))
        self.max_retries = int(os.getenv("MAX_RETRIES", 3))
        self.timeout = int(os.getenv("REQUEST_TIMEOUT", 15))
        self.user_agent = os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

        # Семафор для обмеження кількості одночасних запитів
        self.semaphore = asyncio.Semaphore(concurrency_limit)
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        """Асинхронний Context Manager для створення сесії."""
        connector = aiohttp.TCPConnector(limit=0)  # Ліміт контролюється нашим семафором
        timeout_settings = aiohttp.ClientTimeout(total=self.timeout)

        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout_settings,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
                "Connection": "keep-alive"
            }
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Асинхронний Context Manager для закриття сесії."""
        await self.close()

    async def fetch_html(self, url: str, **kwargs) -> Optional[str]:
        """Асинхронно виконує GET-запит з ретраями та затримками."""
        if not self.session:
            raise RuntimeError("Сесія не ініціалізована. Використовуйте 'async with'.")

        async with self.semaphore:
            for attempt in range(1, self.max_retries + 1):
                try:
                    # Асинхронна затримка
                    if attempt == 1:
                        await asyncio.sleep(self.delay)
                    else:
                        await asyncio.sleep(self.delay * attempt)

                    self.logger.info("Завантаження [Спроба %s/%s]: %s", attempt, self.max_retries, url)

                    async with self.session.get(url, **kwargs) as response:
                        response.raise_for_status()  # Перевірка статусу 200 OK
                        return await response.text()

                except aiohttp.ClientResponseError as http_err:
                    if http_err.status == 404:
                        self.logger.warning("Сторінка не знайдена (404): %s", url)
                        return None
                    self.logger.error("HTTP помилка %s: %s", http_err.status, http_err.message)

                except aiohttp.ClientError as req_err:
                    self.logger.warning("Помилка з'єднання з %s: %s", url, req_err)
                except asyncio.TimeoutError:
                    self.logger.warning("Таймаут з'єднання з %s", url)

                if attempt == self.max_retries:
                    self.logger.error("Критичний збій: не вдалося завантажити %s", url)

        return None

    async def get_soup(self, url: str, **kwargs) -> Optional[BeautifulSoup]:
        """Отримує HTML та повертає BeautifulSoup."""
        html = await self.fetch_html(url, **kwargs)
        return BeautifulSoup(html, "lxml") if html else None

    async def close(self):
        """Закриває aiohttp сесію."""
        if self.session:
            await self.session.close()