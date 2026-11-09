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

## 目录说明

- `common.py`：公共常量与工具函数。
- `record.py`：录音（边录边落盘）、m4a 压缩、转写；`--live` 暂未实现。退出码：0 成功，1 录音失败，2 录音已保存但转写失败。
- `transcribe.py`：转写核心与文字稿渲染。
- `tools/`：离线评估工具。
- `tests/`：单元测试。
- `spike/`：可行性验证脚本。
- `docs/`：设计与计划文档。

## 验收记录

- Task 3 真实麦克风冒烟：未测
