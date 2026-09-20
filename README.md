# 实时字幕 → 记事本（live_captions_scribe）

把**电脑正在播放的语音**实时转成文字，并自动写进一个记事本窗口。

做法不是自己去采集音频做识别，而是复用 Windows 11 自带的「实时辅助字幕」
（Live Captions）：它已经把「系统音频环回采集 + 语音识别 + 断句」都做完了，
本程序只负责用 **UI Automation** 把字幕文本框里的内容读出来，再落到记事本和 txt 文件。

**全程不抢键盘鼠标焦点**，程序跑起来之后你可以照常干别的事。

---

## 1. 快速开始

```bash
# 首次准备（也可以用 run.bat 自动完成）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 直接跑（会自己启动 Live Captions、打开记事本）
.venv\Scripts\python.exe live_captions_scribe.py
```

然后**播放任何有语音的音频**（视频、会议、播客……），记事本里就会一句句出现文字。
按 `Ctrl+C` 退出。

想不找视频也能验证链路通不通，用自检脚本放一段语音合成：

```bash
.venv\Scripts\python.exe selftest.py --list        # 看看本机有哪些语音
.venv\Scripts\python.exe selftest.py --voice Hortense
```

> ⚠️ 朗读的语言必须和实时辅助字幕的**字幕语言**一致，否则识别不出来。
> 字幕语言在「设置 → 辅助功能 → 实时辅助字幕」里看。

---

## 2. 命令行参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--interval` | `0.3` | 轮询字幕的间隔（秒）。调小更灵敏，调大更省 CPU |
| `--settle-ms` | `1200` | 一行字幕稳定多久才认定为定稿 |
| `--idle-flush` | `6.0` | 字幕多久没变化就把残句也收下（语音停顿时兜底） |
| `--out-dir` | 脚本目录 | txt 输出目录 |
| `--no-notepad` | 关 | 只写 txt，不打开记事本 |
| `--no-hide` | 关 | 不隐藏 Live Captions 窗口 |
| `--restart` | 关 | 先关掉已在运行的 Live Captions 再重开 |
| `--force-tab` | 关 | 允许程序自动切回转录标签页（默认绝不替你切） |
| `--keep-notepad-focus` | 关 | 打开记事本后不把前台窗口还回去 |
| `--timestamp` | 关 | 每句前面加时间戳 |
| `--duration` | `0` | 运行 N 秒后自动退出，0 = 一直跑 |
| `--debug` | 关 | 打印原始字幕文本，排查用 |

---

## 3. 工作原理

```
LiveCaptions.exe（系统组件：环回采集 + ASR + 断句）
        │
        │  UI Automation
        ▼
AutomationId = "CaptionsTextBlock"  ← 转录文本就藏在它的 Name 属性里
        │
        ▼
   Committer（挑出已定稿的句子）
        │
        ├─► FileSink     追加写 live_captions_<时间戳>.txt
        └─► NotepadSink  无焦点写进记事本
```

### 3.1 实测到的 Live Captions 行为（Windows 11 build 26200）

这些是实际跑出来观察到的，不是猜的，直接决定了判定逻辑：

1. `CaptionsTextBlock` **只在有音频、字幕真正开始工作时才存在**。没有声音时读不到它是正常的，
   程序会自动重试，不需要重启。
2. 文本块由若干「字幕单元」组成，用换行分隔。**最后一个是正在识别的**，会被不断改写；
   前面的已经定稿。
3. 句末标点是识别完成后才补上的：`...en temps réel` → 之后才出现 `.`。
4. 文本块只保留最近的一小段，更早的单元会**滚出去**，所以必须及时读取。
5. 已定稿的单元仍可能被小幅修订（实测出现过 `123` 后来变成 `12345`）。
6. 窗口被最小化 + 从任务栏隐藏后，**UIA 依然能读到内容**（已实测 60 秒以上）。

### 3.2 定稿判定（`Committer`）

规则：

1. **非最后一个单元 = 定稿**，整段收下 —— 即使没有句末标点也收（这点很关键，
   早期版本就是因为"必须有标点"而丢掉了整句）。
