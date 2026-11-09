# meeting-recorder

本地会议录音、转写与说话人区分工具。全部在本机运行。

## 安装

需要 Python 3.13 和 [uv](https://github.com/astral-sh/uv)。

```bash
uv venv --python /opt/homebrew/bin/python3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

测试不联网，不加载真实模型，不使用真实麦克风。

## 可行性检查

```bash
.venv/bin/python spike/feasibility.py
```

会下载声纹与分割模型到临时目录，确认声纹向量为 512 维。

## 实时转写

```bash
.venv/bin/python record.py --live                      # 边录边转写，每句输出 [HH:MM:SS] 文字
.venv/bin/python record.py --live --live-model REPO    # 指定实时转写模型（默认同转写默认模型）
```

- 每句话同时显示在终端并追加到会议目录的 `live.txt`；停止录音后仍会照常生成完整的 `transcript.md`。
- 首次使用会下载约 644 KB 的 VAD 模型（silero_vad.onnx）到 `~/.cache/meeting-recorder/models`；下载失败只警告，录音照常进行，只是没有实时转写。
- 实时转写出错或跟不上时只警告（跟不上时丢弃较早的句子），绝不影响录音；停止时最多等 10 秒收尾。

## 目录说明

- `common.py`：公共常量与工具函数。
- `record.py`：录音（边录边落盘）、m4a 压缩、转写；`--live` 边录边转写（见下）。退出码：0 成功，1 录音失败，2 录音已保存但转写失败。
- `live.py`：实时转写（Silero VAD 切句 + 后台逐句转写 + live.txt）。
- `transcribe.py`：转写核心与文字稿渲染。
- `tools/`：离线评估工具。
- `tests/`：单元测试。
- `spike/`：可行性验证脚本。
- `docs/`：设计与计划文档。

## 验收记录

- Task 3 真实麦克风冒烟：未测
- Task 6 实时转写真实试跑：未测
