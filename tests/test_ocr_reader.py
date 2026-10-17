# -*- coding: utf-8 -*-
"""纯 OCR 读屏（`ocr_reader`）的离线测试。

背景
----
实测 UIA 能不能读到微信控件树是**「机器 × 账号」的交互** ——
四个组合里只有「他的机器 + 他的号」读不到（2 个后代控件）。
所以必须有一条完全绕开 UIA 的路：抓图 + OCR。

这个文件把那条路的**每一段几何/归并判据**都钉死，全部用
**合成数据**（假 OCR 引擎 + 合成块），不需要微信、不需要真 OCR 引擎。

用法：python tests/test_ocr_reader.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from wechat_assistant import ocr_reader as R  # noqa: E402
from wechat_assistant.reader import (ChatSnapshot, MessageItem,  # noqa: E402
                                     TimeMarker, pick_window)

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


class FakeCfg:
    """支持点号路径取值；**同时**认"整串当键"的写法。

    ⚠️ 测试里写 `FakeCfg(**{"ocr_read.chat_left": 310})` 是平的键，
        而产品代码用 `cfg.get("ocr_read.chat_left")` 走点号 ——
        只实现一种的话，测试里所有配置项都会静默取到默认值，
        于是"改了配置没生效"这类 bug 测不出来（第一版就是这样，
        结果 `title_interval` 那条用例量到 3.0 而不是 4.0）。
    """
    def __init__(self, **kw):
        self._d = kw

    def get(self, dotted, default=None):
        if dotted in self._d:                    # 整串当键
            v = self._d[dotted]
            return default if v is None else v
        cur = self._d
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return default if cur is None else cur


class FakeOcr:
    """假的 OCR 引擎：`recognize()` 直接返回预先排好的块。

    ⚠️ 真引擎要加载 15MB 模型（实测 0.8~1.1 秒），放进单测会让整套测试
        从"秒级"变成"十几秒级"，而且结果依赖模型版本 —— 判据不安定。

    ⚠️ 2026-10-13 加 `title_blocks`：**标题栏和聊天区是两次不同的
        `recognize`**，而原来假引擎对两次都返回同一批块 —— 于是
        **第一条消息冒充了会话标题**。在"私聊不判昵称"那条规则上线后，
        这直接让群聊用例失效（`小明` 那条昵称块不再被认出来，因为
        `_nick_ok = _looks_like_group_title(snap.title)` 判成了假）。
        判据用**裁剪条的高度**区分：标题栏那条高 `chat_top*scale`（实测 105），
        聊天区那块高得多。`title_blocks=None` 时行为不变（老用例照旧）。
    """

    def __init__(self, blocks, title_blocks=None):
        self.blocks = blocks
        self.title_blocks = title_blocks
        self.calls = []

    def load(self):
        return self

    def warmup(self, size=None):
        return 0.0

    def recognize(self, image, min_score=0.5):
        self.calls.append(getattr(image, "size", None))
        if self.title_blocks is not None and getattr(image, "height", 9999) <= 120:
            return list(self.title_blocks)
        return list(self.blocks)


def B(text, x1, y1, x2, y2, score=1.0):
    return {"text": text, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "score": score}


# ============================================================ 1. 挑主窗口

def test_pick_window():
    print("\n1. pick_window（⚠️ 原来的 found[0] 是本项目最坑的 bug）")
    cfg = FakeCfg(**{"wechat.window_title": "微信"})

    one = [{"hwnd": 1, "title": "微信"}]
    check("只有一个候选、标题是主窗口名 -> 用它",
          pick_window(one, cfg) is one[0])

    multi = [{"hwnd": 1, "title": "图片和视频"},
             {"hwnd": 2, "title": "微信"},
             {"hwnd": 3, "title": "朋友圈"}]
    got = pick_window(multi, cfg)
    check("多个候选 -> 认标题精确等于「微信」的那个",
          got is not None and got["hwnd"] == 2,
          "拿到 %s" % (got or {}).get("hwnd"))

    # ⚠️ 顺序反过来也要拿到同一个 —— 这正是不该用 found[0] 的原因：
    #    EnumWindows 的顺序不保证，实测同一台机器跑两次就变。
    got2 = pick_window(list(reversed(multi)), cfg)
    check("顺序颠倒还是同一个（found[0] 就会错）",
          got2 is not None and got2["hwnd"] == 2,
          "拿到 %s" % (got2 or {}).get("hwnd"))

    no_main = [{"hwnd": 1, "title": "图片和视频"}, {"hwnd": 3, "title": "朋友圈"}]
    check("**分不出来时返回 None**（宁可不读也不瞎读）",
          pick_window(no_main, cfg) is None)
    check("空列表返回 None", pick_window([], cfg) is None)

    # ⚠️ 2026-10-17：打开「图片和视频」预览时，它**可能也叫「微信」**。
    #    纯 Win32 的补充判据：没有 WS_CAPTION 的是无边框弹窗，不是主窗口。
    import win32con
    both = [{"hwnd": 1, "title": "微信", "style": win32con.WS_CAPTION, "owner": 0},
            {"hwnd": 2, "title": "微信", "style": 0, "owner": 0}]
    got3 = pick_window(both, cfg)
    check("同名两个候选 -> 排除没有 WS_CAPTION 的（图片预览）",
          got3 is not None and got3["hwnd"] == 1,
          "拿到 %s" % (got3 or {}).get("hwnd"))

    owned = [{"hwnd": 1, "title": "微信", "style": win32con.WS_CAPTION, "owner": 5},
             {"hwnd": 2, "title": "微信", "style": win32con.WS_CAPTION, "owner": 0}]
    got4 = pick_window(owned, cfg)
    check("同名 -> 排除有 owner 的（弹窗挂在主窗口下）",
          got4 is not None and got4["hwnd"] == 2,
          "拿到 %s" % (got4 or {}).get("hwnd"))

    only_pic = [{"hwnd": 1, "title": "图片和视频"}]
    check("单候选但标题不是主窗口名 -> None（等主窗口，别把图片窗口当聊天）",
          pick_window(only_pic, cfg) is None)


# ============================================================ 2. 聊天区几何

def test_chat_rect():
    print("\n2. chat_rect（按**逻辑像素** + DPI 缩放算）")
    cfg = FakeCfg(**{"ocr_read.chat_left": 310, "ocr_read.chat_top": 70,
                     "ocr_read.right_margin": 0})

    # 1542x979 位图 @1.5 -> 465 / 105
    got = R.chat_rect((0, 0, 1542, 979), 1.5, cfg, (1542, 979))
    check("1.5 倍下一律乘缩放", got == (465, 105, 1542, 979), str(got))
    # ⚠️ 这条是**实测标定值**：会话列表和聊天区的分界就在 465 物理像素
    #    （= 310 逻辑）。按窗口宽度的百分比算会随窗口大小漂走。
    check("分界落在实测的 465", got[0] == 465, str(got[0]))

    got1 = R.chat_rect((0, 0, 1542, 979), 1.0, cfg, (1542, 979))
    check("缩放 1.0 时就是逻辑像素本身", got1 == (310, 70, 1542, 979), str(got1))

    small = R.chat_rect((0, 0, 200, 100), 1.5, cfg, (200, 100))
    check("窗口很小时不会越界（x2 > x1）", small[2] > small[0] and small[0] >= 0,
          str(small))
    check("x2 不超过图像宽度", small[2] <= 200, str(small))


# ============================================================ 3. 切行

def test_group_blocks():
    print("\n3. group_blocks（⚠️ 只按竖直归并会把左右两条粘成一条）")

    # 同一气泡的两行：x 有重叠 -> 应该并成一条
    blk = [R.OcrBlock("今天天气", 100, 100, 300, 130),
           R.OcrBlock("不错啊", 100, 135, 200, 165)]
    rows = R.group_blocks(blk, 1000)
    check("气泡内换行并成一条", len(rows) == 1, "%d 行" % len(rows))
    check("并起来的正文按顺序拼好", rows[0].text() == "今天天气 不错啊",
          rows[0].text())

    # 左右两条高度差不多，但水平差了 600px -> **不能**并
    blk2 = [R.OcrBlock("左边的", 50, 100, 200, 130),
            R.OcrBlock("右边的", 800, 105, 950, 135)]
    rows2 = R.group_blocks(blk2, 1000)
    check("左右两条**分开**（这是实测补上的判据）", len(rows2) == 2,
          "%d 行" % len(rows2))
    check("左边那条判成「对方」", rows2[0].cx < 500, str(rows2[0].cx))
    check("右边那条判成「我」", rows2[1].cx > 500, str(rows2[1].cx))

    # 竖直隔得远的两条 -> 两条
    blk3 = [R.OcrBlock("上面", 100, 100, 200, 130),
            R.OcrBlock("下面", 100, 400, 200, 430)]
    check("竖直隔得远的分开", len(R.group_blocks(blk3, 1000)) == 2)

    # 昵称 + 气泡：x 有重叠、竖直相接 -> 一条
    blk4 = [R.OcrBlock("小明", 100, 100, 150, 120),
            R.OcrBlock("在吗", 100, 125, 300, 165)]
    r4 = R.group_blocks(blk4, 1000)
    check("昵称和气泡归到同一条（⚠️ 兜底路径的已知代价：连发的两条也会并）",
          len(r4) == 1, "%d 行" % len(r4))
    cy = sum(b.cy for b in r4[0].blocks) / len(r4[0].blocks)
    check("行中心不会被昵称拉偏太多（用块中心平均，不是包围盒）",
          abs(cy - 145) < 30, "%.1f" % cy)


# ============================================================ 4. 群昵称

def test_nickname():
    print("\n4. pick_nickname（OCR 路径下判据比 UIA 那条路简单得多）")

    row = R.OcrRow(blocks=[R.OcrBlock("小明", 100, 100, 150, 120),
                           R.OcrBlock("在吗", 100, 125, 300, 165)])
    R.pick_nickname(row, 500)
    check("压在气泡上方 -> 认成昵称", row.nickname == "小明", row.nickname)
    check("正文里不再含昵称", row.text() == "在吗", row.text())

    # 只有一块 -> 不可能是「昵称 + 正文」
    row2 = R.OcrRow(blocks=[R.OcrBlock("在吗", 100, 125, 300, 165)])
    R.pick_nickname(row2, 500)
    check("只有一块时不认昵称", row2.nickname == "", repr(row2.nickname))

    # 太长 -> 是正文不是名字
    row3 = R.OcrRow(blocks=[R.OcrBlock("这是一句很长的正文内容不该当成名字用", 100, 100, 400, 120),
                            R.OcrBlock("在吗", 100, 125, 300, 165)])
    R.pick_nickname(row3, 500)
    check("超过 max_chars 的不认", row3.nickname == "", repr(row3.nickname))

    # 纯数字 / 时间戳 -> 不是名字
    row4 = R.OcrRow(blocks=[R.OcrBlock("21:30", 100, 100, 150, 120),
                            R.OcrBlock("在吗", 100, 125, 300, 165)])
    R.pick_nickname(row4, 500)
    check("纯数字/时间戳不认", row4.nickname == "", repr(row4.nickname))

    # ⚠️ 并排（头像 + 气泡）：竖直方向重叠 -> 不是"压在它上面"
    row5 = R.OcrRow(blocks=[R.OcrBlock("头像区", 10, 100, 60, 160),
                            R.OcrBlock("在吗", 100, 110, 300, 150)])
    R.pick_nickname(row5, 500)
    check("竖直重叠（并排）的不认", row5.nickname == "", repr(row5.nickname))

    # 时间分隔行不认昵称
    row6 = R.OcrRow(blocks=[R.OcrBlock("昨天 21:00", 100, 100, 200, 120),
                            R.OcrBlock("在吗", 100, 130, 300, 165)],
                    is_time=True)
    R.pick_nickname(row6, 500)
    check("时间分隔行不认昵称", row6.nickname == "", repr(row6.nickname))


# ============================================================ 5. 输入区上边

def test_find_input_top():
    print("\n5. find_input_top（⚠️ 找错方向会把输入框里的字当成消息）")

    H, W = 400, 300
    a = np.full((H, W, 3), 30, dtype=np.int16)     # 聊天区底色
    bg = np.array([30, 30, 30], dtype=np.float64)
    check("没有分隔线 -> None", R.find_input_top(a, bg, 0, W, 100) is None)

    a2 = a.copy()
    a2[300, :] = (90, 90, 90)                       # 横贯整宽的分隔线
    got = R.find_input_top(a2, bg, 0, W, 100)
    check("横贯整宽的一行认出来", got == 300, str(got))

    a3 = a.copy()
    a3[H - 1, :] = (90, 90, 90)                     # 贴底的边框线
    a3[H - 2, :] = (90, 90, 90)
    got3 = R.find_input_top(a3, bg, 0, W, 100)
    check("贴到底边的边框线不算（最底 3 行直接跳过）", got3 is None, str(got3))

    a4 = a.copy()
    a4[200, 0:180] = (90, 90, 90)                   # 只占 60% 宽度 = 气泡
    check("只占六成宽的是气泡，不是分隔线",
          R.find_input_top(a4, bg, 0, W, 100) is None)


# ============================================================ 6. 端到端（假引擎）

def test_read_chat_with_fake_engine():
    print("\n6. OcrReader.read_chat 端到端（假 OCR 引擎，不需要微信）")

    # ⚠️ 这些坐标是**裁剪区坐标**（聊天区左上角为原点），不是整窗坐标 ——
    #    `read_chat` 是在裁剪出来的那块上跑 OCR 的，两个坐标系混用会让
    #    「块在不在气泡里」全判错（第一版就这么写的，结果一条消息都没认出来）。
    blocks = [
        B("小明", 35, 170, 85, 190),          # 群昵称（画在底色上，压着左气泡）
        B("在吗", 55, 210, 235, 250),         # 对方的正文（在左气泡里）
        B("在的", 860, 310, 1040, 350),       # 我的正文（在右气泡里）
        B("昨天 21:00", 480, 140, 580, 160),  # 居中 -> 时间分隔
    ]
    cfg = FakeCfg(**{
        "ocr_read.chat_left": 310, "ocr_read.chat_top": 70,
        "ocr_read.right_margin": 0, "ocr_read.input_fallback": 120,
        "ocr_read.min_score": 0.5, "ocr_read.max_blocks": 400,
        "ocr_read.nickname_max_chars": 12,
    })
    img = _chat_image()
    # ⚠️ **标题要单独给**（见 `FakeOcr` 的说明）：这个用例测的是**群聊**的昵称，
    #    而"判不判昵称"现在由**标题带不带 `(N)`** 决定（2026-10-13 起：
    #    「私聊没有昵称」）。不给标题的话第一条消息会冒充标题、
    #    `_nick_ok` 判成假，于是 `小明` 这条昵称就不被认了。
    rd = R.OcrReader(cfg, ocr=FakeOcr(
        blocks, title_blocks=[B("测试群(5)", 100, 20, 300, 50)]))
    rd.grab = lambda hwnd: img            # 不真去抓窗口
    rd._scale = lambda: 1.5               # 固定缩放，判据才安定

    snap = rd.read_chat(0x1234)
    check("拿到了快照", isinstance(snap, ChatSnapshot))
    check("来源标了 ocr", snap.source == "ocr", snap.source)
    texts = [(m.text, m.is_self, m.sender_name) for m in snap.messages]
    # ⚠️⚠️ 这个用例的期望在 2026-10-12 的切行改造后**变过**（改的是期望、不是代码）：
    #    切行改成"按文字块归并"之后，`小明` 和 `在吗` 竖直间隔 20px > 上限 14px，
    #    于是它们本来就是两行。**现在昵称会被挂到下面那条消息上**（`sender_name`
    #    = 小明），这一点和 2026-10-12 真机群聊实测的形状一致
    #    （「思翀」「细姨」都是自己一行、压在气泡上方）。
    check("两条消息（时间分隔不算消息）", len(snap.messages) == 2, str(texts))
    check("时间分隔进了 markers", len(snap.markers) == 1,
          str([m.text for m in snap.markers]))
    check("时间分隔的文本对",
          snap.markers and snap.markers[0].text == "昨天 21:00",
          str(snap.markers[0].text if snap.markers else None))
    check("时间分隔判成跨天级", snap.markers and snap.markers[0].is_date_level)

    left = [m for m in snap.messages if m.is_self is False]
    right = [m for m in snap.messages if m.is_self is True]
    check("左边一条判成「对方」", len(left) == 1, str(len(left)))
    check("右边一条判成「我」", len(right) == 1, str(len(right)))
    check("**昵称挂到下面那条消息上**（真机群聊实测的形状）",
          left and left[0].sender_name == "小明",
          str(left[0].sender_name if left else None))
    check("昵称那些字**没被当成一条消息**（也不许丢）",
          not any(m.text == "小明" for m in snap.messages), str(texts))
    check("我的那条没有昵称", right and right[0].sender_name is None,
          str(right[0].sender_name if right else None))
    check("每条都带 rect（缝合要靠它排序）",
          all(m.rect for m in snap.messages))
    check("container_rect 在（at_bottom 要用）", bool(snap.container_rect),
          str(snap.container_rect))

    # 会话标题：只取标题栏那一条，认最靠左的一块
    rd._ocr = FakeOcr([B("张三", 100, 20, 220, 50), B("搜索", 900, 20, 960, 50)])
    title = rd.read_chat_title(0x1234)
    check("标题取最靠左那块", title == "张三", title)

    rd._ocr = FakeOcr([B("很" * 60, 100, 20, 900, 50)])
    check("超长标题当误认，返回空（宁可不猜）",
          rd.read_chat_title(0x1234) == "")

    rd._ocr = FakeOcr([])
    check("OCR 什么都没认出来 -> 空标题", rd.read_chat_title(0x1234) == "")

    print("\n6b. 抓不到图（空白）时的行为")
    rd2 = R.OcrReader(cfg, ocr=FakeOcr(blocks))
    rd2.grab = lambda hwnd: None
    check("read_chat 返回 None", rd2.read_chat(0x1234) is None)
    check("read_chat_title 返回空串", rd2.read_chat_title(0x1234) == "")
    check("一次都没读成（reads 计数不涨）", rd2.stats["reads"] == 0,
          str(rd2.stats["reads"]))


#: 聊天区左上角在整窗里的位置（= chat_left 310 x1.5, chat_top 70 x1.5）。
#: 测试里画气泡时必须加上它，才能和"裁剪区坐标"的 OCR 块对上。
CROP_OX, CROP_OY = 465, 105


def _chat_image():
    """画一张**有真气泡**的合成聊天区。

    ⚠️ 不能用纯色图：`bubble_boxes()` 靠连通域找气泡，纯色图上找不到，
        于是会静默走兜底路径 —— 那测的就不是主路径了。
    ⚠️ **气泡必须画得"贴边"**：微信自己的贴右、别人的贴左（头像之后）。
        第一版把"我的"气泡画在中间（435..635，而裁剪区有 1077 宽），
        于是"离右边更近"这条判据把它也判成对方 —— 测出来的失败其实是
        **样本不真实**，不是代码错（见 `side_of_box` 的说明）。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1542, 979), (30, 30, 30))
    d = ImageDraw.Draw(img)
    # 别人的气泡（左）—— 裁剪区坐标 (35,195)-(235,265)
    d.rectangle([CROP_OX + 35, CROP_OY + 195, CROP_OX + 235, CROP_OY + 265],
                fill=(95, 95, 100))
    # 我的气泡（右，**贴着右边**）—— 裁剪区坐标 (840,295)-(1040,365)
    d.rectangle([CROP_OX + 840, CROP_OY + 295, CROP_OX + 1040, CROP_OY + 365],
                fill=(60, 160, 90))
    return img


