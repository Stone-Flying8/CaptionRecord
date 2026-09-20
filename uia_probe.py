# -*- coding: utf-8 -*-
"""UIA 诊断探针。

用途：在正式跑主程序之前，先确认本机 Windows 版本下
  1) Live Captions 的转录文本框还能不能通过 AutomationId 找到；
  2) 记事本的文档元素是否支持可写的 ValuePattern、编辑区是不是 RichEdit 子窗口。

用法：
  python uia_probe.py livecaptions            # 打印 Live Captions 的 UIA 树
  python uia_probe.py livecaptions --launch   # 先启动再打印
  python uia_probe.py notepad                 # 启动记事本并打印其 UIA 树
  python uia_probe.py notepad --test-write "你好"   # 额外测试无焦点 WM_SETTEXT 写入
"""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import tempfile
import time

import comtypes
import uiautomation as auto

import winutils as wu

LIVE_CAPTIONS_CLASS = "LiveCaptionsDesktopWindow"
LIVE_CAPTIONS_EXE = r"C:\Windows\System32\LiveCaptions.exe"

# 需要在探针里逐个试一遍的 UIA 模式
PATTERN_IDS = {
    "ValuePattern": auto.PatternId.ValuePattern,
    "TextPattern": auto.PatternId.TextPattern,
    "TextEditPattern": auto.PatternId.TextEditPattern,
    "LegacyIAccessiblePattern": auto.PatternId.LegacyIAccessiblePattern,
    "InvokePattern": auto.PatternId.InvokePattern,
    "ScrollPattern": auto.PatternId.ScrollPattern,
    "TogglePattern": auto.PatternId.TogglePattern,
    "WindowPattern": auto.PatternId.WindowPattern,
}

# 各模式里可以安全尝试读一下的属性
PATTERN_PROBES = {
    "ValuePattern": ("CurrentValue", "CurrentIsReadOnly"),
    "LegacyIAccessiblePattern": ("CurrentValue", "CurrentName", "CurrentRole"),
    "TogglePattern": ("CurrentToggleState",),
    "WindowPattern": ("CurrentIsModal",),
    "ScrollPattern": ("CurrentVerticalScrollPercent",),
}


def safe(fn, default=None):
    """执行 UIA 调用，任何异常都吞掉并返回默认值（元素可能随时失效）。"""
    try:
        return fn()
    except Exception:
        return default


def exists(ctrl, seconds: float = 0.0) -> bool:
    """判断 uiautomation 搜出来的控件是否真的存在。

    注意：uiautomation 找不到元素时不会返回 None，而是返回一个"不存在"的控件对象，
    所以必须显式判断，否则会误判为"找到了"。
    """
    if ctrl is None:
        return False
    try:
        return bool(ctrl.Exists(maxSearchSeconds=seconds, searchIntervalSeconds=0.1))
    except Exception:
        return False


def describe_element(elem) -> str:
    """拼一行元素摘要。"""
    ct = safe(lambda: elem.ControlTypeName, "?")
    cls = safe(lambda: elem.ClassName, "")
    aid = safe(lambda: elem.AutomationId, "")
    name = safe(lambda: elem.Name, "") or ""
    name = name.replace("\r\n", "\\n").replace("\n", "\\n")
    if len(name) > 70:
        name = name[:70] + f"…(+{len(name) - 70})"
    parts = [ct]
    if cls:
        parts.append(f"cls={cls}")
    if aid:
        parts.append(f"AID={aid}")
    if name:
        parts.append(f"name={name!r}")
    return " ".join(parts)


def probe_patterns(elem, indent: str) -> list[str]:
    """返回该元素支持的模式描述行。"""
    lines = []
    for label, pid in PATTERN_IDS.items():
        pat = safe(lambda: elem.GetPattern(pid))
        if pat is None:
            continue
        extra = []
        for attr in PATTERN_PROBES.get(label, ()):
            val = safe(lambda: getattr(pat, attr))
            if val is not None:
                extra.append(f"{attr}={val!r}")
        ro = safe(lambda: getattr(pat, "CurrentIsReadOnly"))
        if label == "ValuePattern":
            extra.append("可写" if ro is False else f"只读({ro})")
        lines.append(f"{indent}  · {label}" + (f"  [{' '.join(extra)}]" if extra else ""))
    return lines


