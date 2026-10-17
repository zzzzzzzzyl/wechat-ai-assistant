# -*- coding: utf-8 -*-
"""定位微信主窗口 —— **纯 Win32**，不碰 UIA。

为什么要单独一个模块（2026-10-17）

    `reader`（读取）和 `follow`（跟随）**必须用同一套判据**挑窗口。
    原来两边口径不一样：

      · `reader.pick_window()` 按**标题精确「微信」**挑（对）；
      · `follow.find_wechat_window()` 按**面积最大**挑（错）——
        微信的「图片和视频」预览实测 **711480 px²，比主窗口 544887 还大**，
        于是打开图片时面板会贴到图片窗口上。

    现在两边都走 `pick_window()` / `find_main_hwnd()`。

⚠️ 只 import win32gui / win32api / win32process / win32con ——
   **不许 import uiautomation**（`follow` 是每拍跑的，UIA 有 provider 卡死风险）。
"""

import os
from typing import List, Optional

import win32api
import win32con
import win32gui
import win32process

#: 主窗口标题（config 里可改，兼容英文版）。
DEFAULT_TITLE = "微信"


def list_windows(cfg) -> List[dict]:
    """列出所有「像微信主窗口」的候选（纯 Win32，不碰 UIA）。

    只按进程名 + 窗口类过滤，**不做取舍** —— 取舍在 `pick_window()` 里，
    这样两者可以分开测。

    每个候选额外带两个判据字段（2026-10-17 加）：
      · `style` —— `GWL_STYLE`，用来认 `WS_CAPTION`（主窗口有标题栏，
        「图片和视频」预览那种 popup 没有）；
      · `owner` —— `GW_OWNER`（弹窗/子窗口的 owner 是主窗口，主窗口是 0）。
    """
    proc_name = (cfg.get("wechat.proc_name", "Weixin.exe") or "").lower()
    class_hint = (cfg.get("wechat.window_class_contains", "QWindowIcon") or "").lower()
    found = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            cls = win32gui.GetClassName(hwnd)
            if class_hint and class_hint not in cls.lower():
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            h = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            try:
                exe = win32process.GetModuleFileNameEx(h, 0)
            finally:
                win32api.CloseHandle(h)
            if os.path.basename(exe).lower() != proc_name:
                return True
            rect = win32gui.GetWindowRect(hwnd)
            try:
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            except Exception:
                style = 0
            try:
                owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
            except Exception:
                owner = 0
            found.append({
                "hwnd": hwnd, "pid": pid, "exe": exe, "class": cls,
                "title": win32gui.GetWindowText(hwnd) or "",
                "rect": rect,
                "area": max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1]),
                "style": style,
                "owner": owner or 0,
            })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found


def pick_window(cands: List[dict], cfg) -> Optional[dict]:
    """从候选里挑出**真正的主窗口**。挑不出来返回 None。

    ⚠️⚠️ 这里以前是 `found[0]` —— **盲选第一个**，是本项目最坑的一个 bug
    （2026-09-27 第三十八轮发现，一直没改）。

    微信同时会开好几个同类窗口：主窗口 / **「图片和视频」预览** / 朋友圈 / 设置，
    它们**同名同进程同类名**，而 `EnumWindows` 的返回顺序**不保证**
    —— 实测同一台机器跑两次顺序就变了。挑中「图片和视频」时**什么都读不到**，
    而输出看起来和「微信改版了 / UIA 坏了」**完全分不开**。

    排除法都试过、都不成立：
      · **「取面积最大的」不行** —— 实测「图片和视频」预览 711480 px²，
        **比主窗口的 544887 px² 还大**。
      · 置顶过的、最后激活的也不行（用户可能正好在看别的窗口）。

    判据（按顺序，纯 Win32）：
      ① **标题精确等于「微信」** 且唯一 —— 直接用它；
      ② 只有一个候选 —— 标题为空（改版/其他语言）或等于主窗口名才用；
         ⚠️ 标题非空又不是主窗口名 → 返回 None（那多半是「图片和视频」/朋友圈，
            把它的内容当聊天读，比读不到更难查）；
      ③ 多个候选且标题都叫「微信」时（实测图片预览可能也同名）——
         依次排除 **有 owner 的**（弹窗挂主窗口下）、**没有 `WS_CAPTION` 的**
         （预览是无边框 popup）、**`WS_EX_TOOLWINDOW` 的**，剩下唯一才用；
      ④ 还分不出 -> None。调用方（`WeChatReader.find_window`）会再试试
         「哪个窗口的 UIA 树最丰富」，或直接**等待**，不会瞎读。

    ⚠️ 不要在这里加 UIA 查询：`find_window()` 每拍都调，而 UIA 有 provider
       卡死风险（`follow` 也复用这个函数）。
    """
    if not cands:
        return None
    want = (cfg.get("wechat.window_title", DEFAULT_TITLE) or "").strip()

    def title(c):
        return (c.get("title") or "").strip()

    exact = [c for c in cands if title(c) == want]
    if len(exact) == 1:
        return exact[0]

    # ② 只有一个候选：标题为空（改版/其他语言）或正好是主窗口名才敢用
    if len(cands) == 1:
        c = cands[0]
        return c if (not title(c) or title(c) == want) else None

    # ③ 多个候选（exact 也可能是多个）：用纯 Win32 特征做差集
    pool = exact or cands
    pool = [c for c in pool if not c.get("owner")]
    pool = [c for c in pool if (c.get("style") or 0) & win32con.WS_CAPTION]
    pool = [c for c in pool if not c.get("tool")]
    if len(pool) == 1:
        return pool[0]
    return None


def find_main_hwnd(cfg) -> int:
    """给 `follow` 用的简版：返回主窗口 HWND，挑不出来返回 0。

    ⚠️ 挑不出来时返回 0（**不是**退回"面积最大"那个）——
       跟随时拿不到主窗口就保持原地不动，比贴到图片窗口上安全。
    """
    win = pick_window(list_windows(cfg), cfg)
    return int(win["hwnd"]) if win else 0
