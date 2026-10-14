# -*- coding: utf-8 -*-
"""验：群聊里给**图片消息**读昵称，会不会把图里的字当成昵称。

风险链条（源码在 `sender.py` 的 resolve()）：
    row_h >= min_row_height(100)  →  裁这一行**上 55%**  →  OCR  →  取最靠上的短文本

那道「行高 ≥ 100」的门槛，本意是「只有第一行才带昵称」，但它**挡不住图片**：
实测图片 rect 高度 228 / 373 / 399，全部过门槛。而图片消息的 rect
**上面是昵称、下面是图**，所以那 55% 的条带里**混着图片内容**。

这条探针把「现在会读到什么」和「真正应该读到什么」并排打出来。
纯只读。用法：
    python probes/probe_nickname_vs_image.py            # 当前会话
    python probes/probe_nickname_vs_image.py --dump     # 顺带存图
"""

import ctypes
import os
import sys

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PLACEHOLDERS = ("图片", "动画表情", "视频", "文件")

sys.path.insert(0, "probes")


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    import win32gui
    from wechat_assistant.capture import capture_window, crop_window_region
    from wechat_assistant.config import load_config
    from wechat_assistant.reader import WeChatReader
    from wechat_assistant.sender import SenderResolver, looks_like_noise, _norm
    from wechat_assistant.uia_worker import UiaWorker

    from demo_image_ocr import largest_blob_bbox

    dump = "--dump" in sys.argv
    cfg = load_config()
    reader = WeChatReader(cfg, UiaWorker(20.0))
    win = reader.find_window()
    if not win:
        print("没找到微信窗口")
        return 1
    hwnd = win["hwnd"]

    snap = reader.read_chat(hwnd)
    if snap is None:
        print("读聊天失败")
        return 1

    print("当前会话：%s   群聊=%s   消息 %d 条"
          % (snap.title or "?", snap.is_group, len(snap.messages)))
    if not snap.is_group:
        print()
        print("⚠️ 这不是群聊 —— 昵称 OCR 只在群聊里跑。请切到一个**带图片的群聊**再跑本探针。")
        return 2

    res = SenderResolver(cfg, logger=lambda m: None)
    min_h = res.min_row_height
    ratio = res.top_nick_ratio

    from wechat_assistant.ocr import get_engine
    eng = get_engine()
    eng.load()

    wrect = win32gui.GetWindowRect(hwnd)
    winimg = capture_window(hwnd)
    if winimg is None:
        print("capture_window 失败")
        return 3

    crect = snap.container_rect
    outdir = os.path.join("out", "nickname_vs_image")
    if dump:
        os.makedirs(outdir, exist_ok=True)

    print("门槛 min_row_height = %d，取上 %.0f%%" % (min_h, ratio * 100))
    print()
    print("=" * 78)

    hits = []
    for i, m in enumerate(snap.messages):
        t = (m.text or "").strip()
        if t not in PLACEHOLDERS or not m.rect:
            continue
        x1, y1, x2, y2 = m.rect
        row_h = y2 - y1
        gx1 = max(0, x1, crect[0] if crect else 0)
        gy1 = max(0, y1, crect[1] if crect else 0)
        gx2 = min(winimg.width + wrect[0], x2, crect[2] if crect else x2)
        gy2 = min(winimg.height + wrect[1], y2, crect[3] if crect else y2)

        crop_h = max(12, int((gy2 - gy1) * ratio))
        would_ocr = row_h >= min_h
        print("【%s】rect=%s  行高=%d  %s"
              % (t, m.rect, row_h,
                 "过门槛 → **会做昵称 OCR**" if would_ocr else "不过门槛，跳过"))

        top = crop_window_region(winimg, wrect, (gx1, gy1, gx2, gy1 + crop_h + wrect[1] - wrect[1]))
        if top is None:
            print("   上 55% 条带裁不出来")
            continue
        full = crop_window_region(winimg, wrect, (gx1, gy1, gx2, gy2))
        box = largest_blob_bbox(full) if full else None
        print("   行内内容块（最大连通域）= %s" % (str(box),))
        if box:
            print("   → 图片本体的顶边在行内 y=%d，而昵称条带占到 y=%d"
                  % (box[1], crop_h))
            if box[1] < crop_h:
                print("   ⚠️ **条带和图片重叠了 %d px** —— 这部分是图片内容不是昵称"
                      % (crop_h - box[1]))

        items = eng.recognize(top)
        cands = []
        for it in items:
            tt = (it["text"] or "").strip()
            if not tt or len(tt) > 16:
                continue
            if _norm(tt) == _norm(m.text):
                continue
            if looks_like_noise(tt):
                continue
            cands.append((it["y1"], it["x1"], tt))
        cands.sort()
        print("   条带 OCR 出 %d 块，通过筛选的候选 %d 个" % (len(items), len(cands)))
        for c in cands[:4]:
            print("        y=%-4d x=%-4d %r" % c)
        picked = cands[0][2] if cands else "(无 → 沿用上一条昵称)"
        print("   >>> 现在这条会被判成：%r" % picked)
        if box and cands and cands[0][0] >= box[1]:
            print("   >>> ⚠️ 这个候选落在**图片区域里面**，是图里的字，不是昵称")
            hits.append((t, picked))
        elif box and cands:
            print("   >>> 候选在图片上方，看起来是真的昵称")
        print()

        if dump:
            full.save(os.path.join(outdir, "%02d_full.png" % i))
            top.save(os.path.join(outdir, "%02d_top55.png" % i))

    print("=" * 78)
    if hits:
        print("结论：**确认有问题** —— %d 条图片消息会把图里的字当成昵称：" % len(hits))
        for t, p in hits:
            print("   %s -> %r" % (t, p))
        print()
        print("⚠️ 而且 nick 一旦定下来会写进 last_nickname，")
        print("   下面那些「行高不够」的消息会**继承这个错名字**（sender.py 第 332 行）。")
    else:
        print("这一屏没有「图里的字被当昵称」的情况（可能是图里恰好没字）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
