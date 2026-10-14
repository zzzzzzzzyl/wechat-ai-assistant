# -*- coding: utf-8 -*-
"""演示：图片 OCR 接上之后，喂给 AI 的上下文会长成什么样。

**完全只读**，不改生产代码、不写库、不调 AI。

做法：
  1. 读当前聊天屏（或缝合后的历史，加 --stitch）
  2. 找出所有占位符条目（图片 / 动画表情）
  3. 裁图 -> 收紧到最大连通域 -> 放大到长边 ~736 -> OCR
  4. 并排打印「现在的样子」和「接上 OCR 之后的样子」

用法：
    python probes/demo_image_ocr.py            # 只看当前屏
    python probes/demo_image_ocr.py --stitch   # 用缝合后的上下文（更接近真实 prompt）
    python probes/demo_image_ocr.py --all      # 连同没有文字的结果也打出来
"""

import ctypes
import sys
import time

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PLACEHOLDERS = ("图片", "动画表情", "视频", "文件")

MAX_SIDE = 736           # 与 ocr.py 的 limit_side_len 保持一致


# ----------------------------------------------------------------- 裁图

def largest_blob_bbox(img, tol=30, pad=4):
    """非背景像素里面积最大的连通域的外接矩形（排除头像 / 群昵称）。"""
    import numpy as np
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    h, w, _ = a.shape
    edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    colors, counts = np.unique(edge.reshape(-1, 3), axis=0, return_counts=True)
    bg = colors[counts.argmax()]
    mask = (np.abs(a - bg).sum(axis=2) > tol).astype(np.uint8)
    if mask.sum() == 0:
        return None
    try:
        import cv2
        n, _labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        if n <= 1:
            return None
        idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        x = int(stats[idx, cv2.CC_STAT_LEFT])
        y = int(stats[idx, cv2.CC_STAT_TOP])
        bw = int(stats[idx, cv2.CC_STAT_WIDTH])
        bh = int(stats[idx, cv2.CC_STAT_HEIGHT])
    except ImportError:
        cols = mask.sum(axis=0) > 0
        best, i = (0, 0), 0
        while i < len(cols):
            if cols[i]:
                j = i
                while j < len(cols) and cols[j]:
                    j += 1
                if j - i > best[1] - best[0]:
                    best = (i, j)
                i = j
            else:
                i += 1
        if best[1] - best[0] == 0:
            return None
        sub = mask[:, best[0]:best[1]]
        rows = np.where(sub.sum(axis=1) > 0)[0]
        x, y = best[0], int(rows.min())
        bw, bh = best[1] - best[0], int(rows.max() - rows.min() + 1)
    return (max(0, x - pad), max(0, y - pad),
            min(w, x + bw + pad), min(h, y + bh + pad))


def prepare(img):
    """rect 裁出来的图 -> 送进 OCR 的图。返回 (图, 说明)。"""
    from PIL import Image

    before = "%dx%d" % (img.width, img.height)
    box = largest_blob_bbox(img)
    note = ""
    if box:
        tight = img.crop(box)
        note = "收紧 %s -> %dx%d" % (before, tight.width, tight.height)
    else:
        tight = img
        note = "收紧失败，用原图"

    # ⚠️ 只放大，绝不缩小（实测 50% 会静默归零，且放大救不回来）
    if max(tight.width, tight.height) < MAX_SIDE:
        k = MAX_SIDE / float(max(tight.width, tight.height))
        nw, nh = int(tight.width * k), int(tight.height * k)
        tight = tight.resize((nw, nh), Image.BICUBIC)
        note += "，放大到 %dx%d" % (nw, nh)
    return tight, note


def ocr_blocks(img):
    from wechat_assistant.ocr import get_engine
    eng = get_engine()
    eng.load()
    t0 = time.perf_counter()
    res = eng.recognize(img)
    return res, (time.perf_counter() - t0) * 1000


