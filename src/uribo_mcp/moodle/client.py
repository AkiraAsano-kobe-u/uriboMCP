"""Read-only HTTP client for the Moodle-based LMS BEEF.

BEEF (``https://beef.center.kobe-u.ac.jp/<academic year>/``) is a Moodle 4.x
site separate from BEEF+.  Quizzes, assignment submission and forum posting
are reachable through the same session, so this client exposes a small
allowlist of view pages, file downloads and two read-only AJAX functions.
Redirects are followed manually so every hop is checked against the same
allowlist.  Callers supply a ``requests.Session`` that carries Moodle cookies.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from uribo_mcp.course.client import DownloadedFile, safe_file_name

HOST = "beef.center.kobe-u.ac.jp"
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5
_SESSION_ERRORS = frozenset({"servicerequireslogin", "invalidsesskey", "requireloginerror"})


class MoodleSessionExpired(PermissionError):
    """The Moodle session cookie is no longer valid; a new SSO login is needed."""


def academic_year(now: datetime | None = None) -> int:
    """Japanese academic year: April through March."""
    now = now or datetime.now()
    return now.year if now.month >= 4 else now.year - 1


def moodle_base_url(year: int | None = None) -> str:
    return f"https://{HOST}/{year or academic_year()}"


@dataclass
class MoodleReadOnlyClient:
    session: Any
    base_url: str = field(default_factory=moodle_base_url)
    timeout_seconds: int = 60
    user_agent: str = "uribo-mcp/0.1 (personal read-only course sync)"
    _sesskey: str | None = field(default=None, init=False)

    # View pages only.  Query parameters that switch a view page into an
    # editing/submission action are refused in ``_assert_allowed``.
    _GET_PATHS = frozenset(
        {
            "/my/courses.php",
            "/course/view.php",
            "/mod/resource/view.php",
            "/mod/folder/view.php",
            "/mod/page/view.php",
            "/mod/assign/view.php",
            "/mod/forum/view.php",
            "/mod/forum/discuss.php",
        }
    )
    _FILE_PREFIX = "/pluginfile.php/"
    _AJAX_PATH = "/lib/ajax/service.php"
    AJAX_METHODS = frozenset(
        {
            "core_course_get_enrolled_courses_by_timeline_classification",
            "core_calendar_get_calendar_monthly_view",
        }
    )
    _FORBIDDEN_PARAMS = frozenset({"action", "attempt", "cmid", "sesskey", "edit"})

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        base = urlparse(self.base_url)
        self._host = base.netloc
        self._prefix = base.path.rstrip("/")

    # ------------------------------------------------------------------ guards
    def _url(self, value: str) -> str:
        url = urljoin(f"{self.base_url}/", value.strip())
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != self._host:
            raise ValueError(f"Refusing a non-Moodle URL: {url}")
        return url

    def _local_path(self, url: str) -> str:
        path = urlparse(url).path
        if not path.startswith(self._prefix + "/"):
            raise PermissionError(f"Refusing a path outside {self._prefix}: {path}")
        return path[len(self._prefix):]

    def _is_login_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.netloc != self._host
            or "/login/" in parsed.path
            or "/auth/shibboleth" in parsed.path
            or "/Shibboleth.sso" in parsed.path
        )

    def _assert_allowed(self, url: str, params: dict | None = None) -> None:
        path = self._local_path(url)
        if path.startswith(self._FILE_PREFIX):
            return
        if path not in self._GET_PATHS:
            raise PermissionError(f"Endpoint is outside the read-only allowlist: {path}")
        query = {**parse_qs(urlparse(url).query), **(params or {})}
        blocked = self._FORBIDDEN_PARAMS.intersection(query)
        if blocked:
            raise PermissionError(f"Refusing action parameters {sorted(blocked)} on {path}")

    def is_file_url(self, url: str) -> bool:
        try:
            return self._local_path(self._url(url)).startswith(self._FILE_PREFIX)
        except (PermissionError, ValueError):
            return False

    # ---------------------------------------------------------------- requests
    def _get(
        self,
        value: str,
        *,
        params: dict[str, Any] | None = None,
        follow: bool = True,
        stream: bool = False,
    ):
        """GET with manual redirects; every hop is re-checked against the allowlist.

        A hop to KNOSSOS/IdP or to Moodle's login pages raises
        MoodleSessionExpired.  With ``follow=False`` a redirect response is
        returned (closed, body unread) so the caller can inspect ``Location``.
        """
        url = self._url(value)
        self._assert_allowed(url, params)
        for _ in range(_MAX_REDIRECTS + 1):
            response = self.session.get(
                url,
                params=params,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout_seconds,
                allow_redirects=False,
                stream=stream,
            )
            if response.status_code not in REDIRECT_CODES:
                try:
                    if self._is_login_url(str(response.url)):
                        raise MoodleSessionExpired(
                            "Moodle session expired; a new SSO login is required"
                        )
                    response.raise_for_status()
                except Exception:
                    response.close()
                    raise
                return response
            location = urljoin(url, response.headers.get("Location") or "")
            response.close()
            if self._is_login_url(location):
                raise MoodleSessionExpired("Moodle session expired; a new SSO login is required")
            if not follow:
                return response
            url, params = self._url(location), None
            self._assert_allowed(url)
        raise RuntimeError(f"Too many redirects from {value}")

    def fetch_page(self, value: str, *, params: dict[str, Any] | None = None) -> str:
        return self._get(value, params=params).text

    def fetch_my_courses(self) -> str:
        html = self.fetch_page("my/courses.php")
        self.configure_sesskey(html)
        return html

    def fetch_course(self, course_id: int | str) -> str:
        return self.fetch_page("course/view.php", params={"id": str(course_id)})

    @property
    def has_session_key(self) -> bool:
        return bool(self._sesskey)

    def configure_sesskey(self, html: str) -> str | None:
        match = re.search(r'"sesskey"\s*:\s*"([^"]+)"', html)
        if match:
            self._sesskey = match.group(1)
        return self._sesskey

    def ajax(self, method: str, args: dict[str, Any]) -> Any:
        if method not in self.AJAX_METHODS:
            raise PermissionError(f"Moodle AJAX function is not allowlisted: {method}")
        if not self._sesskey:
            self.fetch_my_courses()
        if not self._sesskey:
            raise MoodleSessionExpired("Moodle page did not expose a session key")
        url = self._url(self._AJAX_PATH.lstrip("/"))
        response = self.session.post(
            url,
            params={"sesskey": self._sesskey, "info": method},
            data=json.dumps([{"index": 0, "methodname": method, "args": args}]),
            headers={
                "User-Agent": self.user_agent,
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=self.timeout_seconds,
            allow_redirects=False,
        )
        if response.status_code in REDIRECT_CODES:
            raise MoodleSessionExpired("Moodle AJAX redirected; a new SSO login is required")
        response.raise_for_status()
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Moodle AJAX {method} returned non-JSON") from exc
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise RuntimeError(f"Unexpected Moodle AJAX payload for {method}")
        first = payload[0]
        if first.get("error"):
            exception = first.get("exception") or {}
            code = exception.get("errorcode")
            if code in _SESSION_ERRORS:
                raise MoodleSessionExpired("Moodle session expired; a new SSO login is required")
            raise RuntimeError(f"Moodle AJAX {method} failed: {code or exception}")
        return first.get("data")

    def enrolled_courses(self) -> list[dict[str, Any]]:
        data = self.ajax(
            "core_course_get_enrolled_courses_by_timeline_classification",
            {"classification": "all", "limit": 0, "offset": 0, "sort": "fullname"},
        )
        return list((data or {}).get("courses") or [])

    def calendar_month(self, year: int, month: int) -> list[dict[str, Any]]:
        data = self.ajax(
            "core_calendar_get_calendar_monthly_view",
            {
                "year": year,
                "month": month,
                "courseid": 1,
                "categoryid": 0,
                "includenavigation": False,
                "mini": True,
                "day": 1,
            },
        )
        events: list[dict[str, Any]] = []
        for week in (data or {}).get("weeks") or []:
            for day in week.get("days") or []:
                events.extend(day.get("events") or [])
        return events

    def resolve_resource_file(self, cmid: int | str) -> str | None:
        """Return the pluginfile URL behind a file resource without downloading it."""
        start = self._url("mod/resource/view.php")
        response = self._get(start, params={"id": str(cmid), "redirect": "1"}, follow=False)
        if response.status_code in REDIRECT_CODES:
            target = urljoin(start, response.headers.get("Location") or "")
            return target.split("?", 1)[0] if self.is_file_url(target) else None
        from uribo_mcp.moodle.parser import pluginfile_links

        links = pluginfile_links(response.text, base_url=self.base_url)
        return links[0]["url"] if links else None

    # ---------------------------------------------------------------- download
    def download_attachment(
        self,
        attachment: dict[str, Any],
        *,
        destination_dir: Path,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> DownloadedFile:
        url = self._url(str(attachment.get("url") or attachment.get("source_url") or ""))
        if not self.is_file_url(url):
            raise PermissionError(f"Only pluginfile downloads are allowed: {url}")
        file_name = self.file_name_from_url(url) or str(attachment.get("name") or "attachment")
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = (destination_dir / safe_file_name(file_name)).resolve()
        if destination_dir.resolve() not in target.parents:
            raise ValueError("Attachment path escaped the cache directory")
        temporary = target.with_name(target.name + ".part")
        response = self._get(url, params={"forcedownload": "1"}, stream=True)
        total = 0
        try:
            declared = str(response.headers.get("Content-Length") or "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise ValueError(f"Attachment exceeds size limit: {declared} > {max_bytes}")
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"Attachment exceeds size limit: > {max_bytes}")
                    handle.write(chunk)
            os.replace(temporary, target)
        finally:
            response.close()
            temporary.unlink(missing_ok=True)
        media_type = response.headers.get("Content-Type") or mimetypes.guess_type(file_name)[0]
        return DownloadedFile(
            path=target,
            file_name=file_name,
            media_type=media_type.split(";", 1)[0] if media_type else None,
            size_bytes=total,
            response_url=str(response.url),
        )

    @staticmethod
    def file_name_from_url(url: str) -> str | None:
        # Moodle sends a raw UTF-8 Content-Disposition that requests decodes as
        # Latin-1; the pluginfile path carries the real name.
        name = PurePosixPath(unquote(urlparse(url).path)).name
        return unicodedata.normalize("NFC", name) if name else None


__all__ = [
    "HOST",
    "REDIRECT_CODES",
    "MoodleReadOnlyClient",
    "MoodleSessionExpired",
    "academic_year",
    "moodle_base_url",
]
