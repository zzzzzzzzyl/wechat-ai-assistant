# -*- coding: utf-8 -*-
"""量微信那**几个同类窗口**之间的差别，给「怎么挑主窗口」找判据。

背景：`reader.find_window()` 现在返回 `found[0]` —— 枚举到的第一个。
而微信会同时开好几个 `Qt51514QWindowIcon` 类的窗口：
    微信（主）、图片和视频、朋友圈、设置、订阅号……
**枚举顺序不保证**（实测同一台机器两次跑，顺序就变了），
于是主窗口可能被选错 —— 表现是「什么都读不到」，和真实故障分不开。

这里量每类窗口的：rect / 面积 / 是否可见 / owner / 扩展样式 / 标题 / aid 特征。
只读。
"""

import ctypes
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

PROC_NAMES = ("weixin.exe", "wechat.exe")


def exe_of(pid):
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        sz = ctypes.c_ulong(1024)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                h, 0, buf, ctypes.byref(sz)):
            return buf.value
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def main():
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                     ctypes.c_void_p)
    rows = []

    def _cb(hwnd, _lp):
        try:
            buf = ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(hwnd, buf, 512)
            cls = buf.value
            if "qwindowicon" not in cls.lower():
                return True
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            exe = exe_of(pid.value)
            if not exe.lower().endswith(PROC_NAMES):
                return True
            user32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value
            r = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            owner = user32.GetWindow(hwnd, 4)          # GW_OWNER
            exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            rows.append({
                "hwnd": hwnd, "cls": cls, "title": title,
                "rect": (r.left, r.top, r.right, r.bottom),
                "area": w * h, "w": w, "h": h,
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "iconic": bool(user32.IsIconic(hwnd)),
                "owner": owner,
                "tool": bool(exstyle & WS_EX_TOOLWINDOW),
                "app": bool(exstyle & WS_EX_APPWINDOW),
            })
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), None)

    print("=" * 100)
    print("微信进程下的同类顶层窗口（按枚举顺序）")
    print("=" * 100)
    print("%-3s %-10s %-16s %-22s %-14s %-6s %-6s %-6s %-6s"
          % ("#", "hwnd", "标题", "rect", "面积", "可见", "最小化", "owner", "TOOL"))
    for i, d in enumerate(rows):
        print("%-3d %-10s %-16s %-22s %-14d %-6s %-6s %-6s %-6s"
              % (i, hex(d["hwnd"]), d["title"][:14], str(d["rect"]),
                 d["area"], d["visible"], d["iconic"],
                 hex(d["owner"]) if d["owner"] else "无",
                 d["tool"]))

    print()
    print("=" * 100)
    print("候选判据")
    print("=" * 100)
    if not rows:
        print("没有微信窗口")
        return 1

    by_area = sorted(rows, key=lambda d: -d["area"])
    print("① 按面积最大： %s 「%s」 (%d)" % (hex(by_area[0]["hwnd"]),
                                        by_area[0]["title"], by_area[0]["area"]))
    named = [d for d in rows if d["title"] == "微信"]
    print("② 标题正好是「微信」： %s"
          % ([hex(d["hwnd"]) for d in named] or "（无）"))
    print("③ 没有 owner 的： %s"
          % ([hex(d["hwnd"]) for d in rows if not d["owner"]] or "（无）"))
    print()
    print("说明：真正的判据是「哪个窗口的 UIA 树最丰富」—— 但那要 UIA。")
    print("      这里是想找一个**纯 Win32** 就能算的近似判据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
