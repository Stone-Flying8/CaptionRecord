# -*- coding: utf-8 -*-
"""live_captions_scribe —— 把电脑正在播放的语音实时转成文字，并写进记事本。

工作原理
--------
1. 启动 Windows 11 自带的实时辅助字幕（LiveCaptions.exe）。
2. 用 UI Automation 读取字幕窗口里 AutomationId 为 `CaptionsTextBlock` 的元素，
   该元素的 Name 属性就是当前转录出来的文本（这是 Live Captions 自己完成
   "系统音频环回采集 + 语音识别" 之后的产物）。
3. 从文本块里挑出**已经定稿的句子**（正在识别中的最后一句会被丢弃）。
4. 把定稿句子追加到 .txt 文件，同时通过**不抢焦点**的方式写进自动打开的记事本。

为什么写记事本要用 Win32 消息 / UIA，而不是模拟键盘：
   模拟键盘（SendInput）必须先激活记事本窗口，会打断你正在做的事。
   本程序全程只投递消息、只读 UIA 属性，不改变前台窗口，也不移动鼠标。

用法见 README.md。
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import time
from typing import Optional

import comtypes
import uiautomation as auto

import winutils as wu

# ---------------------------------------------------------------- 常量

LIVE_CAPTIONS_CLASS = "LiveCaptionsDesktopWindow"
LIVE_CAPTIONS_EXE = r"C:\Windows\System32\LiveCaptions.exe"
CAPTION_AUTOMATION_ID = "CaptionsTextBlock"

# 句末标点（含中英文）
SENTENCE_ENDINGS = ".!?。！？…⁇⁈⁉"
# 跟在句末标点后面、应当一起算进句子的收尾字符
CLOSERS = "\"'”’）)】」』》>"

WHITESPACE_RE = re.compile(r"\s+")

NOTEPAD_TITLE_SUFFIX = " - Notepad"


# ---------------------------------------------------------------- 小工具

_DEBUG = False
_ORIGINAL_CONSOLE_CP: Optional[int] = None


def setup_console() -> None:
    """让控制台能正确输出中文和 ✓ 之类的符号。

    坑：Windows 控制台默认代码页常常是 GBK(936) 或 cp437，此时 print 一个
    代码页里没有的字符（比如 ✓）会直接抛 UnicodeEncodeError 把程序打断。
    打包成 exe 后尤其容易踩到。这里做两件事：
      1. 把控制台输出代码页切到 UTF-8；
      2. 把 stdout/stderr 的编码也设成 UTF-8，并加 errors="replace" 兜底，
         保证任何字符都不会再让程序崩掉。
    """
    global _ORIGINAL_CONSOLE_CP
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _ORIGINAL_CONSOLE_CP = int(kernel32.GetConsoleOutputCP())
        if _ORIGINAL_CONSOLE_CP != 65001:
            kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def restore_console() -> None:
    """退出时把控制台代码页还原，免得影响你后面在这个窗口里的其他操作。"""
    if _ORIGINAL_CONSOLE_CP is None:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleOutputCP(_ORIGINAL_CONSOLE_CP)
    except Exception:
        pass


def log(msg: str, *, debug: bool = False) -> None:
    """统一的控制台输出。debug=True 的消息只在 --debug 下打印。"""
    if debug and not _DEBUG:
        return
    try:
        print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)
    except Exception:
        # 控制台实在写不出来也不能让程序挂掉
        pass


def normalize(text: str) -> str:
    """把任意空白压成单个空格。"""
    return WHITESPACE_RE.sub(" ", text).strip()


def app_dir() -> str:
    """程序所在目录。

    用 PyInstaller 打包成 exe 之后，`__file__` 指向的是临时解包目录，
    配置文件和转录结果应该放在 exe 旁边，所以这里要区分对待。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def split_sentences(text: str) -> tuple[list[str], str]:
    """把文本切成句子。

    返回 (完整句子列表, 结尾的残句)。
    例如 "A. B. C" -> (["A.", "B."], "C")
    """
    sentences: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        buf.append(ch)
        if ch in SENTENCE_ENDINGS:
            # 把紧跟其后的收尾引号/括号一起并入本句
            j = i + 1
            while j < n and text[j] in CLOSERS:
                buf.append(text[j])
                j += 1
            sentences.append("".join(buf).strip())
            buf = []
            i = j
            continue
        i += 1
    tail = "".join(buf).strip()
    return [s for s in sentences if s], tail


