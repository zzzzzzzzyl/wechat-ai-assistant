# -*- coding: utf-8 -*-
"""量「气泡盒里装的到底是什么」—— 给"图片 / 卡片 vs 文本"定判据用。

    python probes/measure_bubbles.py

⚠️ **纯只读**：抓一次微信窗口，把每个气泡盒的几何/像素特征打出来。
   不改任何产品代码。

为什么需要它（2026-10-11 用户的思路）：

    官方的判据是"只分**是不是纯文本气泡**"（不是多分类）——
    不是纯文本的（图片/表情/语音/文件/链接卡片）一律算"附带了文字的东西"，
    **内容保留、身份标注**（`[图片] 图里的字…`），而不是丢掉。

    而 OCR 后端**拿不到类名**（`mmui::ChatBubbleReferItemView` 是 UIA 才有的），
    所以判据只能从**几何 + 像素**里找。这个探针就是去量那几个候选判据。

量了什么（每一列都是一个候选判据）：

    宽高比 / 贴边距离   -> 「方块 + 贴边 + 近似正方形」= **头像候选**
    四角底色占比        -> **圆角 vs 直角**（微信文本气泡是圆角、图片和卡片是直角）
    盒内非底色占比      -> 文本气泡只有笔画（稀疏）；**图片铺满（接近 100%）**
    盒内唯一颜色数      -> 文本气泡 ~2 种（底色+字色）；照片几百上千种

⚠️ **不调 OCR**：这样探针快、依赖少，而"铺满 vs 稀疏"这条**不用认字就分得开**。
   以后要"文字面积占比"再加。

怎么用（一次摆好一屏，跑一次）：
    在微信里依次发（**最好让对面发**，才测得到头像那一侧）：

        【文本对照】普通一句话          <- 必须有对照组
        【纯图片】一张没字的照片
        【带字截图】一篇文章的截图
        【表情包】有透明通道的那种
        【语音】
        【文件】
        【链接卡片】公众号文章 / 小红书分享

    ⚠️ 每条前面**发一句文字标签**（比如「①纯图片」），
       输出里就能对号入座 —— 否则一堆盒子分不清哪个是哪个。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402

from wechat_assistant import dpi as _dpi  # noqa: E402

_dpi.enable()                     # ⚠️ 必须在抓图之前（否则坐标全是错的）

from wechat_assistant import capture  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402
from wechat_assistant.ocr import get_engine  # noqa: E402
from wechat_assistant.ocr_reader import (  # noqa: E402
    OcrReader, bubble_boxes, chat_rect)
from wechat_assistant.reader import list_windows, pick_window  # noqa: E402

#: 量四角用的方块边长（物理像素）。太小会被抗锯齿糊掉，太大会量到内部。
CORNER = 6


def corner_bg_frac(a, bg, box, side=CORNER):
    """四个角里有多少比例的像素**接近底色** —— 用来判圆角/直角。

    圆角矩形的角上是**底色**（图形没画到那儿）；直角矩形的角上是**它自己的颜色**。
    """
    x1, y1, x2, y2 = box
    h, w = a.shape[:2]
    patches = [(x1, y1), (x2 - side, y1), (x1, y2 - side), (x2 - side, y2 - side)]
    tot = near = 0
    for px, py in patches:
        px, py = max(0, px), max(0, py)
        sub = a[py:min(py + side, h), px:min(px + side, w)]
        if sub.size == 0:
            continue
        tot += sub.shape[0] * sub.shape[1]
        near += int((np.abs(sub.astype(np.int32)
                            - np.asarray(bg, dtype=np.int32)).sum(axis=2) <= 30).sum())
    return (float(near) / tot) if tot else 0.0



def _modal(a, q=8):
    """一块区域里的众数颜色（按 q 量化，和产品代码 `_bg` 同一套做法）。"""
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


def bg_diagnosis(ca, bg_used):
    """把「几种取法算出来的底色」并排打出来（查巨盒用）。"""
    h, w = ca.shape[:2]
    cands = [
        ("顶行 30px（探针现在用的）", ca[0:min(30, h)]),
        ("底部 30px", ca[max(0, h - 30):h]),
        ("左 30px 列", ca[:, 0:min(30, w)]),
        ("右 30px 列", ca[:, max(0, w - 30):w]),
        ("整块（**最可信**）", ca),
    ]
    print("\n== 背景诊断：聊天区底色到底是多少 ==")
    print("   %-26s %-18s %s" % ("取法", "众数颜色", "占比"))
    for name, region in cands:
        col, share = _modal(region)
        mark = ""
        if col is not None and bg_used is not None:
            d = sum(abs(int(a) - int(b)) for a, b in zip(col, bg_used))
            if d <= 8:
                mark = "  ← 和探针用的一致"
        print("   %-26s %-18s %5.1f%%%s"
              % (name, str(tuple(int(v) for v in col)), share * 100, mark))
    return cands


def two_level(sub, tol=45, min_blob=20):
    """在**盒子内部**用盒子自己的众数色当"局部底色"，看里面的块结构。

    返回 (有效块数, 最大块占盒子的比例, 局部众数色占比)。
    """
    from wechat_assistant.sender import _cv2
    cv2 = _cv2()
    fill, fill_share = _modal(sub)
    if cv2 is None or fill is None or sub.size == 0:
        return -1, 0.0, fill_share
    d = np.abs(sub.astype(np.int32) - np.asarray(fill, dtype=np.int32)).sum(axis=2)
    mask = (d > tol).astype(np.uint8)
    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(mask, 8)
    areas = sorted((int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, n)),
                   reverse=True)
    keep = [a for a in areas if a >= min_blob]
    box = float(sub.shape[0] * sub.shape[1])
    # ⚠️ 用户指出的 padding 问题：拿**盒子面积**当分母时，盒子含不含 padding
    #    取决于气泡填充色有没有超过 tol（实测差 56 / tol 45，只差 11）→ 阈值会飘。
    #    换成**全部非填充像素**：分子分母都只和"内容"有关，与 padding/主题无关。
    total = float(sum(areas)) or 1.0
    return (len(keep),
            ((areas[0] / box) if areas else 0.0),
            fill_share,
            ((areas[0] / total) if areas else 0.0))

def main():
    cfg = load_config()
    win = pick_window(list_windows(cfg), cfg)
    if not win:
        print("没找到微信窗口 —— 先打开微信并**点开一个会话**")
        return 1
    img = capture.capture_window(win["hwnd"])
    if img is None:
        print("抓图失败（先跑 probes/check_capture_selftest.py 排除抓图本身的问题）")
        return 1

    rd = OcrReader(cfg)
    scale = rd._scale()
    W, H = img.width, img.height
    a = np.asarray(img.convert("RGB"))          # ⚠️ 必须转数组再喂
    x1, y1, x2, y2 = chat_rect((0, 0, W, H), scale, cfg, (W, H))
    ca = a[y1:y2, x1:x2]
    print("窗口 %dx%d  缩放 %.2f  聊天区 (%d,%d)-(%d,%d)"
          % (W, H, scale, x1, y1, x2, y2))

    bg = rd._bg(ca, 0, ca.shape[1], 0)
    if bg is None:
        print("算不出聊天区底色（_bg 返回 None）—— 探针没法继续")
        return 1
    print("聊天区底色 =", tuple(int(v) for v in bg))
    bg_diagnosis(ca, bg)

    boxes = bubble_boxes(ca, bg)
    if not boxes:
        print("\n一个气泡盒都没认出来 —— 聊天区里没有消息？（或 cv2 没装）")
        return 1

    # ---- 跑一次 OCR，把"盒子里有什么字"也打出来（对号入座用）
    print("\n正在跑 OCR（一次约 0.4s）…")
    words = []
    try:
        eng = get_engine()
        raw = eng.recognize(ca)
        for d in (raw or []):
            t = (d.get("text") or "").strip()
            if not t:
                continue
            # ⚠️ 坐标字段名**没核实过**，同时接受几种写法
            xs = [d.get(k) for k in ("x1", "left", "x", "cx")]
            ys = [d.get(k) for k in ("y1", "top", "y", "cy")]
            if len(xs) == 1 and isinstance(xs[0], (list, tuple)):
                xs = list(xs[0])
            if len(ys) == 1 and isinstance(ys[0], (list, tuple)):
                ys = list(ys[0])
            nums_x = [v for v in (d.get("x1"), d.get("x2"), d.get("left"),
                                  d.get("right")) if isinstance(v, (int, float))]
            nums_y = [v for v in (d.get("y1"), d.get("y2"), d.get("top"),
                                  d.get("bottom")) if isinstance(v, (int, float))]
            if len(nums_x) >= 2 and len(nums_y) >= 2:
                cx = (min(nums_x) + max(nums_x)) / 2.0
                cy = (min(nums_y) + max(nums_y)) / 2.0
            elif len(xs) >= 2 and len(ys) >= 2:
                cx = (xs[0] + xs[1]) / 2.0
                cy = (ys[0] + ys[1]) / 2.0
            else:
                cx = cy = None
            words.append((t, cx, cy))
        if words and words[0][1] is None:
            print("   ⚠️ OCR 出来了 %d 块，但**坐标字段名对不上**（大概是 "
                  "x1/x2/y1/y2），所以这一列只能空着。原始键名：%s"
                  % (len(words), sorted((raw or [{}])[0].keys())))
            words = []
    except Exception as e:
        print("   OCR 失败（不影响别的列）：%s: %s" % (type(e).__name__, e))
        words = []

    def text_in(box):
        """落在盒子里面的 OCR 文字（按 y 排序拼起来）。"""
        bx1, by1, bx2, by2 = box
        hit = [(t, cy) for (t, cx, cy) in words
               if cx is not None and bx1 <= cx <= bx2 and by1 <= cy <= by2]
        hit.sort(key=lambda p: p[1])
        return " ".join(t for t, _ in hit)

    print("\n共 %d 个气泡盒。每一行后面按（宽高比 / 贴边 / 圆角 / 铺满度 / 颜色数）"
          "猜一个类型：\n" % len(boxes))
    print("  #  尺寸(w×h)   宽高比  贴边   圆角   铺满度  │局部色 块数 最大块/盒 最大块/内容 │ 盒内文字")
    print("  " + "-" * 118)
    for i, (bx1, by1, bx2, by2) in enumerate(boxes):
        w, h = bx2 - bx1, by2 - by1
        sub = ca[by1:by2, bx1:bx2]
        if sub.size == 0:
            continue
        diff = np.abs(sub.astype(np.int32) - np.asarray(bg, dtype=np.int32)).sum(axis=2)
        filled = float((diff > 45).mean())
        colors = len(np.unique(sub.reshape(-1, 3), axis=0))
        cbg = corner_bg_frac(ca, bg, (bx1, by1, bx2, by2))
        # 贴边：离聊天区左右边缘多远（头像贴边）
        left_d = bx1
        right_d = ca.shape[1] - bx2
        edge = min(left_d, right_d)
        aspect = (w / float(h)) if h else 0.0
        # ---- 猜（**只是提示**，真正的阈值要等数据出来再定）
        if filled > 0.85 and colors > 200:
            guess = "图片（铺满+颜色多）"
        elif cbg < 0.15 and filled < 0.45:
            guess = "文本气泡（直角?+稀疏）"
        elif cbg >= 0.15 and filled < 0.45:
            guess = "文本气泡（圆角+稀疏）"
        else:
            guess = "？"
        if abs(aspect - 1.0) < 0.25 and h < int(60 * scale) and edge < int(8 * scale):
            guess += " · **头像候选**"
        nblob, maxfrac, fshare, max_content = two_level(sub)
        print("  %-3d %4d×%-4d %6.2f  %4d  %6.2f  %6.2f  │ %5.1f%% %4d %7.1f%% %8.1f%% │ %s"
              % (i, w, h, aspect, edge, cbg, filled,
                 fshare * 100, nblob, maxfrac * 100, max_content * 100,
                 text_in((bx1, by1, bx2, by2))[:70] or "（盒里没有字）"))

    # ---- 每个盒子的"主色占比"：接近 100% 的就是**假盒子**（其实是背景）
    print("\n== 每个盒子的主色占比（接近 100% = 这个盒子其实是背景，是假盒子）==")
    for i, (bx1, by1, bx2, by2) in enumerate(boxes):
        sub = ca[by1:by2, bx1:bx2]
        if sub.size == 0:
            continue
        col, share = _modal(sub)
        # ⚠️ 判据是「主色 **等于** 聊天底色」，**不是**「占比高」（2026-10-11 修的：
        #    真气泡的填充色也能占到 88.5%，按占比判会把真消息标成假盒子）。
        same = (col is not None and bg is not None
                and sum(abs(int(a) - int(b)) for a, b in zip(col, bg)) <= 8)
        flag = "  ← ⚠️ **假盒子**（主色就是聊天底色）" if same else ""
        print("   #%-3d %4d×%-4d 主色 %-16s %5.1f%%%s"
              % (i, bx2 - bx1, by2 - by1,
                 str(tuple(int(v) for v in col)), share * 100, flag))

    print("""
怎么读这张表（判据的候选，**阈值等数据出来再定**）：

  · **头像**：宽高比≈1、尺寸小且恒定、**贴边**（`贴边` 那列很小）。
    它和"纯图片"一样都是"有盒子但没文字" —— 所以**排除头像**是第一步要定的。
  · **纯图片**：`盒内非底色` 接近 1.0（照片铺满整个盒子）、`颜色数` 几百上千。
  · **文本气泡**：`盒内非底色` 很低（只有笔画）、`颜色数` 只有个位数到十几。
  · **链接卡片**：介于两者之间（缩略图 + 几行字）。
  · **圆角/直角**：看 `角上底色` —— 文本气泡是圆角（角上是底色，值偏高），
    图片/卡片是直角矩形（角上是它自己，值接近 0）。
    ⚠️ 这条如果数据支持，就是**最硬的一条判据**（形状是微信画的，不受内容影响）。

⚠️ 看过数据再决定阈值 —— 别直接照抄上面那个 `guess`（那只是让表好读）。""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
