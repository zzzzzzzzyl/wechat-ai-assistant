# -*- coding: utf-8 -*-
"""纯 OCR 读屏：可行性 + 成本 + **几何标定**。

    python probes/probe_ocr_read.py --image out/printwindow_flags2.png
    python probes/probe_ocr_read.py                  # 抓当前微信窗口
    python probes/probe_ocr_read.py --calibrate      # 标定聊天区几何
    python probes/probe_ocr_read.py --ascii          # 打字符画看布局

它回答三个问题（这三条决定了「能不能不靠 UIA」）：

  ① 聊天区能不能**只靠像素**框出来
  ② 消息行能不能切开、发送者能不能判出来
  ③ **一屏 OCR 要多久** —— 这一条直接决定「后台连续记录」可不可行

⚠️ 纯只读。抓图走 `capture.capture_window()`（PrintWindow），
   不置顶、不点击、不改变微信任何状态。

⚠️ 它和产品代码**共用** `wechat_assistant/ocr_reader.py`，
   不在这里重写一份算法 —— 否则量出来的数不代表真实跑的那份。
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ⚠️⚠️ **必须和 `run_watch.py` 一样，在 import Qt / 建窗口之前把 DPI 感知设好。**
#
#    不设的话 `dpi.is_aware()` 是 False，`OcrReader._scale()` 返回 **1.0** ——
#    而这个进程量出来的位图是**物理像素**（本机 1.5 倍屏）。
#    于是配置里 `chat_left: 310`（逻辑）被当成 310 **物理**用，
#    裁剪区整个**偏左切进会话列表**：实测在那个 804 宽的窗口上，
#    读出来的是「中科.. 19:18 光放长远，」这种**会话列表的预览文字**，
#    而看起来和"OCR 认不准"一模一样。
#
#    ⚠️ 这个坑很隐蔽：`--image` 离线模式我手动带 `--scale 1.5`，就把问题掩盖了；
#       只有**实时抓图**这一条路会真的踩到（而且量出来的耗时也偏小，
#       因为裁错了区域、面积小）。判据是打印里的 `DPI 缩放` 那一行。
from wechat_assistant import dpi as _dpi  # noqa: E402

print("DPI 感知: %s" % _dpi.enable())

import numpy as np  # noqa: E402

from wechat_assistant import capture  # noqa: E402
from wechat_assistant import ocr_reader as R  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.reader import list_windows, pick_window  # noqa: E402


def get_image(cfg, args):
    """返回 (PIL.Image, 说明文字)。"""
    if args.image:
        from PIL import Image

        return Image.open(args.image).convert("RGB"), "离线文件"
    cands = list_windows(cfg)
    print("-- 候选窗口 --")
    for c in cands:
        r = c["rect"]
        print("   hwnd=0x%X title=%r %dx%d"
              % (c["hwnd"], c["title"], r[2] - r[0], r[3] - r[1]))
    win = pick_window(cands, cfg)
    if win is None:
        print("   **挑不出主窗口** —— 这正是 reader.pick_window 要解决的问题")
        return None, ""
    print("   选中：title=%r hwnd=0x%X（判据：标题 == %r）"
          % (win["title"], win["hwnd"], cfg.get("wechat.window_title", "微信")))
    img = capture.capture_window(win["hwnd"])
    if img is None or capture.is_blank(img):
        print("\n   **PrintWindow 抓到空白**")
        print("   ⚠️ 最常见的原因：微信停在**扫码登录窗** —— 那个窗口也叫「微信」、")
        print("      约 300x400、PrintWindow 全黑。先确认微信已登录且聊天窗正常。")
        print("      自检：python probes/check_capture_selftest.py")
        return None, ""
    return img, "实时抓图"


def calibrate(cfg, img):
    """标定聊天区几何，打印可以直接抄进 config.yaml 的值。

    这不是"猜"：三条边界各有一个**结构性**判据 ——
      · 左边界：会话列表那一列的底色**不是聊天区底色**，聊天区那一列是
      · 上边界：标题栏下面第一段"整行都是聊天区底色"的地方
      · 下边界：输入区上面那条**横贯整宽的分隔线**
    """
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    H, W, _ = a.shape
    sc = scale_of(img)
    print("\n== 标定 ==  位图 %dx%d  物理/逻辑 = %.2f" % (W, H, sc))

    # 聊天区底色：用**众数**，而且只在"配置的聊天区"那一片里统计。
    #
    # ⚠️ 别拿最右边几列去平均 —— 那儿经常是**滚动条 / 窗口边框**，
    #    实测抓到过 [84,84,84] 这种明显偏亮的颜色，于是"和底色接近"的判据
    #    整列都不成立、`detect_chat_left` 直接返回 None（第一次就是这么错的）。
    #    众数对"聊天区大部分是底色、小部分是气泡"这个事实最稳。
    bg = R.OcrReader(cfg)._bg(a, int(W * 0.2), W, int(H * 0.15))
    if bg is None:
        bg = a[H // 6: H * 5 // 6, -3:].reshape(-1, 3).mean(axis=0)
    print("   聊天区底色（众数）= %s" % bg.round(1).tolist())

    # ---- 左边界：逐列看"这一列有多大比例等于聊天区底色"
    diff = np.abs(a - bg).sum(axis=2)
    close = (diff <= 30).mean(axis=0)
    print("\n   逐列底色占比的跳变（只列 x>8%% 宽度的）—— **分界就在第一次由低变高**：")
    prev = None
    shown = 0
    for x in range(int(W * 0.08), W, 2):
        tag = "底色" if close[x] >= 0.9 else "内容"
        if tag != prev and shown < 12:
            print("      x=%4d  %-4s  占比 %.2f  该列均色 %s"
                  % (x, tag, close[x], np.round(a[H // 6:H * 5 // 6, x].mean(axis=0), 0).tolist()))
            prev = tag
            shown += 1

    det = R.detect_chat_left(a, bg)
    cfg_left = int(round(float(cfg.get("ocr_read.chat_left", 310)) * sc))
    print("\n   detect_chat_left  = %s  （= %.0f 逻辑像素）"
          % (det, det / sc if det else -1))
    print("   配置 chat_left     = %d  （= %.0f 逻辑像素）" % (cfg_left, cfg_left / sc))
    if det and abs(det - cfg_left) > 20:
        print("   ⚠️ 差 %d 像素（%.0f 逻辑）—— **建议把 ocr_read.chat_left 改成 %.0f**"
              % (abs(det - cfg_left), abs(det - cfg_left) / sc, det / sc))
    else:
        print("   ✅ 两者一致（差 %s 像素以内）"
              % (abs(det - cfg_left) if det else "?"))

    # ---- 下边界：输入区分隔线
    it = R.find_input_top(a, bg, max(0, (det or cfg_left)), W, int(H * 0.4))
    fb = int(round(float(cfg.get("ocr_read.input_fallback", 150)) * sc))
    print("\n   输入区分隔线：检测到 y=%s" % it)
    print("     兜底值 = H - %d 物理(%d 逻辑) = y=%d" % (fb, fb / sc, H - fb))
    if it:
        print("     → 从底部扣 **%.0f 逻辑像素**（= input_fallback）" % ((H - it) / sc))
    else:
        print("     → 检测不到，只能靠 input_fallback。**它太大会切掉消息**，"
              "太小会把输入框里的字当消息 —— 实测取两者更靠上的那个")

    # ---- 上边界：正文开始的第一行
    x1 = det or cfg_left
    diff2 = np.abs(a[:, x1:W] - bg).sum(axis=2)
    differ = (diff2 > 18).mean(axis=1)
    top = None
    for y in range(1, int(H * 0.35)):
        if differ[y] > 0.02:
            top = y
            break
    print("\n   正文出现的第一行 y = %s（= %.0f 逻辑）"
          % (top, (top or 0) / sc))
    print("   ⚠️ 它不是 chat_top —— 标题本身就在标题栏里、也在正文之上。")
    print("      标题栏高度用 config 的 chat_top（默认 70 逻辑 = %d 物理），"
          "本次实际用 %d" % (int(70 * sc), int(round(float(cfg.get('ocr_read.chat_top', 70)) * sc))))


def scale_of(img) -> float:
    try:
        from wechat_assistant import dpi
        if not dpi.is_aware():
            return 1.0
        return max(1.0, min(4.0, float(dpi.current_dpi()) / 96.0))
    except Exception:
        return 1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=None, help="离线：拿存好的窗口截图跑")
    ap.add_argument("--scale", type=float, default=None,
                    help="离线时的手动 DPI 缩放（物理/逻辑）。"
                         "⚠️ 离线文件没有「进程 DPI」可问，不指定就按 1.0 算 —— "
                         "而本机是 1.5，按 1.0 算会把聊天区切进会话列表里。")
    ap.add_argument("--ascii", action="store_true", help="打一张字符画")
    ap.add_argument("--calibrate", action="store_true", help="标定几何并打印配置值")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    img, how = get_image(cfg, args)
    if img is None:
        return 1
    W, H = img.width, img.height
    sc = args.scale if args.scale else scale_of(img)
    print("\n-- 图像 --  %s  %dx%d  DPI 缩放 %.2f%s"
          % (how, W, H, sc, "（--scale 指定）" if args.scale else ""))

    if args.calibrate:
        calibrate(cfg, img)

    # ---- 从这里开始：**直接跑产品代码**，不再自己拼一遍 ----
    #
    # ⚠️ 这是这一轮改掉的一个真问题：探针原来自己算 `chat_rect` + 自己调
    #    `group_blocks` + 自己判左右，**和产品走的是两套逻辑** ——
    #    于是它报出来的几何是配置值（没用上自动检测）、切行用的是**兜底路径**
    #    （不是气泡盒），量出来的结果和真跑起来**不是一回事**。
    #    探针的价值在于"量的就是产品跑的那份"，所以现在统一走 `rd.read_chat()`。
    rd = R.OcrReader(cfg)
    rd.grab = lambda _hwnd: img          # 线上/离线统一入口
    if args.scale:
        rd._scale = lambda: args.scale

    a = np.asarray(img.convert("RGB")).astype(np.int16)
    box = R.chat_rect((0, 0, W, H), sc, cfg, (W, H))
    bg = rd._bg(a, box[0], box[2], box[1])
    det = R.detect_chat_left(a, bg) if bg is not None else None
    print("\n-- 几何 --")
    print("   配置 chat_left = %d 物理（%.0f 逻辑）" % (box[0], box[0] / sc))
    print("   自动检测       = %s 物理%s"
          % (det, "" if det is None else "（%.0f 逻辑）" % (det / sc)))
    print("   候选顺序 = %s  ← 检测优先、配置兜底"
          % list(rd._geometry_candidates(a, bg, box[0])))

    # ⚠️ **必须先把引擎加载并预热，再开始计时**。
    #    第一版把 `eng.load()`（读 15MB 模型 + 建 onnxruntime session，0.8~1.6 秒）
    #    算进了"一次读屏耗时"，量出来 1714ms —— 而稳态其实是 ~250ms，
    #    **差了 6 倍**，差点因此把「后台慢速记录」这个选项整个否掉。
    print("\n-- 预热引擎（不计时）--")
    t0 = time.perf_counter()
    rd._engine().load()
    rd._engine().warmup()
    print("   引擎就绪 %.0f ms" % ((time.perf_counter() - t0) * 1000))

    t0 = time.perf_counter()
    snap = rd.read_chat(0)
    t_all = (time.perf_counter() - t0) * 1000
    if snap is None:
        print("\n**read_chat 返回 None** —— 几何不对（_geom_bad=%s）" % rd._geom_bad)
        return 1

    crop = rd.last_crop
    print("\n-- 实际用的裁剪框 -- %s   = %dx%d"
          % (crop, crop[2] - crop[0], crop[3] - crop[1]))

    if args.ascii:
        import subprocess
        tmp = os.path.join(os.environ.get("TEMP", "."), "probe_ocr_chat.png")
        img.crop(crop).save(tmp)
        print("   已存 %s" % tmp)
        subprocess.run([sys.executable,
                        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "ascii_shot.py"), tmp, "110"])

    print("\n-- 结果 --")
    print("   标题（会话主键）= %r" % snap.title)
    print("   群聊 = %s   消息 %d 条   时间分隔 %d 个"
          % (snap.is_group, len(snap.messages), len(snap.markers)))
    for m in snap.messages:
        print("   [%s] y=%-4s x=%s..%-5s %s%s"
              % ("我" if m.is_self else "对方",
                 (m.rect or (0, 0, 0, 0))[1],
                 (m.rect or (0, 0, 0, 0))[0], (m.rect or (0, 0, 0, 0))[2],
                 ("(%s) " % m.sender_name) if m.sender_name else "",
                 m.text[:70]))
    for k in snap.markers:
        print("   [时间] y=%-4s %s" % (k.rect[1] if k.rect else "?", k.text))

    print("\n-- 成本 --")
    print("   整条路（抓图 + 几何 + 标题 OCR + 正文 OCR + 切行）= %.0f ms" % t_all)
    print("   （抓图已含在内；对照：UIA 一整拍约 100 ms）")
    for iv in (0.2, 1.0, 5.0):
        print("   若每 %.1fs 拍一次 -> 常驻占用一个核的 %.0f%%"
              % (iv, t_all / (iv * 1000) * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main())
