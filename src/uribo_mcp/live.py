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
    from selenium.webdriver.support.ui import WebDriverWait

    credentials = load_credentials()  # fail before touching the SSO pages
    driver.get(start_url)
    wait = WebDriverWait(driver, 25)
    wait.until(
        lambda current: "knossos" in current.current_url
        or expected_host in current.current_url
    )
    if expected_host in driver.current_url and "knossos" not in driver.current_url:
        return
    _submit_knossos_form(driver, expected_host, credentials)


def _url_label(url: str) -> str:
    """Host and path only; SAML/OIDC query strings never go into messages."""
    parsed = urlparse(str(url))
    return f"{parsed.netloc}{parsed.path}"


def _submit_knossos_form(driver, expected_host: str, credentials=None) -> None:
    """Fill and submit the KNOSSOS form on the current page exactly once.

    Any failure after the submit click is reported as AuthenticationRequired
    so the caller blocks further SSO attempts in the same run.
    """
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    credentials = credentials or load_credentials()
    wait = WebDriverWait(driver, 25)
    wait.until(lambda current: current.find_elements(By.ID, "username"))
    driver.find_element(By.ID, "username").send_keys(credentials.username)
    driver.find_element(By.ID, "password").send_keys(credentials.password)
    # One attempt only. Retrying can lock the university account.
    try:
        try:
            driver.find_element(By.ID, "kc-login").click()
        except TimeoutException:
            pass
        # After the form, SAML hops through the IdP host before reaching the
        # service; wait for the service host itself, not merely "left KNOSSOS".
        try:
            WebDriverWait(driver, 25).until(
                lambda current: expected_host in urlparse(current.current_url).netloc
            )
        except TimeoutException:
            pass
        final = urlparse(str(driver.current_url)).netloc
    except Exception as exc:
        raise AuthenticationRequired(
            f"KNOSSOS login was submitted but did not complete ({type(exc).__name__}); "
            "the server will not retry automatically."
        ) from exc
    if "knossos" in final or expected_host not in final:
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


def _wait_for_sso_settle(
    driver, signed_in, timeout: float = 25.0, *, clock=None, sleep=None
) -> None:
    """Poll until the browser is on a signed-in page or the KNOSSOS form."""
    import time

    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = clock() + timeout
    while clock() < deadline:
        url = str(driver.current_url)
        if signed_in(url) or "knossos" in urlparse(url).netloc:
            return
        sleep(0.5)


def _moodle_login(driver, landing: str, host: str) -> None:
    _submit_knossos_form(driver, host)


def _restore_moodle(
    vault: CookieVault, source: str, base_url: str
) -> MoodleReadOnlyClient | None:
    """Client from saved cookies if they still open ``my/courses.php``."""
    cookies = vault.load(source)
    if not cookies:
        return None
    client = MoodleReadOnlyClient(_requests_from_cookies(cookies), base_url=base_url)
    try:
        client.fetch_my_courses()
    except PermissionError:
        return None
    return client if client.has_session_key else None


def open_moodle(
    settings: Settings,
    *,
    base_url: str | None = None,
    source: str = "moodle",
    driver_factory=None,
    login=None,
    clock=None,
    sleep=None,
) -> MoodleReadOnlyClient:
    """Return a signed-in Moodle client: saved cookies first, else one SSO login.

    ``driver_factory``/``login`` are injectable for tests; the real login
    submits the KNOSSOS form at most once and never retries.
    """
    from uribo_mcp.moodle.client import moodle_base_url

    base_url = (base_url or moodle_base_url()).rstrip("/")
    host = urlparse(base_url).hostname or ""
    landing = f"{base_url}/my/courses.php"
    vault = CookieVault(settings.paths.credentials_dir)
    restored = _restore_moodle(vault, source, base_url)
    if restored is not None:
        return restored
    if login is None:
        load_credentials()  # fail before opening a browser or the SSO pages

    def signed_in(url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.hostname == host
            and "/login/" not in parsed.path
            and "/auth/shibboleth" not in parsed.path
            and "/Shibboleth.sso" not in parsed.path
        )

    driver = (driver_factory or make_chrome_driver)()
    try:
        # Start from a clean browser: the saved cookies were just proven stale
        # by _restore_moodle, and a stale MoodleSession injected before the
        # Shibboleth round trip can make Moodle reject the new session
        # (auth/shibboleth errorcode=4).  The first, successful live login ran
        # with an empty vault.
        driver.get(landing)
        # Shibboleth hops through beef.center.../Shibboleth.sso and the IdP
        # with auto-submitting pages, so get() can return mid-chain while the
        # URL is still on the Moodle host.  Wait until the chain settles on
        # either the Moodle page or KNOSSOS before deciding to log in;
        # _login_once would otherwise see the Moodle host and skip the form.
        _wait_for_sso_settle(driver, signed_in, clock=clock, sleep=sleep)
        if not signed_in(driver.current_url):
            if "knossos" not in urlparse(str(driver.current_url)).netloc:
                raise AuthenticationRequired(
                    f"Moodle sign-in did not reach KNOSSOS (stopped at {_url_label(driver.current_url)})"
                )
            # Already on the KNOSSOS form: submit it once, without re-navigating
            # (re-navigating would restart the Shibboleth chain).
            (login or _moodle_login)(driver, landing, host)
            _wait_for_sso_settle(driver, signed_in, clock=clock, sleep=sleep)
            if not signed_in(driver.current_url):
                driver.get(landing)
                _wait_for_sso_settle(driver, signed_in, clock=clock, sleep=sleep)
        if not signed_in(driver.current_url):
            raise AuthenticationRequired(
                f"Could not open authenticated Moodle page: {landing} "
                f"(stopped at {_url_label(driver.current_url)})"
            )
        vault.save(source, driver.get_cookies())
        session = _requests_from_driver(driver)
    finally:
        driver.quit()
    client = MoodleReadOnlyClient(session, base_url=base_url)
    client.fetch_my_courses()  # also loads the AJAX session key
    return client


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
    "open_moodle",
]
