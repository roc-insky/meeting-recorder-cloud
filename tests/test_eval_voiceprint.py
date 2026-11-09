"""声纹离线评估算法的单元测试（只用手造分数，不读文件、不用音频）。"""
import unittest

from tools.eval_voiceprint import (
    Recommendation,
    diarization_error_rate,
    far_frr,
    recommend_thresholds,
)


def _grid(lo, hi, n):
    """生成 n 个均匀分布在 [lo, hi] 的分数。"""
    return [round(lo + (hi - lo) * i / (n - 1), 4) for i in range(n)]


class FarFrrTest(unittest.TestCase):
    def test_far_frr_basic(self):
        self.assertEqual(
            far_frr([0.8, 0.7, 0.6, 0.4], [0.5, 0.3, 0.2, 0.1], 0.5),
            (0.25, 0.25),
        )

    def test_far_frr_threshold_inclusive(self):
        # 冒名分数恰等于阈值算误接受；本人分数恰等于阈值不算误拒绝
        far, frr = far_frr([0.5], [0.5], 0.5)
        self.assertEqual((far, frr), (1.0, 0.0))

    def test_far_frr_empty(self):
        with self.assertRaises(ValueError):
            far_frr([], [0.1], 0.5)
        with self.assertRaises(ValueError):
            far_frr([0.9], [], 0.5)


class RecommendTest(unittest.TestCase):
    def test_separable(self):
        genuine = _grid(0.7, 0.9, 12)
        impostor = _grid(0.1, 0.3, 25)
        rec = recommend_thresholds(genuine, impostor)
        self.assertFalse(rec.limited)
        # 最小满足 FAR<=1% 的候选：冒名最高分 0.3 之后的第一个候选就是 0.7
        self.assertAlmostEqual(rec.high, 0.7)
        # low = FAR<=10% 的最小候选：25 个冒名里最多 2 个 >= t，即第 24 小的分数
        self.assertAlmostEqual(rec.low, sorted(impostor)[-2])
        self.assertLess(far_frr(genuine, impostor, rec.low)[0], 0.1 + 1e-9)
        self.assertGreater(far_frr(genuine, impostor, sorted(impostor)[-3])[0], 0.1)

    def test_overlap(self):
        # 冒名 100 个：0.01..1.00 步长 0.01 太散，手造：
        # 99 个在 0.10~0.40，1 个在 0.60
        impostor = _grid(0.10, 0.40, 99) + [0.60]
        genuine = _grid(0.55, 0.95, 20)
        rec = recommend_thresholds(genuine, impostor)
        # FAR<=1% 允许 1 个冒名超过阈值 -> 最小候选是 0.55（只有 0.60 一个冒名 >=0.55）
        self.assertAlmostEqual(rec.high, 0.55)
        # FAR<=10% 的最小候选：101 个冒名里最多 10 个 >= t -> 在 0.40 附近
        self.assertTrue(0 < rec.low < rec.high)
        self.assertLessEqual(far_frr(genuine, impostor, rec.low)[0], 0.10)

    def test_far_exactly_one_percent(self):
        impostor = [0.1] * 99 + [0.5]  # 恰好 1 个 >= 0.5
        genuine = _grid(0.5, 0.9, 12)
        rec = recommend_thresholds(genuine, impostor, min_impostor=20)
        self.assertIsNotNone(rec.high)
        far, _ = far_frr(genuine, impostor, rec.high)
        self.assertLessEqual(far, 0.01)

    def test_indistinguishable(self):
        genuine = _grid(0.1, 0.3, 12)
        impostor = _grid(0.5, 0.9, 25)
        rec = recommend_thresholds(genuine, impostor)
        self.assertIsNone(rec.high)
        self.assertIsNone(rec.low)
        self.assertIn("无法区分", rec.note)

    def test_small_sample_limited(self):
        rec = recommend_thresholds([0.8, 0.9, 0.7], [0.1, 0.2, 0.3, 0.15, 0.25])
        self.assertTrue(rec.limited)
        self.assertIn("置信有限，阈值仅作初值", rec.note)

    def test_min_gap_when_low_equals_high(self):
        # 99 个冒名在 0.1，1 个在 0.5：FAR<=1% 与 FAR<=10% 的最小候选都是 0.5
        impostor = [0.1] * 99 + [0.5]
        genuine = _grid(0.5, 0.9, 12)
        rec = recommend_thresholds(genuine, impostor, min_impostor=20)
        self.assertAlmostEqual(rec.high, 0.5)
        self.assertAlmostEqual(rec.low, 0.45)
        self.assertIn("灰区取最小宽度", rec.note)

    def test_min_gap_keeps_low_positive(self):
        impostor = [0.01] * 99 + [0.04]
        genuine = _grid(0.04, 0.9, 12)
        rec = recommend_thresholds(genuine, impostor, min_impostor=20)
        self.assertGreater(rec.low, 0)
        self.assertLess(rec.low, rec.high)


class DerTest(unittest.TestCase):
    def test_perfect(self):
        ref = [(0.0, 5.0, "A"), (5.0, 10.0, "B")]
        self.assertEqual(diarization_error_rate(ref, list(ref)), 0.0)

    def test_label_swap(self):
        ref = [(0.0, 5.0, "A"), (5.0, 10.0, "B")]
        hyp = [(0.0, 5.0, "x"), (5.0, 10.0, "y")]
        self.assertEqual(diarization_error_rate(ref, hyp), 0.0)

    def test_half_missed(self):
        ref = [(0.0, 10.0, "A")]
        hyp = [(0.0, 5.0, "A")]
        self.assertAlmostEqual(diarization_error_rate(ref, hyp), 0.5, places=2)

    def test_too_many_speakers(self):
        ref = [(0.0, 7.0, "A")]
        hyp = [(float(i), float(i + 1), f"s{i}") for i in range(7)]
        with self.assertRaises(ValueError):
            diarization_error_rate(ref, hyp)


if __name__ == "__main__":
    unittest.main()
