"""可行性门槛：验证 3D-Speaker eres2net 声纹模型输出 512 维，并检查依赖可用。

模型下载到临时目录，不写入 ~/.cache。运行：.venv/bin/python spike/feasibility.py
"""
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import numpy as np

BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
EMBED_URL = BASE + "speaker-recongition-models/3dspeaker_speech_eres2net_large_sv_zh-cn_3dspeaker_16k.onnx"
SEG_URL = BASE + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"


def download(url: str, dest: Path, retries: int = 3) -> None:
    """带重试的下载。"""
    last = None
    for i in range(retries):
        try:
            urllib.request.urlretrieve(url, dest)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"下载失败（第 {i + 1} 次）：{e}")
    raise RuntimeError(f"下载失败：{url}: {last}")


def main() -> int:
    import sherpa_onnx

    tmp = Path(tempfile.mkdtemp(prefix="mr-spike-"))
    print("临时目录：", tmp)
    try:
        embed = tmp / "eres2net.onnx"
        download(EMBED_URL, embed)
        seg_tar = tmp / "seg.tar.bz2"
        download(SEG_URL, seg_tar)
        with tarfile.open(seg_tar) as t:
            t.extractall(tmp, filter="data")
        print("分割模型：", sorted(p.name for p in tmp.glob("sherpa-onnx-pyannote*/*")))

        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(embed), num_threads=1)
        extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=np.zeros(16000, dtype=np.float32))
        stream.input_finished()
        vec = extractor.compute(stream)
        print("extractor.dim =", extractor.dim, "len(vec) =", len(vec))
        ok = extractor.dim == 512

        import mlx_whisper  # noqa: F401
        print("mlx_whisper 可导入")
        r = subprocess.run(["afconvert", "-h"], capture_output=True, text=True)
        print("afconvert 可用，退出码", r.returncode, "路径", shutil.which("afconvert"))
        print("验收：", "通过" if ok else "失败（dim != 512）")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
