"""前端静态文件的字符串/正则断言（不起浏览器）。

真正的页面行为（录音、转写、标注定位、弹窗）需要真实浏览器和麦克风，见 README「验收记录」。
"""
import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
OWN_FILES = ("index.html", "style.css", "app.js", "interview.js")
MARKED_VERSION = "18.1.2"
PURIFY_VERSION = "3.4.17"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


class StaticFilesTest(unittest.TestCase):
    def test_files_exist(self):
        for name in OWN_FILES + ("marked.min.js", "purify.min.js"):
            self.assertTrue((STATIC / name).is_file(), name)

    def test_no_cdn(self):
        """自有文件不得有任何外链（vendored 库本身的注释链接不在检查范围）。"""
        for name in OWN_FILES:
            text = read(name)
            self.assertNotRegex(text, r"https?://", name)
            self.assertNotRegex(text, r"(?<![:\w])//[a-z0-9.-]+\.[a-z]{2,}/", name)   # 协议相对地址

    def test_vendored_libs(self):
        marked = read("marked.min.js")
        purify = read("purify.min.js")
        self.assertIn(f"marked v{MARKED_VERSION}", marked)
        self.assertIn(f"DOMPurify {PURIFY_VERSION}", purify)
        self.assertNotIn("sourceMappingURL", marked + purify)     # 不引用不存在的 .map 文件

    def test_index_loads_local_scripts_in_order(self):
        html = read("index.html")
        srcs = re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)
        self.assertEqual(srcs, ["/static/marked.min.js", "/static/purify.min.js",
                               "/static/app.js", "/static/interview.js"])
        self.assertIn('href="/static/style.css"', html)

    def test_no_inline_handlers_or_scripts(self):
        html = read("index.html")
        self.assertNotRegex(html, r"\son[a-z]+\s*=")
        self.assertNotRegex(html, r"<script(?![^>]*\ssrc=)")
        self.assertNotIn("javascript:", html)

    def test_no_innerhtml_except_interview(self):
        for name in ("app.js", "index.html"):
            text = read(name)
            for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
                self.assertNotIn(bad, text, f"{name} 含 {bad}")
        interview = read("interview.js")
        self.assertNotIn("insertAdjacentHTML", interview)
        self.assertNotIn("outerHTML", interview)
        uses = re.findall(r"innerHTML", interview)
        self.assertEqual(len(uses), 1)
        self.assertRegex(interview, r"innerHTML\s*=\s*DOMPurify\.sanitize\(")
        self.assertIn("marked.parse(", interview)

    def test_tab_order(self):
        html = read("index.html")
        ids = re.findall(r'id="(tab-[a-z]+)"', html)
        self.assertEqual(ids, ["tab-meeting", "tab-interview", "tab-voiceprint"])
        labels = re.findall(r'id="tab-[a-z]+"[^>]*>([^<]+)<', html)
        self.assertEqual([s.strip() for s in labels], ["当前会议", "面试帮助", "声纹登记"])
        views = re.findall(r'id="(view-[a-z]+)"', html)
        self.assertEqual(views, ["view-meeting", "view-interview", "view-voiceprint"])

    def test_no_consent_checkbox(self):
        for name in OWN_FILES:
            self.assertNotIn("已取得被登记者同意", read(name), name)
        html = read("index.html")
        self.assertNotIn("consent", html.lower())
        self.assertIn("明确替换同名声纹", html)

    def test_voiceprint_form_fields(self):
        """声纹表单只有姓名与“明确替换”两个输入。"""
        html = read("index.html")
        form = re.search(r'<form[^>]*id="vp-form".*?</form>', html, re.S)
        self.assertIsNotNone(form)
        inputs = re.findall(r"<input[^>]*>", form.group(0))
        self.assertEqual(len(inputs), 2)
        self.assertEqual(sum('type="checkbox"' in s for s in inputs), 1)
        self.assertIn('id="voiceprint-dialog"', html)

    def test_step_any(self):
        self.assertRegex(read("index.html"), r'id="ann-time"[^>]*step="any"')

    def test_kinds(self):
        js = read("app.js")
        m = re.search(r"const KINDS\s*=\s*\{(.*?)\}", js, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertRegex(body, r"note\s*:\s*['\"]📝笔记['\"]")
        self.assertRegex(body, r"highlight\s*:\s*['\"]⭐重点['\"]")
        self.assertRegex(body, r"comment\s*:\s*['\"]💬注释['\"]")

    def test_render_line_uses_textcontent(self):
        js = read("app.js")
        m = re.search(r"function renderTranscriptLine\(raw, onClick, anchor\)\s*\{(.*?)\n\}\n", js, re.S)
        self.assertIsNotNone(m, "找不到 renderTranscriptLine(raw, onClick, anchor)")
        body = m.group(1)
        self.assertIn("textContent", body)
        self.assertNotIn("innerHTML", body)

    def test_summary_and_status_ids(self):
        html = read("index.html")
        for ident in ("summary-speaker", "summary-result", "summary-prompt", "summary-submit",
                      "summary-advanced", "final-status"):
            self.assertIn(f'id="{ident}"', html)
        self.assertRegex(read("app.js"), r"status-pill final-")

    def test_speakers_input_range(self):
        html = read("index.html")
        self.assertRegex(html, r'id="speakers"[^>]*min="1"')
        self.assertRegex(html, r'id="speakers"[^>]*max="20"')
        self.assertIn("当前会议人数", html)

    def test_media_query_three_columns(self):
        css = read("style.css")
        self.assertRegex(css, r"@media\s*\(min-width:\s*1041px\)")
        self.assertIn("overflow-y: auto", css)

    def test_urls_use_encodeuricomponent(self):
        for name in ("app.js", "interview.js"):
            js = read(name)
            for m in re.finditer(r"`/api/[^`]*\$\{([^}]*)\}", js):
                self.assertIn("encodeURIComponent", m.group(1), f"{name}: {m.group(0)}")


if __name__ == "__main__":
    unittest.main()
