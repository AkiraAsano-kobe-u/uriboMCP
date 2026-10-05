"""Read-only HTTP client for the Moodle-based LMS BEEF.

BEEF (``https://beef.center.kobe-u.ac.jp/<academic year>/``) is a Moodle 4.x
site separate from BEEF+.  Quizzes, assignment submission and forum posting
are reachable through the same session, so this client exposes a small
allowlist of view pages, file downloads and three read-only AJAX functions.
Callers supply a ``requests.Session`` that already carries Moodle cookies.
"""

from __future__ import annotations

import json
import mimetypes
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from uribo_mcp.course.client import DownloadedFile, Session

HOST = "beef.center.kobe-u.ac.jp"


def academic_year(now: datetime | None = None) -> int:
    """Japanese academic year: April through March."""
    now = now or datetime.now()
    return now.year if now.month >= 4 else now.year - 1


def moodle_base_url(year: int | None = None) -> str:
    return f"https://{HOST}/{year or academic_year()}"


@dataclass
class MoodleReadOnlyClient:
    session: Session
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
    _GET_PREFIXES = ("/pluginfile.php/",)
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

    def _assert_allowed(self, method: str, url: str, params: dict | None = None) -> None:
        path = self._local_path(url)
        if method == "POST":
            if path != self._AJAX_PATH:
                raise PermissionError(f"Refusing a Moodle POST endpoint: {path}")
            return
        if path.startswith(self._GET_PREFIXES):
            return
        if path not in self._GET_PATHS:
            raise PermissionError(f"Endpoint is outside the read-only allowlist: {path}")
        query = {**parse_qs(urlparse(url).query), **(params or {})}
        blocked = self._FORBIDDEN_PARAMS.intersection(query)
        if blocked:
            raise PermissionError(f"Refusing action parameters {sorted(blocked)} on {path}")

    def _assert_authenticated(self, response: Any) -> None:
        final = urlparse(response.url)
        path = final.path
        if (
            final.netloc != self._host
            or "/login/" in path
            or "/auth/shibboleth" in path
        ):
            raise PermissionError("Moodle session expired; a new SSO login is required")

    # ---------------------------------------------------------------- requests
    def _get(
        self,
        value: str,
        *,
        params: dict[str, Any] | None = None,
        allow_redirects: bool = True,
        stream: bool = False,
    ):
        url = self._url(value)
        self._assert_allowed("GET", url, params)
        response = self.session.get(
            url,
            params=params,
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout_seconds,
            allow_redirects=allow_redirects,
            stream=stream,
        )
        if allow_redirects:
            response.raise_for_status()
            self._assert_authenticated(response)
        return response

    def fetch_page(self, value: str, *, params: dict[str, Any] | None = None) -> str:
        return self._get(value, params=params).text

    def fetch_my_courses(self) -> str:
        html = self.fetch_page("my/courses.php")
        self.configure_sesskey(html)
        return html

    def fetch_course(self, course_id: int | str) -> str:
        return self.fetch_page("course/view.php", params={"id": str(course_id)})

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
            raise PermissionError("Moodle page did not expose a session key")
        url = self._url("lib/ajax/service.php")
        self._assert_allowed("POST", url)
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
        response.raise_for_status()
        payload = json.loads(response.text)
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"Unexpected Moodle AJAX payload for {method}")
        first = payload[0]
        if first.get("error"):
            exception = first.get("exception") or {}
            code = exception.get("errorcode")
            if code in {"servicerequireslogin", "invalidsesskey", "requireloginerror"}:
                raise PermissionError("Moodle session expired; a new SSO login is required")
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
        response = self._get(
            "mod/resource/view.php",
            params={"id": str(cmid), "redirect": "1"},
            allow_redirects=False,
        )
        location = response.headers.get("Location") or ""
        if response.status_code in {301, 302, 303, 307, 308} and location:
            target = self._url(location)
            if self._is_pluginfile(target):
                return target
            self._assert_authenticated(_Final(target))
            return None
        response.raise_for_status()
        self._assert_authenticated(response)
        from uribo_mcp.moodle.parser import pluginfile_links

        links = pluginfile_links(response.text, base_url=self.base_url)
        return links[0]["url"] if links else None

    # ---------------------------------------------------------------- download
    def _is_pluginfile(self, url: str) -> bool:
        try:
            return self._local_path(url).startswith(self._GET_PREFIXES)
        except PermissionError:
            return False

    def download_attachment(
        self,
        attachment: dict[str, Any],
        *,
        destination_dir: Path,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> DownloadedFile:
        source_url = str(attachment.get("url") or attachment.get("source_url") or "")
        url = self._url(source_url)
        if not self._is_pluginfile(url):
            raise PermissionError(f"Only pluginfile downloads are allowed: {url}")
        destination_dir.mkdir(parents=True, exist_ok=True)
        response = self._get(url, params={"forcedownload": "1"}, stream=True)
        try:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise ValueError(f"Attachment exceeds size limit: {declared} > {max_bytes}")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=1 << 16):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"Attachment exceeds size limit: > {max_bytes}")
                chunks.append(chunk)
            content = b"".join(chunks)
        finally:
            response.close()
        file_name = self.file_name_from_url(url) or str(attachment.get("name") or "attachment")
        target = (destination_dir / self._safe_name(file_name)).resolve()
        if destination_dir.resolve() not in target.parents:
            raise ValueError("Attachment path escaped the cache directory")
        target.write_bytes(content)
        media_type = response.headers.get("Content-Type") or mimetypes.guess_type(file_name)[0]
        return DownloadedFile(
            path=target,
            file_name=file_name,
            media_type=media_type.split(";", 1)[0] if media_type else None,
            size_bytes=len(content),
            response_url=response.url,
        )

    @staticmethod
    def file_name_from_url(url: str) -> str | None:
        # Moodle sends RFC 2231-less non-ASCII Content-Disposition values that
        # requests decodes as Latin-1; the pluginfile path carries the real name.
        name = PurePosixPath(unquote(urlparse(url).path)).name
        return unicodedata.normalize("NFC", name) if name else None

    @staticmethod
    def _safe_name(value: str) -> str:
        value = unicodedata.normalize("NFC", value)
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:180] or "attachment"


@dataclass(frozen=True)
class _Final:
    url: str


__all__ = ["HOST", "MoodleReadOnlyClient", "academic_year", "moodle_base_url"]