def disable_uia_logging(*, keep_console: bool = False) -> None:
    """关掉 uiautomation 自带的日志噪音。

    默认情况下它会在当前目录生成 @AutomationLog.txt，还会往 stdout 打时间戳。
    这里把日志文件路径设为空（不生成文件）；keep_console=True 时保留控制台输出。
    """
    try:
        auto.Logger.SetLogFile("")
    except Exception:
        pass
    if not keep_console:
        try:
            auto.Logger.WriteLine = staticmethod(lambda *a, **k: None)
        except Exception:
            pass


# ---------------------------------------------------------------- Live Captions

class LiveCaptions:
    """负责找到/启动字幕窗口，并把它转录出的文本读出来。"""

    def __init__(self, *, restart: bool = False, hide: bool = True, debug: bool = False):
        self.restart = restart
        self.hide = hide
        self.debug = debug
        self.hwnd: Optional[int] = None
        self._window = None
        self._caption_elem = None
        self._hidden = False

    # -------- 生命周期 --------
    def connect(self, timeout: float = 25.0) -> bool:
        existing = wu.find_top_window(class_name=LIVE_CAPTIONS_CLASS)
        if existing and self.restart:
            log("按 --restart 要求，先关掉已有的 Live Captions …")
            self._kill_all()
            existing = None

        if existing:
            log(f"已连接正在运行的 Live Captions（hwnd={existing}）")
        else:
            if not os.path.exists(LIVE_CAPTIONS_EXE):
                log(f"[错误] 找不到 {LIVE_CAPTIONS_EXE}")
                return False
            log("正在启动实时辅助字幕 …")
            subprocess.Popen([LIVE_CAPTIONS_EXE])
            deadline = time.time() + timeout
            while time.time() < deadline:
                existing = wu.find_top_window(class_name=LIVE_CAPTIONS_CLASS)
                if existing:
                    break
                time.sleep(0.25)
            if not existing:
                self._print_startup_help()
                return False
            log(f"Live Captions 已就绪（hwnd={existing}）")

        self.hwnd = existing
        self._window = auto.ControlFromHandle(existing)
        if self.hide:
            self.hide_window()
        return True

    def _kill_all(self) -> None:
        for w in wu.enum_top_windows():
            if w["class"] == LIVE_CAPTIONS_CLASS:
                pid = w["pid"]
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)

    @staticmethod
    def _print_startup_help() -> None:
        log("[错误] 等不到实时辅助字幕窗口。请依次检查：")
        log("       1) 手动按 Win+Ctrl+L 能否打开「实时辅助字幕」；")
        log("       2) 设置 → 时间和语言 → 语言和区域 → 对应语言 → 语言选项，")
        log("          确认已安装「语音」语言包（字幕需要它）；")
        log("       3) 首次使用时窗口里的隐私提示是否已同意。")

    def hide_window(self) -> None:
        """最小化 + 从任务栏隐藏。最小化后 UIA 依然能读到内容。"""
        if not self.hwnd:
            return
        wu.hide_from_taskbar_and_minimize(self.hwnd)
        self._hidden = True
        log("字幕窗口已最小化并从任务栏隐藏（内容仍在被读取）")

    def show_window(self) -> None:
        if self.hwnd and self._hidden and wu.is_window(self.hwnd):
            wu.restore_window(self.hwnd)
            self._hidden = False

    def alive(self) -> bool:
        return bool(self.hwnd) and wu.is_window(self.hwnd)

    # -------- 读取文本 --------
    def _find_caption_element(self):
        """找转录文本框。

        首选 AutomationId=CaptionsTextBlock；找不到时退化为"窗口里 Name 最长的
        文本控件"——用于抵御不同 Windows 版本改动内部 AutomationId 的风险。
        注意：该元素**只在字幕真正开始工作时才存在**，没有音频时读不到是正常的。
        """
        if self._window is None:
            return None
        elem = self._window.Control(searchDepth=12, AutomationId=CAPTION_AUTOMATION_ID)
        try:
            if elem.Exists(0):
                return elem
        except Exception:
            pass
        # 兜底：全树找 Name 最长的 TextControl
        best = None
        best_len = 0
        try:
            for ctrl, _depth in auto.WalkTree(
                self._window,
                getChildren=lambda c: c.GetChildren(),
                includeTop=True,
                maxDepth=12,
            ):
                try:
                    if ctrl.ControlTypeName != "TextControl":
                        continue
                    name = ctrl.Name or ""
                except Exception:
                    continue
                if len(name) > best_len:
                    best, best_len = ctrl, len(name)
        except Exception:
            return None
        if best is not None and best_len >= 8:
            log(f"未找到 {CAPTION_AUTOMATION_ID}，已退化为最长文本控件（{best_len} 字）", debug=True)
            return best
        return None

    def read(self) -> Optional[str]:
        """读一次字幕文本。返回 None 表示当前没有字幕（没有音频或还没开始）。"""
        if not self.alive():
            return None
        if self._caption_elem is None:
            self._caption_elem = self._find_caption_element()
            if self._caption_elem is None:
                return None
        try:
            return self._caption_elem.Name or ""
        except Exception:
            # 元素失效（窗口重建、字幕块被回收）——清缓存，下一轮重找
            self._caption_elem = None
            return None

    def reconnect_if_needed(self) -> bool:
        """窗口没了就重连。返回 True 表示已恢复。"""
        if self.alive():
            return True
        log("字幕窗口不见了，尝试重新连接 …")
        self.hwnd = None
        self._window = None
        self._caption_elem = None
        self._hidden = False
        return self.connect(timeout=20.0)


