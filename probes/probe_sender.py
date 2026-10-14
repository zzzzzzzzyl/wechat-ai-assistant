# -*- coding: utf-8 -*-
"""
发送者区分实验：UIA 取每行 rect + 截图分析气泡水平位置。

为什么需要这一步：
    消息项的 rect 是整行宽度，UIA 不给气泡位置；ItemStatus/HelpText 全空；
    消息项也不支持 TextPattern。所以「哪句是对方说的、哪句是自己说的」只能靠像素。

原理：
    微信聊天列表左右两侧都有背景留白，气泡（含头像）只出现在一侧：
        对方的消息 -> 头像 + 白色气泡靠左
        自己的消息 -> 浅绿色气泡 + 头像靠右
    因此对每一行截图：
      1. 用该行左右边缘各若干像素估计「背景色」；
      2. 找出与背景色明显不同的像素 = 内容像素（头像/气泡/文字）；
      3. 统计内容像素在行内左半 / 右半的分布，偏哪边就是谁发的。

注意：
    本脚本会**临时把微信窗口置顶**（HWND_TOPMOST）以保证截图不被遮挡，
    结束后立即恢复为普通层级。全程不发消息、不点击、不输入。

用法：
    python probe_sender.py                # 默认置顶后截图
    python probe_sender.py --no-topmost   # 不置顶（若微信本来就在最前面）
"""

import argparse
import ctypes
import datetime as dt
import os
import sys
import threading

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

import numpy as np
import win32api
import win32con
import win32gui
import win32process
from PIL import ImageGrab

TARGET_PROC = "weixin.exe"
SCOPE_DESCENDANTS = 4


def find_wechat_hwnd():
    best = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if "QWindowIcon" not in win32gui.GetClassName(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            try:
                path = win32process.GetModuleFileNameEx(h, 0)
            finally:
                win32api.CloseHandle(h)
            if os.path.basename(path).lower() == TARGET_PROC:
                best.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return best[0] if best else None


def read_messages(hwnd, out):
    """UIA：拿当前聊天的标题和每条消息的文本/类型/rect。"""
    import comtypes

    comtypes.CoInitialize()
    import uiautomation as auto
    from uiautomation import uiautomation as core

    with auto.UIAutomationInitializerInThread(debug=False):
        uia = core._AutomationClient.instance().IUIAutomation
        el = auto.ControlFromHandle(hwnd).Element
        arr = el.FindAll(SCOPE_DESCENDANTS, uia.CreateTrueCondition())
        title = ""
        ml = None
        msgs = []
        for i in range(arr.Length):
            e = arr.GetElement(i)
            cls = e.CurrentClassName or ""
            aid = e.CurrentAutomationId or ""
            if "current_chat_name_label" in aid:
                title = e.CurrentName or ""
            elif aid == "chat_message_list":
                r = e.CurrentBoundingRectangle
                ml = (r.left, r.top, r.right, r.bottom)
            elif aid.startswith("chat_message_list") and cls.endswith("ItemView"):
                r = e.CurrentBoundingRectangle
                msgs.append(
                    {
                        "name": e.CurrentName or "",
                        "cls": cls,
                        "rect": (r.left, r.top, r.right, r.bottom),
                    }
                )
        out["title"] = title
        out["container"] = ml
        out["messages"] = msgs


def _safe_read(hwnd, out):
    try:
        read_messages(hwnd, out)
    except Exception as e:
        import traceback

        out["error"] = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-800:])


