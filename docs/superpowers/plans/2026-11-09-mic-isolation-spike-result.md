# 录音隔离 spike 结果（Task 9 / #2a）

日期：2026-11-09
对应计划：`docs/superpowers/plans/2026-11-09-reliability-first.md` Task 9（第 230–242 行）
探针：`spike/mic_isolation_probe.py`
状态：**未测**。本机没有可用的麦克风输入设备，三种模式都没有产生实测数值。

## 1. 环境与方法

| 项 | 值 |
|---|---|
| 机器 | Mac mini（Apple M4） |
| 系统 | macOS 26.2（Build 25C56） |
| Python | 3.13.12（`.venv`；计划写的是 3.14，实际以 `.venv` 为准） |
| sounddevice | 0.5.6 |
| 音频设备 | 只有 `Mac mini扬声器`（0 个输入通道），没有输入设备 |
| 轮数 | 计划要求 20 轮；本次三种模式都在设备检查阶段退出，未进入轮次 |

探针参数与 `record.py` 保持一致：16000 Hz、单声道、int16、块 1600 帧；
开流超时默认 15 秒，关闭超时 5 秒，卡顿阈值 5 秒，整轮实验总时长上限 300 秒。
每次可能卡死的调用都放在守护线程里等待，超时就放弃并记一次“超时放弃”，
所以 PortAudio 卡死也不会让探针本身挂住。

三种模式：

- `persistent`：只开一次 `sd.InputStream`，循环 N 次“开始写盘/停止写盘”。
  不写盘时回调只判断一次 `threading.Event` 就返回，不拷贝、不入队；写盘时经队列交给写线程。
- `abort`：每轮新开流，录一小段后用 `stream.abort()` 再 `stream.close()`，
  开流、abort、close 分别计时。
- `subprocess`：每轮起一个子进程开流录 2 秒写 wav，父进程只收 wav 和旁的 JSON
  （JSON 里记子进程内部的开流/关闭耗时），并记录子进程退出耗时。

自检：`--help` 正常；另外用假 `sounddevice`（仅内存回调，不产生真实音频）
跑通了三种模式的控制流，并用“开流永久卡住”的假实现确认 1 秒超时保护生效。
这些假设备数据只用于验证代码路径，**不是实测数据，未写入下表**。

## 2. 实测结果

| 方案 | 20 轮最大开流耗时 | 是否卡顿（>5 秒） | 切设备后表现 | 空转 CPU（仅常驻流） | 改动面（按现有代码估计，未实测） |
|---|---|---|---|---|---|
| persistent 常驻流 | 未测 | 未测 | 未测 | 未测 | 中：录音生命周期从“每场会议开/关流”改为进程级常驻，回调空转直接返回；需 `--no-isolate` 回退 |
| abort 关闭 | 未测 | 未测 | 未测 | 不适用 | 小：`record.close_stream` 里先 `abort()` 再 `close()`，保留现有超时线程 |
| subprocess 子进程 | 未测 | 未测 | 未测 | 不适用 | 大：新增子进程入口、wav 交接/退出码处理、异常退出与残留进程清理 |

**未测原因（实测证据）：**

```
$ system_profiler SPAudioDataType
    Devices:
        Mac mini扬声器:
          Default Output Device: Yes
          Output Channels: 2
          Transport: Built-in

$ .venv/bin/python -c "import sounddevice as sd; [print(i, repr(d['name']), 'in=', d['max_input_channels'], 'out=', d['max_output_channels']) for i, d in enumerate(sd.query_devices())]"
0 'Mac mini扬声器' in= 0 out= 2 rate= 48000.0

$ .venv/bin/python -c "import sounddevice as sd; print(sd.default.device)"
[-1, 0]
```

直接开流也失败：

```
$ .venv/bin/python -c "import sounddevice as sd; sd.InputStream(samplerate=16000, channels=1, dtype='int16', blocksize=1600)"
sounddevice.PortAudioError: Error querying device -1
```

本机是 Mac mini，硬件本身没有麦克风输入通道（不是“权限未授予但设备存在”）。
因此无法测开流耗时、卡顿、切设备、空转 CPU、指示灯或蓝牙恢复；
没有用假数据填充上表。

三种模式的实际输出相同（都在设备检查处退出）：

```
$ .venv/bin/python spike/mic_isolation_probe.py --mode persistent --cycles 20
错误：没有可用的麦克风输入设备（本机可能没有麦克风，或 macOS 麦克风权限未授予）
结论：未测（没有可用的麦克风输入设备，不能生成实测数据）
退出码=2
$ .venv/bin/python spike/mic_isolation_probe.py --mode abort --cycles 20
错误：没有可用的麦克风输入设备（本机可能没有麦克风，或 macOS 麦克风权限未授予）
结论：未测（没有可用的麦克风输入设备，不能生成实测数据）
退出码=2
$ .venv/bin/python spike/mic_isolation_probe.py --mode subprocess --cycles 20
错误：没有可用的麦克风输入设备（本机可能没有麦克风，或 macOS 麦克风权限未授予）
结论：未测（没有可用的麦克风输入设备，不能生成实测数据）
退出码=2
```

