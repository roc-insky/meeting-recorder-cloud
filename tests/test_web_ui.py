"""前端静态文件的字符串/正则断言（不起浏览器）。

真正的页面行为（录音、转写、标注定位、弹窗）需要真实浏览器和麦克风，见 README「验收记录」。
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
OWN_FILES = ("index.html", "style.css", "app.js", "interview.js")
MARKED_VERSION = "18.1.2"
PURIFY_VERSION = "3.4.17"


def url_concat_violations(js: str):
    """扫描 '/api/…' + 动态片段 的拼接，返回 (未用 encodeURIComponent 包裹的片段列表, 拼接总数)。

    从字符串字面量后的 + 开始，按括号深度切分到同层的 , 或 ) 为止；
    各片段里除字符串字面量外都必须以 encodeURIComponent( 开头。
    """
    bad, count = [], 0
    for m in re.finditer(r"'/api/[^']*'\s*\+", js):
        count += 1
        i, depth, piece, pieces, quote = m.end(), 0, "", [], None
        while i < len(js):
            c = js[i]
            if quote:
                piece += c
                if c == quote:
                    quote = None
            elif c in "'\"":
                quote = c
                piece += c
            elif c in "([{":
                depth += 1
                piece += c
            elif c in ")]}":
                if depth == 0:
                    break
                depth -= 1
                piece += c
            elif c == "," and depth == 0:
                break
            elif c == "+" and depth == 0:
                pieces.append(piece.strip())
                piece = ""
            else:
                piece += c
            i += 1
        pieces.append(piece.strip())
        for p in pieces:
            if p and not re.fullmatch(r"'[^']*'", p) and not p.startswith("encodeURIComponent("):
                bad.append(p)
    return bad, count


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
        """所有 '/api/…' + 动态片段 的拼接，动态片段都必须包在 encodeURIComponent( 里。"""
        total = 0
        for name in ("app.js", "interview.js"):
            js = read(name)
            bad, count = url_concat_violations(js)
            self.assertEqual(bad, [], f"{name} 有未编码的 URL 片段：{bad}")
            total += count
        self.assertGreaterEqual(total, 10, "找到的拼接 URL 太少，检查逻辑可能失效")

    def test_url_check_detects_missing_encode(self):
        """变异验证：去掉任意一处 encodeURIComponent，检查必须报错（防止测试空转）。"""
        js = read("app.js")
        mutated = js.replace("'/api/meetings/' + encodeURIComponent(id) + '/summary'",
                             "'/api/meetings/' + id + '/summary'")
        self.assertNotEqual(js, mutated)
        bad, _ = url_concat_violations(mutated)
        self.assertEqual(len(bad), 1)
        bad, _ = url_concat_violations("api('GET', '/api/x/' + encodeURIComponent(a) + '/y' + b)")
        self.assertEqual(len(bad), 1)
        bad, _ = url_concat_violations("api('GET', '/api/x/' + encodeURIComponent(a) + '/y', {k: 1 + 2})")
        self.assertEqual(bad, [])

    def test_sanitize_whitelist_pinned(self):
        """净化白名单固定：不得出现 script/img/style/iframe 等标签与 onerror/style 等属性，链接只允许 http(s)/mailto。"""
        js = read("interview.js")
        opts = re.search(r"const SANITIZE_OPTIONS\s*=\s*\{(.*?)\n\};", js, re.S)
        self.assertIsNotNone(opts, "找不到 SANITIZE_OPTIONS")
        body = opts.group(1)
        tags = re.search(r"ALLOWED_TAGS:\s*\[(.*?)\]", body, re.S)
        attrs = re.search(r"ALLOWED_ATTR:\s*\[(.*?)\]", body, re.S)
        self.assertIsNotNone(tags)
        self.assertIsNotNone(attrs)
        tag_list = re.findall(r"'([^']+)'", tags.group(1))
        attr_list = re.findall(r"'([^']+)'", attrs.group(1))
        self.assertTrue(tag_list)
        for forbidden in ("script", "img", "style", "iframe", "object", "embed", "form", "input", "svg",
                          "math", "link", "meta", "base", "video", "audio", "button", "textarea"):
            self.assertNotIn(forbidden, tag_list)
        self.assertEqual(attr_list, ["href"])
        self.assertRegex(body, r"ALLOW_DATA_ATTR:\s*false")
        self.assertNotRegex(body, r"ADD_TAGS|ADD_ATTR|ALLOW_UNKNOWN_PROTOCOLS|USE_PROFILES|FORBID_|WHOLE_DOCUMENT")
        self.assertIn("DOMPurify.sanitize(marked.parse(String(markdown), { async: false }), SANITIZE_OPTIONS)", js)
        # URI 正则：只放行 http / https / mailto
        m = re.search(r"ALLOWED_URI_REGEXP:\s*/(.*)/([a-z]*),", body)
        self.assertIsNotNone(m)
        self.assertTrue(m.group(1).startswith("^"), "URI 正则必须以 ^ 锚定开头")
        rx = re.compile(m.group(1), re.I if "i" in m.group(2) else 0)
        for ok in ("http://a.b/c", "https://a.b/c", "HTTPS://a.b", "mailto:a@b.c"):
            self.assertIsNotNone(rx.search(ok), ok)
        for bad in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html;base64,AAAA", "vbscript:x",
                    "file:///etc/passwd", "ftp://a.b", "//evil.com/x", "/relative", " javascript:1",
                    "javascript:alert('http://x')", "xhttp://a.b", "data:text/html,https://a.b"):
            self.assertIsNone(rx.search(bad), bad)

    def test_sanitize_check_detects_mutation(self):
        """变异验证：往白名单加 script/img 或属性 onerror，上面用到的提取逻辑必须能看见它们。"""
        js = read("interview.js")
        mutated = js.replace("'blockquote',", "'blockquote', 'script', 'img',").replace(
            "ALLOWED_ATTR: ['href']", "ALLOWED_ATTR: ['href', 'onerror']")
        self.assertNotEqual(js, mutated)
        body = re.search(r"const SANITIZE_OPTIONS\s*=\s*\{(.*?)\n\};", mutated, re.S).group(1)
        tags = re.findall(r"'([^']+)'", re.search(r"ALLOWED_TAGS:\s*\[(.*?)\]", body, re.S).group(1))
        attrs = re.findall(r"'([^']+)'", re.search(r"ALLOWED_ATTR:\s*\[(.*?)\]", body, re.S).group(1))
        self.assertIn("script", tags)
        self.assertIn("img", tags)
        self.assertNotEqual(attrs, ["href"])

    def test_voiceprint_busy_recovers_and_start_clears_late(self):
        """声纹忙碌标记要能自己恢复；开始录音失败时不能先清掉界面。"""
        js = read("app.js")
        self.assertIn("function pollVpBusy", js)
        fin = re.search(r"function finishVoiceprint\(.*?\n\}\n", js, re.S).group(0)
        self.assertIn("pollVpBusy()", fin)
        cancel = re.search(r"async function cancelVoiceprint\(.*?\n\}\n", js, re.S).group(0)
        self.assertIn("pollVpBusy()", cancel)
        start = re.search(r"async function startRecording\(.*?\n\}\n", js, re.S).group(0)
        self.assertNotIn("clearView()", start)
        self.assertLess(start.index("api('POST', '/api/recording/start'"), start.index("beginRecordingView("))
        begin = re.search(r"function beginRecordingView\(.*?\n\}\n", js, re.S).group(0)
        self.assertIn("clearView()", begin)


    def test_job_save_response_after_selection_change_is_ignored_structure(self):
        """结构断言：保存/删除前记录选择版本，响应回来只在版本未变时才改 currentId / 重置表单。"""
        js = read("interview.js")
        self.assertRegex(js, r"rev:\s*0")
        for name in ("resetJobForm", "editJob"):
            body = re.search(r"function %s\(.*?\n\}\n" % name, js, re.S).group(0)
            self.assertIn("J.rev += 1", body, name)
        save = re.search(r"async function saveJob\(.*?\n\}\n", js, re.S).group(0)
        self.assertLess(save.index("const rev = J.rev"), save.index("await api("))
        self.assertRegex(save, r"if \(J\.rev === rev\) \{\s*J\.currentId = saved\.id")
        self.assertNotRegex(save, r"encodeURIComponent\(J\.currentId\)")   # PUT 必须用请求发出时的 id
        delete = re.search(r"async function deleteJob\(.*?\n\}\n", js, re.S).group(0)
        self.assertIn("if (J.rev === rev) resetJobForm()", delete)

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_job_save_race_runs_real_interview_js(self):
        """在 node vm 里执行真实的 interview.js（假 DOM）：保存 A 期间切到 B，A 的响应不得改走选择。"""
        harness = Path(__file__).resolve().parent / "job_race_harness.js"
        res = subprocess.run(["node", str(harness)], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        # 场景 1：选择仍是 B，表单仍显示 B；随后保存必须是对 B 的 PUT，不是覆盖 A
        self.assertEqual(out["afterSwitch"], {"currentId": "B", "title": "职位B", "req": "rb"})
        self.assertEqual(out["secondSave"]["url"], "/api/jobs/B")
        self.assertEqual(out["secondSave"]["body"]["title"], "职位B")
        # 场景 2：新建保存期间切到 A，新职位只出现在列表里，不抢选择
        self.assertEqual(out["afterCreateSwitch"], {"currentId": "A", "title": "职位A"})
        # 场景 3：选择没变时，新建的职位照常成为当前职位
        self.assertEqual(out["afterCreateSame"], {"currentId": "NEW2"})
        # 场景 4：删除期间切到 B，不得重置 B 的表单
        self.assertEqual(out["afterDeleteSwitch"], {"currentId": "B", "title": "职位B"})


if __name__ == "__main__":
    unittest.main()
