# -*- coding: utf-8 -*-
"""同一张图片消息，连续拍多次，看它的**哈希稳不稳**。

为什么必须验：
记录器快速档一拍 0.2s，同一张图会被反复读到。要不要重复 OCR，
取决于「按内容算的哈希能不能命中」。两种失效方式：

  ① **动画表情是 GIF** —— 每帧像素不同 -> 哈希每拍都变 -> 缓存永远不命中
     -> 每拍都 OCR 一次 -> CPU 烧死，而且贴纸通常**没有文字**，纯属白干。
  ② **裁剪区域抖动** —— 收紧用的 bbox 若在两拍之间漂移 1~2px，
     哈希跟着变 -> 同样不命中。

做法：对每个占位符条目，隔一小段时间连拍 N 次，
      每次重新钳制/收紧/算 dHash，统计「不同哈希数」和「bbox 是否稳定」。

dHash：缩到 9x8 灰度，比较相邻像素 -> 64 bit。对 1~2px 抖动不敏感，
       对真实内容变化敏感。

纯只读。
"""

import ctypes
import sys
import time

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PLACEHOLDERS = ("图片", "动画表情", "视频", "文件")


def dhash(img, size=8):
    """差值哈希，返回 64 bit 整数。"""
    g = img.convert("L").resize((size + 1, size))
    px = list(g.getdata())
    bits = 0
    for y in range(size):
        for x in range(size):
            left = px[y * (size + 1) + x]
            right = px[y * (size + 1) + x + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


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

    sys.path.insert(0, "probes")
    from demo_image_ocr import clamp_to_container, largest_blob_bbox

    n_shot = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    gap = float(sys.argv[2]) if len(sys.argv) > 2 else 0.35

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
    print("会话：%s" % (snap.title or "?"))

    targets = [(i, m) for i, m in enumerate(snap.messages)
               if (m.text or "").strip() in PLACEHOLDERS and m.rect]
    if not targets:
        print("这一屏没有图片/表情")
        return 2

    print("占位符条目 %d 条，每条约 %d 次采样，间隔 %.2fs（共约 %.1fs）"
          % (len(targets), n_shot, gap, n_shot * gap))
    print()

    stats = {}
    for i, m in targets:
        stats[i] = {"text": (m.text or "").strip(), "hashes": [], "boxes": [],
                    "rects": set()}

    wrect = win32gui.GetWindowRect(hwnd)
    for k in range(n_shot):
        img = capture_window(hwnd)
        if img is None:
            print("capture_window 失败")
            return 3
        for i, m in targets:
            st = stats[i]
            # rect 每次都重新读（模拟真实：每拍都是新的快照）
            cur = None
            snap2 = reader.read_chat(hwnd)
            if snap2:
                cand = [x for x in snap2.messages
                        if (x.text or "").strip() in PLACEHOLDERS and x.rect]
                # 按出现顺序配对（同一屏内顺序稳定）
                order = [j for j, mm in targets]
                pos = order.index(i)
                if pos < len(cand):
                    cur = cand[pos].rect
            if cur:
                st["rects"].add(cur)
            rect = clamp_to_container(cur or m.rect, snap2.container_rect if snap2 else None)
            if not rect:
                st["hashes"].append(None)
                st["boxes"].append(None)
                continue
            crop = crop_window_region(img, wrect, rect)
            if crop is None:
                st["hashes"].append(None)
                st["boxes"].append(None)
                continue
            box = largest_blob_bbox(crop)
            st["boxes"].append(box)
            st["hashes"].append(dhash(crop.crop(box) if box else crop))
        time.sleep(gap)

    print("=" * 72)
    for i, st in stats.items():
        hs = [h for h in st["hashes"] if h is not None]
        uniq = set(hs)
        print("【%s】采样 %d 次，有效 %d" % (st["text"], len(st["hashes"]), len(hs)))
        print("   不同 dHash 个数：%d  %s"
              % (len(uniq),
                 "-> 稳定，缓存能命中 ✅" if len(uniq) <= 1 else
                 ("-> 轻微抖动" if len(uniq) <= max(2, len(hs) // 5) else
                  "-> ⚠️ 一直在变，缓存基本不命中")))
        if len(hs) > 1:
            base = hs[0]
            ds = [hamming(base, h) for h in hs[1:]]
            print("   与首次的汉明距离：min %d / max %d（0=完全相同，64=完全无关）"
                  % (min(ds), max(ds)))
        print("   收紧 bbox 出现的种数：%d %s"
              % (len(set(b for b in st["boxes"] if b)),
                 "" if len(set(b for b in st["boxes"] if b)) <= 2 else "  ⚠️ bbox 在漂"))
        print("   消息 rect 出现的种数：%d %s"
              % (len(st["rects"]),
                 "" if len(st["rects"]) <= 2 else "  （说明滚动/内容在变）"))
        print()

    print("=" * 72)
    print("结论怎么读：")
    print("  · 「不同 dHash 个数 = 1」-> 用 phash 缓存就够了，同一张图只 OCR 一次")
    print("  · 「一直在变」    -> 那是 GIF 动画帧，按内容缓存**救不了**，")
    print("                        必须按「位置」或「直接跳过动画表情」来处理")
    return 0


if __name__ == "__main__":
    sys.exit(main())