# ---------------------------------------------------------------- 定稿判定

class Committer:
    """从不断变化的字幕文本块里挑出"已经定稿的句子"。

    实测到的 Live Captions 行为（Windows 11 build 26200）：
      · 文本块由若干"字幕单元"组成，用换行分隔；最后一个是正在识别的，会被不断改写；
      · 每个单元内部会持续追加，句末标点是识别完成后才补上的；
      · 文本块只保留最近的一小段，更早的单元会滚出去；
      · 已定稿的单元仍可能被小幅修订（例如 "123" 后来变成 "12345"）。

    判定规则（三条）：
      1. 非最后一个单元 = 定稿，整段收下（**即使没有句末标点**，这点很关键）；
      2. 最后一个单元里，只有已经带句末标点的完整句子算定稿，正在识别的残句丢掉；
      3. 整块文本超过 --idle-flush 秒没变化，说明语音停了，最后一段也整段收下。
    另外还有三道保险：
      · 单元先稳定 --settle-ms 毫秒再输出，避开识别中途的小幅修订；
      · 但稳定期没到就被顶出字幕块的单元会**立刻补输出**——否则短句会随滚动永久丢失；
      · 待定队列非空时，暂缓输出最后一行里的小句，保证句子顺序不会乱。
    再加上"已输出尾部重叠裁剪 + 句子去重"防重复。
    """

    def __init__(self, idle_flush: float = 6.0, settle_ms: float = 1200.0,
                 dedup_window: int = 300, unit_memory: int = 400):
        self.idle_flush = idle_flush
        self.settle = settle_ms / 1000.0
        self._dedup_window = dedup_window
        self._unit_memory = unit_memory

        self._emitted_tail = ""          # 最近输出过的文本（用于重叠裁剪）
        self._recent: list[str] = []     # 最近输出的句子（规范化小写，用于去重）
        self._emitted_units: list[str] = []   # 已按"整单元"处理过的单元
        # 已认定为定稿、但还没到稳定期（或还没轮到输出）的单元：文本 -> 首次出现时间
        self._pending: "collections.OrderedDict[str, float]" = collections.OrderedDict()

        self._last_raw: str | None = None
        self._last_change = time.time()

    # -------- 主入口 --------
    def feed(self, raw: str) -> list[str]:
        """喂入一次原始字幕文本，返回本次新定稿的句子。"""
        raw = raw or ""
        now = time.time()
        if raw != self._last_raw:
            self._last_raw = raw
            self._last_change = now

        units = [normalize(u) for u in raw.split("\n")]
        units = [u for u in units if u]
        if not units:
            # 字幕被清空（比如识别块被回收）：把待定的都补出去，别丢
            return self._drain_pending(now, gone_all=True)

        out: list[str] = []
        include_live = (now - self._last_change) >= self.idle_flush

        # --- 1) 把新出现的"已定稿单元"登记进待定队列（保持先后顺序）---
        settled = units if include_live else units[:-1]
        for u in settled:
            if u in self._pending or u in self._emitted_units:
                continue
            self._pending[u] = now

        # --- 2) 该输出的输出 ---
        #   · 稳定期已到 -> 输出
        #   · 还没到稳定期就被顶出字幕块了 -> 也输出（否则这句话永远拿不回来了）
        #   · include_live（语音停了）-> 全部输出
        out.extend(self._drain_pending(now, present=set(units), force=include_live))

        # --- 3) 最后一行里已经成句的部分 ---
        # 待定队列非空时先不输出，避免出现"后面的句子先写进去"的乱序
        if not include_live and not self._pending:
            sentences, _tail = split_sentences(units[-1])
            emitted = []
            for s in sentences:
                if self._is_duplicate(s):
                    continue
                self._remember(s)
                emitted.append(s)
            if emitted:
                out.extend(emitted)
                self._emitted_tail = (self._emitted_tail + " " + " ".join(emitted))[-800:]

        return out

    def _drain_pending(self, now: float, present: set[str] | None = None,
                       force: bool = False, gone_all: bool = False) -> list[str]:
        """按顺序输出满足条件的待定单元。"""
        out: list[str] = []
        for u in list(self._pending.keys()):
            age = now - self._pending[u]
            gone = gone_all or (present is not None and u not in present)
            if not (force or gone or age >= self.settle):
                continue
            del self._pending[u]
            self._emitted_units.append(u)
            if len(self._emitted_units) > self._unit_memory:
                self._emitted_units.pop(0)
            out.extend(self._emit(u, force=True))
        return out

        # --- 2) 最后一个单元里已经成句的部分 ---
        if not include_live:
            sentences, _tail = split_sentences(units[-1])
            for s in sentences:
                if self._is_duplicate(s):
                    continue
                self._remember(s)
                out.append(s)
            if sentences:
                self._emitted_tail = (self._emitted_tail + " " + " ".join(sentences))[-800:]

        return out

    def flush(self, raw: str) -> list[str]:
        """程序退出前调用：把待定单元和最后一句残句全部吐出来。"""
        out: list[str] = []
        out.extend(self._drain_pending(time.time(), force=True))
        units = [normalize(u) for u in (raw or "").split("\n")]
        units = [u for u in units if u]
        for u in units:
            if u in self._emitted_units:
                continue
            self._emitted_units.append(u)
            out.extend(self._emit(u, force=True))
        return out

    # -------- 内部工具 --------
    def _emit(self, text: str, *, force: bool) -> list[str]:
        """把一段文本切成句子输出。force=True 时即使没有句末标点也输出。"""
        text = self._trim_overlap(text)
        if not text:
            return []
        sentences, tail = split_sentences(text)
        if tail and force:
            sentences.append(tail)
        out: list[str] = []
        for s in sentences:
            if self._is_duplicate(s):
                continue
            self._remember(s)
            out.append(s)
        if out:
            self._emitted_tail = (self._emitted_tail + " " + " ".join(out))[-800:]
        return out

    def _trim_overlap(self, new_text: str) -> str:
        """裁掉开头与"已输出内容尾部"重复的部分（应对字幕块滚动、单元被重复提交）。

        只认长度 >= 6 的重叠，避免用一两个字符误裁。
        """
        tail = self._emitted_tail
        if not tail:
            return new_text
        for size in range(min(len(new_text), len(tail)), 5, -1):
            if new_text.startswith(tail[-size:]):
                return new_text[size:].strip()
        return new_text

    def _is_duplicate(self, sentence: str) -> bool:
        key = normalize(sentence).lower()
        return any(key == s for s in self._recent)

    def _remember(self, sentence: str) -> None:
        self._recent.append(normalize(sentence).lower())
        if len(self._recent) > self._dedup_window:
            self._recent.pop(0)


