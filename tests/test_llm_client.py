"""llm_client 的测试：全部使用假 opener，不联网。"""
import io
import json
import socket
import unittest
import urllib.error

import llm_client
from llm_client import LLMError

CFG = {"base_url": "https://llm.example.com/v1/", "api_key": "sk-secret-123", "model": "m-1",
       "timeout_s": 7, "max_input_chars": 1000}


class FakeResp:
    def __init__(self, body=b"", status=200, headers=None):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status = status
        self.headers = headers or {}

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok_body(content="你好"):
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}, ensure_ascii=False)


class FakeOpener:
    """依次返回/抛出预置结果，并记录每次请求。"""

    def __init__(self, *results):
        self.results = list(results)
        self.requests = []
        self.timeouts = []

    def open(self, req, data=None, timeout=None):
        self.requests.append(req)
        self.timeouts.append(timeout)
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def http_error(code, body=b""):
    return urllib.error.HTTPError("https://llm.example.com/v1/chat/completions", code, "x", {}, io.BytesIO(body))


class ChatTests(unittest.TestCase):
    def test_chat_ok(self):
        op = FakeOpener(FakeResp(ok_body("  摘要内容\n")))
        out = llm_client.chat_summary(CFG, "系统", "用户", opener=op)
        self.assertEqual(out, "摘要内容")
        req = op.requests[0]
        self.assertEqual(req.full_url, "https://llm.example.com/v1/chat/completions")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-secret-123")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(op.timeouts, [7])
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], "m-1")
        self.assertEqual(body["messages"], [{"role": "system", "content": "系统"}, {"role": "user", "content": "用户"}])
        self.assertIs(body.get("stream", False), False)

    def test_redirect_not_followed(self):
        op = FakeOpener(FakeResp(b"", 302, {"Location": "https://evil.example.com/"}), FakeResp(ok_body()))
        with self.assertRaises(LLMError) as cm:
            llm_client.chat_summary(CFG, "s", "u", opener=op)
        self.assertEqual(len(op.requests), 1)
        self.assertNotIn("evil", cm.exception.message)

    def test_redirect_http_error_not_followed(self):
        op = FakeOpener(http_error(307), FakeResp(ok_body()))
        with self.assertRaises(LLMError):
            llm_client.chat_summary(CFG, "s", "u", opener=op)
        self.assertEqual(len(op.requests), 1)

    def test_redirect_handler_raises(self):
        handler = llm_client._NoRedirect()
        req = llm_client.urllib.request.Request("https://llm.example.com/v1/chat/completions", data=b"{}")
        with self.assertRaises(LLMError):
            handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {}, "https://evil.example.com/")

    def test_error_body_with_key_not_leaked(self):
        op = FakeOpener(http_error(500, b'{"error":"bad key sk-secret-123"}'))
        with self.assertRaises(LLMError) as cm:
            llm_client.chat_summary(CFG, "s", "u", opener=op)
        err = cm.exception
        for text in (err.message, str(err), repr(err), repr(err.args)):
            self.assertNotIn("sk-secret-123", text)
        self.assertEqual(err.status, 500)

    def test_error_status_in_ok_response_body(self):
        op = FakeOpener(FakeResp("bad key sk-secret-123", 503))
        with self.assertRaises(LLMError) as cm:
            llm_client.chat_summary(CFG, "s", "u", opener=op)
        self.assertNotIn("sk-secret-123", repr(cm.exception) + cm.exception.message)

    def test_timeout_message(self):
        for exc in (socket.timeout("timed out"), TimeoutError(), urllib.error.URLError(socket.timeout("t"))):
            with self.subTest(exc=repr(exc)):
                with self.assertRaises(LLMError) as cm:
                    llm_client.chat_summary(CFG, "s", "u", opener=FakeOpener(exc))
                self.assertIn("超时", cm.exception.message)

    def test_connection_error_message(self):
        for exc in (urllib.error.URLError(ConnectionRefusedError("sk-secret-123")), ConnectionResetError("x"),
                    OSError("boom")):
            with self.subTest(exc=repr(exc)):
                with self.assertRaises(LLMError) as cm:
                    llm_client.chat_summary(CFG, "s", "u", opener=FakeOpener(exc))
                self.assertIn("连接", cm.exception.message)
                self.assertNotIn("sk-secret", repr(cm.exception))
                self.assertNotIn("超时", cm.exception.message)

    def test_http_status_messages_distinct_and_fixed(self):
        msgs = {}
        for code in (401, 403, 404, 429, 400, 500, 502):
            with self.assertRaises(LLMError) as cm:
                llm_client.chat_summary(CFG, "s", "u", opener=FakeOpener(http_error(code, b"secret body")))
            self.assertEqual(cm.exception.status, code)
            self.assertNotIn("secret body", cm.exception.message)
            msgs[code] = cm.exception.message
        self.assertIn("Key", msgs[401])
        self.assertNotEqual(msgs[429], msgs[500])

    def test_empty_or_bad_content(self):
        bad = [ok_body(""), ok_body("   \n"), json.dumps({"choices": []}), json.dumps({}), "not json",
               json.dumps({"choices": [{"message": {"content": None}}]}), "[1]",
               json.dumps({"choices": [{"message": {"content": 5}}]})]
        for body in bad:
            with self.subTest(body=body):
                with self.assertRaises(LLMError):
                    llm_client.chat_summary(CFG, "s", "u", opener=FakeOpener(FakeResp(body)))

    def test_content_parts_list(self):
        body = json.dumps({"choices": [{"message": {"content": [{"type": "text", "text": "甲"},
                                                                {"type": "text", "text": "乙"}]}}]})
        self.assertEqual(llm_client.chat_summary(CFG, "s", "u", opener=FakeOpener(FakeResp(body))), "甲乙")

    def test_bad_base_url(self):
        for url in ("ftp://x/y", "file:///etc/passwd", "no-scheme", ""):
            with self.subTest(url=url):
                op = FakeOpener()
                with self.assertRaises(LLMError):
                    llm_client.chat_summary(dict(CFG, base_url=url), "s", "u", opener=op)
                self.assertEqual(op.requests, [])

    def test_oversized_response_rejected(self):
        op = FakeOpener(FakeResp(b"x" * (llm_client.MAX_RESPONSE_BYTES + 10)))
        with self.assertRaises(LLMError):
            llm_client.chat_summary(CFG, "s", "u", opener=op)


