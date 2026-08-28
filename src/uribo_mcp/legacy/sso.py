"""SSO support with credential and browser factories injected by the caller."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

IDP_HOST = "knossos"


class LoginError(RuntimeError):
    pass


class Browser(Protocol):
    current_url: str
    def get(self, url: str) -> None: ...
    def add_cookie(self, cookie: dict) -> None: ...
    def get_cookies(self) -> list[dict]: ...


def inject_cookies(driver: Browser, cookie_path: Path, base_url: str, domain_substr: str) -> bool:
    if not cookie_path.exists(): return False
    driver.get(base_url.rstrip("/") + "/favicon.ico")
    try: cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return False
    for cookie in cookies:
        cookie = {k: v for k, v in cookie.items() if k != "expiry"}
        if domain_substr in cookie.get("domain", ""):
            try: driver.add_cookie(cookie)
            except Exception: pass
    return True


def save_cookies(driver: Browser, cookie_path: Path) -> None:
    """Persist only to a caller-selected protected credential location."""
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    cookie_path.write_text(json.dumps(driver.get_cookies(), ensure_ascii=False), encoding="utf-8")


def idp_login(driver, start_url: str, is_back: Callable[[object], bool], credentials: Callable[[], tuple[str, str]], *, wait: Callable[[Callable[[], bool]], None], find: Callable[[str], object]) -> None:
    """Perform exactly one Keycloak form submission using injected browser operations."""
    driver.get(start_url); wait(lambda: IDP_HOST in driver.current_url or is_back(driver))
    if is_back(driver): return
    user, password = credentials()
    try:
        find("username").send_keys(user); find("password").send_keys(password); find("kc-login").click()
        wait(lambda: IDP_HOST not in driver.current_url)
    except Exception as exc:
        raise LoginError("KNOSSOS login could not complete; check credentials, OTP, or page structure") from exc
    if not is_back(driver): raise LoginError("KNOSSOS login did not return to the service provider")
