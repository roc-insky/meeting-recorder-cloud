"""web_config 的单元测试（只用临时目录，不碰真实配置）。"""
import tempfile
import unittest
from pathlib import Path

import web_config


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, text: str, name: str = "config.toml") -> Path:
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return p


class LoadPortTests(ConfigTestCase):
    def test_none_path_default(self):
        self.assertEqual(web_config.load_port(None), 8765)

    def test_missing_file_default(self):
        self.assertEqual(web_config.load_port(self.dir / "nope.toml"), 8765)

    def test_missing_section_default(self):
        self.assertEqual(web_config.load_port(self.write("[llm]\nmodel='x'\n")), 8765)

    def test_missing_key_default(self):
        self.assertEqual(web_config.load_port(self.write("[web]\n")), 8765)

    def test_valid_port(self):
        self.assertEqual(web_config.load_port(self.write("[web]\nport = 9000\n")), 9000)

    def test_boundaries(self):
        self.assertEqual(web_config.load_port(self.write("[web]\nport = 1\n")), 1)
        self.assertEqual(web_config.load_port(self.write("[web]\nport = 65535\n")), 65535)

    def test_invalid_values_default(self):
        for value in ("0", "65536", "-1", "true", "false", "'8000'", "80.5", "[1]", "{a=1}"):
            with self.subTest(value=value):
                self.assertEqual(web_config.load_port(self.write(f"[web]\nport = {value}\n")), 8765)

    def test_web_not_a_table_default(self):
        self.assertEqual(web_config.load_port(self.write("web = 5\n")), 8765)

    def test_malformed_toml_default(self):
        self.assertEqual(web_config.load_port(self.write("[web\nport = = 9\n")), 8765)

    def test_non_utf8_default(self):
        p = self.dir / "bad.toml"
        p.write_bytes(b"[web]\nport = 9000\n\xff\xfe")
        self.assertEqual(web_config.load_port(p), 8765)

    def test_directory_as_path_default(self):
        self.assertEqual(web_config.load_port(self.dir), 8765)


LLM_OK = "[llm]\nbase_url = 'https://api.example.com/v1'\napi_key = 'sk-abc'\nmodel = 'm1'\n"


class LoadLlmTests(ConfigTestCase):
    def test_none_path(self):
        self.assertIsNone(web_config.load_llm(None))

    def test_missing_file(self):
        self.assertIsNone(web_config.load_llm(self.dir / "nope.toml"))

    def test_missing_section(self):
        self.assertIsNone(web_config.load_llm(self.write("[web]\nport = 9000\n")))

    def test_defaults(self):
        cfg = web_config.load_llm(self.write(LLM_OK))
        self.assertEqual(cfg, {
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-abc",
            "model": "m1",
            "timeout_s": 120,
            "max_input_chars": 60000,
        })

    def test_overrides(self):
        cfg = web_config.load_llm(self.write(LLM_OK + "timeout_s = 30\nmax_input_chars = 1000\n"))
        self.assertEqual(cfg["timeout_s"], 30)
        self.assertEqual(cfg["max_input_chars"], 1000)

    def test_float_timeout_allowed(self):
        cfg = web_config.load_llm(self.write(LLM_OK + "timeout_s = 2.5\n"))
        self.assertEqual(cfg["timeout_s"], 2.5)

    def test_invalid_optional_falls_back(self):
        for extra in ("timeout_s = 0\n", "timeout_s = -3\n", "timeout_s = true\n", "timeout_s = 'x'\n"):
            with self.subTest(extra=extra):
                self.assertEqual(web_config.load_llm(self.write(LLM_OK + extra))["timeout_s"], 120)
        for extra in ("max_input_chars = 0\n", "max_input_chars = 1.5\n", "max_input_chars = true\n", "max_input_chars = 'x'\n"):
            with self.subTest(extra=extra):
                self.assertEqual(web_config.load_llm(self.write(LLM_OK + extra))["max_input_chars"], 60000)

    def test_each_required_key_missing(self):
        full = {"base_url": "https://a/v1", "api_key": "k", "model": "m"}
        for missing in full:
            body = "[llm]\n" + "".join(f"{k} = '{v}'\n" for k, v in full.items() if k != missing)
            with self.subTest(missing=missing):
                self.assertIsNone(web_config.load_llm(self.write(body)))

    def test_empty_or_non_string_required(self):
        for bad in ("''", "'   '", "5", "true", "['a']"):
            with self.subTest(bad=bad):
                body = f"[llm]\nbase_url = 'https://a/v1'\napi_key = {bad}\nmodel = 'm'\n"
                self.assertIsNone(web_config.load_llm(self.write(body)))

    def test_malformed_toml(self):
        self.assertIsNone(web_config.load_llm(self.write("[llm\nbase_url = ")))

    def test_llm_not_a_table(self):
        self.assertIsNone(web_config.load_llm(self.write("llm = 'x'\n")))


if __name__ == "__main__":
    unittest.main()