class TestConnectionTests(unittest.TestCase):
    """test_connection：只返回固定的中文原因，不发第二次请求，不回传服务器原文或 Key。"""

    OK = "连接成功"
    NOT_CONFIGURED = "未配置大模型"
    CONNECT = "无法连接，请检查地址和网络"
    AUTH = "认证失败，请检查 API Key"
    SERVER = "服务返回错误"
    TIMEOUT = "请求超时"
    REDIRECT = "地址发生了重定向，已拒绝"

    def test_ok(self):
        op = FakeOpener(FakeResp(ok_body("好")))
        self.assertEqual(llm_client.test_connection(CFG, opener=op), (True, self.OK))
        self.assertEqual(len(op.requests), 1)
        req = op.requests[0]
        self.assertEqual(req.full_url, "https://llm.example.com/v1/chat/completions")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-secret-123")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(op.timeouts, [7])
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], "m-1")
        self.assertEqual(body["max_tokens"], 16)
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertIs(body.get("stream", False), False)

    def test_not_configured(self):
        for cfg in (None, {}, {"base_url": "https://llm.example.com/v1/", "api_key": ""},
                    {"base_url": "https://llm.example.com/v1/", "api_key": "sk-secret-123"}):
            with self.subTest(cfg=cfg):
                op = FakeOpener()
                self.assertEqual(llm_client.test_connection(cfg, opener=op),
                                 (False, self.NOT_CONFIGURED))
                self.assertEqual(op.requests, [])

    def test_auth_failed(self):
        for code in (401, 403):
            with self.subTest(code=code):
                op = FakeOpener(http_error(code, b"bad key sk-secret-123"))
                result = llm_client.test_connection(CFG, opener=op)
                self.assertEqual(result, (False, self.AUTH))
                self.assertNotIn("sk-secret-123", repr(result))

    def test_server_error_body_has_key(self):
        op = FakeOpener(http_error(500, b"bad key sk-secret-123"))
        result = llm_client.test_connection(CFG, opener=op)
        self.assertEqual(result, (False, self.SERVER))
        self.assertNotIn("sk-secret-123", repr(result))

    def test_server_error_statuses(self):
        for code in (400, 404, 429, 500, 502, 503):
            with self.subTest(code=code):
                result = llm_client.test_connection(CFG, opener=FakeOpener(http_error(code, b"secret body")))
                self.assertEqual(result, (False, self.SERVER))
                self.assertNotIn("secret body", repr(result))

    def test_redirect_refused(self):
        for first in (http_error(302), http_error(307),
                      FakeResp(b"", 302, {"Location": "https://evil.example.com/"})):
            with self.subTest(first=repr(first)):
                op = FakeOpener(first, FakeResp(ok_body()))
                result = llm_client.test_connection(CFG, opener=op)
                self.assertEqual(result, (False, self.REDIRECT))
                self.assertEqual(len(op.requests), 1)
                self.assertNotIn("evil", repr(result))

    def test_timeout(self):
        for exc in (socket.timeout("timed out"), TimeoutError(),
                    urllib.error.URLError(socket.timeout("t"))):
            with self.subTest(exc=repr(exc)):
                result = llm_client.test_connection(CFG, opener=FakeOpener(exc))
                self.assertEqual(result, (False, self.TIMEOUT))
                self.assertNotIn("timed out", repr(result))

    def test_connect_failed(self):
        for exc in (urllib.error.URLError(ConnectionRefusedError("sk-secret-123")),
                    ConnectionResetError("x"), OSError("boom")):
            with self.subTest(exc=repr(exc)):
                result = llm_client.test_connection(CFG, opener=FakeOpener(exc))
                self.assertEqual(result, (False, self.CONNECT))
                self.assertNotIn("sk-secret", repr(result))
                self.assertNotIn("boom", repr(result))

    def test_bad_base_url_not_requested(self):
        for url in ("ftp://x/y", "file:///etc/passwd", "no-scheme"):
            with self.subTest(url=url):
                op = FakeOpener()
                result = llm_client.test_connection(dict(CFG, base_url=url), opener=op)
                self.assertEqual(result, (False, self.CONNECT))
                self.assertEqual(op.requests, [])

    def test_unexpected_exception_is_generic(self):
        result = llm_client.test_connection(CFG, opener=FakeOpener(RuntimeError("boom sk-secret-123")))
        self.assertEqual(result, (False, self.SERVER))
        self.assertEqual(result[1], self.SERVER)
        for text in ("boom", "sk-secret-123"):
            self.assertNotIn(text, repr(result))

    def test_reasons_belong_to_fixed_set(self):
        fixed = {self.OK, self.NOT_CONFIGURED, self.CONNECT, self.AUTH,
                 self.SERVER, self.TIMEOUT, self.REDIRECT}
        results = [
            llm_client.test_connection(CFG, opener=FakeOpener(FakeResp(ok_body()))),
            llm_client.test_connection(None, opener=FakeOpener()),
            llm_client.test_connection(CFG, opener=FakeOpener(http_error(401))),
            llm_client.test_connection(CFG, opener=FakeOpener(http_error(500, b"x"))),
            llm_client.test_connection(CFG, opener=FakeOpener(TimeoutError())),
            llm_client.test_connection(CFG, opener=FakeOpener(http_error(302))),
        ]
        for ok, reason in results:
            self.assertIsInstance(ok, bool)
            self.assertIn(reason, fixed)


class DescribeImagesTests(unittest.TestCase):
    IMG = "data:image/png;base64,AAAA"

    def test_images_message_shape(self):
        op = FakeOpener(FakeResp(ok_body("简历文字")))
        out = llm_client.describe_images(CFG, [self.IMG, "data:image/jpeg;base64,BBBB"], opener=op)
        self.assertEqual(out, "简历文字")
        body = json.loads(op.requests[0].data.decode("utf-8"))
        msgs = body["messages"]
        self.assertEqual(msgs[-1]["role"], "user")
        parts = msgs[-1]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual([p["type"] for p in parts[1:]], ["image_url", "image_url"])
        self.assertEqual(parts[1]["image_url"]["url"], self.IMG)
        self.assertEqual(op.requests[0].get_header("Authorization"), "Bearer sk-secret-123")

    def test_no_images_is_value_error(self):
        with self.assertRaises(ValueError):
            llm_client.describe_images(CFG, [], opener=FakeOpener())

    def test_llm_error_attributes(self):
        err = LLMError(502, "出错了")
        self.assertEqual((err.status, err.message, str(err)), (502, "出错了", "出错了"))


if __name__ == "__main__":
    unittest.main()