2. **最后一个单元**里，只有已经带句末标点的完整句子算定稿，正在识别的残句丢掉。
3. 整块文本超过 `--idle-flush` 秒没变化 → 认为语音停了，最后一段也整段收下。
4. 单元要先稳定 `--settle-ms` 毫秒才输出，避开识别中途的小幅修订。

再加上两道防重复保险：**已输出尾部重叠裁剪**（应对字幕滚动、同一单元被重复提交）
和**句子级去重**。

这套逻辑有一份回归测试，用的是实测记录下来的真实字幕序列：

```bash
.venv\Scripts\python.exe test_committer.py
```

### 3.3 写记事本：为什么不能用模拟键盘

模拟键盘（`SendInput`）必须先**激活记事本窗口**，会打断你正在做的事。
本程序全程只投递 Win32 消息、只读写 UIA 属性，**不改变前台窗口，也不移动鼠标**。

三级降级，启动时自动探测并打印实际用的是哪一级：

| 级别 | 手段 | 抢焦点 |
|---|---|---|
| `wm_settext` | 向编辑区子窗口 `RichEditD2DPT` 投递 `WM_SETTEXT` + `EM_SCROLLCARET`（自动滚到最新） | 否 |
| `valuepattern` | UIA `ValuePattern.SetValue` 写文档元素 | 否 |
| `file_only` | 都不可用 → 只写 txt | 否 |

### 3.4 记事本多标签的安全守卫（重要）

Windows 11 的记事本是**单实例多标签**应用，`notepad.exe 文件` 通常只是往已有窗口里
**加一个标签页**，而编辑区控件是所有标签**共享**的。也就是说，盲目写入有可能覆盖你
正在看的另一个文档。

所以程序每次写入前都会确认「窗口标题里含我们的文件名」（标题会跟随当前标签页变化）。
不是我们的标签就**跳过本次写入**，并在控制台提示一次：

> 记事本当前显示的不是转录标签页，已暂停写入以免覆盖你的其他文档。
> （.txt 文件仍在完整记录；切回转录标签页即会自动补齐。）

默认**绝不替你切标签**。如果你希望程序自动切回去，加 `--force-tab`。
切换用的是 UIA `SelectionItemPattern.Select()`，实测同样不抢焦点。

---

## 4. 故障排查

| 现象 | 处理 |
|---|---|
| 提示「等不到实时辅助字幕窗口」 | 手动按 `Win+Ctrl+L` 试一次；去「设置 → 时间和语言 → 语言和区域 → 语言选项」确认已安装**语音**语言包；确认首启的隐私提示已同意 |
| 一直「已连接，等待语音……」 | 确认电脑真的有声音在播（且没静音）；确认**字幕语言**和音频语言一致 |
| 识别出来全是乱码/外语 | 字幕语言设错了。改「设置 → 辅助功能 → 实时辅助字幕 → 语言」 |
| 记事本里没内容，但 txt 有 | 说明命中了 3.4 的守卫。切回转录标签页，或加 `--force-tab` |
| 想确认元素定位是否还有效 | `python uia_probe.py livecaptions --launch` 打印字幕窗口的 UIA 树 |
| 想确认记事本写入方式 | `python uia_probe.py notepad --test-write "测试"` |

---

## 5. 已知限制

- `CaptionsTextBlock` 是 **Live Captions 的内部实现细节，不是公开契约**，未来 Windows
  版本可能改掉。程序已经做了对冲（找不到就退化为"窗口里 Name 最长的文本控件"），
  真出问题时用 `uia_probe.py` 一看就知道。
- 识别准确率、断句、延迟都由 Live Captions 决定，本程序改不了（实测延迟约 1 秒级）。
- 已定稿的句子仍可能被 Live Captions 事后小幅修订，本程序不做回溯改写。
- 写入记事本会让该标签页变成「已修改」状态（内容其实和磁盘文件一致），
  关窗口时如果提示保存，直接保存即可，不会出问题。
- 退出时程序会把 Live Captions 窗口恢复显示，但**不会关掉它**，你可以继续用。

---

## 6. 技术路线对比：为什么选 Live Captions + UIA