# ---------------------------------------------------------------- 输出：文件

class FileSink:
    """把定稿句子追加写入 .txt。追加写，崩溃也不丢已写内容。"""

    def __init__(self, path: str, with_timestamp: bool = False):
        self.path = path
        self.with_timestamp = with_timestamp
        self._fh = open(path, "a", encoding="utf-8", newline="\n")

    def write(self, sentence: str) -> None:
        if self.with_timestamp:
            self._fh.write(f"[{dt.datetime.now():%H:%M:%S}] {sentence}\n")
        else:
            self._fh.write(sentence + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 输出：记事本

class NotepadSink:
    """把完整转录文本写进记事本，全程不抢焦点。

    三级降级：
      wm_settext    向编辑区子窗口（RichEditD2DPT）投递 WM_SETTEXT + EM_SCROLLCARET
      valuepattern  UIA ValuePattern.SetValue（记事本的文档元素支持）
      file_only     都不可用时退化为纯文件模式

    安全守卫（很重要）：
      Win11 记事本是**单实例多标签**，编辑区控件是所有标签共享的。
      如果盲目写入，可能覆盖你正在看的另一个文档。
      所以每次写之前都先确认"窗口标题里含我们的文件名"（标题会跟随当前标签页变化），
      不是我们的标签就跳过本次写入（.txt 文件仍在完整记录），
      等你切回来时会自动补齐。除非显式给出 --force-tab，否则绝不替你切标签。
    """

    def __init__(self, file_path: str, *, force_tab: bool = False,
                 restore_focus: bool = True, debug: bool = False):
        self.file_path = os.path.abspath(file_path)
        self.file_name = os.path.basename(self.file_path)
        self.force_tab = force_tab
        self.restore_focus = restore_focus
        self.debug = debug
        self.mode = "file_only"
        self.hwnd: Optional[int] = None
        self._window = None
        self._doc = None
        self._richedit: Optional[int] = None
        self._last_written = ""
        self._warned = False

    # -------- 打开 --------
    def open(self) -> bool:
        before = {w["hwnd"] for w in wu.enum_top_windows()}
        # 启动记事本本身会把它的窗口带到前台，先记下你原来在用什么窗口，稍后还回去
        prev_foreground = wu.get_foreground_window()
        subprocess.Popen(["notepad.exe", self.file_path])

        deadline = time.time() + 20
        while time.time() < deadline:
            hwnd = self._pick_window(before)
            if hwnd:
                self.hwnd = hwnd
                break
            time.sleep(0.3)
        if not self.hwnd:
            log("[警告] 打不开记事本，改为只写文件")
            return False

        self._window = auto.ControlFromHandle(self.hwnd)
        # 给记事本一点时间把文件读进来
        time.sleep(0.6)
        if self.restore_focus:
            if wu.set_foreground_window(prev_foreground):
                log("已把前台窗口还给你原来的窗口（不打断你的操作）")
        self._resolve_targets()
        log(f"记事本已打开（hwnd={self.hwnd}，写入方式：{self.mode}）")
        if self.mode == "file_only":
            log("[警告] 记事本不支持无焦点写入，本次只写 .txt 文件。")
        return True

    def _pick_window(self, before: set) -> Optional[int]:
        """优先找新出现的记事本窗口；没有就用已有的。"""
        candidates = []
        for w in wu.enum_top_windows():
            if not w["visible"]:
                continue
            if "notepad" not in wu.get_process_name(w["pid"]).lower():
                continue
            candidates.append(w)
        for w in candidates:
            if w["hwnd"] not in before:
                return w["hwnd"]
        return candidates[0]["hwnd"] if candidates else None

    def _resolve_targets(self) -> None:
        """定位编辑区：优先 Win32 子窗口 RichEdit，其次 UIA 文档元素。"""
        self._richedit = wu.find_descendant_by_class(self.hwnd, ("richedit",))
        if self._richedit:
            self.mode = "wm_settext"
            return
        self._doc = self._window.DocumentControl(searchDepth=8)
        try:
            if self._doc.Exists(0):
                vp = self._doc.GetPattern(auto.PatternId.ValuePattern)
                if vp is not None and not vp.IsReadOnly:
                    self.mode = "valuepattern"
                    return
        except Exception:
            pass
        self.mode = "file_only"

    # -------- 写入 --------
    def _tab_is_ours(self) -> bool:
        """窗口标题是否指向我们的文件（标题跟随当前标签页）。"""
        if not self.hwnd or not wu.is_window(self.hwnd):
            return False
        title = wu.get_window_title(self.hwnd)
        return self.file_name in title

    def _select_our_tab(self) -> bool:
        """用 UIA 把我们的标签页选中（无焦点切换）。"""
        try:
            listview = self._window.ListControl(searchDepth=8, AutomationId="TabListView")
            if not listview.Exists(0):
                return False
            for item in listview.GetChildren():
                try:
                    if self.file_name in (item.Name or ""):
                        pattern = item.GetPattern(auto.PatternId.SelectionItemPattern)
                        if pattern is not None:
                            pattern.Select()
                            time.sleep(0.3)
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def write(self, full_text: str) -> bool:
        """写入完整文本（覆盖式）。返回是否成功。"""
        if self.mode == "file_only":
            return False
        if not self.hwnd or not wu.is_window(self.hwnd):
            return False
        if full_text == self._last_written:
            return True

        if not self._tab_is_ours():
            if self.force_tab and self._select_our_tab() and self._tab_is_ours():
                pass
            else:
                if not self._warned:
                    log("记事本当前显示的不是转录标签页，已暂停写入以免覆盖你的其他文档。")
                    log("（.txt 文件仍在完整记录；切回转录标签页即会自动补齐。"
                        "想让程序自动切回，可加 --force-tab）")
                    self._warned = True
                return False
        self._warned = False

        payload = full_text.replace("\n", "\r\n")
        ok = False
        if self.mode == "wm_settext" and self._richedit:
            ok = wu.set_window_text_via_message(self._richedit, payload)
            wu.caret_to_end_and_scroll(self._richedit)
        if not ok and self._doc is not None:
            try:
                vp = self._doc.GetPattern(auto.PatternId.ValuePattern)
                if vp is not None:
                    ok = bool(vp.SetValue(payload))
                    self.mode = "valuepattern"
            except Exception:
                ok = False

        if ok:
            self._last_written = full_text
        else:
            log("[警告] 本次写入记事本失败，将继续重试（文件记录不受影响）", debug=self.debug)
        return ok


# ---------------------------------------------------------------- 主流程

# ---------------------------------------------------------------- 配置文件

SETTINGS_FILE_NAME = "setting.json"

# 配置键 -> (默认值, 中文说明)
SETTINGS_SPEC: dict[str, tuple[object, str]] = {
    "show_live_captions": (
        False,
        "是否让 Windows「实时辅助字幕」窗口显示出来。false = 最小化并从任务栏隐藏，"
        "程序照样能读到字幕内容；true = 保持窗口可见，方便肉眼确认识别是否在工作",
    ),
    "open_notepad": (True, "运行后是否自动打开记事本并把转录结果写进去"),
    "notepad_restore_focus": (
        True,
        "打开记事本后，是否立刻把前台窗口还给你原来在用的窗口。true = 不打断你的操作",
    ),
    "force_switch_notepad_tab": (
        False,
        "记事本停在别的标签页时，是否允许程序自动切回转录标签页。"
        "false = 暂停写入（绝不覆盖你的其他文档），切回来会自动补齐",
    ),
    "restart_live_captions": (False, "启动前是否先关掉已经在运行的实时辅助字幕"),
    "poll_interval_seconds": (0.3, "轮询字幕的间隔（秒）。调小更灵敏，调大更省 CPU"),
    "settle_ms": (1200, "一行字幕稳定多久才认定为定稿（毫秒）"),
    "idle_flush_seconds": (6.0, "字幕多久没变化就把没标点的残句也收下（秒）"),
    "timestamp": (False, "每句前面是否加时间戳"),
    "output_dir": (None, "转录 txt 的输出目录；null = 脚本所在目录"),
    "debug": (False, "是否打印原始字幕文本（排查用）"),
}


def settings_path() -> str:
    return os.path.join(app_dir(), SETTINGS_FILE_NAME)


def write_default_settings(path: str) -> None:
    """生成带说明的 setting.json。"""
    data: dict[str, object] = {
        "_说明": "本文件由 live_captions_scribe.py 自动生成。改完保存后重新运行程序即可生效；"
                "命令行参数会覆盖这里的设置。删掉本文件可重新生成。"
    }
    for key, (value, desc) in SETTINGS_SPEC.items():
        data[f"_说明_{key}"] = desc
        data[key] = value
    # 让 _说明_xxx 紧跟在自己那个键前面，读起来更顺
    ordered: dict[str, object] = {"_说明": data.pop("_说明")}
    for key in SETTINGS_SPEC:
        ordered[f"_说明_{key}"] = data.pop(f"_说明_{key}")
        ordered[key] = data.pop(key)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_settings(path: str, *, quiet: bool = False) -> dict[str, object]:
    """读配置；不存在就生成一份默认的。缺失的键用默认值补齐。"""
    if not os.path.exists(path):
        write_default_settings(path)
        if not quiet:
            log(f"首次运行，已生成配置文件：{path}")
            log("（想改行为就编辑它，然后重新运行程序；命令行参数会覆盖它）")
        return {k: v for k, (v, _) in SETTINGS_SPEC.items()}

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        if not quiet:
            log(f"[警告] 读不懂配置文件 {path}（{e}），本次使用默认设置。")
        return {k: v for k, (v, _) in SETTINGS_SPEC.items()}

    if not isinstance(raw, dict):
        if not quiet:
            log(f"[警告] 配置文件内容不是对象，本次使用默认设置。")
        return {k: v for k, (v, _) in SETTINGS_SPEC.items()}

    result: dict[str, object] = {}
    for key, (default, _desc) in SETTINGS_SPEC.items():
        if key in raw:
            value = raw[key]
            # 类型兜底：类型不对就用默认值，避免一个手滑的引号让程序崩掉
            if default is not None and not isinstance(value, type(default)) and value is not None:
                if isinstance(default, bool) and isinstance(value, (int, float)):
                    value = bool(value)
                elif isinstance(default, float) and isinstance(value, int):
                    value = float(value)
                else:
                    if not quiet:
                        log(f"[警告] 配置项 {key} 类型不对（应为 {type(default).__name__}），已忽略")
                    value = default
            result[key] = value
        else:
            result[key] = default
    return result


# ---------------------------------------------------------------- 命令行

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="把电脑正在播放的语音实时转成文字，并写进记事本"
                    "（基于 Windows 实时辅助字幕 + UI Automation）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="以上布尔开关都支持 --xxx / --no-xxx 两种写法；不写就用 setting.json 里的值。",
    )
    p.add_argument("--settings", default=None,
                   help=f"配置文件路径（默认脚本同目录的 {SETTINGS_FILE_NAME}）")
    p.add_argument("--regen-settings", action="store_true",
                   help="把 setting.json 重写为默认值后退出")
    p.add_argument("--probe", action="store_true",
                   help="诊断模式：打印实时辅助字幕窗口的 UI 结构后退出（排查用）")
    p.add_argument("--show-live-captions", action=argparse.BooleanOptionalAction, default=None,
                   help="是否显示实时辅助字幕窗口（默认不显示，隐藏后仍能读取）")
    p.add_argument("--notepad", action=argparse.BooleanOptionalAction, default=None,
                   help="是否自动打开记事本并写入")
    p.add_argument("--restore-focus", action=argparse.BooleanOptionalAction, default=None,
                   help="打开记事本后是否把前台窗口还回去")
    p.add_argument("--force-tab", action=argparse.BooleanOptionalAction, default=None,
                   help="是否允许程序自动切回记事本的转录标签页")
    p.add_argument("--restart", action=argparse.BooleanOptionalAction, default=None,
                   help="是否先关掉已在运行的实时辅助字幕")
    p.add_argument("--timestamp", action=argparse.BooleanOptionalAction, default=None,
                   help="每句前面是否加时间戳")
    p.add_argument("--debug", action=argparse.BooleanOptionalAction, default=None,
                   help="是否打印原始字幕文本")
    p.add_argument("--interval", type=float, default=None, help="轮询字幕的间隔（秒）")
    p.add_argument("--settle-ms", type=float, default=None, help="一行字幕稳定多久算定稿（毫秒）")
    p.add_argument("--idle-flush", type=float, default=None, help="字幕多久没变化就收下残句（秒）")
    p.add_argument("--out-dir", default=None, help="txt 输出目录")
    p.add_argument("--duration", type=float, default=0.0,
                   help="运行指定秒数后自动退出（默认 0 = 一直运行到 Ctrl+C）")
    return p


