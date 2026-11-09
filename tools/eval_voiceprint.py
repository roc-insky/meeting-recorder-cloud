"""声纹离线评估：FAR/FRR、推荐阈值、说话人区分错误率（DER）。

纯函数，不读任何目录，不依赖运行时模块。真实数据只在人工运行时使用。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

Segment = Tuple[float, float, str]  # (起, 止, 说话人)

MAX_DER_SPEAKERS = 6
LIMITED_NOTE = "置信有限，阈值仅作初值"


def far_frr(genuine: Sequence[float], impostor: Sequence[float],
            threshold: float) -> Tuple[float, float]:
    """返回 (FAR, FRR)。冒名分数 >= 阈值算误接受；本人分数 < 阈值算误拒绝。"""
    if not genuine or not impostor:
        raise ValueError("样本为空")
    far = sum(1 for s in impostor if s >= threshold) / len(impostor)
    frr = sum(1 for s in genuine if s < threshold) / len(genuine)
    return far, frr


@dataclass(frozen=True)
class Recommendation:
    high: Optional[float]
    low: Optional[float]
    limited: bool
    note: str


def recommend_thresholds(genuine: Sequence[float], impostor: Sequence[float], *,
                         min_genuine: int = 10, min_impostor: int = 20,
                         min_gap: float = 0.05) -> Recommendation:
    """推荐高低阈值。

    high = 候选分数中 FAR<=1% 的最小值；
    low  = 候选分数中 FAR<=10% 的最小值（灰区里的疑似结论冒名率不超过 10%）；
           若 high - low 小于 min_gap，则取 high - min_gap（不足以为正时取 high / 2）。
    """
    if not genuine or not impostor:
        raise ValueError("样本为空")
    limited = len(genuine) < min_genuine or len(impostor) < min_impostor
    notes = []
    if limited:
        notes.append(LIMITED_NOTE)

    candidates = sorted(set(genuine) | set(impostor))
    high = next((t for t in candidates if far_frr(genuine, impostor, t)[0] <= 0.01), None)
    if high is None:
        notes.append("两类分数无法区分")
        return Recommendation(None, None, limited, "；".join(notes))

    low = next(t for t in candidates if far_frr(genuine, impostor, t)[0] <= 0.10)
    if high - low < min_gap:
        low = high - min_gap if high - min_gap > 0 else high / 2
        notes.append("灰区取最小宽度")
    return Recommendation(high, low, limited, "；".join(notes))


def _frames(segs: Sequence[Segment], n: int, step: float):
    out = [None] * n
    for start, end, who in segs:
        a = max(0, int(round(start / step)))
        b = min(n, int(round(end / step)))
        for i in range(a, b):
            out[i] = who
    return out


def diarization_error_rate(reference: Sequence[Segment], hypothesis: Sequence[Segment],
                           *, step: float = 0.01) -> float:
    """按帧计算 DER = (漏检 + 虚警 + 混淆) / 参考语音帧数。

    说话人映射穷举取最优；假设说话人数超过 6 抛 ValueError。假设片段不得重叠。
    """
    hyp_spk = sorted({w for _, _, w in hypothesis})
    if len(hyp_spk) > MAX_DER_SPEAKERS:
        raise ValueError("假设说话人太多，无法穷举映射")
    ref_spk = sorted({w for _, _, w in reference})
    ends = [e for _, e, _ in list(reference) + list(hypothesis)]
    n = int(round(max(ends) / step)) if ends else 0
    ref_f = _frames(reference, n, step)
    hyp_f = _frames(hypothesis, n, step)

    ref_speech = sum(1 for r in ref_f if r is not None)
    if ref_speech == 0:
        raise ValueError("参考里没有语音")
    miss = sum(1 for r, h in zip(ref_f, hyp_f) if r is not None and h is None)
    false_alarm = sum(1 for r, h in zip(ref_f, hyp_f) if r is None and h is not None)
    both = sum(1 for r, h in zip(ref_f, hyp_f) if r is not None and h is not None)

    # 共现表，再穷举 假设说话人 -> 参考说话人（可不映射）取匹配帧数最大者
    co = {}
    for r, h in zip(ref_f, hyp_f):
        if r is not None and h is not None:
            co[(h, r)] = co.get((h, r), 0) + 1
    pool = ref_spk + [None] * len(hyp_spk)
    best = 0
    for perm in set(itertools.permutations(pool, len(hyp_spk))):
        matched = sum(co.get((h, r), 0) for h, r in zip(hyp_spk, perm) if r is not None)
        best = max(best, matched)
    return (miss + false_alarm + (both - best)) / ref_speech
