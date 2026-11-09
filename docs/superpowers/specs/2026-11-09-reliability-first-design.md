# 可信度优先（Reliability-First）设计

日期：2026-11-09。来源：CEO 评审（选择性扩展，方案 B）+ 工程评审。本机评审时没有代码，所有对代码的描述来自 docs/plan/meeting-recorder-master-plan.md，未对照实现核实。

## 目标
让录音不再被系统音频卡死拖垮、崩溃后的录音可恢复、声纹识别结果带置信度分档，并提供自检页和可重复的离线评估脚本。

## 范围（做）
- #0 把阶段 13、14 的未提交改动分两次提交。
- #1 doctor 自检（GET 只读）+「测试连接」（POST）。
- #2a 录音隔离 spike；#2b 实现另立计划，由 spike 结论决定。
- #3 中断录音恢复（「未完成」标记、补转写、重新转写）。
- #4 离线评估脚本（FAR/FRR/推荐阈值、DER）。
- #5 声纹分档（高/低两个阈值，灰区写「疑似X」）。

## 范围（不做）
本地 LLM 预设与隐私界面；跨会议搜索、纪要、待办；拆分面试助手；`status.json`；把 transcript.md 表头改成 YAML/JSON；自动测试里使用真实音频；隐私文案修正（TODO，下一轮）。

## 已定决策
| 编号 | 决策 |
|---|---|
| 0 | spike 比较三条路：常驻麦克风流、`abort()` 代替 `stop()/close()`、子进程；取最小可行者。常驻流必须验收：空转回调不拷贝不入队、1 小时空转 CPU、指示灯、蓝牙切换 |
| 1A | 「未完成」由文件推导（有 meeting.wav 无 transcript.md）+ 控制器内存里「正在转写」的会议 ID 集合；transcript.md 临时文件加改名原子写；无 status.json |
| 1B | 分数写在新增表头行 `- 声纹匹配：`；`- 说话人：` 行格式不变；灰区写「疑似张三」 |
| 1C | diarize.py、live.py 各拆出纯函数 `models_present(cache_dir) -> list[缺失]`；ensure_* 与 doctor 都用它；doctor 零网络零写入 |
| 1D | doctor 只检查 [llm] 配置齐全，不发网络请求；另有「测试连接」按钮，只返回通/不通 + 固定中文原因，不回传服务器原文 |
| 1E | 自检是 GET，响应不含绝对路径和设备序列号；「测试连接」是 POST，走 Origin 守卫 |
| 1F | 阈值放 config.toml 的 [voiceprint] `high`/`low`，代码有默认值；校验 `0 < low < high <= 1`，非法则报中文错误并退回默认值；`--voice-threshold` 只覆盖 high |
| 2A | 「疑似」标签集中在 voiceprint.py：`SUSPECT_PREFIX`、`suspect_label`、`parse_label`；发言归纳按名字聚合 |
| 2B | 表头解析收敛到一个函数；先核对现状，没有就在 meeting_store.py 抽出 `parse_header` |
| 2C | 补转写只处理没有 transcript.md 的会议；已有的不碰；用户点「重新转写」时先改名为 `transcript.md.bak` |
| 3A | 评估脚本的算法测试用手造分数集；真实数据只用于人工运行，不进自动测试和仓库 |
| 4A | doctor 的麦克风枚举放线程里并加超时，超时报「设备枚举无响应」；启动扫描只做 stat |
| TODO | 评估数据（≥2 场真实会议、每场 ≥3 人、≥1 人已登记、手工标注）不挡 #0–#3；#4 的最终运行和 #5 的阈值校准等数据到位 |