def has_cjk(s):
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def join_blocks(res, keep=8):
    """把 OCR 块拼成一行像句子的话。

    ⚠️ 朴素的「按坐标取前 N 块」效果很差（实测）：手机截图里有大量
    英文/数字小字（App 的按钮、版本号、英文水印），OCR 会返回一堆
    `val` / `Fy` / `tive` / `URGNa` 这种碎片，而且**置信度还不低**，
    于是把真正有用的中文挤掉了。

    规则：带中文的块优先，纯 ASCII 的短碎片（<4 字符或全是大写乱串）丢掉。
    """
    rows = []
    for r in res:
        t = r["text"].strip()
        if len(t) < 2:
            continue
        cjk = has_cjk(t)
        if not cjk:
            # 纯 ASCII：只留像样的（长度够、有空格或数字），否则基本是噪声
            letters = sum(1 for c in t if c.isalpha())
            if len(t) < 4 or (letters >= 3 and t.isalpha() and not t.islower()):
                continue
        if cjk and r["score"] < 0.45:
            continue
        if not cjk and r["score"] < 0.6:
            continue
        rows.append((0 if cjk else 1, r["y1"], r["x1"], t))
    rows.sort()
    return " ".join(t for _c, _y, _x, t in rows[:keep]).strip()


# ----------------------------------------------------------------- 主流程

def collect_screens(reader, cfg, hwnd, use_stitch):
    """返回 (会话名, [(text, rect, who)], 消息容器 rect)"""
    if not use_stitch:
        snap = reader.read_chat(hwnd)
        if snap is None:
            return None, [], None
        return (snap.title, [(m.text, m.rect, m.who) for m in snap.messages],
                snap.container_rect)

    # 走 ScreenStore 缝一遍（和真实上下文同一条路径）
    from wechat_assistant.window import ScreenStore
    path = cfg.get("history.screens_path", "state/screens.db")
    store = ScreenStore(path)
    snap = reader.read_chat(hwnd)
    title = snap.title if snap else ""
    crect = snap.container_rect if snap else None
    items = store.stitch(title)
    # ⚠️ 屏快照库里**只存了文字，没有 rect**（见 window.items_from_snapshot：
    #    rect 只用来排序，入库存的是 {text,is_self,sender,kind}）。
    #    所以缝合出来的历史条目**取不到像素**，做不了 OCR。
    return title, [(it.get("text", ""), None, it.get("who", "")) for it in items], crect


