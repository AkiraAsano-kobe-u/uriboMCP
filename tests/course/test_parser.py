from uribo_mcp.course import parse_announcement_detail, parse_assignment_detail, parse_course_page

COURSE_HTML = """
<h1 class='course-title'>情報学序論</h1><div class='course-overview'>授業の概要です。</div>
<section class='topic-group'><h2>第1回</h2>
  <article class='contents-material' data-module='material' data-content-id='17'>
    <a class='material-title' href='/lms/course/material?idnumber=20261X0161001&materialId=17'>講義資料</a>
    <div class='description'>配布資料の説明</div><a href='/download/17/slide.pdf' download>slide.pdf</a>
  </article>
  <article class='contents-item' data-module='report' data-content-id='88'>
    <a class='report-title' href='/lms/course/report?idnumber=20261X0161001&reportId=88'>レポート1</a>
    <span>提出期限：2026/08/01 23:59</span><span>未提出</span>
  </article>
  <a href='/lms/course/surveys/take?idnumber=20261X0161001&surveyId=9'>第1回アンケート</a>
  <a href='/lms/course/examination?idnumber=20261X0161001&examinationId=3'>小テスト</a>
  <a href='/lms/course/attendance?idnumber=20261X0161001&attendanceId=1'>出席</a>
</section>
"""


def test_course_page_parses_all_non_side_effecting_content_metadata():
    page = parse_course_page(COURSE_HTML, idnumber="20261X0161001")
    assert page["title"] == "情報学序論"
    assert page["overview"] == "授業の概要です。"
    by_category = {item["category"]: item for item in page["contents"]}
    assert set(by_category) == {"material", "assignment", "survey", "test", "attendance"}
    assert by_category["assignment"]["stable_id"] == "assignment/20261X0161001/88"
    assert by_category["assignment"]["deadline"] == "2026-08-01T23:59"
    assert by_category["assignment"]["submission_status"] == "not_submitted"
    assert by_category["material"]["attachments"][0]["name"] == "slide.pdf"


def test_detail_parsers_have_stable_lms_ids_and_submission_status():
    announcement = parse_announcement_detail("<div id='modalContents'><h2>休講</h2><div class='body'>明日は休講です。</div></div>", idnumber="C1", info_id="44")
    assignment = parse_assignment_detail("<main><h1>課題</h1><div class='detail-body'>説明</div><p>提出期限：2026/08/01 12:00 提出済</p></main>", idnumber="C1", report_id="9")
    assert announcement["stable_id"] == "announcement/C1/44"
    assert announcement["body"] == "明日は休講です。"
    assert assignment["stable_id"] == "assignment/C1/9"
    assert assignment["deadline"] == "2026-08-01T12:00"
    assert assignment["submission_status"] == "submitted"


def test_current_beef_material_rows_include_download_recipe():
    html = """
    <h1 id="courseName">システム情報学入門</h1>
    <div id="materialList">
      <div id="material268932"
           class="course-result-list contents-display-flex materialCss clearfix">
        <label class="material-file-name link-txt bold-txt fileDownload">
          講義資料
        </label>
        <span class="fileName">lecture.pdf</span>
        <span class="objectName">2026/aa/bb/object-id</span>
        <span class="scanStatus">1</span>
        <span class="resource_Id">268932</span>
        <span class="openEndDate">2026-09-30 23:59:00.0</span>
        <input id="dlMaterialId" value="131923">
      </div>
    </div>
    """
    page = parse_course_page(html, idnumber="20261X0161001")
    material = page["contents"][0]
    attachment = material["attachments"][0]
    assert material["category"] == "material"
    assert material["title"] == "講義資料"
    assert attachment["name"] == "lecture.pdf"
    assert attachment["object_name"] == "2026/aa/bb/object-id"
    assert attachment["resource_id"] == "268932"
    assert attachment["content_id"] == "131923"
    assert attachment["metadata"]["end_date_raw"] == "2026-09-30 23:59:00.0"


def test_assignment_detail_parses_report_download_recipe():
    html = """
    <main>
      <h1>期末課題</h1>
      <a href="/lms/timetable">時間割へ戻る</a>
      <form id="reportDownloadForm">
        <input name="reportId" value="66853">
        <input name="idnumber" value="20261X0031001">
      </form>
      <div class="result-list">
        <div>
          <div class="link-txt downloadFile">提出物.pdf</div>
          <div class="fileName">提出物.pdf</div>
          <div class="objectName">2026/aa/bb/submitted-object</div>
          <div class="scanStatus">1</div>
        </div>
      </div>
    </main>
    """
    detail = parse_assignment_detail(
        html,
        idnumber="20261X0031001",
        report_id="66853",
    )
    assert len(detail["attachments"]) == 1
    attachment = detail["attachments"][0]
    assert attachment["name"] == "提出物.pdf"
    assert attachment["object_name"] == "2026/aa/bb/submitted-object"
    assert attachment["content_id"] == "66853"
    assert attachment["metadata"]["download_kind"] == "report_submission"
    assert attachment["metadata"]["report_id"] == "66853"
