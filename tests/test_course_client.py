from pathlib import Path
from urllib.parse import unquote

import pytest

from uribo_mcp.course.client import BeefReadOnlyClient


class FakeResponse:
    def __init__(
        self,
        url: str,
        *,
        text: str = "",
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.text = text
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "make/tempfile" in url:
            return FakeResponse(url, text="temporary-file-id")
        if "material/setfiledown" in url:
            return FakeResponse(
                url,
                content=b"material",
                headers={
                    "Content-Type": "application/pdf",
                    "Content-Disposition": 'attachment; filename="lecture.pdf"',
                },
            )
        if "download" in url:
            return FakeResponse(
                url,
                content=b"material",
                headers={
                    "Content-Type": "application/pdf",
                    "Content-Disposition": 'attachment; filename="lecture.pdf"',
                },
            )
        if url.endswith("/lms/course/information/list"):
            return FakeResponse(
                url,
                text=(
                    '<form id="informationDtl">'
                    '<input name="_csrf" value="csrf-token">'
                    '<input name="viewPage" value="1">'
                    '<input name="viewkind" value="9">'
                    '<input name="informationId" value="">'
                    "</form>"
                ),
            )
        return FakeResponse(url, text="<h1>Course</h1>")

    def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(url, text="<h2>Announcement</h2>")


def test_read_only_allowlist_blocks_activity_actions() -> None:
    session = FakeSession()
    client = BeefReadOnlyClient(session)
    assert "Course" in client.fetch_course("C1")
    assert "Announcement" in client.fetch_announcement_detail(
        information_id="1", idnumber="C1"
    )
    post = next(call for call in session.calls if call[0] == "POST")
    assert post[2]["data"]["_csrf"] == "csrf-token"
    assert post[2]["data"]["informationId"] == "1"
    assert post[2]["params"] == {"idnumber": "C1"}
    assert post[2]["headers"]["Referer"].endswith(
        "/lms/course/information/list?idnumber=C1"
    )
    with pytest.raises(PermissionError):
        client._get("/lms/course/test/start?idnumber=C1")
    with pytest.raises(PermissionError):
        client.fetch_assignment_detail("/lms/course/report/submit?idnumber=C1")
    with pytest.raises(PermissionError):
        client._get("/lms/course/unknown-action?idnumber=C1")


def test_direct_download_is_confined_to_destination(tmp_path: Path) -> None:
    client = BeefReadOnlyClient(FakeSession())
    result = client.download_attachment(
        {"url": "/lms/course/download?id=1", "name": "../lecture.pdf"},
        destination_dir=tmp_path,
    )
    assert result.path.parent == tmp_path.resolve()
    assert result.path.read_bytes() == b"material"


def test_safe_name_normalizes_unicode() -> None:
    assert BeefReadOnlyClient._safe_name("レホ\u309aート.pdf") == "レポート.pdf"


def test_current_beef_two_step_download_recipe(tmp_path: Path) -> None:
    session = FakeSession()
    client = BeefReadOnlyClient(session)
    result = client.download_attachment(
        {
            "name": "講義 資料.pdf",
            "object_name": "2026/aa/bb/object-id",
            "resource_id": "268932",
            "content_id": "131923",
            "open_end_at": "2026-09-30T23:59",
            "metadata": {
                "idnumber": "20261X0161001",
                "end_date_raw": "2026-09-30 23:59:00.0",
            },
        },
        destination_dir=tmp_path,
    )
    assert result.path.name == "lecture.pdf"
    prepare = next(call for call in session.calls if "make/tempfile" in call[1])
    download = next(
        call for call in session.calls if "material/setfiledown" in call[1]
    )
    assert prepare[2]["params"]["id"] == "268932"
    assert prepare[2]["params"]["idnumber"] == "20261X0161001"
    assert prepare[2]["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert prepare[2]["headers"]["Referer"].endswith(
        "/lms/course?idnumber=20261X0161001"
    )
    assert "講義_資料.pdf" in unquote(download[1])
    assert download[2]["params"]["fileId"] == "temporary-file-id"
    assert download[2]["params"]["contentId"] == "131923"
    assert download[2]["params"]["endDate"] == "2026-09-30 23:59:00.0"


def test_report_submission_download_recipe(tmp_path: Path) -> None:
    session = FakeSession()
    client = BeefReadOnlyClient(session)
    result = client.download_attachment(
        {
            "name": "提出 物.pdf",
            "object_name": "2026/aa/bb/submitted-object",
            "content_id": "66853",
            "metadata": {
                "idnumber": "20261X0031001",
                "download_kind": "report_submission",
                "report_id": "66853",
            },
        },
        destination_dir=tmp_path,
    )
    assert result.path.read_bytes() == b"material"
    download = next(
        call for call in session.calls if "report/submission_download" in call[1]
    )
    assert "提出_物.pdf" in unquote(download[1])
    assert download[2]["params"]["reportId"] == "66853"
    assert download[2]["params"]["objectName"] == "2026/aa/bb/submitted-object"
    assert download[2]["headers"]["Referer"].endswith(
        "/lms/course/report/submission"
        "?idnumber=20261X0031001&reportId=66853"
    )