def dump_tree(elem, max_depth: int, indent: str = "", depth: int = 0) -> None:
    print(f"{indent}{describe_element(elem)}")
    for line in probe_patterns(elem, indent):
        print(line)
    if depth >= max_depth:
        return
    children = safe(lambda: elem.GetChildren(), []) or []
    for child in children:
        dump_tree(child, max_depth, indent + "  ", depth + 1)


def find_live_captions_window(timeout: float) -> int | None:
    deadline = time.time() + timeout
    while True:
        hwnd = wu.find_top_window(class_name=LIVE_CAPTIONS_CLASS)
        if hwnd:
            return hwnd
        if time.time() >= deadline:
            return None
        time.sleep(0.25)


def launch_live_captions() -> None:
    if not os.path.exists(LIVE_CAPTIONS_EXE):
        print(f"[!] 找不到 {LIVE_CAPTIONS_EXE}")
        sys.exit(1)
    print(f"[*] 启动 {LIVE_CAPTIONS_EXE}")
    subprocess.Popen([LIVE_CAPTIONS_EXE])


def cmd_livecaptions(args) -> int:
    hwnd = wu.find_top_window(class_name=LIVE_CAPTIONS_CLASS)
    if hwnd is None and args.launch:
        launch_live_captions()
        hwnd = find_live_captions_window(args.wait)
    if hwnd is None:
        print(f"[!] 没找到 Live Captions 窗口（class={LIVE_CAPTIONS_CLASS}）。")
        print("    可以先手动按 Win+Ctrl+L 跑一次，确认功能可用（可能需要先装语音语言包）。")
        print("\n[*] 当前所有顶层窗口，供排查：")
        for w in wu.enum_top_windows():
            if w["visible"]:
                print(f"    hwnd={w['hwnd']:<10} pid={w['pid']:<7} {w['class']:<40} {w['title']!r}")
        return 2

    print(f"[+] 找到窗口 hwnd={hwnd} pid={wu.get_pid(hwnd)} title={wu.get_window_title(hwnd)!r}")
    print(f"    进程名 = {wu.get_process_name(wu.get_pid(hwnd))}")
    print("[*] UIA 树：")

    ctrl = auto.ControlFromHandle(hwnd)
    if ctrl is None:
        print("[!] ControlFromHandle 失败")
        return 3
    dump_tree(ctrl, args.max_depth)

    print("\n[*] 查找 AutomationId='CaptionsTextBlock'：")
    found = ctrl.Control(searchDepth=args.max_depth + 6, AutomationId="CaptionsTextBlock")
    if not exists(found):
        print("    [!] 没找到。常见原因：当前没有音频在播、字幕还没开始工作；")
        print("        或者本机版本的 AutomationId 变了（那就看上面的树换定位规则）。")
    else:
        text = safe(lambda: found.Name, "")
        print(f"    [+] 找到了。Name 长度 = {len(text or '')}")
        print(f"        Name = {(text or '')[:300]!r}")

    print("\n[*] 窗口内的 Win32 子窗口：")
    for c in wu.enum_child_windows(hwnd):
        print(f"    hwnd={c['hwnd']:<10} cls={c['class']:<40} {c['title']!r}")
    return 0


def find_notepad_window() -> int | None:
    """找任意一个记事本顶层窗口（记事本是单实例+多标签，可能已经开着）。"""
    for w in wu.enum_top_windows():
        if w["visible"] and "notepad" in wu.get_process_name(w["pid"]).lower():
            return w["hwnd"]
    return None


def launch_notepad(path: str) -> int | None:
    """启动/复用记事本，返回它的顶层窗口 hwnd。

    注意：Win11 的 notepad.exe 是个跳板，真正跑起来的是 WindowsApps 里的 Notepad.exe，
    pid 会变；而且记事本是**单实例多标签**，文件很可能只是作为一个新标签页打开，
    不会有新的顶层窗口。所以这里两种都接受。
    """
    before = {w["hwnd"] for w in wu.enum_top_windows()}
    subprocess.Popen(["notepad.exe", path])
    deadline = time.time() + 20
    while time.time() < deadline:
        for w in wu.enum_top_windows():
            if w["hwnd"] in before or not w["visible"]:
                continue
            if "notepad" in wu.get_process_name(w["pid"]).lower():
                return w["hwnd"]
        time.sleep(0.3)
    # 没等到新窗口 —— 说明文件被塞进了已有的记事本窗口
    return find_notepad_window()