def resolve_config(args, settings: dict) -> dict:
    """把 setting.json 和命令行参数合并：命令行显式给的值优先。"""
    cfg = dict(settings)

    def take(arg_value, key):
        if arg_value is not None:
            cfg[key] = arg_value

    take(args.show_live_captions, "show_live_captions")
    take(args.notepad, "open_notepad")
    take(args.restore_focus, "notepad_restore_focus")
    take(args.force_tab, "force_switch_notepad_tab")
    take(args.restart, "restart_live_captions")
    take(args.timestamp, "timestamp")
    take(args.debug, "debug")
    take(args.interval, "poll_interval_seconds")
    take(args.settle_ms, "settle_ms")
    take(args.idle_flush, "idle_flush_seconds")
    take(args.out_dir, "output_dir")

    # 合理范围兜底
    cfg["poll_interval_seconds"] = max(0.05, float(cfg["poll_interval_seconds"]))
    cfg["settle_ms"] = max(0.0, float(cfg["settle_ms"]))
    cfg["idle_flush_seconds"] = max(1.0, float(cfg["idle_flush_seconds"]))
    return cfg


def main(argv: Optional[list[str]] = None) -> int:
    global _DEBUG
    setup_console()
    try:
        return _run(argv)
    finally:
        restore_console()