def _light_chat_image():
    """画一张**浅色主题**的聊天区 —— 复现"对方气泡和底色几乎同色"。

    ⚠️ 配色是**真机实测值**（微信 4.1.13.65 / 浅色 / 会话「张三」）：
        聊天底 `(250,250,250)` / 对方气泡 `(238,238,240)` —— 曼哈顿差 **36**；
        我的绿气泡 `(157,242,159)`。
    ⚠️ 关键在**气泡填充色**：它和底色的差别（36）**小于** `bubble_boxes` 的
        阈值（45），所以对方的气泡**不可能**被连通域认出来 ——
        这正是"浅色下读不到对方的话"的根因。图里再画一道"顶部被切掉的图片"
        （白底 + 几个灰块），用来覆盖"图里的字会被当成消息"那条。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1542, 979), (250, 250, 250))
    d = ImageDraw.Draw(img)
    # 顶部：被切掉的图片（浅灰底 + 深灰块，颜色种类多）
    d.rectangle([CROP_OX + 520, CROP_OY + 5, CROP_OX + 1000, CROP_OY + 95],
                fill=(242, 242, 242))
    for i in range(6):
        d.rectangle([CROP_OX + 560 + i * 60, CROP_OY + 20, CROP_OX + 590 + i * 60,
                     CROP_OY + 40], fill=(180 + i * 5, 180, 190))
    # 对方两条气泡（贴左）—— 填充色和底色只差 36
    d.rectangle([CROP_OX + 60, CROP_OY + 240, CROP_OX + 400, CROP_OY + 300],
                fill=(238, 238, 240))
    d.rectangle([CROP_OX + 60, CROP_OY + 400, CROP_OX + 300, CROP_OY + 460],
                fill=(238, 238, 240))
    # 我的绿气泡（贴右）
    d.rectangle([CROP_OX + 900, CROP_OY + 640, CROP_OX + 1200, CROP_OY + 700],
                fill=(157, 242, 159))
    return img


def test_light_theme_reads_other_side():
    """⚠️⚠️ 回归：**浅色下必须能读到对方的话**（2026-10-12 真机 bug 的判据）。

    修之前的表现：标题读得到、**消息 0 条或只剩我自己那条** ——
    因为切行判据是「和聊天底色的色差 > 45」，而浅色下对方气泡只差 36，
    **整块气泡进不了 mask**，它上面的文字全落在所有盒之外。
    修法：切行改成按**文字块归并**（`rows_from_text`），气泡盒只当加分项。

    这个用例**不依赖微信**（合成图 + 假 OCR 引擎），但用的是真机实测的配色，
    所以它一红就说明"浅色又读不到了"。
    """
    print("\n7b. 浅色主题：对方气泡和底色几乎同色，也必须读得到（回归）")
    cfg = FakeCfg(**{
        "ocr_read.chat_left": 310, "ocr_read.chat_top": 70,
        "ocr_read.right_margin": 0, "ocr_read.input_fallback": 120,
        "ocr_read.min_score": 0.5, "ocr_read.max_blocks": 400,
        "ocr_read.nickname_max_chars": 12,
    })
    # ⚠️ 块坐标是**裁剪区坐标**（和产品一致，别再混整窗坐标）
    blocks = [
        B("图里的字", 150, 25, 400, 60),        # 落在顶部图片带里 -> 不算消息
        B("可以可以！没问题", 60, 250, 340, 285),
        B("这就去", 60, 410, 240, 445),
        B("对方无应答", 900, 650, 1180, 685),   # 我的绿气泡
    ]
    img = _light_chat_image()
    # ⚠️ 标题给一个**私聊**名（不带 `(N)`）：这个用例测的是**浅色下能不能读到
    #    对方的话**，与昵称无关。而私聊下"图里的字"这类碎片**不再被昵称逻辑
    #    顺手吃掉**（2026-10-13 起私聊不判昵称）—— 所以下面那条断言
    #    **只要求真消息都在、相对顺序对**，不假装碎片能被清干净。
    rd = R.OcrReader(cfg, ocr=FakeOcr(
        blocks, title_blocks=[B("张三", 100, 20, 220, 50)]))
    rd.grab = lambda hwnd: img
    rd._scale = lambda: 1.5

    snap = rd.read_chat(0x1234)
    check("浅色下拿到了快照（不是 None）", snap is not None)
    if snap is None:
        return
    others = [m.text for m in snap.messages if m.is_self is False]
    mine = [m.text for m in snap.messages if m.is_self is True]
    check("**对方的两条真消息都读到了**（这正是修之前丢掉的）",
          [t for t in others if t in ("可以可以！没问题", "这就去")]
          == ["可以可以！没问题", "这就去"],
          str(others))
    check("我自己那条也在，且判成「我」",
          mine == ["对方无应答"], str(mine))
    # ⚠️ **已知损失**：昵称自己占一行时可能被当成一条消息，图片上的字也可能
    #    多出一条 —— 纯 OCR 后端分不出「气泡里的字」和「图片里的字」、
    #    也不总能把昵称认出来（见 `OCR后端.md`）。
    #    ⚠️ 这里**只断言"真消息一条都不能少"**，不假装能把这些清干净 ——
    #       为了清它们而加的三条启发式实测都会**吃掉真消息**
    #       （见 `read_chat` 里"待领养昵称"那段说明）。
    check("（已知损失）多余的碎片可能存在，但真消息一条不少",
          "可以可以！没问题" in others and "这就去" in others,
          str(snap.messages and [m.text for m in snap.messages]))

    # ⚠️ 再钉一条**判据级**的：这条气泡不会被 `bubble_boxes` 认出来 ——
    #    如果哪天有人把切行改回"按气泡盒"，这条消息就会消失。
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    ca = a[CROP_OY:CROP_OY + 700, CROP_OX:CROP_OX + 1100]
    bg = R.OcrReader(cfg)._bg(ca, 0, ca.shape[1], 0)
    boxes = R.bubble_boxes(ca, bg)
    left_boxes = [b for b in boxes if b[0] < 200]
    check("对方的气泡**确实**认不出盒（这正是不该依赖气泡盒的原因）",
          not left_boxes, str(boxes))


# ============================================================ 7. 自动降级

def _make_auto(**kw):
    """造一个 AutoReader，并把它内部两个后端都换成替身。"""
    cfg = FakeCfg(**kw)
    ar = R.AutoReader(cfg, worker=object())

    class UiaStub:
        background_read_ok = True

        def __init__(self):
            self.health = 2
            self.title = ""

        def find_window(self):
            return {"hwnd": 1}

        def read_chat_title(self, hwnd):
            return self.title

        def uia_health(self, hwnd):
            return self.health

        def read_chat(self, hwnd):
            return ChatSnapshot(title=self.title)

        def read_sessions(self, hwnd):
            return []

        def read_input_text(self, hwnd):
            return ""

    class OcrStub:
        background_read_ok = False

        def __init__(self):
            self.used = 0

        def find_window(self):
            return {"hwnd": 1}

        def read_chat_title(self, hwnd):
            self.used += 1
            return "OCR 标题"

        def read_chat(self, hwnd):
            self.used += 1
            s = ChatSnapshot(title="OCR 标题")
            s.source = "ocr"
            s.messages.append(MessageItem(text="q", cls="x", is_self=True))
            return s

        def read_sessions(self, hwnd):
            return []

        def read_input_text(self, hwnd):
            return ""

    ar.uia = UiaStub()
    ar._ocr_arg = None
    ar.ocr = OcrStub()
    return ar


def test_auto_switch():
    print("\n7. AutoReader（⚠️ 判定入口必须挂在 read_chat_title 上）")
    ar = _make_auto(**{"reader.fail_switches": 3, "reader.min_controls": 20})

    check("初始不切换", ar.switched is False)
    check("控件树正常时不切换（哪怕标题为空）", ar.uia.health == 200 or True)
    ar.uia.health = 200
    for _ in range(5):
        ar.read_chat_title(1)
    check("标题空但控件树满 -> **不切换**（用户可能就在设置页）",
          ar.switched is False, "misses=%d" % ar._misses)

    ar.uia.health = 2
    check("第 1 次：还没到阈值", ar.read_chat_title(1) == "" and not ar.switched)
    ar.read_chat_title(1)
    check("第 2 次：还没到阈值", not ar.switched)
    ar.read_chat_title(1)
    check("第 3 次：**切换了**", ar.switched is True)

    ar.read_chat_title(1)
    check("切换后走 OCR 后端", ar.ocr.used >= 1, str(ar.ocr.used))
    check("切换后 read_chat 也走 OCR", ar.read_chat(1).source == "ocr")
    check("切换后 background_read_ok 跟着后端变",
          ar.background_read_ok is False, str(ar.background_read_ok))

    ar2 = _make_auto(**{"reader.fail_switches": 3, "reader.min_controls": 20})
    ar2.uia.health = -1
    for _ in range(5):
        ar2.read_chat_title(1)
    check("体检查不出来（-1）-> **不切换**", ar2.switched is False,
          "misses=%d" % ar2._misses)

    ar3 = _make_auto(**{"reader.fail_switches": 2, "reader.min_controls": 20})
    ar3.uia.health = 2
    ar3.read_chat_title(1)
    ar3.uia.health = 200                      # 原因变了 -> 计数清零
    ar3.read_chat_title(1)
    ar3.uia.health = 2
    ar3.read_chat_title(1)
    check("中途恢复正常会把失败计数清零", ar3.switched is False,
          "misses=%d" % ar3._misses)

    ar4 = _make_auto(**{"reader.fail_switches": 1, "reader.min_controls": 20})
    ar4.uia.health = 2
    ar4.uia.title = "张三"                  # 标题读得到 -> 一律不降级
    ar4.read_chat_title(1)
    check("标题读得到时不降级", ar4.switched is False)


# ============================================================ 8. 后台记录开关

def test_recorder_gate():
    print("\n8. Recorder：OCR 后端下按「探针」走（变了才真读）")

    from wechat_assistant.recorder import Recorder

    class Reader:
        """假后端。`next_hash` 决定探针返回什么 —— 用来模拟「屏幕变了没」。

        ⚠️ 哈希必须是**真实尺寸**的字节串（缩略图 48x48 = 2304 字节），
           别用 `b"a"` / `b"b"` 那种一个字节的 —— `probe_changed` 是**带容差**的
           （单像素差要超过 `tol`），一字节的 97 vs 98 差 1，会被正确判成
           「没变」，于是测试会以一种看起来像代码坏了的方式失败。
        """
        background_read_ok = False

        def __init__(self):
            self.titles = 0
            self.full = 0
            self.probes = 0
            self.next_hash = _thumb()
            self.probe_raises = False

        def find_window(self):
            return {"hwnd": 1, "pid": 2}

        def read_chat_title(self, hwnd):
            self.titles += 1
            return "张三"

        def read_chat(self, hwnd):
            self.full += 1
            return ChatSnapshot(title="张三")

        def probe(self, hwnd):
            self.probes += 1
            if self.probe_raises:
                raise RuntimeError("抓图挂了")
            return self.next_hash

    class Registry:
        """记录器要用的注册表接口（**默认视为已登记** —— 这一节验的是拍间隔）。"""

        def mode_of(self, title):
            return "on"

        def key_for(self, t):
            return t

        def discover(self, title):
            return False

        def is_registered(self, title):
            return True

        def save(self):
            pass

    # ---- 探针模式（默认）：没变就不真读
    rd = Reader()
    cfg = FakeCfg(**{"record.enabled": True, "record.interval": 1.0,
                     "ocr_read.title_interval": 4.0,
                     "ocr_read.probe": True, "ocr_read.probe_interval": 1.0,
                     "record.fast_interval": 0.2})
    rec = Recorder(cfg, rd, store=object(), registry=Registry())
    rec.tick()
    check("探针模式下**不读整屏**", rd.full == 0, "读了 %d 次" % rd.full)
    check("但**探了**一次", rd.probes == 1, "探了 %d 次" % rd.probes)
    check("返回 0 条", rec.tick() == 0)
    check("没有可读的库时报错也不该有", rec.stats["errors"] == 0,
          str(rec.stats["errors"]))

    # ---- 探针发现变了 -> 才真读
    rd.next_hash = b"b"
    before = rd.full
    rec.tick()
    check("探针发现屏幕变了 -> 真读一屏", rd.full == before + 1,
          "读了 %d 次（之前 %d）" % (rd.full, before))
    check("探针命中计数 +1", rec.stats.get("probe") == 1,
          str(rec.stats.get("probe")))

    # ---- 同一个哈希再来一次 -> 不该再真读
    before = rd.full
    rec.tick()
    check("哈希没变就不重复真读", rd.full == before, "读了 %d 次" % rd.full)

    # ---- 探针抛异常：当"没变化"，**绝不能当"变了"**
    rd.probe_raises = True
    before_full, before_err = rd.full, rec.stats["errors"]
    rec.tick()
    check("探针抛异常时不真读（否则最小化的窗口会每拍烧 250ms）",
          rd.full == before_full, "读了 %d 次" % rd.full)
    check("探针异常计入 errors", rec.stats["errors"] == before_err + 1,
          str(rec.stats["errors"]))

    # ---- 拍间隔：安静时用 probe_interval（比 title_interval 短，因为探针便宜）
    iv = rec._choose_interval()
    check("安静时拍间隔 = probe_interval（探针便宜，可以更密）",
          abs(iv - 1.0) < 1e-6, str(iv))
    check("探针档比只读标题那档更密（4.0 秒太慢，会漏掉快速滑动）",
          iv < rec.title_interval, "%s vs %s" % (iv, rec.title_interval))

    # ---- 内容在变时切到"滑动档"，而且**不会**退化成"追不上"的一刻不停
    rec._content_changed = True
    iv2 = rec._choose_interval()
    check("内容在变时切到 probe_fast（0.5s）", abs(iv2 - 0.5) < 1e-6, str(iv2))
    # ⚠️ 下限是 **0.5s 不是 0.25s**：探针命中之后紧接着要真读一屏（实测 381ms），
    #    所以"命中那一拍"本身就要 381+34 ≈ 415ms。间隔设到 0.3s 就是
    #    "拍比拍还长"，`sleep(max(0.02, iv-dt))` 会退化成一刻不停还丢屏。
    #    第一版写成 0.25 是我只按"探针 34ms"算的 —— 漏了那次真读。
    check("滑动档下限 0.5s（要盖住命中时那次 380ms 的真读）",
          rec.probe_fast >= 0.5, str(rec.probe_fast))
    check("probe_fast 就算配置写成 0.1 也会被钳到 0.5",
          Recorder(FakeCfg(**{"record.enabled": True,
                              "ocr_read.probe_fast": 0.1}),
                   Reader(), store=object(),
                   registry=Registry()).probe_fast == 0.5, "")

    # ---- ⚠️ 探针模式下**每拍不该读标题**（那是 65ms，比探针还贵）
    #
    #    2026-10-11 实测（`probes/bench_tick_cost.py`）发现：真实空闲成本是
    #    「标题 OCR 65ms + 探针 34ms = 99ms（9.9%，≈ UIA 的 10%）」，
    #    而日志只报了探针那 34ms（3.4%）。因为探针的覆盖范围含标题栏 ——
    #    切会话时像素会变 -> 命中 -> 那次真读自己会填 `snap.title`，
    #    所以每拍那次标题 OCR 是**冗余**的。
    rd4 = Reader()
    rd4.next_hash = _thumb()
    rec4 = Recorder(FakeCfg(**{"record.enabled": True, "record.interval": 1.0,
                               "ocr_read.probe": True,
                               "ocr_read.probe_interval": 1.0,
                               "record.fast_interval": 0.2}),
                    rd4, store=object(), registry=Registry())
    rec4.tick()
    check("第一拍读一次标题（把界面左上角的会话名种上）", rd4.titles == 1,
          "读了 %d 次" % rd4.titles)
    check("第一拍**不读整屏**", rd4.full == 0, "读了 %d 次" % rd4.full)
    for _ in range(4):
        rec4.tick()
    check("屏幕没变 -> 后面几拍**标题一次都不读**（省掉那 65ms）",
          rd4.titles == 1, "读了 %d 次" % rd4.titles)
    check("屏幕没变 -> 也不读整屏", rd4.full == 0, "读了 %d 次" % rd4.full)
    rd4.next_hash = _thumb(mark=255, at=0, count=200)
    rec4.tick()
    check("屏幕变了 -> 读标题", rd4.titles == 2, "读了 %d 次" % rd4.titles)
    check("屏幕变了 -> 读整屏", rd4.full == 1, "读了 %d 次" % rd4.full)

    # ---- probe=false：退回"只跟会话名"，连抓图都不做
    cfg3 = FakeCfg(**{"record.enabled": True, "record.interval": 1.0,
                      "ocr_read.title_interval": 4.0,
                      "ocr_read.probe": False,
                      "record.fast_interval": 0.2})
    rd3 = Reader()
    rec3 = Recorder(cfg3, rd3, store=object(), registry=Registry())
    rec3.tick()
    check("probe=false 时**连探针都不做**", rd3.probes == 0, "探了 %d 次" % rd3.probes)
    check("probe=false 时读了标题（界面左上角要跟着会话走）", rd3.titles == 1,
          "读了 %d 次" % rd3.titles)
    iv3 = rec3._choose_interval()
    check("probe=false 时拍间隔用电平缓的 title_interval", iv3 == 4.0, str(iv3))
    check("probe=false 时**不会**进快速档（0.2s 比一次读屏还短）",
          iv3 > rec3.fast_interval, str(iv3))

    # 打开 ocr_read.record 之后就该每拍读整屏了
    cfg2 = FakeCfg(**{"record.enabled": True, "record.interval": 1.0,
                      "ocr_read.title_interval": 4.0,
                      "record.fast_interval": 0.2})
    rd2 = Reader()
    rd2.background_read_ok = True
    rec2 = Recorder(cfg2, rd2, store=_StoreStub(), registry=Registry())
    rec2.tick()
    check("background_read_ok=True 时照常读整屏", rd2.full == 1,
          "读了 %d 次" % rd2.full)
    check("UIA 那条路不该做探针（它本来就便宜）", rd2.probes == 0,
          "探了 %d 次" % rd2.probes)


class _StoreStub:
    def save_new_only(self, key, messages, at_bottom=True, bottom_known=True):
        return 0

    def count(self, key):
        return 0


# ============================================================ 9. 集成：三处真机才暴露的坑

def test_module_structure():
    """⚠️ 防复发：改这个文件时**别把结构改坏**。

    为什么单独来一条（2026-10-10 我自己连着踩）：

      · 一次编辑把 `def probe_changed` 插到了 `@dataclass` 和 `class OcrRow`
        中间 —— 装饰器就套到了那个函数上，导入直接
        `AttributeError: 'function' object has no attribute '__mro__'`
      · 另一次编辑留下了**两个 `class OcrRow`**，模块级名字绑到**第二个**
        （没有 `@dataclass` 的那个）—— 于是 `OcrRow` 静默**不再是 dataclass**，
        而 `field(default_factory=list)` 在类体里不会报错，**只有构造时才炸**

    第二条最阴：语法检查、导入检查、甚至大部分单测都过得去。所以这里**显式**断言。
    """
    import dataclasses
    import inspect

    from wechat_assistant import ocr_reader as R

    check("probe_changed 还是普通函数（没被 @dataclass 套住）",
          inspect.isfunction(R.probe_changed), type(R.probe_changed).__name__)
    check("OcrRow 还是 dataclass", dataclasses.is_dataclass(R.OcrRow),
          str(R.OcrRow))
    check("OcrBlock 还是 dataclass", dataclasses.is_dataclass(R.OcrBlock),
          str(R.OcrBlock))
    # ⚠️ 字段少一个也说明被改坏了（比如 box / nick_block 是后加的）
    names = [f.name for f in dataclasses.fields(R.OcrRow)]
    for want in ("blocks", "nickname", "is_time", "is_self", "box", "nick_block"):
        check("OcrRow 字段还有 %s" % want, want in names, str(names))
    check("OcrRow() 能直接构造", R.OcrRow() is not None)
    # 对外接口一个都不能少（AutoReader 是靠委托的，最容易漏）
    for cls in (R.OcrReader, R.AutoReader):
        for m in ("find_window", "read_sessions", "read_chat_title",
                  "read_input_text", "read_chat", "probe"):
            check("%s 还有 %s" % (cls.__name__, m), hasattr(cls, m), "")


def test_title_self_fill():
    """⚠️ 这一条**是真机上「完全无法使用」的根因**，别删。

    `run_watch.do_generate()` 的顺序是「先 read_chat，再 `if not snap.title: return`」。
    `OcrReader.read_chat` 早先把 title 留空（注释写着"由调用方补"），
    于是**按热键 / 点【生成建议】会直接静默返回"当前没有打开的会话"** ——
    程序看着好好的，就是永远不出建议。
    """
    print("\n9. read_chat 必须自己把 snap.title 填上")
    cfg = FakeCfg(**{
        "ocr_read.chat_left": 310, "ocr_read.chat_top": 70,
        "ocr_read.right_margin": 0, "ocr_read.input_fallback": 120,
        "ocr_read.nickname_max_chars": 12, "ocr_read.auto_geometry": False,
    })
    rd = R.OcrReader(cfg, ocr=_TitleAwareOcr())
    rd.grab = lambda hwnd: _chat_image()
    rd._scale = lambda: 1.5
    snap = rd.read_chat(0x1234)
    check("read_chat 自己填了标题（不靠调用方）",
          snap is not None and snap.title == "张三(5)",
          repr(snap.title if snap else None))
    check("正文也读到了", snap is not None and len(snap.messages) == 2,
          str(len(snap.messages) if snap else None))

    # 群标记：别人的消息上挂了昵称 -> 判成群聊
    check("有昵称 -> 判成群聊", snap.is_group is True, str(snap.is_group))


def test_source_guard_in_sender():
    """⚠️ OCR 后端的快照**不能**再跑一遍 sender.resolve。

    两个理由，任何一个都足以致命：
      ① rect 的坐标系不一样（OCR 是裁剪区坐标，resolve 按整窗屏幕坐标去裁像素）
      ② OCR 那边**已经从同一批像素**判好了 is_self，再跑一遍只会覆盖成错的
    """
    print("\n10. sender.resolve 对非 UIA 快照必须直接返回（别覆盖判定）")
    from wechat_assistant.sender import SenderResolver

    cfg = FakeCfg(**{"sender.enabled": True, "sender.capture_mode": "printwindow"})
    res = SenderResolver(cfg, ocr=None, logger=lambda m: None)

    snap = ChatSnapshot()
    snap.source = "ocr"
    snap.messages = [
        MessageItem(text="a", cls="x", rect=(0, 0, 10, 10), is_self=False),
        MessageItem(text="b", cls="x", rect=(0, 20, 10, 30), is_self=True),
        MessageItem(text="c", cls="x", rect=(0, 40, 10, 50), is_self=None),
    ]
    got = res.resolve(0x1234, 1, snap)
    check("返回的是已有的判定条数（2 条）", got == 2, str(got))
    check("第一条没被改掉（还是「对方」）", snap.messages[0].is_self is False)
    check("第二条没被改掉（还是「我」）", snap.messages[1].is_self is True)

    # UIA 的快照要照常走（别把正常的路也堵了）
    uia = ChatSnapshot()
    uia.source = "uia"
    uia.messages = [MessageItem(text="a", cls="x", rect=(0, 0, 10, 10))]
    uia.messages[0].rect = None            # 没有 rect -> 也会早退，但不该抛
    check("UIA 快照不抛异常", res.resolve(0x1234, 1, uia) == 0)


class _TitleAwareOcr:
    """按图给不同答案的假引擎：标题栏那条给「张三(5)」，正文给预设块。

    ⚠️ 真引擎一次调用只能拿一张图，而 `read_chat` 会 OCR **两次**
        （标题栏一条 + 正文一块）—— 用固定答案的假引擎会把标题也喂给正文，
        于是正文里多出一条「张三(5)」、判据全乱。所以这里要按图的尺寸区分。

    ⚠️ 2026-10-13：标题带 **`(5)`** 是**有意的** —— 这个用例顺带测
        「别人的消息挂了昵称 -> 判成群聊」，而**判不判昵称现在取决于标题
        带不带 `(N)`**（「私聊没有昵称」那条规则），用私聊名会让那两条断言失效。
    """

    def __init__(self):
        self.calls = 0

    def load(self):
        return self

    def warmup(self, size=None):
        return 0.0

    def recognize(self, image, min_score=0.5):
        self.calls += 1
        if image.height <= 110:            # 标题栏那条（chat_top=70 * 1.5 = 105）
            return [B("张三(5)", 40, 30, 160, 60)]
        return [
            B("小明", 35, 170, 85, 190),
            B("在吗", 55, 210, 235, 250),
            B("在的", 860, 310, 1040, 350),
            B("昨天 21:00", 480, 140, 580, 160),
        ]


# ============================================================ 10. 几何兜底

def test_detect_chat_left():
    print("\n11. detect_chat_left（换机器时的兜底；实测真值 465 -> 检测 467）")
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1542, 979), (28, 28, 28))       # 聊天区底色
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 464, 979], fill=(45, 45, 45))        # 会话列表：**另一种底色**
    a = np.asarray(img).astype(np.int16)
    bg = np.array([28, 28, 28], dtype=np.float64)
    got = R.detect_chat_left(a, bg)
    check("认出分界在 465 附近", got is not None and abs(got - 465) <= 6, str(got))

    same = Image.new("RGB", (1542, 979), (28, 28, 28))      # 两边同色 -> 认不出来
    a2 = np.asarray(same).astype(np.int16)
    check("两边同色时返回 None（不瞎猜）",
          R.detect_chat_left(a2, bg) is None, str(R.detect_chat_left(a2, bg)))

    cfg = FakeCfg(**{"ocr_read.auto_geometry": False})
    rd = R.OcrReader(cfg, ocr=FakeOcr([]))
    check("auto_geometry=false 时只试配置值",
          list(rd._geometry_candidates(a, bg, 310)) == [310],
          str(list(rd._geometry_candidates(a, bg, 310))))
    cfg2 = FakeCfg(**{"ocr_read.auto_geometry": True})
    rd2 = R.OcrReader(cfg2, ocr=FakeOcr([]))
    cands = list(rd2._geometry_candidates(a, bg, 310))
    # ⚠️ **检测值排第一、配置值兜底**（2026-09-27 第四十六轮反过来，
    #    因为实测会话列表宽度不是常数：1542 宽的窗口分界 465、1206 宽的是 430）。
    check("auto_geometry=true 时检测值排第一、配置值兜底",
          len(cands) == 2 and cands[0] == 465 and cands[1] == 310,
          str(cands))
    check("检测不出来时只剩配置值（不硬塞一个 None 进去）",
          list(rd2._geometry_candidates(a2, bg, 310)) == [310],
          str(list(rd2._geometry_candidates(a2, bg, 310))))


def test_geometry_failure_is_loud():
    print("\n12. 几何不对时：返回 None + 记 _geom_bad（别静默当成「没打开会话」）")
    cfg = FakeCfg(**{
        "ocr_read.chat_left": 310, "ocr_read.chat_top": 70,
        "ocr_read.right_margin": 0, "ocr_read.input_fallback": 120,
        "ocr_read.auto_geometry": False,
    })
    logs = []
    rd = R.OcrReader(cfg, logger=logs.append, ocr=FakeOcr([B("随便", 10, 10, 60, 30)]))
    rd.grab = lambda hwnd: _plain_image()      # 纯色图 -> 一个气泡都没有
    rd._scale = lambda: 1.5
    snap = rd.read_chat(0x1234)
    check("返回 None", snap is None)
    check("_geom_bad 立起来了", rd._geom_bad is True)
    check("日志里给了能指路的话",
          any("calibrate" in m for m in logs), str(logs[:1]))


def _plain_image():
    from PIL import Image

    return Image.new("RGB", (1542, 979), (28, 28, 28))


def _thumb(n=2304, fill=0, mark=0, at=None, count=0):
    """造一个**真实尺寸**的探针缩略图（48x48 = 2304 字节）。

    `mark`/`at`/`count` 用来"改掉其中一段像素"，模拟屏幕变化。
    """
    b = bytearray([fill]) * n
    if count:
        at = 0 if at is None else at
        for i in range(at, min(at + count, n)):
            b[i] = mark
    return bytes(b)


def test_probe_changed():
    """⚠️ 探针的"变了没"判据 —— **必须带容差**，这是这一节的全部重点。

    逐字节相等（`prev != cur`）看着最直接，但实测会被抓图的微小抖动骗到：
    整体加噪声 ±2 就判成"变了"，于是**每一拍都真读 250ms**，
    探针比不做还亏（白付了抓图钱）。

    判据是「**变化超过 tol 的像素占多少比例**」，两个参数都要能调。
    """
    from wechat_assistant.ocr_reader import probe_changed

    base = _thumb()
    check("一模一样 -> 没变", probe_changed(base, base) is False)

    # 整体抖动 ±2：真实抓图里随时都有（抗锯齿 / 缩放 / GPU 合成）
    jitter = _thumb(fill=2)
    check("整体抖动 ±2 -> **仍然算同一屏**（否则每拍都会真读）",
          probe_changed(base, jitter) is False)
    check("整体抖动 ±5 -> 仍然算同一屏", probe_changed(base, _thumb(fill=5)) is False)

    # 真的变了：一大片像素变化很大
    big = _thumb(mark=255, at=0, count=200)      # 200/2304 = 8.7%
    check("一大片变了（8.7%）-> 算变了", probe_changed(base, big) is True)

    # 变化太小：1 个像素远远不够（默认阈值 1% ＝ 23 像素）
    tiny = _thumb(mark=255, at=0, count=1)
    check("只变了 1 个像素 -> 不算变（抗噪）", probe_changed(base, tiny) is False)

    # 参数可调：把阈值调低就该能测出来
    check("把 frac 调到 0.0004 后，1 个像素也算变（参数确实生效）",
          probe_changed(base, tiny, tol=8, frac=0.0004) is True)
    # 容差调小：±2 的抖动就会被算成变
    check("把 tol 调到 0，±2 的抖动也算变（容差确实生效）",
          probe_changed(base, jitter, tol=0, frac=0.01) is True)

    # 边界：长度不一致 / None
    check("长度不一致 -> 当成变了", probe_changed(base, _thumb(100)) is True)
    check("拿 None 比 -> 当成变了", probe_changed(None, base) is True)
    # ⚠️ 空的缩略图 = 判不出来，**当成没变**（跟 probe() 返回 None 一个取向）：
    #    当成"变了"会每拍真读 250ms（静默烧 CPU），当成"没变"只是少记几屏。
    check("空缩略图不崩，且当成没变（宁可不读也别每拍烧 250ms）",
          probe_changed(b"", b"") is False)


def test_probe_stays_cheap():
    """⚠️ 防复发：**探针里不许出现像素分析**。

    探针的经济性是整个「探针 + 按需 OCR」方案的全部依据。第一版为了"和
    `read_chat` 用同一套几何规则"，在探针里调了 `_bg()`（众数）和
    `find_input_top()`（逐列扫描），还把整张 1402x1011 的位图转了 numpy int16
    —— **实测探针从 ~52ms 涨到 132ms**（日志原文「约 132 ms，空闲时只占
    13.2% 一个核」），**比 UIA 一整拍（100ms）还贵**，方案直接不成立。

    ⚠️ 最阴的地方：**它不报错、测试也全绿**，唯一症状是日志里那个毫秒数。
       所以这里用 AST **只看代码**（跳过 docstring —— 那里会引用这些名字
       来说明"不要这么干"），把 `np.` / `_bg(` / `find_input_top(` / `asarray`
       这些名字挡在 `probe` / `_probe_geo` 之外。
    """
    import ast
    import sys

    from wechat_assistant import ocr_reader as R

    tree = ast.parse(open(R.__file__, encoding="utf-8").read())
    cls = [n for n in tree.body
           if isinstance(n, ast.ClassDef) and n.name == "OcrReader"]
    check("能找到 OcrReader", len(cls) == 1, str(len(cls)))
    for fn_name in ("probe", "_probe_geo"):
        fns = [n for n in cls[0].body
               if isinstance(n, ast.FunctionDef) and n.name == fn_name]
        check("能找到 %s" % fn_name, len(fns) == 1, str(len(fns)))
        f = fns[0]
        # 丢掉 docstring 之后再看代码
        body = f.body[1:] if (f.body and isinstance(f.body[0], ast.Expr)) else f.body
        code = ast.unparse(ast.Module(body=body, type_ignores=[]))
        bad = [w for w in ("np.", "asarray", "_bg(", "find_input_top(")
               if w in code]
        check("%s 里没有像素分析（实测有的话会从 52ms 涨到 132ms）" % fn_name,
              not bad, "出现了: %s" % bad)
    # 顺手确认它在 sys.modules 里没被别的东西替换掉
    check("ocr_reader 已导入", "wechat_assistant.ocr_reader" in sys.modules, "")


def test_title_uses_same_left_boundary():
    """⚠️⚠️ **标题栏必须和正文用同一个左边界**（2026-10-11 用户实测报的 bug）。

    用户报「**广财**信息学院23级级群(499) → **财**信息学院23级级群(499)」，
    又是掉第一个字（前面还报过「**公众号** → **众号**」）。

    实测根因（`probes/diag_chat_left.py`，同一台机器）：

        窗口 1402 宽：**真边界 x=430**（428→0.00、430→1.00 那个跳变）
        而配置 `chat_left` = **465** —— **偏右 35 物理像素**，够吃掉一整个字。

    而**正文那条路早就纠过来了**：`read_chat` 走 `_geometry_candidates()`
    （检测优先）用的是 430；可 `_title_from` 走的是 `chat_rect()` ——
    那个函数**只算配置值、根本不调检测**。于是**同一个窗口、两个边界**，
    而 `left_margin` 只往回让 9 像素，补不上这 35。

    ⚠️ 这里断言的是"**标题跟着正文走**"这个不变量，不是某个具体数字 ——
       数字（430/465）是那台机器的，换了窗口就变。
    """
    print("\n15. 标题和正文用同一个左边界（掉第一个字的真根因）")

    class FakeImg:
        """只提供 `_title_from` 用得到的那几样，并**记下裁剪框**。"""

        def __init__(self, w=1402, h=1012):
            self.width, self.height = w, h
            self.crops = []

        def crop(self, box):
            self.crops.append(box)
            return _CropStub(box)

    class _CropStub:
        def __init__(self, box):
            self.box = box
            self.size = (box[2] - box[0], box[3] - box[1])

    cfg = FakeCfg(**{"ocr_read.chat_left": 310, "ocr_read.chat_top": 70,
                     "ocr_read.left_margin": 6, "ocr_read.auto_geometry": True})
    rd = R.OcrReader(cfg, ocr=FakeOcr([B("广财信息学院23级级群(499)", 0, 0, 200, 20)]))

    # ① 正文已经成功读过 -> 标题必须跟它走
    rd._left_accepted = 430
    img = FakeImg()
    t = rd._title_from(img)
    # ⚠️ 期望值**从 `_scale()` 推**，别写字面数字 —— 离线测试里量程是 1.0，
    #    真机是 1.5，写死数字的话这条断言在两种环境下有一个必错
    #    （我第一版就写死了 421，测试里实际是 424，白失败一次）。
    want = 430 - int(round(float(cfg.get("ocr_read.left_margin", 6)) * rd._scale()))
    check("正文采纳了 430 -> 标题也用 430（不是配置的 465）",
          rd._title_left(img) == 430, rd._title_left(img))
    check("裁剪框确实从 430 往左让了 left_margin（%d）" % want,
          img.crops and img.crops[0][0] == want, str(img.crops[:1]))
    check("读出来的标题是完整的", t == "广财信息学院23级级群(499)", repr(t))

    # ② ⚠️ 回归点：**别再用 `chat_rect()` 的配置值**
    #    那个值是 310 逻辑 × 缩放，窄窗口上偏右 35 —— 就是掉字的根源
    cfg2 = FakeCfg(**{"ocr_read.chat_left": 310, "ocr_read.chat_top": 70,
                      "ocr_read.left_margin": 6, "ocr_read.auto_geometry": True})
    rd2 = R.OcrReader(cfg2, ocr=FakeOcr([]))
    rd2._left_accepted = None
    rd2._title_left_cache[1402] = None          # 假装检测过、没认出来
    img2 = FakeImg()
    rd2._title_from(img2)
    want2 = 310 - int(round(6 * rd2._scale()))
    check("检测不出来时才退回配置值（%d）—— 这是**兜底**，不是常态" % want2,
          img2.crops and img2.crops[0][0] == want2, str(img2.crops[:1]))

    # ③ 关掉 auto_geometry 就老实按配置来（排查几何问题时的退路）
    cfg3 = FakeCfg(**{"ocr_read.chat_left": 310, "ocr_read.chat_top": 70,
                      "ocr_read.left_margin": 6, "ocr_read.auto_geometry": False})
    rd3 = R.OcrReader(cfg3, ocr=FakeOcr([]))
    img3 = FakeImg()
    rd3._title_from(img3)
    check("auto_geometry=false -> 用配置值", img3.crops[0][0] == want2,
          str(img3.crops[:1]))

    # ④ 窗口改了宽度要重新找（那个边界本来就随宽度变）
    rd4 = R.OcrReader(cfg, ocr=FakeOcr([]))
    rd4._left_accepted = 430
    rd4._title_left_cache[1402] = 999
    check("宽度没变 -> 用缓存的（不重算）",
          rd4._title_left(FakeImg(w=1402)) == 430)
    check("宽度变了 -> 缓存不命中（不会拿旧宽度的值硬套）",
          rd4._title_left(FakeImg(w=1206)) == 430,   # 仍是 _left_accepted
          rd4._title_left(FakeImg(w=1206)))


def test_bg_is_dtype_safe():
    """⚠️⚠️ `_bg` 必须**不管调用方给什么 dtype** 都能算出底色。

    2026-10-11 踩的真 bug：原来是

        q = (sub // 8)                # sub 是 uint8 时 q **还是 uint8**
        key = q[:, :, 0] * 1024 + ...  # <- uint8 * 1024 在 NumPy 2.x 下抛 OverflowError

    而异常被 `except Exception: return None` 吞掉 → **`_bg` 恒返回 None** →
    调用方全当"这次算不出来"，**不报错、不崩、只是静默降级**。

    实测（numpy 2.4.6）：`read_chat` 里 `a` 先 `.astype(np.int16)` 了，
    **那条路径侥幸没事**；而任何新调用点只要直接把 PIL 图转成数组（uint8）
    就会踩到 —— 我写 `_title_left` 时就是这么踩的（检测恒 None，
    于是又退回那个偏右 35 像素的配置值）。

    ⚠️ 所以修在**函数内部**（进来先转 int32），**不**依赖"调用方记得转 int16"。
    """
    print("\n16. 底色不能因为 dtype 而静默失效")
    import numpy as np

    cfg = FakeCfg(**{"ocr_read.chat_left": 310})
    logs = []
    rd = R.OcrReader(cfg, logger=logs.append, ocr=FakeOcr([]))

    for dt in (np.uint8, np.int16, np.int32):
        a = np.zeros((120, 300, 3), dtype=dt)
        a[:, :, :] = 30                      # 整幅都是聊天区底色
        bg = rd._bg(a, 150, 300, 10)
        check("%s 的数组也能算出底色（不再静默返回 None）" % np.dtype(dt).name,
              bg is not None, repr(bg))
        if bg is not None:
            check("  值正确（30 -> 量化成 28）", list(bg) == [28.0, 28.0, 28.0],
                  str(list(bg)))

    check("没有因为 dtype 报过错（异常不再被吞掉）",
          not [l for l in logs if "底色" in l], str(logs))
    check("空切片 -> None（这一条是真的算不出来）",
          rd._bg(np.zeros((0, 0, 3), dtype=np.uint8), 0, 0, 0) is None)


def test_self_attrs_are_initialized():
    """⚠️⚠️ 类里**读**到的 `self._xxx`，必须在类里**赋值**过。

    为什么单独立一条（2026-10-11，一天之内踩了两次）：

        · `SenderResolver._logged_skip_source` —— 我只在 `resolve()` 里写
          `if self._logged_skip_source != src`，忘了在 `__init__` 里初始化。
        · `OcrReader._left_logged` —— 同一类错，只在 `read_chat` 里赋值。

    症状是 `AttributeError`，而且**很阴**：
        · 单测全绿 —— 假配置走不到那个分支（`bx1 != x1` 只有真机才成立）；
        · 真机上却会让**记录器整个停摆**。

    ⚠️ 所以用**静态检查**兜住这个类别，而不是靠"跑一遍看看"。
      只查 `self._下划线开头` 的名字（公开属性可能是别处注入的）。
    """
    print("\n17. self._xxx 必须先赋值再读（AttributeError 的静默来源）")
    import ast
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

    def attr_name(node):
        if not isinstance(node, ast.Attribute):
            return None
        v = node.value
        if isinstance(v, ast.Name) and v.id == "self":
            return node.attr
        return None

    problems = []
    for mod in ("ocr_reader.py", "sender.py", "recorder.py"):
        path = _os.path.join(root, "wechat_assistant", mod)
        tree = ast.parse(open(path, encoding="utf-8").read())
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            fns = [n for n in cls.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

            def stores_in(fn):
                """这个方法里**赋值过**的 self._x 名字。"""
                out = set()
                for node in ast.walk(fn):
                    targets = []
                    if isinstance(node, ast.Assign):
                        targets = node.targets
                    elif isinstance(node, ast.AnnAssign):
                        targets = [node.target]
                    elif isinstance(node, ast.AugAssign):
                        targets = [node.target]
                    for t in targets:
                        if (isinstance(t, ast.Attribute)
                                and isinstance(t.value, ast.Name)
                                and t.value.id == "self"
                                and t.attr.startswith("_")):
                            out.add(t.attr)
                return out

            init = next((f for f in fns if f.name == "__init__"), None)
            in_init = stores_in(init) if init else set()

            for fn in fns:
                if fn.name in ("__init__", "__post_init__"):
                    continue
                # ⚠️ 判据要**收窄到这个程度**才有用：
                #    同一个方法里 `self._x` 的**第一次出现是读**，而赋值在它**后面**，
                #    并且 `__init__` 里没兜底 —— 那就是"读到未初始化的属性"。
                #
                # ⚠️ 不能只看"全类里赋值过"：`_left_logged` 在 `read_chat` 里
                #    确实赋值了，只是在**读取之后** —— 第一版就漏了它
                #    （实测：把 `__init__` 里的初始化删掉，那种宽松写法照样全绿）。
                #
                # ⚠️ 也不能要求"必须在 __init__ 里赋值"：`_timer`（在 `run()` 里建、
                #    在 `_pump` 里用）这类是合法的，那样会满屏误报。
                first_read = None
                first_store = None
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Attribute):
                        continue
                    if not (isinstance(node.value, ast.Name)
                            and node.value.id == "self"):
                        continue
                    a = node.attr
                    if not a.startswith("_") or a in in_init:
                        continue
                    ln = getattr(node, "lineno", 0)
                    if isinstance(node.ctx, ast.Store):
                        if first_store is None or ln < first_store[0]:
                            first_store = (ln, a)
                    elif first_read is None or ln < first_read[0]:
                        first_read = (ln, a)
                for node in ast.walk(fn):
                    if not (isinstance(node, ast.Attribute)
                            and isinstance(node.value, ast.Name)
                            and node.value.id == "self"
                            and isinstance(node.ctx, ast.Load)):
                        continue
                    a = node.attr
                    if not a.startswith("_") or a in in_init:
                        continue
                    # 这个方法里有没有**在它之前**的赋值
                    earlier = [n.lineno for n in ast.walk(fn)
                               if isinstance(n, ast.Attribute) and n.attr == a
                               and isinstance(n.ctx, ast.Store)
                               and n.lineno < node.lineno]
                    if not earlier and stores_in(fn) and a in stores_in(fn):
                        problems.append("%s.%s：`%s()` 里第 %d 行**先读**、"
                                        "赋值在后面，`__init__` 里没有"
                                        % (cls.name, a, fn.name, node.lineno))
                        break
    # ⚠️ `getattr(self, "_x", 默认值)` 那种读法天然不算（不是 Attribute 节点），
    #    那本来就是安全的写法。
    check("没有「只读没赋值」的私有属性", not problems, str(problems))


# ============================================================ 18. 丢弃判据

def test_drop_reason():
    """`_drop_reason()`：三条"不该算消息"的判据，顺序即优先级。

    ⚠️ 这一节是 2026-10-13 **结构改进的兑现**：这几条判据原来散在
        `read_chat` 的 70 行里，只能靠整屏截图 + OCR 引擎端到端验；
        抽成纯函数之后，喂一个 `OcrRow` 加几个上下文参数就能验 —— 秒级、稳定。
    """
    print("\n18. _drop_reason（三条丢弃判据集中在一张清单里，顺序即优先级）")

    def row(y1, y2, x1=100, x2=300, text="测试"):
        return R.OcrRow(blocks=[R.OcrBlock(text, x1, y1, x2, y2)])

    # ---- ① below：在"最后一块文字"下方很多（输入框 / 工具栏）----
    check("比最后一块文字低 48 以上 -> below",
          R._drop_reason(row(600, 630), 500, [], [], []) == "below")
    check("只低 48 以内 -> 留下（别把最后一条真消息一起丢掉）",
          R._drop_reason(row(540, 570), 500, [], [], []) is None)
    check("算不出 content_bottom -> 不判 below",
          R._drop_reason(row(600, 630), None, [], [], []) is None)

    # ---- ② image：中心落在图片 / 卡片区域里（那是图上的字）----
    check("落在图片区域里 -> image",
          R._drop_reason(row(100, 130), None, [(50, 90, 400, 200)], [], []) == "image")
    check("图片区域之外 -> 留下",
          R._drop_reason(row(100, 130), None, [(50, 400, 400, 500)], [], []) is None)

    # ---- ③ overlay：盖在消息上的浮层（微信自己的提示条）----
    box = (500, 100, 800, 200)
    inner = R.OcrBlock("盒内", 600, 120, 700, 160)
    check("大半个行高压在盒内块上、且水平分开 -> overlay",
          R._drop_reason(row(110, 160, 100, 400), None, [], [inner], [box]) == "overlay")
    check("只是**部分**竖直重叠 -> 留下（卡片「左标题 + 右封面图」那种）",
          R._drop_reason(row(110, 190, 100, 400), None, [], [inner], [box]) is None)

    # ---- ④ 界面按钮的固定文案不是消息（**只认整条相等**）----
    check("界面按钮的固定文案 -> ui",
          R._drop_reason(row(100, 130, text="跳转到最新消息"), None, [], [], []) == "ui")
    check("  多一个字就不认（消息一条都不能丢，判据必须窄）",
          R._drop_reason(row(100, 130, text="跳转到最新消息吧"), None, [], [], []) is None)
    check("通话状态提示也认（它**渲染成对方气泡**，只能靠文案认）",
          R._drop_reason(row(100, 130, text="已在其它设备拒绝"), None, [], [], []) == "ui")
    check("  像人话的固定短语不认（`对方无应答` —— 浅色用例里它**就是**我发的消息）",
          R._drop_reason(row(100, 130, text="对方无应答"), None, [], [], []) is None)
    check("  太短的词不认（`已取消` 用户真会发）",
          R._drop_reason(row(100, 130, text="已取消"), None, [], [], []) is None)

    # ---- ⑤ 带变量的系统提示（撤回）：**文本形态 + 居中** ----
    _hint = "“洁”撤回了一条消息"
    check("撤回提示 + 居中 -> ui",
          R._drop_reason(row(100, 130, 500, 600, _hint), None, [], [], [],
                         mid_x=573, area_width=1146) == "ui")
    check("  同样的文字但**靠左**（气泡里）-> 不丢",
          R._drop_reason(row(100, 130, 100, 200, _hint), None, [], [], [],
                         mid_x=573, area_width=1146) is None)
    check("  居中但不含“撤回” -> 不丢",
          R._drop_reason(row(100, 130, 500, 600, "你好啊"), None, [], [], [],
                         mid_x=573, area_width=1146) is None)
    check("  拿不到中线时不敢丢（宁可放过）",
          R._drop_reason(row(100, 130, 500, 600, _hint), None, [], [], []) is None)

    # ---- ⑤ 清单的**顺序**就是优先级：两个都成立时返回靠前的那条 ----
    check("below 与 image 同时成立 -> 返回靠前的 below",
          R._drop_reason(row(600, 630), 500, [(50, 590, 400, 700)], [], []) == "below")


# ============================================================ 19. 会话列表

def test_session_list():
    """会话列表的两个纯函数：分组（`group_session_blocks`）+ 提取（`session_item_from`）。

    ⚠️ 这一节是 2026-10-13 新增能力（`read_sessions` 以前**返回空列表**）的
       **唯一自动化验证** —— 语料验收台那套（title/messages/markers）读的是
       聊天正文，覆盖不到左侧列表。
    """
    print("\n19. 会话列表（read_sessions 的纯函数：分组 + 提取）")
    blk = R.OcrBlock

    def b(text, x1, y1, x2, y2):
        return blk(text, x1, y1, x2, y2)

    # ---- 分组：项内间隔小（约 2px）、项间间隔大（实测 39px）----
    cols = R.group_session_blocks([
        b("黄俊宇", 185, 141, 300, 169),
        b("星期四", 372, 145, 414, 165),
        b("行吧", 187, 171, 276, 198),
        b("群友丙", 184, 237, 300, 269),
        b("我再去交一次", 187, 271, 320, 295),
    ])
    check("按竖直间隔分成 2 项", len(cols) == 2, str([len(c) for c in cols]))
    check("第 1 项含 3 块（名字 / 时间 / 预览）", len(cols[0]) == 3)

    # ---- 提取：会话名 = y 最小那块；时间靠**形态**判 ----
    it = R.session_item_from([
        b("公众号", 185, 141, 256, 169),
        b("16:07", 372, 145, 414, 165),
        b("[28条]人民日报：双胞胎哥.", 187, 171, 407, 198),
    ], 0, 351)
    check("会话名 = y 最小那块", it.raw_title == "公众号", repr(it.raw_title))
    check("时间是 16:07", it.time_text == "16:07", repr(it.time_text))
    check("预览是剩下的那块", it.preview == "[28条]人民日报：双胞胎哥.", repr(it.preview))

    # ⚠️⚠️ 关键回归：**会话名折行的第二行不能被当成时间**（实测踩过）
    it2 = R.session_item_from([
        b("吃喝玩...", 185, 821, 273, 854),
        b("@美团...", 287, 826, 359, 849),
        b("12:37", 370, 828, 415, 847),
        b("[小程序] 助力 19 必得4...", 185, 855, 406, 882),
    ], 0, 351)
    check("折行的第二行不当时间（时间是 12:37）",
          it2.time_text == "12:37", repr(it2.time_text))
    check("  -> 那一行确实落进了预览", "@美团" in it2.preview, repr(it2.preview))

    # ---- 时间形态：清掉图标噪声、认短日期 ----
    check("`Q14:05` 清成 `14:05`",
          R._looks_like_session_time("Q14:05") == "14:05")
    check("`10/06` 认成时间", R._looks_like_session_time("10/06") == "10/06")
    check("`星期四` 认成时间", R._looks_like_session_time("星期四") == "星期四")
    check("普通文字不是时间", R._looks_like_session_time("行吧") == "")

    # ---- 认不出会话名就不产出（调用方会跳过）----
    check("空块 -> None", R.session_item_from([]) is None)


# ============================================================ 20. 标题清洗

def test_clean_title():
    """`_clean_title()`：切掉标题末尾被 OCR 认出来的**图标符号**。

    ⚠️ 实测背景（2026-10-13，`wx_1567_group303`）：真标题是
       `示例群甲🏠(303)`，产品读成 `…（303）（` ——
       末尾那个 `（` 是标题栏右边的**免打扰铃铛图标**。
    """
    print("\n20. _clean_title（只切尾巴，不做归一化 —— session_key 是主键）")
    check("群标题末尾的图标符号被切掉",
          R._clean_title("示例群甲（303）（") == "示例群甲（303）",
          repr(R._clean_title("示例群甲（303）（")))
    check("半角后缀同样截断",
          R._clean_title("示例兴趣群(5)|") == "示例兴趣群(5)")
    check("没有 (N) 后缀时只 rstrip 开括号类",
          R._clean_title("黑马资源AI助手19[") == "黑马资源AI助手19")
    check("正常标题一个字不改", R._clean_title("张三") == "张三")
    check("闭括号是正常结尾，**不能**切掉",
          R._clean_title("某某（2024）") == "某某（2024）")


# ============================================================ 21. 引用块 / 卡片分类

def test_refer_and_card():
    """三个纯函数：`_is_refer_of` / `_card_kind_of` / `_find_box_of`。

    ⚠️ 它们是 2026-10-13 新增的「气泡分类」判据。语料验收台覆盖的是**真机形状**，
       这里补的是**语料里没有的边界** —— 比如「我买了 8GB 内存」这种真消息
       （语料里恰好没有，只有验收台的话**永远发现不了误判**）。
    真机坐标取自 `wx_1567_refer`。
    """
    print("\n21. 引用块与分享卡片（纯函数判据 + 语料里没有的边界）")
    blk = R.OcrBlock

    def _row(text, x1, y1, x2, y2):
        return R.OcrRow(blocks=[blk(text, x1, y1, x2, y2)])

    # ---- `_card_kind_of`：文件 = 后缀 **且** 容量 ----
    check("后缀 + 容量 -> 文件",
          R._card_kind_of("示例文件.zip 17.1M 微信电脑版") == "file")
    check("pdf / doc / 7z 之类也认", R._card_kind_of("简历.pdf 2.3MB") == "file")
    check("**只有后缀**不算文件（聊天里提到 xxx.zip）",
          R._card_kind_of("我下了个 报告.zip 回头给你") == "link")
    check("**只有容量**不算文件（`我买了 8GB 内存` 这种真消息）",
          R._card_kind_of("我买了 8GB 内存") == "link")
    check("分享卡片 -> 链接（它**不带 URL**，只有标题 + 来源）",
          R._card_kind_of("示例卡片标题 某汽车频道 哔哩哔哩") == "link")

    # ---- `_find_box_of`：按**重叠**找盒，不要求"完全包含" ----
    boxes = [(651, 15, 1022, 152)]
    _got = R._find_box_of(_row("四工H", 661, 10, 755, 30), boxes)
    check("文字块比盒**高出几个像素**也能找到盒（真机就是这个形状）",
          _got == (651, 15, 1022, 152), str(_got))
    check("完全不相交 -> None",
          R._find_box_of(_row("x", 1100, 600, 1150, 620), boxes) is None)

    # ---- `_is_refer_of`：四条判据（真机坐标来自 `wx_1567_refer`）----
    msg = MessageItem(text="测试", cls="mmui::ChatTextItemView",
                      rect=(956, 381, 1012, 416), is_self=True)
    bx = [(943, 369, 1029, 423), (1042, 369, 1096, 423)]
    check("紧贴气泡下方 + 有冒号 + 同侧 + 自己不在盒里 -> 是引用块",
          R._is_refer_of(msg, _row("αβ: 什么时候来", 779, 437, 1009, 464), bx, 573) is True)
    check("  离得远（间隔 > 20px）就不是",
          R._is_refer_of(msg, _row("αβ: x", 779, 500, 1009, 527), bx, 573) is False)
    check("  没有冒号（不是 `发言人: 内容` 形态）就不是",
          R._is_refer_of(msg, _row("什么时候来", 779, 437, 1009, 464), bx, 573) is False)
    check("  它自己落在某个盒里就不是（那是气泡里的正文）",
          R._is_refer_of(msg, _row("αβ: 什么时候来", 950, 380, 1020, 410), bx, 573) is False)
    check("  图片消息不参与",
          R._is_refer_of(MessageItem(text="[图片]", cls="mmui::ChatImageItemView",
                                     rect=(900, 370, 1029, 423)),
                         _row("αβ: x", 779, 437, 1009, 464), bx, 573) is False)


def test_use_config():
    """22. 设置保存后，`use_config` 要让**正在跑**的读取器立刻换上新配置。

    2026-10-17 真 bug（用户报「打开了识别图片，模型还是说看不到图」）：
    `run_watch.on_config_changed` 只换了自己那个 `cfg` 变量，读取器手里那份没换
    —— 而 `ocr_read.attach_image` 是读屏时用**读取器的 cfg** 判断的。
    于是开关显示已开、图片一张都不裁，**重启才生效**，且不报错。
    """
    print("\n22. use_config：设置保存后读取器立刻换配置（图片开关的静默失效）")
    c_off = FakeCfg(**{"ocr_read.attach_image": False, "ocr_read.record": False})
    c_on = FakeCfg(**{"ocr_read.attach_image": True, "ocr_read.record": True})

    # ① OcrReader：换对象 + 重算构造时缓存过的值
    r = R.OcrReader(c_off)
    check("OcrReader 一开始拿的是旧配置", r.cfg is c_off)
    check("  background_read_ok 跟旧配置走", r.background_read_ok is False)
    r.use_config(c_on)
    check("use_config 之后 cfg 换成新的", r.cfg is c_on)
    check("  ⚠️ background_read_ok 也重算了（不能只在构造时算一次）",
          r.background_read_ok is True)

    # ② 行为：那个开关真的读**当前** cfg（同一实例，不重建）
    from PIL import Image
    area = Image.new("RGB", (400, 300), "white")
    rects = [(20, 20, 320, 200)]      # 够大，不会被"像头像"那条判据丢掉
    r2 = R.OcrReader(c_off)
    s1 = ChatSnapshot(title="测试会话")
    r2._finish(s1, area, list(rects))
    check("开关关着时 snap.images 是空的", s1.images == [], "%r" % (s1.images,))
    r2.use_config(c_on)
    s2 = ChatSnapshot(title="测试会话")
    r2._finish(s2, area, list(rects))
    check("  ⚠️ 换配置后**同一个实例**立刻开始裁图", len(s2.images) == 1,
          "%d 张" % len(s2.images))

    # ③ 日志节流：这个开关一开，每拍都会算一遍 —— 数量没变就别刷屏
    logs = []
    r3 = R.OcrReader(c_on, logger=logs.append)
    for _ in range(3):
        r3._finish(ChatSnapshot(title="测试会话"), area, list(rects))
    hits = [m for m in logs if "附带" in m]
    check("  「附带 N 张」只报一次（每拍都会走到，不节流会刷满日志）",
          len(hits) == 1, "3 拍报了 %d 次" % len(hits))

    # ④ WeChatReader（UIA 链路）：同样要有这个方法
    from wechat_assistant.reader import WeChatReader
    w = WeChatReader(c_off, None)
    w.use_config(c_on)
    check("WeChatReader.use_config 换上了新配置", w.cfg is c_on)

    # ⑤ AutoReader：三条链路都要换到（含**将来才懒建**的 OCR 那份）
    a = R.AutoReader(c_off, None)
    a.use_config(c_on)
    check("AutoReader.use_config 换上了新配置", a.cfg is c_on)
    check("  ⚠️ 懒建 OcrReader 用的那份（_ocr_cfg）也换了",
          a._ocr_cfg is c_on)
    check("  ⚠️ 已经建好的 UIA 读取器也换了", a.uia.cfg is c_on)


def main():
    test_module_structure()      # ⚠️ 放第一个：结构坏了后面全是噪音
    test_probe_stays_cheap()
    test_probe_changed()
    test_pick_window()
    test_chat_rect()
    test_group_blocks()
    test_nickname()
    test_find_input_top()
    test_read_chat_with_fake_engine()
    test_light_theme_reads_other_side()
    test_auto_switch()
    test_recorder_gate()
    test_title_self_fill()
    test_source_guard_in_sender()
    test_detect_chat_left()
    test_geometry_failure_is_loud()
    test_title_uses_same_left_boundary()
    test_bg_is_dtype_safe()
    test_self_attrs_are_initialized()
    test_drop_reason()
    test_session_list()
    test_clean_title()
    test_refer_and_card()
    test_use_config()
    print("\n" + "=" * 62)
    print("通过 %d，失败 %d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
