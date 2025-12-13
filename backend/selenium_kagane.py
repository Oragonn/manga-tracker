# backend/selenium_kagane.py

import json
import time
import threading
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    WebDriverException,
    TimeoutException,
    NoSuchElementException,
)

class KaganeSeleniumClient:
    def __init__(self):
        self.driver = None
        self.lock = threading.Lock()
        self.last_call = 0
        self.min_delay = 0.9  # seconds between requests
        self._init_driver()

    def _init_driver(self):
        """Initialize or reinitialize the Chrome driver."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass  # Ignore errors during quit

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.138 Safari/537.36")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)

        try:
            driver = webdriver.Chrome(options=options)
        except Exception as e:
            raise RuntimeError(f"Failed to start Chrome for Kagane: {e}")

        # Anti-detection: hide automation traces
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
                delete navigator.__proto__.webdriver;
                window.chrome = {runtime: {}};
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
            '''
        })

        self.driver = driver

    def fetch_json(self, url, timeout=8):
        """Fetch a URL and parse JSON from the <pre> tag."""
        try:
            self.driver.get(url)
            # Wait up to 5 seconds for <pre> (standard for raw JSON in browsers)
            self.driver.implicitly_wait(5)
            pre_element = self.driver.find_element(By.TAG_NAME, "pre")
            return json.loads(pre_element.text)
        except NoSuchElementException:
            snippet = self.driver.page_source[:500].replace('\n', ' ')
            raise RuntimeError(f"No <pre> tag found at {url}. Page snippet: {snippet}")
        except json.JSONDecodeError as e:
            raw = self.driver.find_element(By.TAG_NAME, "pre").text[:300]
            raise RuntimeError(f"Invalid JSON from {url}: {raw} | Error: {e}")
        except (TimeoutException, WebDriverException) as e:
            raise RuntimeError(f"Browser timeout or crash on {url}: {e}")

    def get_series_info(self, series_id):
        """
        Fetch series metadata and books (chapters) via Kagane API using Selenium.
        Returns (meta_dict, books_list)
        """
        if not series_id:
            raise ValueError("series_id is required")

        with self.lock:
            # Enforce rate limit
            now = time.time()
            elapsed = now - self.last_call
            if elapsed < self.min_delay:
                time.sleep(self.min_delay - elapsed + 0.05)  # small jitter
            self.last_call = time.time()

            try:
                meta_url = f"https://api.kagane.org/api/v1/series/{series_id}"
                books_url = f"https://api.kagane.org/api/v1/books/{series_id}"

                meta = self.fetch_json(meta_url)
                books = self.fetch_json(books_url)

                return meta, books.get('content', [])
            except Exception as e:
                # Log error (optional)
                try:
                    from .error_logger import log_error
                    log_error(
                        source_url=f"https://kagane.org/series/{series_id}",
                        error_message=str(e),
                        series_title="Kagane Selenium Fetch"
                    )
                except:
                    pass

                # Auto-recover: restart browser on failure
                self._init_driver()
                raise RuntimeError(f"Kagane fetch failed after recovery: {e}")

# Singleton instance — used by kagane.py
kagane_selenium = KaganeSeleniumClient()