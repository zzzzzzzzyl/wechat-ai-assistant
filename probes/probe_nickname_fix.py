# -*- coding: utf-8 -*-
"""在**真实屏幕上**对照「旧昵称逻辑」和「新昵称逻辑」的结果。

这是修完之后唯一算数的验证：合成像素只能证明判据按设计工作，
能不能拦住真实数据里的 `灵感出炉` / `新民主主义革命基本思想` 得看这里。

做法：读当前群聊 → 对每条消息，分别用旧逻辑（不给判据）和新逻辑
     （判据①位置 + ②底色）各挑一次昵称 → 并排打出来。

⚠️ 判据③（昵称表）是**跨消息累积**的，单次快照看不出效果，
   所以这里不套用它，只用①②。真机上它是最后一道闸。

纯只读。用法：
    python probes/probe_nickname_fix.py            # 当前会话（群聊）
    python probes/probe_nickname_fix.py --text     # 连非占位符消息也一起看
"""

import ctypes
import sys

sys.path.insert(0, ".")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402


class _DropText:
    """包一层 OCR，把指定文本的识别结果丢掉 —— 用来模拟「真昵称没被读出来」。"""

    def __init__(self, inner, drop):
        self.inner = inner
        self.drop = drop

    def recognize(self, img, min_score=0.5):
        return [x for x in self.inner.recognize(img, min_score)
                if (x["text"] or "").strip() != self.drop]


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    import win32gui
    from wechat_assistant.capture import capture_window, crop_window_region
    from wechat_assistant.config import load_config
    from wechat_assistant.reader import WeChatReader
    from wechat_assistant.sender import (SenderResolver, content_blocks,
                                         content_top_for, estimate_bg)
    from wechat_assistant.uia_worker import UiaWorker

    show_text = "--text" in sys.argv

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

    print("会话：%s   群聊=%s   消息 %d 条" % (snap.title or "?", snap.is_group,
                                              len(snap.messages)))
    if not snap.is_group:
        print("\n⚠️ 不是群聊 —— 昵称 OCR 只在群聊里跑。请切到群里再跑。")
        return 2

    from wechat_assistant.ocr import get_engine
    eng = get_engine()
    eng.load()

    res = SenderResolver(cfg, ocr=eng, logger=lambda m: None)
    wrect = win32gui.GetWindowRect(hwnd)
    winimg = capture_window(hwnd)
    if winimg is None:
        print("capture_window 失败")
        return 3
    crect = snap.container_rect

    def grab(box):
        return crop_window_region(winimg, wrect, box)

    print("门槛 row_h >= %d，取上 %.0f%%\n" % (res.min_row_height,
                                              res.top_nick_ratio * 100))
    print("=" * 80)
    diff = same = mig = 0
    for m in snap.messages:
        t = (m.text or "").strip()
        if not t or not m.rect:
            continue
        if not show_text and t not in ("图片", "动画表情", "视频", "文件"):
            continue
        x1, y1, x2, y2 = m.rect
        row_h = y2 - y1
        if row_h < res.min_row_height:
            continue
        gx1 = max(0, x1, crect[0] if crect else 0)
        gy1 = max(0, y1, crect[1] if crect else 0)
        gx2 = min(winimg.width + wrect[0], x2, crect[2] if crect else x2)
        gy2 = min(winimg.height + wrect[1], y2, crect[3] if crect else y2)

        full = grab((gx1, gy1, gx2, gy2))
        crop_h = max(12, int((gy2 - gy1) * res.top_nick_ratio))
        top = grab((gx1, gy1, gx2, gy1 + crop_h))
        if full is None or top is None:
            continue

        arr = np.asarray(full.convert("RGB")).astype(np.int16)
        bg = estimate_bg(arr)
        blocks = content_blocks(arr, bg)

        # --wide：不只裁上 55%，把**整行**都喂给 OCR。
        # 真实翻车的根因是「真昵称是小灰字、OCR 漏了它，图里的字又大又黑读出来了」；
        # 整行 OCR 会让图里的字大量进入候选池，正好复现这个候选分布。
        top_for_ocr = full if "--wide" in sys.argv else top

        old = res._pick_nickname(top_for_ocr, t)                    # 旧：不给判据
        new = res._pick_nickname(top_for_ocr, t, arr, bg, blocks)   # 新：判据①②

        if "--dump" in sys.argv:
            import os
            d = os.path.join("out", "nickname_fix")
            os.makedirs(d, exist_ok=True)
            full.save(os.path.join(d, "row_%s_%d.png" % (t[:2], gy1)))
            top.save(os.path.join(d, "top_%s_%d.png" % (t[:2], gy1)))
            print("   存图 out/nickname_fix/row_%s_%d.png  rect=%s 钳后=%s"
                  % (t[:2], gy1, m.rect, (gx1, gy1, gx2, gy2)))

        tag = "同" if old == new else "**变了**"
        if old == new:
            same += 1
        else:
            diff += 1
        print("【%-6s】行高=%-4d 内容块=%s   %s"
              % (t[:6], row_h, str(blocks[:3]), tag))
        print("     旧 -> %r" % old)
        print("     新 -> %r" % new)

        # --sim-miss：模拟「OCR 漏掉了真昵称」——把新判据认可的那个候选删掉，
        # 再各跑一次。真实翻车就是这个状态（昵称是小灰字，识别率低，
        # 而图里的字又大又黑稳稳读出来）。
        # ⚠️ 必须包一层 OCR，不能往真引擎上塞属性 —— OcrEngine 没有 blocks，
        #    那样写等于什么都没做（第一版就这么白跑了一轮）。
        if "--sim-miss" in sys.argv and new:
            keep_eng = res.ocr
            res.ocr = _DropText(keep_eng, new)
            old2 = res._pick_nickname(top_for_ocr, t)
            new2 = res._pick_nickname(top_for_ocr, t, arr, bg, blocks)
            res.ocr = keep_eng
            print("     --- 模拟「OCR 漏掉真昵称 %r」之后 ---" % new)
            print("     旧 -> %r   %s" % (old2, "⚠️ 图里的字被当成昵称" if old2 else ""))
            print("     新 -> %r   %s" % (new2, "✅ 拦住了" if not new2 else ""))
            if old2 and not new2:
                mig += 1
        print()

    print("=" * 80)
    print("这一屏：%d 条结果不变，%d 条被新判据改掉" % (same, diff))
    if "--sim-miss" in sys.argv:
        print("模拟「漏掉真昵称」之后：%d 条被新判据救回来（旧会给图里的字）" % mig)
    print()
    print("怎么读：")
    print("  · 旧能挑到真昵称、新也挑到   -> 判据没误伤")
    print("  · 旧挑到图里的字、新返回空   -> ✅ 正是要修的那类")
    print("  · 旧能挑到、新却挑不到       -> ⚠️ 误伤，得回头调阈值")
    return 0


if __name__ == "__main__":
    sys.exit(main())