探针自检：

```
$ .venv/bin/python spike/mic_isolation_probe.py --help
usage: mic_isolation_probe.py [-h] [--mode {persistent,abort,subprocess}]
                              [--cycles CYCLES] ...
（退出码 0，参数齐全）

$ .venv/bin/python spike/mic_isolation_probe.py --list-devices
没有可用的麦克风输入设备。
（退出码 2）
```

## 3. 复现卡死（Step 3）

**未测。** 没有输入设备，无法“一边录音一边切换设备”。

在有麦克风的机器上补测时，按计划记录“开流是否超过 15 秒”：

1. `--mode persistent --cycles 20` 运行中，打开“系统设置 > 声音 > 输入”，
   在“Mac 麦克风 / 蓝牙耳机 / USB 麦克风”之间切换；蓝牙耳机做一次断开再连接。
2. 对 `abort` 重复同样操作（每轮都会重新开流）。
3. 对 `subprocess` 重复；同时观察是否留下未退出的子进程。
4. 每轮看探针打印的“开流 x.xxx s”和汇总里的“超过 5 秒的卡顿 / 超时放弃”。

复现与未复现都要写进结论；**未复现不等于已修复**。

## 4. 常驻流专项验收（Step 4）

**未测。** 仅当 persistent 入选才需要做，本轮没有可用的常驻流。

探针已支持补测入口：

- 空转 CPU：`--mode persistent --cycles 1 --cycle-seconds 0 --idle-seconds 3600 --total-timeout 4000`，
  同时按计划用 `ps -o %cpu -p <pid>` 定时采样，取平均与最大。
  探针默认总时长上限 300 秒，空转 1 小时必须显式加 `--total-timeout 4000`，否则会被提前截断。
- 菜单栏麦克风指示灯：空转 1 小时期间人工观察是否一直亮。
- 蓝牙切换：录音中切到蓝牙耳机，确认流能否自动恢复（看回调是否继续、wav 是否继续增长）。

## 5. 结论

**唯一结论：本机没有可用的麦克风输入设备，persistent、abort、subprocess 三种方案均未获得实测数据，本轮不做选型；#2b 不得依据本文件开工，必须在有麦克风的机器上重跑本探针、把上表填满后再定。**

没有数据就不选“看起来最省事”的常驻流，也不把“未复现”当成“已修复”。

### 补测后的决策规则（待验证，不是本轮结论）

1. persistent 在 20 轮里无 >5 秒卡顿、空转 CPU 可接受、指示灯可接受、
   切蓝牙后能恢复 → 选 persistent，并用 `--no-isolate` 保留回退。
2. persistent 上述任一项不达标，而 subprocess 20 轮稳定、无残留进程 → 选 subprocess。
3. abort 单独不解决“开流卡死”，只能作为关闭路径的改进，不能单独作为隔离方案；
   若 persistent/subprocess 都不可行，再回到 #2b 重新设计。
4. 任一方案出现开流 >15 秒，按“卡死复现”记录，不进入选型。

### `--no-isolate` 回退开关接法（草案，未实现、未评审）

只有 persistent 入选才需要：

- `record.py` 加 `--no-isolate`（默认关闭）。为真时 `deps["stream_factory"]`
  仍用现在的 `_real_stream_factory`（每场会议开/关流）；为假时换成进程级常驻流工厂。
- `meeting_controller` 的录音入口加 `isolate: bool = True` 透传；
  Web 端从 `config.toml` 读取开关并传入，默认开启。
- 回退只影响“流怎么开/关”，不改 wav 写盘、补转写、声纹等逻辑。

## 6. 遗留风险

- 探针没有在真实麦克风上跑过；控制流只用假设备自检，不能替代实测。
- 权限被拒与“设备不存在”在 sounddevice 里可能都表现为枚举不到设备；
  本机是硬件没有输入通道，但换机器后要区分这两种情况。
- subprocess 模式的子进程残留、wav 半成品、退出码处理都未在真实设备上验证。
- persistent 的空转 CPU、指示灯、蓝牙恢复全部未测。
- 超时保护靠守护线程等待：如果 PortAudio 卡死时长时间不释放 GIL，看门狗线程本身也可能
  拿不到 GIL，超时就不生效。这一点只能在有输入设备的机器上确认，不能靠假实现证明。
- 因此 #2b 目前没有选型依据。