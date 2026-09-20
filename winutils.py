# -*- coding: utf-8 -*-
"""Win32 轻量封装（纯 ctypes，不依赖 pywin32）。

只提供本项目用得到的几件事：枚举窗口、按类名/进程找窗口、读写窗口文本、
以及"最小化并从任务栏隐藏"。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------- 常量 ----------------
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E

EM_SETSEL = 0x00B1
EM_SCROLLCARET = 0x00B7
EM_LINESCROLL = 0x00B6

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_MINIMIZE = 6
SW_RESTORE = 9

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

# ---------------- 函数签名 ----------------
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_void_p]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.GetForegroundWindow.restype = wt.HWND
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.EnumChildWindows.argtypes = [wt.HWND, WNDENUMPROC, wt.LPARAM]
user32.EnumChildWindows.restype = wt.BOOL
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.BringWindowToTop.restype = wt.BOOL

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL


# ---------------- 基础读取 ----------------
def get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetClassNameW(hwnd, buf, 512)
    return buf.value


def get_window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    user32.GetWindowTextW(hwnd, buf, 1024)
    return buf.value


def get_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def get_process_name(pid: int) -> str:
    """返回可执行文件名（不含路径），取不到时返回空串。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.split("\\")[-1]
        return ""
    finally:
        kernel32.CloseHandle(h)


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


# ---------------- 枚举 ----------------
def enum_top_windows() -> list[dict]:
    """枚举所有顶层窗口。"""
    out: list[dict] = []

    def cb(hwnd, _lparam):
        out.append(
            {
                "hwnd": hwnd,
                "class": get_class_name(hwnd),
                "title": get_window_title(hwnd),
                "pid": get_pid(hwnd),
                "visible": bool(user32.IsWindowVisible(hwnd)),
            }
        )
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def enum_child_windows(parent: int) -> list[dict]:
    """枚举某个窗口的所有后代子窗口。"""
    out: list[dict] = []

    def cb(hwnd, _lparam):
        out.append(
            {
                "hwnd": hwnd,
                "class": get_class_name(hwnd),
                "title": get_window_title(hwnd),
                "pid": get_pid(hwnd),
                "visible": bool(user32.IsWindowVisible(hwnd)),
            }
        )
        return True

    user32.EnumChildWindows(parent, WNDENUMPROC(cb), 0)
    return out


def find_top_window(class_name: str | None = None, pid: int | None = None) -> int | None:
    """按类名（精确）或进程 id 找第一个顶层窗口。"""
    for w in enum_top_windows():
        if class_name is not None and w["class"] != class_name:
            continue
        if pid is not None and w["pid"] != pid:
            continue
        return w["hwnd"]
    return None


def find_descendant_by_class(parent: int, class_keywords: tuple[str, ...]) -> int | None:
    """在后代子窗口中找类名包含任一关键字者（大小写不敏感）。"""
    lowered = tuple(k.lower() for k in class_keywords)
    for c in enum_child_windows(parent):
        cls = c["class"].lower()
        if any(k in cls for k in lowered):
            return c["hwnd"]
    return None


# ---------------- 文本读写 ----------------
def send_message(hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> int:
    return int(user32.SendMessageW(hwnd, msg, ctypes.c_size_t(wparam), ctypes.c_void_p(lparam)))


def get_window_text_via_message(hwnd: int) -> str:
    """用 WM_GETTEXT 读取控件文本（对 RichEdit 也有效）。"""
    length = send_message(hwnd, WM_GETTEXTLENGTH, 0, 0)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    send_message(hwnd, WM_GETTEXT, length + 1, ctypes.addressof(buf))
    return buf.value


def set_window_text_via_message(hwnd: int, text: str) -> bool:
    """用 WM_SETTEXT 写入控件文本。不抢焦点。"""
    buf = ctypes.c_wchar_p(text)
    ret = send_message(hwnd, WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p).value or 0)
    del buf
    return bool(ret)


def caret_to_end_and_scroll(hwnd: int) -> None:
    """把插入符移到末尾并滚动到可见位置。不抢焦点。

    EM_SETSEL 的 wParam/lParam 都传 -1 表示"取消选区并把插入符放到文本末尾"。
    """
    send_message(hwnd, EM_SETSEL, -1, -1)
    send_message(hwnd, EM_SCROLLCARET, 0, 0)


# ---------------- 窗口可见性 ----------------
def hide_from_taskbar_and_minimize(hwnd: int) -> None:
    """最小化 + 追加 WS_EX_TOOLWINDOW：窗口从任务栏消失，但窗口与 UIA 树仍然存在。

    不使用 SW_HIDE —— 完全隐藏有可能让应用停止渲染，进而影响字幕更新。
    """
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
    user32.ShowWindow(hwnd, SW_MINIMIZE)


def restore_window(hwnd: int) -> None:
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
    user32.ShowWindow(hwnd, SW_RESTORE)


kernel32.GetCurrentThreadId.restype = wt.DWORD
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL


def get_foreground_window() -> int:
    return int(user32.GetForegroundWindow())


def set_foreground_window(hwnd: int) -> bool:
    """把指定窗口设为前台。

    Windows 有"前台锁定"限制，非前台进程直接 SetForegroundWindow 常常无效。
    这里用 AttachThreadInput 把自己挂到当前前台线程上，绕开这个限制。
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.GetForegroundWindow() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_tid and fg_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
    try:
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
    return ok
