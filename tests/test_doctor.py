"""doctor 自检核心测试：全部注入假函数，不联网、不读真实磁盘、不枚举真实设备。"""
import socket
import threading
import time
import unittest
import urllib.request
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

import doctor


def ok_kwargs():
    """六项检查都正常的注入函数与配置。"""
    return dict(
        list_input_devices=lambda: ["内置麦克风"],
        free_bytes=lambda: doctor.MIN_FREE_BYTES,
        models_missing=lambda: [],
        llm_config={"base_url": "https://llm.example.com/v1", "api_key": "sk-secret-123", "model": "m-1"},
        voiceprint_modes=lambda: [0o600, 0o600],
        lingering_threads=lambda: 0,
    )


def by_name(checks):
    return {c.name: c for c in checks}


class CheckTest(unittest.TestCase):
    def test_frozen_dataclass(self):
        c = doctor.Check(name="麦克风", status="ok", detail="麦克风可用")
        with self.assertRaises(FrozenInstanceError):
            c.status = "fail"

    def test_min_free_bytes(self):
        self.assertEqual(doctor.MIN_FREE_BYTES, 2 * 1024 ** 3)


class RunChecksTest(unittest.TestCase):
    def test_all_ok(self):
        checks = doctor.run_checks(**ok_kwargs())
        self.assertEqual([c.name for c in checks], ["麦克风", "磁盘", "模型缓存", "大模型配置", "声纹文件权限", "残留音频线程"])
        self.assertEqual([c.status for c in checks], ["ok"] * 6)

    def test_mic_hang_times_out(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def hang():
            gate.wait(10)
            return ["迟到的设备"]

        t0 = time.monotonic()
        checks = doctor.run_checks(**dict(ok_kwargs(), list_input_devices=hang, device_timeout_s=0.2))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
        mic = by_name(checks)["麦克风"]
        self.assertEqual(mic.status, "fail")
        self.assertEqual(mic.detail, "设备枚举无响应")

    def test_no_input_device(self):
        mic = by_name(doctor.run_checks(**dict(ok_kwargs(), list_input_devices=lambda: [])))["麦克风"]
        self.assertEqual(mic.status, "fail")

    def test_low_disk(self):
        disk = by_name(doctor.run_checks(**dict(ok_kwargs(), free_bytes=lambda: doctor.MIN_FREE_BYTES - 1)))["磁盘"]
        self.assertEqual(disk.status, "warn")

    def test_models_missing(self):
        missing = str(Path.home() / ".cache/meeting-recorder/models/x.onnx")
        cache = by_name(doctor.run_checks(**dict(ok_kwargs(), models_missing=lambda: [missing])))["模型缓存"]
        self.assertEqual(cache.status, "fail")
        self.assertNotIn(missing, cache.detail)
        self.assertNotIn(".onnx", cache.detail)

    def test_llm_config(self):
        def run(cfg):
            return by_name(doctor.run_checks(**dict(ok_kwargs(), llm_config=cfg)))["大模型配置"]

        def boom(*a, **k):
            raise AssertionError("自检不应发起网络请求")

        with mock.patch.object(urllib.request, "urlopen", boom), \
                mock.patch.object(socket, "create_connection", boom):
            self.assertEqual(run({"base_url": "https://a/v1", "model": "m"}).status, "warn")
            self.assertEqual(run(None).status, "warn")
            self.assertEqual(run({}).status, "warn")
            valid = run({"base_url": "http://127.0.0.1:11434/v1", "api_key": "k", "model": "m"})
            self.assertEqual(valid.status, "ok")
            self.assertEqual(run({"base_url": "llm.example.com/v1", "api_key": "k", "model": "m"}).status, "fail")
            self.assertEqual(run({"base_url": "ftp://a/v1", "api_key": "k", "model": "m"}).status, "fail")

    def test_voiceprint_perm(self):
        def run(modes):
            return by_name(doctor.run_checks(**dict(ok_kwargs(), voiceprint_modes=lambda: modes)))["声纹文件权限"]

        self.assertEqual(run([0o600, 0o644]).status, "warn")
        self.assertEqual(run([0o600, 0o600]).status, "ok")
        self.assertEqual(run([]).status, "ok")

    def test_lingering_threads(self):
        thread = by_name(doctor.run_checks(**dict(ok_kwargs(), lingering_threads=lambda: 2)))["残留音频线程"]
        self.assertEqual(thread.status, "warn")

    def test_check_error_only_warns(self):
        def boom():
            raise RuntimeError("底层坏了")

        def boom_devices():
            raise RuntimeError("设备序列号 SN-12345")

        kwargs = dict(
            ok_kwargs(),
            list_input_devices=boom_devices,
            free_bytes=boom,
            models_missing=boom,
            voiceprint_modes=boom,
            lingering_threads=boom,
        )
        checks = doctor.run_checks(**kwargs)
        statuses = [c.status for c in checks]
        # 麦克风枚举抛异常算 fail；其余检查失败只降级为 warn；大模型配置完好仍是 ok
        self.assertEqual(statuses, ["fail", "warn", "warn", "ok", "warn", "warn"])
        for c in checks:
            self.assertNotIn("SN-12345", c.detail)

    def test_no_sensitive_text(self):
        home = str(Path.home())
        kwargs = dict(
            ok_kwargs(),
            list_input_devices=lambda: ["麦克风 SN-12345"],
            free_bytes=lambda: doctor.MIN_FREE_BYTES,
            models_missing=lambda: [str(Path.home() / ".cache/meeting-recorder/models/x.onnx")],
            llm_config={"base_url": "https://llm.example.com/v1", "api_key": "sk-secret-123", "model": "m-1"},
            voiceprint_modes=lambda: [0o644],
            lingering_threads=lambda: 1,
        )
        details = "".join(c.detail for c in doctor.run_checks(**kwargs))
        self.assertNotIn(home, details)
        self.assertNotIn("sk-", details)
        self.assertNotIn("SN-12345", details)

        def boom():
            raise RuntimeError(f"读取 {home} 失败，key=sk-secret-123，设备 SN-12345")

        broken = dict(kwargs, free_bytes=boom, models_missing=boom, voiceprint_modes=boom, lingering_threads=boom)
        details = "".join(c.detail for c in doctor.run_checks(**broken))
        self.assertNotIn(home, details)
        self.assertNotIn("sk-", details)
        self.assertNotIn("SN-12345", details)


if __name__ == "__main__":
    unittest.main()
