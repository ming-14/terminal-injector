"""Windows 剪贴板驱动（框架与驱动层）：UTF-16 文本读写，供 TextBox/TextArea 复制粘贴。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging

logger = logging.getLogger("winui.clipboard")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

OpenClipboard = user32.OpenClipboard
OpenClipboard.restype = wt.BOOL
OpenClipboard.argtypes = [wt.HWND]

CloseClipboard = user32.CloseClipboard
CloseClipboard.restype = wt.BOOL

EmptyClipboard = user32.EmptyClipboard
EmptyClipboard.restype = wt.BOOL

SetClipboardData = user32.SetClipboardData
SetClipboardData.restype = wt.HANDLE
SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]

GetClipboardData = user32.GetClipboardData
GetClipboardData.restype = wt.HANDLE
GetClipboardData.argtypes = [wt.UINT]

GlobalAlloc = kernel32.GlobalAlloc
GlobalAlloc.restype = wt.HGLOBAL
GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]

GlobalFree = kernel32.GlobalFree
GlobalFree.restype = wt.HGLOBAL
GlobalFree.argtypes = [wt.HGLOBAL]

GlobalLock = kernel32.GlobalLock
GlobalLock.restype = ctypes.c_void_p
GlobalLock.argtypes = [wt.HGLOBAL]

GlobalUnlock = kernel32.GlobalUnlock
GlobalUnlock.restype = wt.BOOL
GlobalUnlock.argtypes = [wt.HGLOBAL]

GlobalSize = kernel32.GlobalSize
GlobalSize.restype = ctypes.c_size_t
GlobalSize.argtypes = [wt.HGLOBAL]


def get_text() -> str | None:
    """读取剪贴板 UTF-16 文本；非文本内容或失败时返回 None。"""
    if not OpenClipboard(None):
        return None
    try:
        handle = GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = GlobalLock(handle)
        if not ptr:
            return None
        try:
            size = GlobalSize(handle) // 2  # WCHAR 个数（含结尾 \0）
            buf = ctypes.wstring_at(ptr, size)
            return buf.rstrip("\x00")
        finally:
            GlobalUnlock(handle)
    finally:
        CloseClipboard()


def set_text(text: str) -> bool:
    """写入剪贴板 UTF-16 文本。成功返回 True。"""
    data = (text + "\x00").encode("utf-16-le")
    handle = GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    ptr = GlobalLock(handle)
    if not ptr:
        GlobalFree(handle)
        return False
    try:
        ctypes.memmove(ptr, data, len(data))
    finally:
        GlobalUnlock(handle)

    if not OpenClipboard(None):
        GlobalFree(handle)
        return False
    try:
        EmptyClipboard()
        if not SetClipboardData(CF_UNICODETEXT, handle):
            return False
        # 成功后句柄归剪贴板所有，不得再释放
        return True
    finally:
        CloseClipboard()