def analyze_line(img, bg_margin=8, thresh=45):
    """
    返回该行的内容像素分布分析。
    bg_margin: 用左右各多少像素估计背景色
    thresh   : 与背景色的曼哈顿距离阈值，超过则算内容像素
    """
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    h, w, _ = a.shape
    if w < 4 * bg_margin:
        return None

    left_bg = a[:, :bg_margin].reshape(-1, 3).mean(axis=0)
    right_bg = a[:, -bg_margin:].reshape(-1, 3).mean(axis=0)
    bg = (left_bg + right_bg) / 2.0

    diff = np.abs(a - bg).sum(axis=2)
    mask = diff > thresh

    total = int(mask.sum())
    if total == 0:
        return {"empty": True, "total": 0}

    left_cnt = int(mask[:, : w // 2].sum())
    right_cnt = int(mask[:, w // 2:].sum())
    denom = left_cnt + right_cnt

    cols = mask.sum(axis=0).astype(float)
    xs = np.arange(w)
    cx = float((cols * xs).sum() / cols.sum()) if cols.sum() > 0 else -1
    nz = np.where(cols > 0)[0]

    # 绿色气泡检测：微信「自己发的消息」气泡是绿色（深色主题实测约 (53,210,141)，
    # 浅色主题约 #95EC69）。这个特征跨主题成立，比「与背景色的差值」稳健得多。
    r_, g_, b_ = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    green_mask = (g_.astype(np.int16) - np.maximum(r_, b_).astype(np.int16)) > 20
    green_cnt = int(green_mask.sum())
    green_ratio = green_cnt / float(h * w)

    # 采样该行出现最多的几种颜色（前 5），用于人工核对气泡/背景色
    flat = a.reshape(-1, 3)
    idx = np.random.choice(len(flat), size=min(4000, len(flat)), replace=False)
    sample = flat[idx]
    uniq, cnt = np.unique(sample, axis=0, return_counts=True)
    order = np.argsort(-cnt)[:5]
    top = [(tuple(int(v) for v in uniq[i]), int(cnt[i])) for i in order]

    return {
        "empty": False,
        "w": w,
        "h": h,
        "total": total,
        "left_cnt": left_cnt,
        "right_cnt": right_cnt,
        "left_ratio": (left_cnt / denom) if denom else 0.0,
        "center_x": cx,
        "center_ratio": (cx / w) if cx >= 0 else -1,
        "content_span": (int(nz[0]), int(nz[-1])) if len(nz) else None,
        "bg": tuple(int(v) for v in bg),
        "green_cnt": green_cnt,
        "green_ratio": green_ratio,
        "top_colors": top,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-topmost", action="store_true", help="不把微信临时置顶")
    ap.add_argument("--thresh", type=int, default=45, help="内容像素判定阈值")
    args = ap.parse_args()

    buf = []

    def rec(s=""):
        print(s, flush=True)
        buf.append(str(s))

    hwnd = find_wechat_hwnd()
    if not hwnd:
        rec("!! 未找到微信主窗口")
        return

    rec("=" * 84)
    rec("发送者区分实验：UIA 行 rect + 截图像素分析")
    rec("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    rec("=" * 84)

    out = {}
    t = threading.Thread(target=_safe_read, args=(hwnd, out), daemon=True)
    t.start()
    t.join(30)
    if t.is_alive():
        rec("!! UIA 读取超时")
        return
    if out.get("error"):
        rec("!! %s" % out["error"])
        return

    msgs = out.get("messages", [])
    rec("当前聊天: %r   消息条数: %d" % (out.get("title"), len(msgs)))
    if not msgs:
        rec("!! 当前没有可分析的消息（可能不在聊天界面）")
        return

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    shotdir = os.path.join(outdir, "shots")
    os.makedirs(shotdir, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    sw = win32api.GetSystemMetrics(0)
    sh = win32api.GetSystemMetrics(1)
    rec("屏幕: %dx%d" % (sw, sh))

    topmost = False
    try:
        if not args.no_topmost:
            rec("正在临时置顶微信窗口以保证截图不被遮挡…")
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            except Exception:
                pass
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
            )
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            topmost = True
            import time

            time.sleep(1.0)
            fg = win32gui.GetForegroundWindow()
            rec("当前前台窗口 = %s %s" % (hex(fg), win32gui.GetWindowText(fg)[:30]))

        rec("")
        rec("%-4s %-26s %-28s %-9s %-9s %-9s %-9s %s" % (
            "#", "消息文本", "控件类", "左半占比", "右半占比", "中心比", "绿像素比", "判定"))
        rec("-" * 96)

        results = []
        for i, m in enumerate(msgs, 1):
            x1, y1, x2, y2 = m["rect"]
            # 裁到屏幕内
            cx1, cy1 = max(0, x1), max(0, y1)
            cx2, cy2 = min(sw, x2), min(sh, y2)
            if cx2 - cx1 < 20 or cy2 - cy1 < 10:
                rec("%-4d %-26s 行区域越界/过小，跳过" % (i, repr(m["name"])[:26]))
                continue
            img = ImageGrab.grab(bbox=(cx1, cy1, cx2, cy2), all_screens=True)
            shot_path = os.path.join(shotdir, "%s_msg%02d.png" % (ts, i))
            img.save(shot_path)

            an = analyze_line(img, thresh=args.thresh)
            if an is None or an.get("empty"):
                rec("%-4d %-26s %-28s 无内容像素" % (i, repr(m["name"])[:26], m["cls"].replace("mmui::", "")))
                continue

            lr = an["left_ratio"]
            gr = an.get("green_ratio", 0.0)
            # 主判据：内容像素的左右分布。实测区分度极大（对方 0.94~1.00，自己 0.00），
            #   且不依赖颜色，是最可靠的一条。
            # 兜底判据：绿色气泡占比。注意对方头像里也可能有绿色像素
            #   （实测张三头像贡献了稳定的 1141 绿像素 ≈ 0.0140），
            #   所以阈值取 0.03（自己实测 0.0751），仅在左右分布不明确时才用。
            if lr > 0.75:
                verdict = "对方 (靠左)"
            elif lr < 0.25:
                verdict = "自己 (靠右)"
            elif gr > 0.03:
                verdict = "自己 (绿气泡兜底)"
            else:
                verdict = "?? 不确定"

            rec("%-4d %-26s %-28s %-9.2f %-9.2f %-9.2f %-9.4f %s" % (
                i, repr(m["name"])[:26], m["cls"].replace("mmui::", ""),
                lr, 1 - lr, an["center_ratio"], gr, verdict))
            results.append((i, m, an, verdict, shot_path))

        rec("")
        rec("=" * 84)
        rec("明细（用于核对判定依据）")
        rec("=" * 84)
        for i, m, an, verdict, path in results:
            rec("消息 %d: %r" % (i, m["name"][:40]))
            rec("    行 rect     : %s  尺寸 %dx%d" % (m["rect"], an["w"], an["h"]))
            rec("    估计背景色   : %s" % (an["bg"],))
            rec("    内容像素数   : %d (左 %d / 右 %d)" % (an["total"], an["left_cnt"], an["right_cnt"]))
            rec("    绿色像素数   : %d  占比 %.4f  （微信自己的气泡特征色）" % (
                an.get("green_cnt", 0), an.get("green_ratio", 0.0)))
            rec("    内容水平范围 : %s  中心 x=%.0f (行宽 %d)" % (an["content_span"], an["center_x"], an["w"]))
            rec("    行内主要颜色 : %s" % ", ".join("%s×%d" % (c, n) for c, n in an["top_colors"]))
            rec("    截图         : %s" % path)
            rec("")

        # 汇总
        if results:
            sides = [v for _, _, _, v, _ in results]
            rec("汇总: %s" % "  ".join("%d=%s" % (i, v.split()[0]) for i, _, _, v, _ in results))
            if len(set(sides)) == 1:
                rec("")
                rec("提示：本次所有消息都判定为同一个方向。若想验证「自己发的消息」，")
                rec("      请切到一个你最近也回过话的会话再跑一次。")

    finally:
        if topmost:
            for _ in range(3):  # 多试几次，确保一定恢复
                try:
                    win32gui.SetWindowPos(
                        hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                    )
                    break
                except Exception:
                    import time as _t

                    _t.sleep(0.2)
            rec("")
            rec("[已恢复] 微信窗口层级还原为非置顶")

    path = os.path.join(outdir, "sender_%s.txt" % ts)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(buf))
    rec("[已保存] %s" % path)


if __name__ == "__main__":
    main()