你的需求里还问到另外两条路：`Microsoft.Windows.AI.*`（WinAppSDK 的 AI Speech）
和 `Windows.Media.SpeechRecognition`。结论是**本方案更可靠，而且是当前唯一可行的**。

| 维度 | **A. Live Captions + UIA**（本方案） | B. `Microsoft.Windows.AI.Speech` | C. `Windows.Media.SpeechRecognition` |
|---|---|---|---|
| **Python 可达性** | ✅ 纯 Python（`uiautomation` + `comtypes`） | ❌ **PyPI 上没有投影**（`winrt-Microsoft.Windows.AI` 实测 404），必须自己写 C#/C++ 桥接 | ✅ 有 `winrt-Windows.Media.SpeechRecognition` |
| **打包要求** | 无，普通脚本即可 | ❌ 必须打包成 **MSIX** 且声明 `systemAIModels` capability，`MaxVersionTested` 要 ≥ `10.0.26226` | 无 |
| **成熟度** | 系统正式功能（22H2+） | ⚠️ `AudioConfiguration` 等类标注 **`[Experimental]`**，文档标注 "Windows App SDK **2.0 Experimental**" | 正式，但模型较老 |
| **硬件要求** | 任意 Windows 11 | NPU（Copilot+）或 CPU；**GPU 不支持**；CPU 设备上模型要按需下载（`EnsureReadyAsync` 走 Windows Update） | 任意 |
| **能否抓系统播放的声音** | ✅ **原生支持**（这正是 Live Captions 的用途） | ⚠️ 有 `FromAudioDevice` / `FromFile` / `FromStream`，但**没有环回采集的验证** | ❌ **只吃麦克风**，且不接受外部音频流 |
| **结论** | **当前需求下唯一现实可行且最可靠** | 上限最高，但现在够不着 | 只适合"麦克风听写"，不适合本需求 |

**为什么 A 更可靠：**

- **B 卡在三道门上**：Python 投影缺失（这是硬伤，纯 Python 无解）+ 强制 MSIX 打包
  （一个本地小工具没法满足）+ API 仍是 Experimental（随时可能变）。它的方向是对的，
  但等 Python 投影出现、API GA、且支持非 MSIX 调用之后再考虑。
- **C 根本抓不到系统播放的音频**。`SpeechRecognizer` 只从麦克风取音，
  而"电脑正在播放的声音"必须靠 WASAPI 环回采集，这条路它走不通。
- **A 把最难的活交给了系统现成组件**：环回采集、ASR、断句、标点全都不用自己写，
  Python 侧只需要做一层 UI 读取，依赖也只有两个纯 Python 包，不需要任何打包。

### 还有一条更值得考虑的"第四条路"

如果以后你对**准确率 / 可定制性**的要求高于"实现简单"，正解是：

> **WASAPI 环回采集（`pyaudiowpatch` 或 `soundcard`）+ 本地 Whisper（`faster-whisper` / `whisper.cpp`）**

优点：纯 Python 可达；**完全不依赖 Live Captions 的内部 UI 结构**（因此不受
`CaptionsTextBlock` 变更影响）；可以自选模型大小、加热词表、做说话人分离。

代价：要自己写 VAD 和断句；会吃 CPU/GPU（大模型建议有独显）。

**建议**：现在用 A（零成本、立刻能用）；哪天 A 因为 Windows 改版失效了，
或者你想要更高准确率，就换这条路。

---

## 7. 文件清单

| 文件 | 作用 |
|---|---|
| `live_captions_scribe.py` | 主程序 |
| `winutils.py` | Win32 封装（纯 ctypes，不依赖 pywin32） |
| `uia_probe.py` | 诊断探针：打印 Live Captions / 记事本的 UIA 结构，测写入方式 |
| `selftest.py` | 自检：用系统语音合成放一段话，不用找视频就能验证链路 |
| `test_committer.py` | 定稿判定的回归测试（用实测字幕序列） |
| `requirements.txt` | 依赖 |
| `run.bat` | 双击即用的启动器（会自动建虚拟环境装依赖） |
| `live_captions_<时间戳>.txt` | 每次运行的转录结果 |
