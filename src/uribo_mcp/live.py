"""Factories for live browser and Google API access.

Imports are intentionally lazy so starting the stdio server never launches a
browser or loads Google discovery clients.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlparse

import requests

from uribo_mcp.auth import CookieVault, load_credentials
from uribo_mcp.config import Settings
from uribo_mcp.course.client import BeefReadOnlyClient
from uribo_mcp.errors import AuthenticationRequired
from uribo_mcp.moodle.client import MoodleReadOnlyClient
from uribo_mcp.subprocesses import hidden_selenium_service_kwargs


def make_chrome_driver(*, page_load_strategy: str = "normal"):
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,2000")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.set_capability("unhandledPromptBehavior", "dismiss")
    options.page_load_strategy = page_load_strategy
    driver = webdriver.Chrome(
        service=Service(popen_kw=hidden_selenium_service_kwargs()),
        options=options,
    )
    driver.set_page_load_timeout(60)
    return driver


def _inject_vault(driver, vault: CookieVault, source: str, base_url: str) -> bool:
    cookies = vault.load(source)
    if not cookies:
        return False
    driver.get(base_url.rstrip("/") + "/favicon.ico")
    host = urlparse(base_url).hostname or ""
    for raw in cookies:
        cookie = {
            key: value
            for key, value in raw.items()
            if key
            in {
                "name",
                "value",
                "path",
                "domain",
                "secure",
                "httpOnly",
                "sameSite",
            }
        }
        if host.endswith(str(cookie.get("domain") or "").lstrip(".")):
            try:
                driver.add_cookie(cookie)
            except Exception:
                continue
    return True


def _login_once(driver, start_url: str, expected_host: str) -> None:
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    credentials = load_credentials()
    driver.get(start_url)
    wait = WebDriverWait(driver, 25)
    wait.until(
        lambda current: "knossos" in current.current_url
        or expected_host in current.current_url
    )
    if expected_host in driver.current_url and "knossos" not in driver.current_url:
        return
    wait.until(lambda current: current.find_elements(By.ID, "username"))
    driver.find_element(By.ID, "username").send_keys(credentials.username)
    driver.find_element(By.ID, "password").send_keys(credentials.password)
    # One attempt only. Retrying can lock the university account.
    try:
        driver.find_element(By.ID, "kc-login").click()
    except TimeoutException:
        pass
    try:
        WebDriverWait(driver, 25).until(lambda current: "knossos" not in current.current_url)
    except Exception:
        pass
    if "knossos" in driver.current_url or expected_host not in driver.current_url:
        raise AuthenticationRequired(
            "KNOSSOS login failed. Credentials, MFA, or the login page may have changed; "
            "the server will not retry automatically."
        )


def _requests_from_driver(driver) -> requests.Session:
    session = requests.Session()
    for cookie in driver.get_cookies():
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )
    session.headers.update({"Accept-Language": "ja,en;q=0.8"})
    return session


@contextmanager
def authenticated_lms(
    settings: Settings,
    *,
    base_url: str = "https://beefplus.center.kobe-u.ac.jp",
    source: str = "beef",
    landing_path: str = "/lms/timetable",
) -> Iterator[tuple[object, BeefReadOnlyClient]]:
    """Open one authenticated browser and a cookie-equivalent requests client."""
    driver = make_chrome_driver()
    vault = CookieVault(settings.paths.credentials_dir)
    landing = base_url.rstrip("/") + landing_path
    expected_host = urlparse(base_url).hostname or ""
    try:
        restored = _inject_vault(driver, vault, source, base_url)
        if restored:
            driver.get(landing)
        if not restored or expected_host not in driver.current_url or "/login" in driver.current_url:
            _login_once(driver, base_url.rstrip("/") + "/saml/login?disco=true", expected_host)
            driver.get(landing)
        if expected_host not in driver.current_url or "knossos" in driver.current_url:
            raise AuthenticationRequired(f"Could not open authenticated LMS page: {landing}")
        vault.save(source, driver.get_cookies())
        session = _requests_from_driver(driver)
        yield driver, BeefReadOnlyClient(session, base_url=base_url)
    finally:
        driver.quit()


@contextmanager
def authenticated_moodle(
    settings: Settings,
    *,
    base_url: str | None = None,
    source: str = "moodle",
) -> Iterator[MoodleReadOnlyClient]:
    """Open LMS BEEF (Moodle) with saved cookies, else exactly one SSO login."""
    from uribo_mcp.moodle.client import moodle_base_url

    base_url = (base_url or moodle_base_url()).rstrip("/")
    host = urlparse(base_url).hostname or ""
    landing = f"{base_url}/my/courses.php"
    vault = CookieVault(settings.paths.credentials_dir)

    def signed_in(url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.hostname == host
            and "/login/" not in parsed.path
            and "/auth/shibboleth" not in parsed.path
        )

    cookies = vault.load(source)
    if cookies:
        client = MoodleReadOnlyClient(_requests_from_cookies(cookies), base_url=base_url)
        try:
            restored = bool(client.configure_sesskey(client.fetch_my_courses()))
        except PermissionError:
            restored = False
        if restored:
            # Yield outside the try so a caller's PermissionError propagates.
            yield client
            return
    driver = make_chrome_driver()
    try:
        _inject_vault(driver, vault, source, f"https://{host}")
        driver.get(landing)
        if not signed_in(driver.current_url):
            _login_once(driver, landing, host)
            driver.get(landing)
        if not signed_in(driver.current_url):
            raise AuthenticationRequired(f"Could not open authenticated Moodle page: {landing}")
        vault.save(source, driver.get_cookies())
        session = _requests_from_driver(driver)
    finally:
        driver.quit()
    client = MoodleReadOnlyClient(session, base_url=base_url)
    client.fetch_my_courses()
    yield client


def _requests_from_cookies(cookies: list[dict]) -> requests.Session:
    session = requests.Session()
    for cookie in cookies:
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )
    session.headers.update({"Accept-Language": "ja,en;q=0.8"})
    return session


def fetch_uribo_html(settings: Settings) -> str:
    """Fetch the stable CampusSquare notice-list flow with one SSO attempt."""
    from selenium.common.exceptions import TimeoutException

    host = "kym22-web.ofc.kobe-u.ac.jp"
    base = f"https://{host}/campusweb"
    start = f"{base}/"
    notice_url = f"{base}/campussquare.do?_flowId=KJW0001100-flow"
    driver = make_chrome_driver(page_load_strategy="eager")
    driver.set_page_load_timeout(40)
    vault = CookieVault(settings.paths.credentials_dir)

    def tolerant_get(url: str) -> None:
        try:
            driver.get(url)
        except TimeoutException:
            pass
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass

    try:
        restored = _inject_vault(driver, vault, "uribo", f"https://{host}")
        if restored:
            tolerant_get(start)
        if not restored or host not in driver.current_url:
            _login_once(driver, start, host)
            tolerant_get(start)
        vault.save("uribo", driver.get_cookies())
        tolerant_get(notice_url)
        return driver.page_source
    finally:
        driver.quit()


def _load_google_credentials(
    settings: Settings,
    *,
    token_name: str,
    scopes: list[str],
    interactive: bool,
):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = settings.paths.credentials_dir / token_name
    client_path = settings.paths.credentials_dir / "google_client_secret.json"
    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), scopes)
    if credentials and credentials.valid:
        return credentials
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            raise AuthenticationRequired(
                "Google OAuth token expired; run `uribo-mcp auth google` again."
            ) from exc
    elif interactive:
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not client_path.exists():
            raise AuthenticationRequired(
                f"Place an OAuth desktop client JSON at {client_path}"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes)
        credentials = flow.run_local_server(port=0)
    else:
        raise AuthenticationRequired("Run `uribo-mcp auth google` before mail sync.")
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def gmail_service(settings: Settings, *, interactive: bool = False):
    from googleapiclient.discovery import build

    credentials = _load_google_credentials(
        settings,
        token_name="gmail_token.json",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        interactive=interactive,
    )
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def google_token_summary(settings: Settings) -> dict[str, bool]:
    """Return local setup state without exposing token or secret content."""
    directory = settings.paths.credentials_dir
    return {
        "client_secret_present": (directory / "google_client_secret.json").is_file(),
        "gmail_token_present": (directory / "gmail_token.json").is_file(),
    }


__all__ = [
    "authenticated_lms",
    "fetch_uribo_html",
    "gmail_service",
    "google_token_summary",
    "make_chrome_driver",
]