def clamp_to_container(rect, crect):
    """把消息 rect 钳进消息容器范围内。

    ⚠️ **必须做**（实测）：UIA 给部分滚出可见区的消息的 bounding rect 会**越界**，
    实测一张贴纸的 rect 往上跑进了标题栏，裁出来的图是
    「窗口按钮 + 张三 + 贴纸顶部」，OCR 于是把**会话标题「张三」**
    当成了图内文字。
    `sender.py` 判发送者时早就做了同样的钳制，这里照抄。
    """
    if not rect:
        return None
    if not crect:
        return rect
    x1 = max(rect[0], crect[0])
    y1 = max(rect[1], crect[1])
    x2 = min(rect[2], crect[2])
    y2 = min(rect[3], crect[3])
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    return (x1, y1, x2, y2)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    import win32gui
    from wechat_assistant.capture import capture_window, crop_window_region
    from wechat_assistant.config import load_config
    from wechat_assistant.reader import WeChatReader
    from wechat_assistant.uia_worker import UiaWorker

    use_stitch = "--stitch" in sys.argv
    show_all = "--all" in sys.argv

    cfg = load_config()
    worker = UiaWorker(20.0)
    reader = WeChatReader(cfg, worker)
    win = reader.find_window()
    if not win:
        print("没找到微信窗口")
        return 1
    hwnd = win["hwnd"]

    title, norm, crect = collect_screens(reader, cfg, hwnd, use_stitch)
    if norm is None:
        print("读聊天失败")
        return 1

    print("=" * 74)
    print("会话：%s   来源：%s   共 %d 条"
          % (title or "?", "缝合后的上下文" if use_stitch else "当前屏", len(norm)))
    ph = [(i, t) for i, (t, _r, _w) in enumerate(norm) if (t or "").strip() in PLACEHOLDERS]
    print("其中占位符条目：%d 条" % len(ph))
    print("=" * 74)

    if not ph:
        print("这一屏没有图片/表情。换一屏，或加 --stitch 看缝出来的历史。")
        return 2

    wrect = win32gui.GetWindowRect(hwnd)
    winimg = capture_window(hwnd)
    if winimg is None:
        print("capture_window 失败")
        return 3

    # ⚠️ 只读一次屏。之前这里又读了一次拿 rect，两次之间屏幕一变就错位
    #    （实测把群昵称识别成了图内文字）。rect 直接取同一份快照里的。
    results = {}
    no_rect = 0
    for i, t in ph:
        raw = norm[i][1]
        if not raw:
            results[i] = (None, "没有 rect（缝合出来的历史条目）")
            no_rect += 1
            continue
        rect = clamp_to_container(raw, crect)
        if not rect:
            results[i] = (None, "钳制后没有有效区域")
            continue
        clamped = "（已钳到容器内，原 rect %s）" % (raw,) if rect != raw else ""
        crop = crop_window_region(winimg, wrect, rect)
        if crop is None:
            results[i] = (None, "裁剪越界")
            continue
        prep, note = prepare(crop)
        res, ms = ocr_blocks(prep)
        text = join_blocks(res)
        results[i] = ((text, res, note + clamped, ms, prep.size), note)
        if clamped:
            print("   %s 的 rect %s -> %s%s" % (t, raw, rect, clamped))

        # --dump：把 rect 裁下来的原图和送进 OCR 的图都存下来，
        #         出问题时可以直接看图定位（别再靠猜）
        if "--dump" in sys.argv:
            import os
            d = os.path.join("out", "demo_image_ocr")
            os.makedirs(d, exist_ok=True)
            crop.save(os.path.join(d, "%02d_rect.png" % i))
            prep.save(os.path.join(d, "%02d_prep.png" % i))
            print("   存图 out/demo_image_ocr/%02d_rect.png (%dx%d) 和 %02d_prep.png"
                  % (i, crop.width, crop.height, i))

    if no_rect:
        print("⚠️ 有 %d 条是缝合出来的历史条目，库里只存了文字、**没有 rect**，"
              % no_rect)
        print("   取不到像素，所以做不了 OCR —— 这也是实现时要解决的问题（见结尾）。")
        print()

    # ---------------------------------------------------------- 打印
    print("【现在 AI 看到的】")
    print("-" * 74)
    for i, (t, _r, w) in enumerate(norm):
        mark = "  <<< 占位符" if (t or "").strip() in PLACEHOLDERS else ""
        print("  %-4s %s%s" % (("我:" if w == "我" else (w or "?") + ":"),
                               (t or "").strip(), mark))

    print()
    print("=" * 74)
    print("【接上图片 OCR 之后 AI 看到的】")
    print("-" * 74)
    hit = 0
    for i, (t, _r, w) in enumerate(norm):
        s = (t or "").strip()
        if s not in PLACEHOLDERS:
            print("  %-6s %s" % (("我:" if w == "我" else (w or "?") + ":"), s))
            continue
        got = results.get(i)
        payload = got[0] if got else None
        if not payload:
            print("  %-6s %s" % (("我:" if w == "我" else (w or "?") + ":"), s))
            continue
        text, res, note, ms, size = payload
        if text:
            hit += 1
            print("  %-6s 【%s】图内文字：%s" % (
                ("我:" if w == "我" else (w or "?") + ":"), s, text))
            if show_all or len(sys.argv) == 1:
                print("         └─ %s，%d 块，%.0f ms" % (note, len(res), ms))
        else:
            print("  %-6s 【%s】（图内没有可识别的文字）" % (
                ("我:" if w == "我" else (w or "?") + ":"), s))
            if show_all:
                print("         └─ %s，%d 块，%.0f ms" % (note, len(res), ms))

    print()
    print("=" * 74)
    total = sum(1 for i, _t in ph if results.get(i) and results[i][0])
    ms_all = sum(results[i][0][3] for i, _t in ph if results.get(i) and results[i][0])
    print("占位符 %d 条，其中 %d 条 OCR 出了文字；OCR 总耗时 %.0f ms"
          % (len(ph), hit, ms_all))
    print("（这些都是本地算的，不联网、不花 token）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
