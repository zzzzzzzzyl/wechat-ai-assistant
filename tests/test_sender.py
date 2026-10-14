# -*- coding: utf-8 -*-
"""群聊昵称判据测试（离线，不需要真实微信窗口、不需要 OCR 引擎）。

背景（2026-09-27 血案）
----------------------
原来读昵称靠「行高 ≥ 100 就裁这一行的上 55%，OCR 后取最靠上的短文本」。
这个判据**是缺的**，实测把库污染了 —— 一个 5 人群记着 **40 个不同的人名**：
`灵感出炉`（喜茶截图里的字）、`新民主主义革命基本思想`（马哲课本截图里的字）、
`就说时间上实在排不出来`（**上一条消息的正文**）……

现在有三条判据，这个文件把每一条都钉死：

    ① 位置  content_block_top  候选必须在**内容块正上方**
    ② 底色  sits_on_background 候选两旁必须是**聊天区底色**
    ③ 复现  _accept_nickname   同一会话里读到 >= min_hits 次才认

⚠️ 这个文件用**合成像素**测，不依赖真机：
   深灰底 + 白色文字 = 聊天区底色上的昵称
   深浅不同的色块   = 气泡 / 图片

用法：python tests/test_sender.py
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.sender import (  # noqa: E402
    BG_SAMPLE_TOL, MIN_CONTENT_H, SenderResolver, content_blocks,
    content_top_for, estimate_bg, looks_like_noise, nickname_ok_position,
    sits_on_background)

PASS, FAIL = 0, 0

BG = (40, 40, 42)          # 聊天区底色（深色主题）
BUBBLE = (95, 95, 100)     # 别人发的气泡色（和底色明显不同）
TEXT = (240, 240, 240)     # 文字像素


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


class FakeCfg:
    def __init__(self, **kw):
        self._d = kw

    def get(self, key, default=None):
        return self._d.get(key, default)


class FakeOcr:
    """假的 OCR：直接返回预先排好的块。"""

    def __init__(self, blocks):
        self.blocks = blocks

    def recognize(self, img, min_score=0.5):
        return list(self.blocks)


def row(height=260, width=400):
    a = np.zeros((height, width, 3), dtype=np.int16)
    a[:, :] = BG
    return a


def draw(a, y1, y2, x1, x2, color=TEXT):
    a[y1:y2, x1:x2] = color


# ============================================================ 1. 底色估计

def test_estimate_bg():
    print("\n1. estimate_bg —— 用左右留白估计聊天区底色")
    a = row()
    bg = estimate_bg(a)
    check("纯底色行估出来就是底色", bg is not None and abs(bg[0] - BG[0]) <= 1,
          str(None if bg is None else bg.round(1)))

    b = row()
    draw(b, 100, 200, 50, 350, BUBBLE)          # 中间放个气泡，不影响两侧
    bg2 = estimate_bg(b)
    check("中间有气泡也不影响（只看两侧）",
          bg2 is not None and abs(bg2[0] - BG[0]) <= 1,
          str(None if bg2 is None else bg2.round(1)))

    check("太窄的行估不出来（不瞎猜）", estimate_bg(np.zeros((10, 8, 3), dtype=np.int16)) is None)


# ============================================================ 2. 判据① 位置

def test_content_top():
    print("\n2. content_blocks / content_top_for —— 判据① 内容块的顶边")
    # 结构：[昵称行][空隙][气泡]
    #        0-16     17-30  31-200
    a = row()
    draw(a, 4, 16, 40, 130)                     # 昵称行：一行小字
    draw(a, 31, 200, 30, 370, BUBBLE)           # 气泡：一片连续内容
    bg = estimate_bg(a)
    blocks = content_blocks(a, bg)
    check("找到 1 个内容块（昵称太矮，不算）", len(blocks) == 1, str(blocks))
    check("内容块顶边是**气泡**的 31，不是昵称的 4",
          content_top_for(blocks, 40, 130, 16) == 31,
          str(content_top_for(blocks, 40, 130, 16)))
    check("✅ 昵称（y2=16）在内容块上方 -> 判据① 通过",
          nickname_ok_position(blocks, 40, 130, 16))
    check("⚠️ 候选在内容块**内部**（y2=150）-> 判据① 拦下",
          not nickname_ok_position(blocks, 40, 130, 150))
    check("⚠️ 候选在内容块**下方**（y2=280）-> 判据① 也拦下（下方没有块了）",
          not nickname_ok_position(blocks, 40, 130, 280))

    b = row()
    draw(b, 0, 120, 30, 370, BUBBLE)            # 只有气泡，没有昵称
    bb = content_blocks(b, estimate_bg(b))
    check("没有昵称时唯一的块顶边就是 0", bb and bb[0][0] == 0, str(bb))
    check("⚠️ 这种行里任何候选都落在块上 -> 判据① 拦下（本来就没有昵称）",
          not nickname_ok_position(bb, 30, 370, 20))

    check("整行纯底色 -> 没有内容块", content_blocks(row(), estimate_bg(row())) == [])
    check("底色估不出来 -> 没有内容块", content_blocks(a, None) == [])
    check("空数组不炸", content_blocks(np.zeros((0, 0, 3), dtype=np.int16), (40, 40, 40)) == [])
    check("⚠️ 认不出内容块时判据① **放行**（交给②③，别一刀切）",
          nickname_ok_position([], 10, 20, 5))

    # ⚠️ 判据①存在的理由：**不能把「第一行有内容」当判据**，昵称自己就是第一行内容。
    c = row()
    draw(c, 4, 16, 40, 130)                     # 只有昵称，没有气泡
    check("只有短短一行内容（低于 MIN_CONTENT_H）-> 不算内容块",
          content_blocks(c, estimate_bg(c)) == [],
          "MIN_CONTENT_H=%d" % MIN_CONTENT_H)

    d = row()
    draw(d, 4, 16, 40, 130)
    draw(d, 40, 40 + MIN_CONTENT_H - 1, 30, 370, BUBBLE)   # 差 1 行不够高
    check("气泡刚好差 1 行 -> 不算（边界）",
          content_blocks(d, estimate_bg(d)) == [])
    e = row()
    draw(e, 4, 16, 40, 130)
    draw(e, 40, 40 + MIN_CONTENT_H, 30, 370, BUBBLE)       # 刚好够
    check("气泡刚好够 MIN_CONTENT_H -> 算",
          content_top_for(content_blocks(e, estimate_bg(e)), 40, 130, 16) == 40)

    # 1px 宽度的边缘伪影要被滤掉
    f = row()
    f[:, 0:1] = (200, 200, 200)                 # 左边缘一整列伪影
    draw(f, 60, 200, 30, 370, BUBBLE)
    check("1px 宽的边缘伪影被滤掉，不干扰",
          content_top_for(content_blocks(f, estimate_bg(f)), 40, 130, 20) == 60)


# ================================================ 2b. 判据① 必须跳过头像

def test_avatar_excluded():
    print("\n2b. 判据① 必须跳过左边头像（**实测误伤过**）")
    # 复刻真实那一行：头像是 54x54 的小方块，图片本体是 265x239 的一大片，
    # 真昵称在两者右侧、和头像**并排**（y 区间几乎一样）。
    #
    #   y:  0          13        44
    #   头像   [13~67]  x 31~85
    #   昵称   [10~30]  x 100~130     <- 和头像并排，和图片上下相接
    #   图片   [44~283] x 106~371
    a = row(height=300, width=915)
    draw(a, 13, 67, 31, 85, (180, 160, 200))        # 头像
    draw(a, 10, 30, 100, 130)                       # 真昵称
    draw(a, 44, 283, 106, 371, BUBBLE)              # 图片本体
    bg = estimate_bg(a)
    blocks = content_blocks(a, bg)
    check("找到 2 个内容块（头像 + 图片）", len(blocks) == 2, str(blocks))

    check("✅ 昵称的 x 范围不跟头像重叠 -> 选到的是**图片**的 44，不是头像的 13",
          content_top_for(blocks, 100, 130, 30) == 44,
          "拿到 %s" % content_top_for(blocks, 100, 130, 30))
    check("✅ 于是真昵称（y2=30）通过判据①",
          nickname_ok_position(blocks, 100, 130, 30))

    # ---- 短消息：气泡比头像还小，直接取最大块就会中头像的招
    b = row(height=300, width=915)
    draw(b, 13, 67, 31, 85, (180, 160, 200))        # 头像 54x54 = 2916
    draw(b, 10, 30, 100, 130)                       # 真昵称
    draw(b, 40, 80, 106, 160, BUBBLE)               # 短气泡 54x40 = 2160，比头像小
    bg2 = estimate_bg(b)
    blocks2 = content_blocks(b, bg2)
    check("短消息场景：面积最大的块是**头像**（所以不能只按面积挑）",
          blocks2[0][0] == 13, "拿到 %s" % str(blocks2[0]))
    check("✅ 按「下方最近 + 水平重叠」挑，仍然拿到气泡顶边 40",
          content_top_for(blocks2, 100, 130, 30) == 40,
          "拿到 %s" % content_top_for(blocks2, 100, 130, 30))
    check("✅ 真昵称仍然能通过", nickname_ok_position(blocks2, 100, 130, 30))

    # ---- 上一条消息的气泡更大时，不能拿它的顶边当本条的
    c = row(height=400, width=915)
    draw(c, 0, 250, 30, 500, BUBBLE)                # 上一条：又大又长
    draw(c, 8, 30, 100, 130)                        # 它的正文（会被误当成昵称的候选）
    draw(c, 300, 380, 106, 200, BUBBLE)             # 本条：小得多
    bgc = estimate_bg(c)
    blocksc = content_blocks(c, bgc)
    check("面积最大的块是**上一条**的（0）", blocksc[0][0] == 0, str(blocksc[0]))
    check("⚠️ 候选在上一条的气泡里（y2=30）-> 「下方最近」选到本条气泡 300",
          content_top_for(blocksc, 100, 130, 30) == 300,
          "拿到 %s" % content_top_for(blocksc, 100, 130, 30))
    check("→ 判据① 放行（它的下方确实有内容块），这条交给判据② 去挡",
          nickname_ok_position(blocksc, 100, 130, 30))

    check("没有内容块 -> content_top_for 返回 None", content_top_for([], 10, 20, 5) is None)


# ============================================================ 3. 判据② 底色

def test_sits_on_background():
    print("\n3. sits_on_background —— 判据② 两旁必须是聊天区底色")
    a = row()
    draw(a, 4, 16, 40, 130)                     # 底色上的昵称
    bg = estimate_bg(a)
    check("底色上的字 -> True",
          sits_on_background(a, bg, (40, 4, 130, 16)))

    b = row()
    draw(b, 31, 200, 30, 370, BUBBLE)           # 气泡
    draw(b, 60, 80, 60, 300)                    # 气泡上的正文
    bg2 = estimate_bg(b)
    check("气泡里的字 -> False",
          not sits_on_background(b, bg2, (60, 60, 300, 80)))

    check("bg 为 None -> False（宁可不认，也不瞎认）",
          not sits_on_background(a, None, (40, 4, 130, 16)))
    check("竖直线超出行范围 -> False",
          not sits_on_background(a, bg, (40, 500, 130, 520)))
    check("候选顶到行边缘（取不到样）-> False",
          not sits_on_background(a, bg, (0, 4, 400, 16)))


# ============================================================ 4. 完整挑选逻辑

def make_resolver(blocks, **cfg):
    r = SenderResolver(FakeCfg(**cfg), ocr=FakeOcr(blocks), logger=lambda m: None)
    return r


def test_pick_nickname():
    print("\n4. _pick_nickname —— 三条判据合起来")

    # ---- 场景 A：**OCR 漏掉了真昵称**（这才是真实翻车场景）
    #
    # 昵称是小灰字，识别率本来就低；图里的字又大又黑，稳稳读出来。
    # 于是「按 y 取最靠上」挑到的是**图里的字**。
    a = row(height=300)
    draw(a, 4, 18, 40, 130)                     # 真昵称画在这，但 OCR 没读出来
    draw(a, 40, 300, 30, 370, BUBBLE)           # 内容块（图片）
    bg = estimate_bg(a)
    blocks = content_blocks(a, bg)
    check("场景就绪：昵称在内容块上方 -> 判据① 通过",
          nickname_ok_position(blocks, 40, 130, 18))

    img_blocks = [
        {"text": "订单详情", "score": 0.99, "x1": 60, "y1": 120, "x2": 160, "y2": 150},
        {"text": "（中国社会备阶级的分析）", "score": 0.95,
         "x1": 60, "y1": 200, "x2": 300, "y2": 220},
    ]
    r = make_resolver(img_blocks)
    old = r._pick_nickname(_crop(), "图片")
    check("⚠️ 不传判据时会挑到**图里的字**（这就是旧的 bug）",
          old == "订单详情", "拿到 %r" % old)
    new = r._pick_nickname(_crop(), "图片", a, bg, blocks)
    check("✅ 带上判据①② 后返回空（走继承/显示「对方」），不再瞎认",
          new == "", "拿到 %r" % new)
    only_pos = r._pick_nickname(_crop(), "图片", None, None, blocks)
    check("只上判据① 也够（图里的字被位置挡掉）", only_pos == "",
          "拿到 %r" % only_pos)

    # ---- 场景 B：真昵称和图里的字**都被读出来了**
    # 实测就是这个样子（23 块里 21 块是图里的字，真昵称在最上面侥幸赢了）。
    both = [
        {"text": "订单详情", "score": 0.99, "x1": 60, "y1": 120, "x2": 160, "y2": 150},
        {"text": "小明", "score": 0.60, "x1": 40, "y1": 4, "x2": 130, "y2": 18},
    ]
    r2 = make_resolver(both)
    check("两者都读到 -> 新旧都拿到真昵称（旧代码在这里是**侥幸**，不是判据）",
          r2._pick_nickname(_crop(), "图片") == "小明"
          and r2._pick_nickname(_crop(), "图片", a, bg, blocks) == "小明")

    # ---- 场景 C：上一条消息的**正文**被切进来
    # 上一条气泡够高 -> 它自己就是个内容块；但本条的块在更下面，
    # 「下方最近」选到的是**本条**的块，所以判据① 会放行 —— 这条靠判据② 挡。
    c = row(height=300)
    draw(c, 0, 34, 30, 370, BUBBLE)             # 上一条的气泡（34 行，够 MIN_CONTENT_H）
    draw(c, 8, 24, 50, 200)                     # 它的正文（它坐在那气泡上）
    draw(c, 60, 300, 30, 370, BUBBLE)           # 本条的气泡
    bgc = estimate_bg(c)
    blocksc = content_blocks(c, bgc)
    ctc = content_top_for(blocksc, 50, 200, 24)
    check("「下方最近」选到的是**本条**气泡的顶边 60，不是上一条的 0",
          ctc == 60, "ct=%s" % ctc)
    r3 = make_resolver([{"text": "就说时间上实在排不出来", "score": 0.9,
                         "x1": 50, "y1": 8, "x2": 200, "y2": 24}])
    check("✅ 判据② 挡掉它（它坐在上一条的气泡上，不是聊天区底色）",
          r3._pick_nickname(_crop(), "x", c, bgc, blocksc) == "")
    check("对照：只上判据① 时它就漏过去了（说明判据② 不是多余的）",
          r3._pick_nickname(_crop(), "x", None, None, blocksc) == "就说时间上实在排不出来")

    # ---- 场景 D：上一条的正文**够不着** 判据①（气泡太矮，没算成内容块）
    # 这时靠判据② —— 它坐在气泡上，不是聊天区底色
    d = row(height=300)
    draw(d, 0, MIN_CONTENT_H - 6, 30, 370, BUBBLE)    # 矮气泡：低于 MIN_CONTENT_H
    draw(d, 2, 16, 50, 200)                           # 它的正文
    draw(d, 60, 300, 30, 370, BUBBLE)                 # 本条的气泡
    bgd = estimate_bg(d)
    blocksd = content_blocks(d, bgd)
    ctd = content_top_for(blocksd, 50, 200, 16)
    check("矮气泡不算内容块 -> 只看到本条气泡的 60", ctd == 60, "ct=%s" % ctd)
    r4 = make_resolver([{"text": "就说时间上实在排不出来", "score": 0.9,
                         "x1": 50, "y1": 2, "x2": 200, "y2": 16}])
    check("✅ 判据② 挡掉它（它坐在气泡上，不是聊天区底色）",
          r4._pick_nickname(_crop(), "x", d, bgd, blocksd) == "")
    check("对照：只上判据① 时它就漏过去了（说明判据② 不是多余的）",
          r4._pick_nickname(_crop(), "x", None, None, blocksd) == "就说时间上实在排不出来")

    # ---- 场景 E：真的昵称坐在底色上、在内容块上方 -> 正确通过
    e = row(height=300)
    draw(e, 4, 18, 40, 130)
    draw(e, 60, 300, 30, 370, BUBBLE)
    bge = estimate_bg(e)
    blockse = content_blocks(e, bge)
    r5 = make_resolver([{"text": "李四", "score": 0.88,
                         "x1": 40, "y1": 4, "x2": 130, "y2": 18}])
    check("✅ 正常真昵称正常通过",
          r5._pick_nickname(_crop(), "文本", e, bge, blockse) == "李四")

    # ---- 原有过滤仍然有效
    r6 = make_resolver([{"text": "图片", "score": 0.9,
                         "x1": 40, "y1": 4, "x2": 130, "y2": 18}])
    check("候选项等于本条正文 -> 过滤掉",
          r6._pick_nickname(_crop(), "图片", e, bge, blockse) == "")

    r7 = make_resolver([{"text": "12:30", "score": 0.9,
                         "x1": 40, "y1": 4, "x2": 130, "y2": 18}])
    check("时间戳这类噪声 -> 过滤掉",
          r7._pick_nickname(_crop(), "图片", e, bge, blockse) == "")

    r8 = make_resolver([{"text": "这个昵称实在是太长了一点都不像名字", "score": 0.9,
                         "x1": 40, "y1": 4, "x2": 300, "y2": 18}])
    check("超过 16 字 -> 过滤掉",
          r8._pick_nickname(_crop(), "图片", e, bge, blockse) == "")

    r9 = SenderResolver(FakeCfg(), ocr=None)
    check("没有 OCR 引擎 -> 返回空串", r9._pick_nickname(_crop(), "图片") == "")


def _crop(size=(400, 160)):
    """给 _pick_nickname 用的占位图 —— 内容是假的，块是 FakeOcr 直接给的。"""
    from PIL import Image
    return Image.new("RGB", size, BG)


# ============================================================ 5. 判据③ 昵称表

def test_accept_nickname():
    print("\n5. _accept_nickname —— 判据③ 昵称表（复现才认）")
    r = make_resolver([], **{"sender.nickname_min_hits": 2})

    check("第 1 次读到 -> 不认", not r._accept_nickname("群A", "灵感出炉"))
    check("第 2 次读到同一个 -> 认", r._accept_nickname("群A", "灵感出炉"))
    check("第 3 次 -> 一直认", r._accept_nickname("群A", "灵感出炉"))

    # 图片里的字每个只出现一次 —— 各自永远停在 1 次
    for i, bogus in enumerate(["新民主主义革命基本思想", "（中国社会备阶级的分析）",
                               "在置级干部会议上的讲话》《论", "现论", "航日此争"]):
        check("第 %d 个图里的字仍然不被认" % (i + 1),
              not r._accept_nickname("群A", bogus))

    # 换一个会话 -> 重新数
    check("换会话后重新计数（第 1 次不认）",
          not r._accept_nickname("群B", "灵感出炉"))
    check("换会话后第 2 次才认",
          r._accept_nickname("群B", "灵感出炉"))

    # min_hits = 1 -> 关掉这道闸（退回旧行为）
    r1 = make_resolver([], **{"sender.nickname_min_hits": 1})
    check("min_hits=1 时第 1 次就认（= 关掉判据③）",
          r1._accept_nickname("群C", "随便什么"))

    # 写错的配置不能把上限弄没
    r0 = make_resolver([], **{"sender.nickname_min_hits": 0})
    check("min_hits=0 被钳到 1", r0.nickname_min_hits == 1)
    rn = make_resolver([], **{"sender.nickname_min_hits": -5})
    check("负数也被钳到 1", rn.nickname_min_hits == 1)

    # 会话数上限
    rc = make_resolver([], **{"sender.nickname_min_hits": 1})
    for i in range(rc._max_sessions + 5):
        rc._accept_nickname("会话%d" % i, "x")
    check("会话数有上限，不会无限涨",
          len(rc._name_hits) <= rc._max_sessions,
          "现有 %d 个会话" % len(rc._name_hits))

    # 默认值
    rd = make_resolver([])
    check("默认门槛是 2", rd.nickname_min_hits == 2, str(rd.nickname_min_hits))


# ============================================================ 6. 不回归

def test_no_regression():
    print("\n6. 不回归：原有的过滤规则还在")
    check("空串是噪声", looks_like_noise(""))
    check("纯数字/时间是噪声", looks_like_noise("12:30") and looks_like_noise("2026-09-27"))
    check("单字符符号是噪声", looks_like_noise("?"))
    check("正常昵称不是噪声", not looks_like_noise("小明"))
    check("全长昵称不是噪声", not looks_like_noise("不爱干活的副社"))

    # top_nick_ratio 没被动过（改它会影响裁多少）
    r = make_resolver([])
    check("top_nick_ratio 仍是 0.55", abs(r.top_nick_ratio - 0.55) < 1e-9)
    check("阈值常量没被改动", MIN_CONTENT_H == 24 and BG_SAMPLE_TOL == 22)


if __name__ == "__main__":
    print("=" * 74)
    print("群聊昵称判据 测试")
    print("=" * 74)

    test_estimate_bg()
    test_content_top()
    test_avatar_excluded()
    test_sits_on_background()
    test_pick_nickname()
    test_accept_nickname()
    test_no_regression()

    print("\n" + "=" * 74)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 74)
    sys.exit(1 if FAIL else 0)
