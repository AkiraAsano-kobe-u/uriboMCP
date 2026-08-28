"""Read-only HTTP client for authenticated BEEF+ course content.

The LMS contains endpoints that can start tests, submit reports, or answer
surveys.  This client deliberately exposes only the small read-only allowlist
needed by the synchronizer.  Callers supply an already authenticated
``requests.Session`` (normally copied from a Selenium SSO session).
"""

from __future__ import annotations

import mimetypes
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, urljoin, urlparse

from bs4 import BeautifulSoup


class Response(Protocol):
    content: bytes
    text: str
    headers: dict[str, str]
    url: str

    def raise_for_status(self) -> None: ...


class Session(Protocol):
    def get(self, url: str, **kwargs: Any) -> Response: ...

    def post(self, url: str, **kwargs: Any) -> Response: ...


@dataclass(frozen=True)
class DownloadedFile:
    path: Path
    file_name: str
    media_type: str | None
    size_bytes: int
    response_url: str


@dataclass
class BeefReadOnlyClient:
    session: Session
    base_url: str = "https://beefplus.center.kobe-u.ac.jp"
    timeout_seconds: int = 60
    user_agent: str = "uribo-mcp/0.1 (personal read-only course sync)"
    _common_headers: dict[str, str] = field(init=False)
    _announcement_form: dict[str, str] = field(default_factory=dict, init=False)

    # Assignment detail is a GET page despite the historic "submission" path.
    _GET_PATHS = {
        "/lms/timetable",
        "/lms/task",
        "/lms/course",
        "/lms/course/information/list",
        "/lms/course/report/submission",
        "/lms/course/make/tempfile",
        "/lms/course/setfiledown",
        "/updateinfo",
    }
    _GET_PATH_PREFIXES = (
        "/lms/course/material/setfiledown/",
        "/lms/course/report/submission_download/",
    )
    _POST_PATHS = {"/lms/course/information/listdetail"}
    _FORBIDDEN = (
        "/examination/",
        "/test/start",
        "/question/answer",
        "/survey/answer",
        "/attendance/register",
        "/report/submit",
    )

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._common_headers = {"User-Agent": self.user_agent}

    def _url(self, value: str) -> str:
        url = urljoin(f"{self.base_url}/", value)
        parsed = urlparse(url)
        base = urlparse(self.base_url)
        if parsed.scheme != "https" or parsed.netloc != base.netloc:
            raise ValueError(f"Refusing a non-LMS URL: {url}")
        return url

    def _assert_allowed(self, method: str, url: str) -> None:
        path = urlparse(url).path
        lowered = path.casefold()
        if any(value in lowered for value in self._FORBIDDEN):
            raise PermissionError(f"Refusing an action endpoint: {path}")
        allowed = self._GET_PATHS if method == "GET" else self._POST_PATHS
        prefix_allowed = method == "GET" and any(
            path.startswith(prefix) for prefix in self._GET_PATH_PREFIXES
        )
        if path not in allowed and not prefix_allowed:
            raise PermissionError(f"Endpoint is outside the read-only allowlist: {path}")

    def _get(
        self,
        value: str,
        *,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
        ajax: bool = False,
    ) -> Response:
        url = self._url(value)
        self._assert_allowed("GET", url)
        headers = dict(self._common_headers)
        if referer:
            headers["Referer"] = referer
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        response = self.session.get(
            url,
            params=params,
            headers=headers,
            timeout=self.timeout_seconds,
            allow_redirects=True,
        )
        response.raise_for_status()
        self._assert_authenticated(response)
        return response

    def _post(
        self,
        value: str,
        *,
        data: dict[str, Any],
        params: dict[str, Any] | None = None,
        referer: str | None = None,
    ) -> Response:
        url = self._url(value)
        self._assert_allowed("POST", url)
        response = self.session.post(
            url,
            data=data,
            params=params,
            headers={
                **self._common_headers,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": referer
                or f"{self.base_url}/lms/course/information/list",
            },
            timeout=self.timeout_seconds,
            allow_redirects=True,
        )
        response.raise_for_status()
        self._assert_authenticated(response)
        return response

    @staticmethod
    def _assert_authenticated(response: Response) -> None:
        final = urlparse(response.url)
        if "knossos" in final.netloc or final.path.startswith("/login"):
            raise PermissionError("LMS session expired; a new SSO login is required")

    def fetch_course(self, idnumber: str) -> str:
        return self._get("/lms/course", params={"idnumber": idnumber}).text

    def fetch_assignment_detail(self, url: str) -> str:
        parsed = urlparse(self._url(url))
        if parsed.path != "/lms/course/report/submission":
            raise PermissionError("Only assignment detail pages may be opened here")
        return self._get(url).text

    def configure_announcement_form(self, html: str) -> dict[str, str]:
        """Cache the read-only announcement detail form, including its CSRF token."""
        soup = BeautifulSoup(html, "html.parser")
        form = soup.select_one("#informationDtl, #informationData")
        values: dict[str, str] = {}
        if form:
            for element in form.select("input[name]"):
                name = str(element.get("name") or "").strip()
                if name:
                    values[name] = str(element.get("value") or "")
        self._announcement_form = values
        return dict(values)

    def _ensure_announcement_form(self, idnumber: str | None) -> None:
        if self._announcement_form.get("_csrf"):
            return
        params = {"idnumber": idnumber} if idnumber else None
        response = self._get("/lms/course/information/list", params=params)
        self.configure_announcement_form(response.text)
        if not self._announcement_form.get("_csrf"):
            raise PermissionError(
                "BEEF+ announcement form did not contain a CSRF token"
            )

    def fetch_announcement_detail(
        self,
        *,
        information_id: str,
        idnumber: str | None,
        rishunen: str | None = None,
    ) -> str:
        self._ensure_announcement_form(idnumber)
        data = dict(self._announcement_form)
        data["informationId"] = information_id
        if idnumber:
            data["idnumber"] = idnumber
        if rishunen:
            data["rishunen"] = rishunen
        params = {"idnumber": idnumber} if idnumber else None
        referer = f"{self.base_url}/lms/course/information/list"
        if idnumber:
            referer = f"{referer}?idnumber={idnumber}"
        return self._post(
            "/lms/course/information/listdetail",
            data=data,
            params=params,
            referer=referer,
        ).text

    def download_attachment(
        self,
        attachment: dict[str, Any],
        *,
        destination_dir: Path,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> DownloadedFile:
        """Download one material with a bounded, read-only recipe.

        Direct LMS download URLs are preferred.  For BEEF+'s two-step object
        download, the parser-supplied object/resource metadata is sent first to
        ``make/tempfile`` and then to ``setfiledown``.  No form-action endpoint
        is accepted.
        """
        destination_dir.mkdir(parents=True, exist_ok=True)
        source_url = attachment.get("url") or attachment.get("source_url")
        object_name = attachment.get("object_name")
        metadata = dict(attachment.get("metadata") or {})
        idnumber = metadata.get("idnumber")
        report_id = metadata.get("report_id")
        download_kind = metadata.get("download_kind")
        referer = (
            f"{self.base_url}/lms/course?idnumber={idnumber}"
            if idnumber
            else f"{self.base_url}/lms/course"
        )
        if download_kind == "report_submission" and object_name:
            file_name = str(
                attachment.get("name")
                or attachment.get("file_name")
                or "attachment"
            )
            if not idnumber or not report_id:
                raise ValueError(
                    "Report attachment is missing idnumber or report_id"
                )
            referer = (
                f"{self.base_url}/lms/course/report/submission"
                f"?idnumber={idnumber}&reportId={report_id}"
            )
            route_name = quote(
                self._download_route_name(file_name), safe="-_.!~*'()"
            )
            response = self._get(
                f"/lms/course/report/submission_download/{route_name}",
                params={
                    "reportId": report_id,
                    "idnumber": idnumber,
                    "downloadFileName": file_name,
                    "objectName": object_name,
                    "downloadMode": "",
                },
                referer=referer,
            )
        elif source_url and self._is_download_url(str(source_url)):
            response = self._download_get(str(source_url), referer=referer)
        elif object_name:
            file_name = str(
                attachment.get("name")
                or attachment.get("file_name")
                or "attachment"
            )
            resource_id = attachment.get("resource_id")
            content_id = attachment.get("content_id")
            params = {
                "objectName": object_name,
                "fileName": file_name,
                "id": resource_id,
                "idnumber": idnumber,
            }
            clean_params = {key: value for key, value in params.items() if value}
            prepared = self._get(
                "/lms/course/make/tempfile",
                params=clean_params,
                referer=referer,
                ajax=True,
            )
            file_id = self._temporary_file_id(prepared)
            download_params = {
                "fileName": file_name,
                "fileId": file_id,
                "idnumber": idnumber,
                "resourceId": resource_id,
                "screen": "1",
                "contentId": content_id,
                "endDate": metadata.get("end_date_raw")
                or attachment.get("open_end_at"),
            }
            route_name = quote(
                self._download_route_name(file_name), safe="-_.!~*'()"
            )
            response = self._get(
                f"/lms/course/material/setfiledown/{route_name}",
                params={
                    key: value
                    for key, value in download_params.items()
                    if value is not None and value != ""
                },
                referer=referer,
            )
        else:
            raise ValueError("Attachment does not contain a safe download recipe")

        file_name = self._file_name(response, attachment)
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError(f"Attachment exceeds size limit: {content_length} > {max_bytes}")
        if len(response.content) > max_bytes:
            raise ValueError(
                f"Attachment exceeds size limit: {len(response.content)} > {max_bytes}"
            )
        target = (destination_dir / self._safe_name(file_name)).resolve()
        if destination_dir.resolve() not in target.parents:
            raise ValueError("Attachment path escaped the cache directory")
        target.write_bytes(response.content)
        media_type = response.headers.get("Content-Type") or mimetypes.guess_type(file_name)[0]
        return DownloadedFile(
            path=target,
            file_name=file_name,
            media_type=media_type.split(";", 1)[0] if media_type else None,
            size_bytes=len(response.content),
            response_url=response.url,
        )

    @staticmethod
    def _is_download_url(url: str) -> bool:
        path = urlparse(url).path.casefold()
        return any(
            marker in path
            for marker in ("download", "filedown", "setfiledown", "make/tempfile")
        )

    def _download_get(self, value: str, *, referer: str | None = None) -> Response:
        url = self._url(value)
        path = urlparse(url).path.casefold()
        if not self._is_download_url(url) or any(
            forbidden in path for forbidden in self._FORBIDDEN
        ):
            raise PermissionError(f"Refusing a non-download endpoint: {path}")
        response = self.session.get(
            url,
            headers={
                **self._common_headers,
                **({"Referer": referer} if referer else {}),
            },
            timeout=self.timeout_seconds,
            allow_redirects=True,
        )
        response.raise_for_status()
        self._assert_authenticated(response)
        return response

    @staticmethod
    def _temporary_token(response: Response) -> dict[str, str]:
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type.casefold():
            import json

            value = json.loads(response.text)
            if isinstance(value, dict):
                return {str(key): str(item) for key, item in value.items() if item is not None}
        query = parse_qs(urlparse(response.url).query)
        if query:
            return {key: values[0] for key, values in query.items() if values}
        text = response.text.strip()
        match = re.search(r"(?:tempFile|fileName|token)[\"']?\s*[:=]\s*[\"']([^\"']+)", text)
        if match:
            return {"tempFile": match.group(1)}
        if text and len(text) < 512 and "<" not in text:
            return {"tempFile": text}
        return {}

    @classmethod
    def _temporary_file_id(cls, response: Response) -> str:
        text = response.text.strip()
        if text and len(text) < 512 and "<" not in text:
            return text
        token = cls._temporary_token(response)
        for key in ("fileId", "tempFile", "fileName", "token"):
            if token.get(key):
                return token[key]
        raise ValueError("BEEF+ did not return a temporary file identifier")

    @staticmethod
    def _download_route_name(file_name: str) -> str:
        value = file_name.replace("\u3000", "_").replace(" ", "_")
        return re.sub(r"_+", "_", value)

    @staticmethod
    def _file_name(response: Response, attachment: dict[str, Any]) -> str:
        disposition = response.headers.get("Content-Disposition", "")
        encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
        if encoded:
            from urllib.parse import unquote

            return unquote(encoded.group(1))
        plain = re.search(r'filename="?([^";]+)', disposition, re.I)
        if plain:
            return plain.group(1)
        return str(
            attachment.get("name")
            or attachment.get("file_name")
            or Path(urlparse(response.url).path).name
            or "attachment"
        )

    @staticmethod
    def _safe_name(value: str) -> str:
        value = unicodedata.normalize("NFC", value)
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:180] or "attachment"


__all__ = ["BeefReadOnlyClient", "DownloadedFile"]
