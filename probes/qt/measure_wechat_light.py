# -*- coding: utf-8 -*-
"""量微信**浅色模式**的语义区域配色（给浅色色板提供实测依据）。

    python -X utf8 probes/qt/measure_wechat_light.py

⚠️ 为什么要单独写一个（而不是改 `probe_theme.py`）：
    `probe_theme.py` 把抓图**固定存成 `out/theme_probe.png`**，所以先量的深色
    会被后量的浅色**覆盖掉**。2026-09-26 那次就是这样 —— 现在手上只剩深色那张，
    浅色的证据没了，只剩几个孤零零的色号（`#DCDCE1` 等）和一个"98.5%"的说法，
    **没法复核它到底量的是哪块区域**。而用户看到浅色时说"灰色太深了"，
    恰恰就卡在这个复核不了的地方。
    → 这个探针**按模式分别落盘**，两边证据都留着。

⚠️ 只读：`PrintWindow` 让微信自己渲染成位图，不置顶、不激活、不移动、不发输入。

⚠️ 硬性前置：**微信当前必须是浅色**。不是浅色就直接退出并提示去切，
   绝不在深色下"凑一组浅色值" —— 那正是上次出错的方式。
"""

import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

import win32gui                                                    # noqa: E402

from wechat_assistant.capture import capture_window, is_blank       # noqa: E402

PROC_NAME = "Weixin.exe"


def hexs(rgb):
    return "#%02X%02X%02X" % tuple(rgb[:3])


def lum(rgb):
    r, g, b = rgb[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def find_wechat():
    import win32process

    want = PROC_NAME.lower()
    found = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if win32gui.GetWindowText(hwnd) == "":
                return
            cls = win32gui.GetClassName(hwnd)
            if "QWindowIcon" not in cls:
                return
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            import win32api
            h = win32api.OpenProcess(0x1000, False, pid)
            try:
                exe = win32process.GetModuleFileNameEx(h, 0) or ""
            finally:
                win32api.CloseHandle(h)
            if os.path.basename(exe).lower() == want:
                r = win32gui.GetWindowRect(hwnd)
                found.append((hwnd, r[2] - r[0], r[3] - r[1]))
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    if not found:
        return None
    # 取面积最大的那个（会话列表/聊天都在它里面）
    found.sort(key=lambda t: t[1] * t[2], reverse=True)
    return found[0][0]


def dominant(img, box, n=5):
    """区域内的主色（按出现次数），返回 [(hex, pct), ...]。"""
    W, H = img.size
    x0, y0, x1, y1 = box
    x0, y0 = max(0, int(x0 * W)), max(0, int(y0 * H))
    x1, y1 = min(W, int(x1 * W)), min(H, int(y1 * H))
    if x1 <= x0 or y1 <= y0:
        return []
    reg = img.crop((x0, y0, x1, y1)).convert("RGB")
    # 缩小采样：颜色统计不需要全分辨率
    step = max(1, min(reg.width, reg.height) // 160)
    cnt = Counter()
    px = reg.load()
    for y in range(0, reg.height, step):
        for x in range(0, reg.width, step):
            cnt[px[x, y]] += 1
    total = sum(cnt.values()) or 1
    return [(hexs(c), 100.0 * k / total) for c, k in cnt.most_common(n)]


def main():
    hwnd = find_wechat()
    if not hwnd:
        print("没找到微信主窗口 —— 确认微信已登录且窗口没关")
        return 1

    img = capture_window(hwnd)
    if img is None:
        print("PrintWindow 抓图失败")
        return 1
    if is_blank(img):
        print("抓到空白 —— 微信多半最小化了。请把微信窗口恢复出来再跑。")
        return 1

    W, H = img.size
    # 整窗平均亮度：判断当前到底是浅色还是深色
    small = img.convert("L").resize((100, 100))
    avg = sum(small.getdata()) / 10000.0
    mode = "light" if avg > 128 else "dark"

    outdir = os.path.join(ROOT, "out")
    os.makedirs(outdir, exist_ok=True)
    # ⚠️ **按模式分别落盘**，别共用文件名（共用就会互相覆盖，证据就没了）
    stamp = time.strftime("%Y%m%d_%H%M%S")
    shot = os.path.join(outdir, "wechat_%s_%s.png" % (mode, stamp))
    img.save(shot)

    print("图：%s" % shot)
    print("位图 %dx%d   整窗平均亮度 %.1f  ->  当前是【%s】"
          % (W, H, avg, "浅色" if mode == "light" else "深色"))

    if mode != "light":
        print("\n" + "!" * 66)
        print("微信现在**不是浅色**，这个探针拒绝在深色下给浅色配色。")
        print("请先切：微信 → 设置 → 通用 → 外观 → 浅色，然后重跑本探针。")
        print("（在深色下凑一组「看起来像浅色」的值 = 上次出错的方式，不做。）")
        print("!" * 66)
        return 2

    # 语义区域。微信 4.x 横向大致是：
    #   最左图标条 | 会话列表 | 聊天区（右侧）
    zones = [
        ("窗口顶部横条 (0~6% 高)", (0.0, 0.0, 1.0, 0.06)),
        ("左侧图标条 (0~12% 宽)", (0.0, 0.06, 0.12, 0.95)),
        ("会话列表 (12~38% 宽)", (0.12, 0.06, 0.38, 0.95)),
        ("聊天区上部 (40~100% 宽, 6~60%)", (0.40, 0.06, 1.0, 0.60)),
        ("聊天区下部 (40~100% 宽, 60~88%)", (0.40, 0.60, 1.0, 0.88)),
        ("底部输入区 (40~100% 宽, 88~100%)", (0.40, 0.88, 1.0, 1.0)),
    ]
    for label, box in zones:
        tops = dominant(img, box)
        print("\n--- %s ---" % label)
        for h, pct in tops:
            r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
            print("   %s  %5.1f%%   亮度 %5.1f" % (h, pct, lum((r, g, b))))

    # 沿着一列扫，找分隔线（相邻像素亮度突变的地方）
    print("\n--- 竖直扫描：找横向分隔线（亮度突变处）---")
    px = img.convert("RGB").load()
    x = int(W * 0.70)
    prev = None
    for y in range(0, H):
        c = px[x, y]
        if prev is not None and abs(lum(c) - lum(prev)) >= 8:
            print("   y=%4d (%.0f%%)  %s -> %s"
                  % (y, 100.0 * y / H, hexs(prev), hexs(c)))
        prev = c
    return 0


if __name__ == "__main__":
    sys.exit(main())