def cmd_notepad(args) -> int:
    path = args.file or os.path.join(tempfile.gettempdir(), "uia_probe_notepad.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("UIA probe - 初始内容\n")
    print(f"[*] 临时文件：{path}")

    hwnd = launch_notepad(path)
    if hwnd is None:
        print("[!] 没等到记事本窗口")
        return 2
    print(f"[+] 记事本窗口 hwnd={hwnd} pid={wu.get_pid(hwnd)} title={wu.get_window_title(hwnd)!r}")

    print("[*] 顶层窗口的 UIA 树：")
    ctrl = auto.ControlFromHandle(hwnd)
    dump_tree(ctrl, args.max_depth)

    print("\n[*] 记事本的 Win32 子窗口（找 RichEdit）：")
    children = wu.enum_child_windows(hwnd)
    if not children:
        print("    （没有子窗口 —— 说明它是纯 WinUI，没有独立 HWND 编辑区）")
    for c in children:
        print(f"    hwnd={c['hwnd']:<10} cls={c['class']:<40} {c['title']!r}")

    # 试 ValuePattern
    print("\n[*] 文档元素 ValuePattern 探测：")
    doc = ctrl.DocumentControl(searchDepth=args.max_depth + 6)
    if not exists(doc):
        print("    [!] 没找到 Document 控件")
    else:
        vp = safe(lambda: doc.GetPattern(auto.PatternId.ValuePattern))
        if vp is None:
            print("    [!] Document 不支持 ValuePattern")
        else:
            ro = safe(lambda: vp.CurrentIsReadOnly)
            print(f"    [+] 支持 ValuePattern，CurrentIsReadOnly={ro}")
            print(f"        当前值前 80 字：{(safe(lambda: vp.CurrentValue, '') or '')[:80]!r}")

    # 试 WM_SETTEXT
    if args.test_write is not None:
        print(f"\n[*] 测试无焦点 WM_SETTEXT 写入：{args.test_write!r}")
        target = wu.find_descendant_by_class(hwnd, ("richedit", "edit"))
        if target is None:
            print("    [!] 没找到 RichEdit/Edit 子窗口，无法用消息写入")
        else:
            print(f"    目标 hwnd={target} cls={wu.get_class_name(target)}")
            ok = wu.set_window_text_via_message(target, args.test_write)
            wu.caret_to_end_and_scroll(target)
            time.sleep(0.4)
            back = wu.get_window_text_via_message(target)
            print(f"    WM_SETTEXT 返回 {ok}，读回 = {back[:120]!r}")
            print("    → 若读回内容与写入一致，说明无焦点写入可用")

    print("\n[*] 完成。记事本窗口保留着，看完可以自己关掉。")
    return 0


def main(argv: list[str] | None = None) -> int:
    # 控制台代码页可能不支持中文/符号，先切到 UTF-8 并加兜底，免得输出时崩掉
    try:
        ctypes.WinDLL("kernel32", use_last_error=True).SetConsoleOutputCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass
    auto.SetGlobalSearchTimeout(1.0)
    # 不生成 @AutomationLog.txt
    try:
        auto.Logger.SetLogFile("")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="UIA 诊断探针")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("livecaptions", help="检查 Live Captions 的 UIA 结构")
    p1.add_argument("--launch", action="store_true", help="窗口不存在时先启动 Live Captions")
    p1.add_argument("--wait", type=float, default=25.0, help="等待窗口出现的秒数")
    p1.add_argument("--max-depth", type=int, default=6)
    p1.set_defaults(func=cmd_livecaptions)

    p2 = sub.add_parser("notepad", help="检查记事本的 UIA 结构与可写性")
    p2.add_argument("--file", help="用指定的 txt 文件打开记事本")
    p2.add_argument("--max-depth", type=int, default=8)
    p2.add_argument("--test-write", help="测试向编辑区无焦点写入这段文本")
    p2.set_defaults(func=cmd_notepad)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
