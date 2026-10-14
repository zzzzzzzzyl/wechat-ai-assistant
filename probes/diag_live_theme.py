# -*- coding: utf-8 -*-
"""真机体检：**当前微信是什么主题、气泡切分认不认得出对方的话**。

    python probes\diag_live_theme.py

它一次把这几件事全打出来（**纯只读**：走 `capture.capture_window` 的 PrintWindow，
不置顶、不激活、不移动、不发输入 —— 只让微信把自己画进位图）：

  ① 当前微信渲染出来是深色还是浅色（`theme.sample_wechat_theme`，同一套判据）
  ② 聊天区底色、各档色差(>8/18/30/45/60)的像素占比
  ③ `bubble_boxes(tol=45)` 认出几个盒、每个盒的主色和与底色的色差（按左右分组）
  ④ 真跑一次 `read_chat()`（含 OCR）：标题 / 消息 N 条 / 时间分隔 N 个
     —— 这一步就是 `probes/probe_ocr_read.py` 报的那个「消息 N 条」

⚠️ 它**不改产品代码**，就是把这些函数按真机顺序调一遍。
⚠️ 抓到的图会存到 `%TEMP%`（不进仓库）。要留证据就把打印出来的路径拷到 `out/`。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import dpi as _dpi  # noqa: E402

print("DPI 感知: %s" % _dpi.enable())      # ⚠️ 必须在抓图之前

import numpy as np  # noqa: E402

from wechat_assistant import capture  # noqa: E402
from wechat_assistant import ocr_reader as R  # noqa: E402
from wechat_assistant import theme as _theme  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.reader import list_windows, pick_window  # noqa: E402


def modal(a, q=8):
    if a.size == 0:
        return None, 0.0
    sub = a.astype(np.int32)
    key = ((sub[:, :, 0] // q) * 1024 + (sub[:, :, 1] // q) * 32
           + (sub[:, :, 2] // q))
    vals, counts = np.unique(key.ravel(), return_counts=True)
    i = int(np.argmax(counts))
    k = int(vals[i])
    col = ((k // 1024) * q + q // 2, ((k // 32) % 32) * q + q // 2,
           (k % 32) * q + q // 2)
    return col, float(counts[i]) / float(key.size)


def dist(a, b):
    return sum(abs(int(x) - int(y)) for x, y in zip(a, b))


def main():
    cfg = load_config()
    explicit = None
    if len(sys.argv) > 1:
        explicit = int(sys.argv[1], 0)

    # ⚠️ `list_windows()` 只收**可见**窗口，而微信最小化时可见的是那个
    #    237x39 的占位窗（标题还叫「微信」）—— 抓它必然是空白。
    #    所以这里自己列一遍**全部** Weixin.exe 的窗口，带着可见性/最小化状态，
    #    让人一眼看出"主窗口现在是隐藏的"。
    print("\n-- 全部微信窗口（不看可见性）--")
    all_w = []
    try:
        import win32api
        import win32con
        import win32gui
        import win32process

        def cb(h, _):
            try:
                cls = win32gui.GetClassName(h)
                if "QWindow" not in cls:
                    return True
                _, pid = win32process.GetWindowThreadProcessId(h)
                p = win32api.OpenProcess(
                    win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                try:
                    exe = win32process.GetModuleFileNameEx(p, 0)
                finally:
                    win32api.CloseHandle(p)
                if os.path.basename(exe).lower() != \
                        (cfg.get("wechat.proc_name", "Weixin.exe") or "").lower():
                    return True
                r = win32gui.GetWindowRect(h)
                cr = win32gui.GetClientRect(h)
                title = win32gui.GetWindowText(h) or ""
                vis = bool(win32gui.IsWindowVisible(h))
                ico = bool(win32gui.IsIconic(h))
                all_w.append(dict(hwnd=h, cls=cls, title=title, vis=vis, ico=ico,
                                  size=(r[2] - r[0], r[3] - r[1]),
                                  client=(cr[2], cr[3])))
                print("   hwnd=0x%-8X %-24s vis=%-5s min=%-5s %sx%s "
                      "(客户区 %dx%d) title=%r"
                      % (h, cls, vis, ico, r[2] - r[0], r[3] - r[1],
                         cr[2], cr[3], title))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(cb, None)
    except Exception as e:
        print("   枚举失败：%s: %s" % (type(e).__name__, e))

    want = (cfg.get("wechat.window_title", "微信") or "").strip()
    cands = list_windows(cfg)
    print("\n-- pick_window 的候选（只含可见窗口）--")
    for c in cands:
        r = c["rect"]
        print("   hwnd=0x%X title=%r %dx%d"
              % (c["hwnd"], c["title"], r[2] - r[0], r[3] - r[1]))

    if explicit is not None:
        hwnd = explicit
        print("   用命令行指定的 hwnd=0x%X" % hwnd)
    else:
        win = pick_window(cands, cfg)
        if win is None:
            print("   **挑不出主窗口**（标题不等于 %r）" % want)
            return 1
        hwnd = win["hwnd"]
        print("   选中：title=%r hwnd=0x%X" % (win["title"], hwnd))

    # 主窗口是不是那个"大窗"（最小化时它 vis=0，打印出来的是占位窗）
    big = [w for w in all_w
           if w["client"][0] > 400 and w["client"][1] > 400]
    if big and hwnd not in [w["hwnd"] for w in big]:
        print("\n   ⚠️⚠️ 选中的不是那个大窗，大窗是 %s"
              % ", ".join("0x%X(%sx%s, vis=%s)" % (w["hwnd"], w["client"][0],
                                                   w["client"][1], w["vis"])
                          for w in big))
        print("       最小化时**可见的那两个是 237x39 的占位窗**，抓它们只会得到空白。")
        for w in big:
            print("       直接抓大窗试试：python probes\\diag_live_theme.py 0x%X"
                  % w["hwnd"])

    img = capture.capture_window(hwnd)
    if img is None or capture.is_blank(img):
        print("\n   **PrintWindow 抓到空白**（img=%s）"
              % ("None" if img is None else "%dx%d" % img.size))
        print("   常见原因：① 微信**最小化/隐藏**了（PrintWindow 拿不到内容，")
        print("             这是 Windows 的行为，不是 bug）")
        print("             ② 停在扫码登录窗")
        print("             → 把微信窗口**恢复正常大小**再跑一次，")
        print("               或者直接抓大窗：python probes\\diag_live_theme.py <hwnd>")
        return 1

    W, H = img.width, img.height
    print("\n① 图像 %dx%d" % (W, H))
    t = _theme.sample_wechat_theme(hwnd)
    print("   微信当前渲染的主题 = %s（判据：聊天区中位亮度，<110 = dark）" % t)

    ts = time.strftime("%Y%m%d_%H%M%S")
    shot = os.path.join(os.environ.get("TEMP", "."), "live_%s_%s.png" % (t, ts))
    img.save(shot)
    print("   已存截图（不进仓库）：%s" % shot)

    rd = R.OcrReader(cfg)
    scale = rd._scale()
    x1, y1, x2, y2 = R.chat_rect((0, 0, W, H), scale, cfg, (W, H))
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    print("\n   缩放 %.2f   聊天区（配置几何）= (%d,%d)-(%d,%d)"
          % (scale, x1, y1, x2, y2))
    if x2 - x1 < 100 or y2 - y1 < 100:
        print("   ⚠️ 聊天区太小 —— 微信窗口可能太窄，或当前不在聊天页")
        return 1

    bg = rd._bg(a, x1, x2, y1)
    bg_show = None if bg is None else tuple(int(v) for v in bg)
    print("\n② 聊天区底色 _bg() = %s" % (bg_show,))
    if bg is None:
        print("   算不出底色，后面没法继续")
        return 1
    ca = a[y1:y2, x1:x2]
    d = np.abs(ca - np.asarray(bg, dtype=np.int16)).sum(axis=2)
    col, share = modal(ca)
    print("   整块众数 = %s（%.1f%%）%s"
          % (col, share * 100,
             "  ← **就是底色**" if dist(col, bg) <= 8 else ""))
    for tol in (8, 18, 30, 45, 60):
        print("   色差 > %-3d 的像素占 %.3f%%" % (tol, float((d > tol).mean()) * 100))

    boxes = R.bubble_boxes(ca, bg, tol=45)
    print("\n③ bubble_boxes(tol=45) 认出 **%d** 个盒" % len(boxes))
    for i, b in enumerate(boxes):
        sub = ca[b[1]:b[3], b[0]:b[2]]
        c2, s2 = modal(sub)
        side = "我的" if R.side_of_box(b, ca.shape[1]) else "对方"
        print("   #%-2d %4d×%-4d @(%4d,%4d) %s 主色 %-16s 占%5.1f%% 与底色色差 %3d"
              % (i, b[2] - b[0], b[3] - b[1], b[0], b[1], side, str(c2),
                 s2 * 100, dist(c2, bg)))

    print("\n④ 真跑一次 read_chat()（含 OCR，慢一点）…")
    t0 = time.perf_counter()
    snap = rd.read_chat(hwnd)
    ms = (time.perf_counter() - t0) * 1000
    if snap is None:
        print("   **read_chat 返回 None**（_geom_bad=%s）—— 一个气泡盒都没认出来"
              % rd._geom_bad)
    else:
        print("   标题（会话主键）= %r" % snap.title)
        print("   群聊 = %s   **消息 %d 条**   时间分隔 %d 个   （%.0f ms）"
              % (snap.is_group, len(snap.messages), len(snap.markers), ms))
        for m in snap.messages:
            r = m.rect or (0, 0, 0, 0)
            print("      [%s] y=%-4s x=%s..%-5s %s%s"
                  % ("我" if m.is_self else "对方", r[1], r[0], r[2],
                     ("(%s) " % m.sender_name) if m.sender_name else "",
                     m.text[:70]))
        for k in snap.markers:
            print("      [时间] y=%-4s %s" % (k.rect[1] if k.rect else "?", k.text))

    print("""
判据：
  · ② 里「整块众数 == 底色」且 ③ 只有贴右（我的绿气泡）那 1~2 个盒
    → 就是"对方气泡填充色 == 底色"，切行判据零信号（浅色）。
  · ③ 里对方侧有几个 54×54 左右的**近似正方形**、主色是浅绿/头像色 —— 那是**头像**，
    不是气泡（`BUBBLE_MIN_W/H` 拦不住它们，它们是"有盒没字"，不会变成消息）。
  · ④ 的「消息 N 条」就是 `probes/probe_ocr_read.py` 报的那个数。
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
