import datetime as dt
import os
import sys
import time
import traceback

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

from streamlit_app import STREAMLIT_APPS


LOG_FILE = os.getenv("WAKE_LOG_FILE", "wakeup_log.txt")

CHROME_BINARY = os.getenv("CHROME_BINARY", "").strip()
CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH", "").strip()

PAGELOAD_TIMEOUT_SECONDS = int(os.getenv("PAGELOAD_TIMEOUT_SECONDS", "30"))
WAKE_TIMEOUT_SECONDS = int(os.getenv("WAKE_TIMEOUT_SECONDS", "240"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))


SLEEP_MARKERS = (
    "yes, get this app back up",
    "this app has gone to sleep due to inactivity",
    "this app is waking up",
    "your app is waking up",
    "zzzz",
)

WAKE_BUTTON_LOCATORS = (
    (By.CSS_SELECTOR, "button[data-testid='wakeup-button-viewer']"),
    (By.CSS_SELECTOR, "button[data-testid='wakeup-button-owner']"),
    (By.CSS_SELECTOR, "button[data-testid='wakeup-button']"),
    (
        By.XPATH,
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        "'yes, get this app back up')]",
    ),
)

STREAMLIT_APP_SELECTORS = (
    "[data-testid='stAppViewContainer']",
    "[data-testid='stSidebar']",
    "[data-testid='stHeader']",
    "section.main",
    "main",
)


def log(message: str) -> None:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def create_driver():
    options = Options()
    options.page_load_strategy = "none"

    if CHROME_BINARY:
        options.binary_location = CHROME_BINARY

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--window-size=1365,900")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    service = Service(executable_path=CHROMEDRIVER_PATH) if CHROMEDRIVER_PATH else Service()
    return webdriver.Chrome(service=service, options=options)


def get_body_text(driver) -> str:
    try:
        return driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        return ""


def is_streamlit_auth_redirect(driver) -> bool:
    current_url = (driver.current_url or "").lower()
    return "share.streamlit.io/-/auth" in current_url or "/-/auth/app" in current_url


def sleep_page_present(driver) -> bool:
    body = get_body_text(driver).lower()
    return any(marker in body for marker in SLEEP_MARKERS)


def find_wake_button(driver):
    for locator in WAKE_BUTTON_LOCATORS:
        try:
            buttons = driver.find_elements(*locator)
            for button in buttons:
                if button.is_displayed() and button.is_enabled():
                    return button
        except Exception:
            pass
    return None


def click_wake_button(driver) -> bool:
    button = find_wake_button(driver)
    if button is None:
        return False

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
        time.sleep(0.5)
        button.click()
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", button)
            return True
        except Exception:
            return False


def app_content_loaded(driver) -> bool:
    if is_streamlit_auth_redirect(driver):
        return False

    body = get_body_text(driver)
    body_lower = body.lower()

    if any(marker in body_lower for marker in SLEEP_MARKERS):
        return False

    try:
        for selector in STREAMLIT_APP_SELECTORS:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
    except Exception:
        pass

    current_url = (driver.current_url or "").lower()
    return ".streamlit.app" in current_url and len(body.strip()) >= 30


def check_one_app(url: str) -> tuple[bool, str]:
    log(f"Checking: {url}")

    driver = create_driver()
    try:
        driver.set_page_load_timeout(PAGELOAD_TIMEOUT_SECONDS)

        try:
            driver.get(url)
        except (TimeoutException, WebDriverException):
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

        deadline = time.time() + WAKE_TIMEOUT_SECONDS
        clicked = False

        while time.time() < deadline:
            current_url = driver.current_url or ""
            body = get_body_text(driver)

            if is_streamlit_auth_redirect(driver):
                log(f"AUTH_REDIRECT detected. current_url={current_url}")
                return False, "AUTH_REDIRECT"

            if sleep_page_present(driver):
                if click_wake_button(driver):
                    clicked = True
                    log("Sleep page detected. Wake button clicked.")
                    time.sleep(8)
                else:
                    log("Sleep page detected, but wake button not clickable yet.")
                    time.sleep(POLL_SECONDS)
                continue

            if app_content_loaded(driver):
                if clicked:
                    log(f"WOKEN OK. current_url={current_url}")
                    return True, "WOKEN"
                log(f"AWAKE OK. current_url={current_url}")
                return True, "AWAKE"

            short_body = " ".join(body.split())[:160]
            log(f"Waiting for app content... current_url={current_url} body='{short_body}'")
            time.sleep(POLL_SECONDS)

        log(f"TIMEOUT. current_url={driver.current_url}")
        return False, "TIMEOUT"

    finally:
        try:
            driver.quit()
        except Exception:
            pass


def main() -> int:
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)

    log("Execution started")

    urls = list(dict.fromkeys(STREAMLIT_APPS))
    if not urls:
        log("No Streamlit apps configured.")
        return 1

    ok_count = 0
    fail_count = 0

    for url in urls:
        try:
            ok, status = check_one_app(url)
            if ok:
                ok_count += 1
            else:
                fail_count += 1
                log(f"FAILED: {url} status={status}")
        except Exception as exc:
            fail_count += 1
            log(f"UNEXPECTED ERROR for {url}: {exc}")
            log(traceback.format_exc())

    log(f"Summary: ok={ok_count}, failed={fail_count}")
    log("Execution finished")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