def _run(argv: Optional[list[str]] = None) -> int:
    global _DEBUG
    args = build_parser().parse_args(argv)

    path = os.path.abspath(args.settings) if args.settings else settings_path()

    if args.regen_settings:
        write_default_settings(path)
        print(f"已把默认配置写入：{path}")
        return 0

    if args.probe:
        # 诊断模式：直接把字幕窗口的 UI 结构打出来，方便确认定位规则是否还有效
        import uia_probe

        return uia_probe.main(["livecaptions", "--launch"])

    # 先按命令行的 debug 决定日志级别，再读配置
    if args.debug is not None:
        _DEBUG = bool(args.debug)
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass  # 已经初始化过（uiautomation 导入时可能做过）
    auto.SetGlobalSearchTimeout(0.6)

    settings = load_settings(path)
    cfg = resolve_config(args, settings)
    _DEBUG = bool(cfg["debug"])
    disable_uia_logging(keep_console=_DEBUG)

    out_dir = cfg["output_dir"] or app_dir()
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    txt_path = os.path.normpath(os.path.join(out_dir, f"live_captions_{stamp}.txt"))

    print("=" * 66)
    print("  实时字幕 → 记事本")
    print("=" * 66)

    captions = LiveCaptions(
        restart=bool(cfg["restart_live_captions"]),
        hide=not bool(cfg["show_live_captions"]),
        debug=_DEBUG,
    )
    if not captions.connect():
        return 1

    file_sink = FileSink(txt_path, with_timestamp=bool(cfg["timestamp"]))
    log(f"转录文件：{txt_path}")

    notepad: Optional[NotepadSink] = None
    if cfg["open_notepad"]:
        notepad = NotepadSink(
            txt_path,
            force_tab=bool(cfg["force_switch_notepad_tab"]),
            restore_focus=bool(cfg["notepad_restore_focus"]),
            debug=_DEBUG,
        )
        if not notepad.open():
            notepad = None

    committer = Committer(idle_flush=float(cfg["idle_flush_seconds"]),
                          settle_ms=float(cfg["settle_ms"]))
    transcript: list[str] = []

    stop = {"flag": False}

    def on_signal(_signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, on_signal)
    try:
        signal.signal(signal.SIGBREAK, on_signal)
    except (AttributeError, ValueError):
        pass

    log("开始监听。播放任何有语音的音频即可看到文字。（Ctrl+C 退出）")
    last_raw: Optional[str] = None
    waiting_since = time.time()
    idle_notice_shown = False
    started_at = time.time()

    try:
        while not stop["flag"]:
            if args.duration and time.time() - started_at >= args.duration:
                log(f"已达到 --duration {args.duration:g} 秒，准备退出")
                break
            if not captions.alive():
                if not captions.reconnect_if_needed():
                    time.sleep(2.0)
                    continue

            raw = captions.read()
            if _DEBUG and raw != last_raw:
                log(f"raw = {raw!r}", debug=True)
                last_raw = raw

            if raw:
                for sentence in committer.feed(raw):
                    transcript.append(sentence)
                    file_sink.write(sentence)
                    log(f"✓ {sentence}")
                    if notepad is not None:
                        notepad.write("\n".join(transcript) + "\n")
                waiting_since = time.time()
                idle_notice_shown = False
            else:
                if not idle_notice_shown and time.time() - waiting_since > 8:
                    log("已连接，等待语音……（确认电脑有声音在播放、字幕语言设置正确）")
                    idle_notice_shown = True

            time.sleep(float(cfg["poll_interval_seconds"]))
    except KeyboardInterrupt:
        pass
    finally:
        # 收尾：把最后一句残句也写出去
        try:
            tail_raw = captions.read() or ""
            for sentence in committer.flush(tail_raw):
                transcript.append(sentence)
                file_sink.write(sentence)
                log(f"✓ {sentence}")
        except Exception:
            pass
        if notepad is not None:
            notepad.write("\n".join(transcript) + "\n")
        file_sink.close()
        captions.show_window()
        print("-" * 66)
        log(f"共记录 {len(transcript)} 句，已保存到：{txt_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
