# -*- coding: utf-8 -*-
"""纯 OCR 读屏：UIA 读不到时，从**像素**还原当前页面的聊天记录。

为什么会有这个模块
==================
实测（2026-09-27，第三十九 / 四十三轮）：**UIA 能不能读到微信的控件树，
是「机器 × 账号」的交互**。四个组合全测过 ——

    他的机器 + 他的号   ❌ 只有 2 个后代控件（读不到）
    他的机器 + 我的号   ✅ 160 个
    我的机器 + 他的号   ✅
    我的机器 + 我的号   ✅ 160 个

也就是说**确实存在读不到的环境**，而在这之前**没有任何代码处理它** ——
那种环境下程序「能启动、不报错、什么都读不出来」，比崩溃更难查。

所以这里给出第二条路：完全不碰 UIA，用 `capture.capture_window()`
（PrintWindow，不受遮挡影响、不置顶、不点击）抓整窗位图，再 OCR。

接口刻意和 `reader.WeChatReader` **完全一致**
-------------------------------------------
`find_window()` / `read_chat_title()` / `read_chat()` 三个方法同名同返回类型，
所以 `recorder` / `run_watch` **一行都不用改**就能换后端。
`read_sessions()` / `read_input_text()` 在这个后端下拿不到，返回空
（见文件末尾「做不到的事」）。

⚠️ 只读：不模拟键鼠、不点击微信、不改变微信窗口状态。
   唯一会碰窗口的地方是 PrintWindow，而它不改任何状态。
"""

import base64
import io
import os
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import numpy as np
from PIL import Image

from . import capture
from .reader import (ChatSnapshot, MessageItem, SessionItem, TimeMarker,
                     is_time_text, label_unknown_senders, list_windows,
                     pick_window)

__all__ = [
    "OcrBlock", "OcrRow", "group_blocks", "find_input_top",
    "chat_rect", "OcrReader",
]


# ------------------------------------------------------------------ 数据结构

@dataclass
class OcrBlock:
    """OCR 出来的一块文字（坐标相对**裁剪后的聊天区**）。"""
    text: str
    x1: int
    y1: int
    x2: int
    y2: int
    score: float = 1.0

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2.0

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2.0


def probe_changed(prev, cur, tol: int = 8, frac: float = 0.01) -> bool:
    """两张探针缩略图算不算「屏幕变了」。

    ⚠️⚠️ **绝不能写成 `prev != cur`（逐字节相等）。** 实测
    （`probes/check_probe_discriminates.py`）：位图整体加 **±2** 的随机噪声
    —— 那点抖动在真实抓图里**随时都有**（抗锯齿、缩放、GPU 合成）——
    逐字节比对就判成「变了」。后果是**每一拍都去真读一次 250ms**，
    探针等于白做，而且比不做还慢（白付了抓图的钱）。

    判据是「**有多少比例的像素变化超过了阈值**」：

        · `tol`  单个像素差多少才算「变了这个像素」（默认 8，滤掉抖动）
        · `frac` 至少多少比例的像素变了才算「整屏变了」（默认 1%）

    默认值的来历（chat 区约 937x906 -> 缩到 48x48 ＝ 2304 像素）：
        · 一条 **40px 高**的新消息 ≈ 4.4% 的高度 -> 缩略图上约 **100 像素**
          -> 远超 1%，**抓得住**（实测最小可测高度见上面那个探针的输出）
        · `frac=1%` ＝ 23 像素 -> 小面积的动画（一个表情）不会每拍都触发
        · 真要更灵敏/更迟钝就调 `ocr_read.probe_changed` / `ocr_read.probe_tol`

    ⚠️ 长度不一致时返回 True（换了缩略图尺寸 / 换了窗口大小 -> 当成变了）。
    ⚠️ 纯函数：不碰窗口、不读配置 —— 所以能被单测直接喂合成数据。
    """
    if prev is None or cur is None:
        return True
    if len(prev) != len(cur):
        return True
    a = np.frombuffer(prev, dtype=np.uint8).astype(np.int16)
    b = np.frombuffer(cur, dtype=np.uint8).astype(np.int16)
    if a.size == 0:
        # 空的缩略图 = "判不出来"。**当成没变**（跟 `probe()` 返回 None 一个取向）：
        # 两种错法不对称 —— 当成"变了"会**每拍都真读 250ms**（静默烧 CPU，
        # 而且看起来只是"有点卡"）；当成"没变"只是后台少记几屏，
        # 而按热键生成那条路完全不受影响（它自己会读当前屏）。
        return False
    n = int(np.count_nonzero(np.abs(a - b) > tol))
    return (n / a.size) > frac


@dataclass
class OcrRow:
    """一条消息（= 竖直方向挨在一起的一堆 OCR 块）。

    为什么按「行」而不是按「块」组织：气泡里换行会切成好几块，
    它们属于**同一条消息**；而群聊昵称、时间分隔虽然也是块，却不是消息正文。
    """
    blocks: List[OcrBlock] = field(default_factory=list)
    nickname: str = ""
    is_time: bool = False
    #: 这一条自己发的吗（由**气泡盒离哪边近**判出来的，见 `side_of_box`）
    is_self: Optional[bool] = None
    #: 这条消息的气泡包围盒 (x1,y1,x2,y2)——只有按气泡切的那条路才有
    box: Optional[tuple] = None
    #: 被当成群昵称挑走的那一块 —— `text()` 要把它从正文里排除掉
    nick_block: Optional[OcrBlock] = None

    @property
    def y1(self) -> int:
        return min(b.y1 for b in self.blocks)

    @property
    def y2(self) -> int:
        return max(b.y2 for b in self.blocks)

    @property
    def x1(self) -> int:
        return min(b.x1 for b in self.blocks)

    @property
    def x2(self) -> int:
        return max(b.x2 for b in self.blocks)

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2.0

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2.0

    def text(self) -> str:
        """正文（不含昵称、不含时间）。"""
        parts = [b for b in self.blocks if b is not self.nick_block]
        parts.sort(key=lambda b: (b.y1, b.x1))
        return " ".join(b.text.strip() for b in parts if b.text.strip()).strip()


# ------------------------------------------------------------------ 切行

def _v_gap(a: OcrBlock, b: OcrBlock) -> float:
    """两个块在竖直方向的间隔（重叠或相接时为 0）。"""
    if a.y2 < b.y1:
        return b.y1 - a.y2
    if b.y2 < a.y1:
        return a.y1 - b.y2
    return 0.0


def group_blocks(blocks: List[OcrBlock], width: int,
                 v_ratio: float = 0.35,
                 h_gap_ratio: float = 1.2) -> List[OcrRow]:
    """⚠️ **兜底**切行：没有 cv2、认不出气泡盒时才走这里（见 `bubble_boxes`）。

    主路径是按气泡的连通域切（一个气泡 = 一条消息），那条判据可靠得多。
    这一条只是"总比什么都没有好"，**它有一个已知的代价**：
    同一个人连发的两条短气泡（间隔只有 4~5px）会被并成一条消息。

    两块算同一行，要**同时**满足：

    ① **竖直间隔够小**（阈值 = 较矮那块高度的 `v_ratio` 倍，默认 0.35）。
       ⚠️ 不能用「竖直中心距离」当判据 —— 一个 30px 高的气泡里两行文字，
          中心相距 35px，按中心判就分成了两条消息。
       ⚠️ 也不能用「竖直区间有重叠」当判据 —— 同一条消息的第二行
          和上一条消息**经常恰好相接**，会粘成一条。
    ② **水平方向够近**（间隔不超过较矮那块高度的 `h_gap_ratio` 倍）。
       ⚠️ 只按竖直归并时，**左边一条和右边一条只要高度差不多就会被粘成一条**
          —— 实测出现过 `x=97..1173` 这种横跨整个聊天区、左右来源混在
          一起的"消息"，发送者判定直接失效（取中点永远判成"我"）。
          气泡内部的换行一定和上一行有水平重叠，所以这条不会误伤。

    阈值都用**块高**的比例而不是固定像素 —— 字号会随微信的字体设置变。
    """
    rows: List[OcrRow] = []
    for b in sorted(blocks, key=lambda x: (x.cy, x.x1)):
        hb = b.y2 - b.y1
        hit = None
        for r in rows:
            for o in r.blocks:
                ho = o.y2 - o.y1
                small = min(hb, ho)
                if _v_gap(b, o) <= max(6.0, v_ratio * small) and \
                        _h_near(b, o, max(6.0, h_gap_ratio * small)):
                    hit = r
                    break
            if hit is not None:
                break
        if hit is None:
            rows.append(OcrRow(blocks=[b]))
        else:
            hit.blocks.append(b)
    for r in rows:
        r.blocks.sort(key=lambda b: b.x1)
    rows.sort(key=lambda r: r.y1)
    return rows


def _h_near(a: OcrBlock, b: OcrBlock, gap: float) -> bool:
    """两个块在水平方向是不是"挨着"（重叠，或间隔小于 gap）。"""
    if a.x2 < b.x1:
        return (b.x1 - a.x2) <= gap
    if b.x2 < a.x1:
        return (a.x1 - b.x2) <= gap
    return True


def pick_nickname(row: OcrRow, mid_x: float, max_chars: int = 12,
                  boxes=None) -> None:
    """从**左侧**行里挑出群昵称，认到就写到 `row.nickname` 并把它从正文里摘掉。

    在本后端下这条判据比 UIA 路径**简单得多**，而且更可靠 ——

    UIA 路径要三条判据（位置 / 底色 / 复现）是因为它只能拿到一个
    「候选文本」，不知道它在图片里还是在气泡外。而这里我们已经知道
    **每个块前面是不是还有一个更靠上的块**：
      · 群昵称上面**没有**任何东西（它就是这一行的第一块）
      · 气泡正文上面有昵称，正文自己不是第一块
      · 图片里的字**在气泡内部**，会跟图片一起构成同一行的第二块

    判据（三条全满足才算昵称）：
      ① 是这一行的**第一块**（y 最小），而且**和第二块竖直方向不重叠**
         —— 重叠说明它们是并排的（比如头像和气泡），不是「压在它上面」
      ② 下面还有别的块（否则这行只有一段文字，那不可能是"昵称+正文"）
      ③ 文本短（<= `max_chars`），且不是时间分隔、不是纯数字/表情符
    """
    if row.is_time or len(row.blocks) < 2:
        return
    first = row.blocks[0]
    # ⚠️⚠️ **第一块落在真气泡盒里 -> 它不是昵称。**
    #     2026-10-13 实测（`wx_1323_miniapp`）：小程序卡片的首行
    #     「*邀4人助力立即领取」（10 字，没超过 `max_chars=12`）被判成群昵称，
    #     后果**两处都错**：
    #       · 它被 `row.text()` 当 `nick_block` 排除 -> **卡片正文丢了一句**
    #       · `snap.is_group` 被置真 -> **服务号会话被判成群聊**
    #     判据：群昵称画在**聊天底色上、气泡上方**（必然在盒外），
    #     而卡片正文在**盒里** —— 和 `attach_free` 只用 `free`（盒外的块）同源。
    if boxes:
        for (bx1, by1, bx2, by2) in boxes:
            if bx1 <= first.cx <= bx2 and by1 <= first.cy <= by2:
                return
    rest = row.blocks[1:]
    rest_top = min(b.y1 for b in rest)
    if first.y2 > rest_top:
        return                       # 竖直方向重叠 = 并排，不是"压在它上面"
    t = (first.text or "").strip()
    if not t or len(t) > max_chars or is_time_text(t):
        return
    if re.match(r"^[\d\s:：./\\\-+]+$", t):
        return                       # 纯数字/时间戳，不是名字
    row.nick_block = first
    row.nickname = t


# ------------------------------------------------------------------ 按气泡切行

#: 多小的一块就不算气泡（是文字笔画或头像）。实测昵称行约 60x18、时间分隔约 90x18，
#: 气泡至少 40x24 —— 按**高度**就能把「画在底色上的字」和「气泡本体」分开。
BUBBLE_MIN_W = 40
BUBBLE_MIN_H = 24


def bubble_tol(bg) -> int:
    """按**底色亮度**选气泡切分的容差 `tol`。

    ⚠️ 深浅两套主题的"气泡填充 vs 聊天底色"色差**差一倍**（实测）：
        深色：底 RGB(28,28,28)、气泡约 (44,44,52)       -> 三通道和约 **56**
        浅色：底 RGB(250,250,250)、填充 RGB(238,238,240)  -> 和 **34**
    所以同一个 45 在**浅色**下会把整个卡片/气泡判成背景
    （实测 `wx_1567_light.png`：**22.3% 的像素**落在 25~45 这个带里被丢掉，
    而用户一眼就能看到那些灰底 —— 2026-10-13 用户纠正我："浅色的气泡也挺明显"）。

    ⚠️ 实测不同 tol 检出的**卡片盒**（宽>200 的才算卡片本体）：
        tol=45 -> 0 个      tol=34 -> 0 个（刚好差一点）
        tol=**30** -> **3 个**（三张卡片全对）
        tol=25 -> 多出 `(0, 0, 1146, 661)` **整屏假盒**
    ⚠️ 所以浅色取 **30**：34 不够、25 出假盒，中间只有这一档能用。
    ⚠️ 别把深色那档也一起降 —— 项目验过 `tol=40` 在深色下只会多出假消息。
    """
    try:
        luma = (int(bg[0]) * 299 + int(bg[1]) * 587 + int(bg[2]) * 114) // 1000
    except Exception:
        return 45
    return 45 if luma < 128 else 30


def bubble_boxes(a, bg, tol: int = 45, min_x: int = 0):
    """找**气泡 / 图片本体**的包围盒 [(x1, y1, x2, y2), ...]。找不到返回 []。

    ⚠️ 这是纯 OCR 路径原来的切行判据。**2026-10-12 起它降级成"加分项"**：
        切行改成按**文字块**归并（`rows_from_text`），气泡盒只用来判
        「这条是谁发的 / 有没有昵称」。原因是微信**浅色**下对方气泡的填充色和
        聊天底色只差 36（< `tol`），**整块气泡进不了 mask**、永远不成盒 ——
        真机实测那一屏 3 条对方消息一条都读不出来。详见 `rows_from_text`。

    ⚠️⚠️ **坐标系**：返回的盒坐标和 `a` **同一套**。调用方必须保证
        `a` 和 OCR 用的那张裁剪图**同原点**（最省事的做法：直接把
        `a[y1:input_top, cl:x2]` 这一块传进来）—— 否则盒坐标和
        `OcrBlock` 坐标对不上，身份判定和图片区域判定**都会看错地方**
        （2026-10-12 我在这个坑里绕了很久：`_recognize` 给的是
        **裁剪图坐标**，而盒原来算在另一个原点上）。

    ⚠️ `min_x` = 只看**这一列往右**的内容（裁剪区左边界）。为什么需要：
        调用方为了省掉一次易错的坐标平移，会传一块**带着左侧会话列表**的数组
        （`a[y1:input_top, 0:x2]`）。而会话列表的底色和聊天区**不一样**
        （实测 229 vs 250），于是它自己会连成一大块"气泡"（真机实测造出过
        一个 430×600、里面 714 种颜色的假盒）。
        这里把 `min_x` 左边的像素**当成背景**，并且**丢掉贴着 `min_x` 起头的
        连通域**（那必然是会话列表那一片，不是气泡）。
    """
    if bg is None or a.size == 0:
        return []
    from .sender import _cv2

    cv2 = _cv2()
    if cv2 is None:
        return []
    try:
        mask = (np.abs(a - bg).sum(axis=2) > tol).astype(np.uint8)
        if min_x > 0 and min_x < mask.shape[1]:
            mask[:, :min_x] = 0
        n, _labels, stats, _cent = cv2.connectedComponentsWithStats(mask, 8)
        out = []
        for i in range(1, n):
            x = int(stats[i, cv2.CC_STAT_LEFT])
            y = int(stats[i, cv2.CC_STAT_TOP])
            w = int(stats[i, cv2.CC_STAT_WIDTH])
            h = int(stats[i, cv2.CC_STAT_HEIGHT])
            if w < BUBBLE_MIN_W or h < BUBBLE_MIN_H:
                continue
            if min_x > 0 and x <= min_x:
                # 贴着裁剪区左边界起头 -> 是会话列表那一片，不是气泡
                continue
            out.append((x, y, x + w, y + h))
        out.sort(key=lambda b: b[1])
        return out
    except Exception:
        return []


def side_of_box(box, width: int) -> bool:
    """这条消息是不是**自己发的** —— 按气泡盒离哪边更近来判。

    ⚠️⚠️ **不能用"文本块的中心在中线哪一侧"**（2026-09-27 第四十六轮实测翻车）。

        窗口一窄，左右气泡在 x 方向**几乎重叠**：实测一个 776 宽的聊天区，
        左边气泡的文字落在 351..443，而中线在 388 ——
        **中心 397 跑到了中线右边**，于是「对方的回话」被判成「我发的」。
        那一屏 4 条消息**全被判成"我"**，而界面看起来完全正常。

    正确判据（和 `sender.analyze_side` 是同一个思路）：
        微信的气泡是**贴边**的 —— 别人的贴着左（头像之后），自己的贴着右。
        所以比的是**气泡盒到左边框的距离**和**到右边框的距离**，谁小算哪边。
        这个判据与文字长短无关，也不受"两个气泡在 x 上有重叠"影响。
    """
    left_gap = box[0]
    right_gap = width - box[2]
    return right_gap < left_gap


def rows_from_text(blocks: List[OcrBlock], width: int,
                   v_ratio: float = 0.6, v_min: float = 6.0, v_max: float = 14.0,
                   h_gap_ratio: float = 1.2) -> List[OcrRow]:
    """按**文字块**归并成消息行 —— 气泡盒认不出来时的主路径。

    ⚠️⚠️ 为什么必须有这条路（2026-10-12 真机实测）：

        微信浅色下「对方气泡的填充色」和聊天底色的**曼哈顿色差只有 36**
        （实测 `(238,238,240)` vs `(250,250,250)`），而气泡切分的判据是
        `|像素-底色| > 45` —— **整块气泡进不了 mask**，于是它**永远不成盒**，
        它上面的文字就落在所有盒之外，一条消息都装不出来
        （症状：标题正常、**消息 0 条**；见 `OCR后端.md`）。
        **降 `tol` 治不了**：实测 `tol=40` 才多出一条，而且是假消息 ——
        因为进 mask 的不是气泡本体，而是文字笔画和几个"主色==底色"的假盒子。

    所以切行的**主判据必须是文字本身**，气泡盒只当**加分项**（身份判定用它）。

    两块算同一行，要**同时**满足（和 `group_blocks` 同一套判据，只调紧了阈值）：

    ① **竖直间隔够小**：`_v_gap <= clamp(v_ratio * 较矮块高, v_min, v_max)`。
       ⚠️ 上限 `v_max`（默认 14px）是**关键**：同一气泡内换行的行距约 12px，
       而群里「别人两条不同消息」的文字块间隔是 4~5px —— 单看间隔分不开
       （4 < 12），所以**并列的第二个条件才是真正把它们分开的那条**：
       不同消息几乎不会横向重叠（左气泡和右气泡；或者两条上下叠着的左气泡
       也会因为下面那条**更靠左/更靠右**而错过 `h_gap_ratio` 的窗口）。
    ② **水平方向够近**（`_h_near`，间隔不超过较矮块高的 `h_gap_ratio` 倍）。
       ⚠️ 只按竖直归并时，左边一条和右边一条只要高度差不多就会被粘成一条
       —— 实测出现过 `x=97..1173` 这种横跨整个聊天区、左右来源混在一起的
       "消息"，发送者判定直接失效。
    """
    rows: List[OcrRow] = []
    for b in sorted(blocks, key=lambda x: (x.cy, x.x1)):
        hb = b.y2 - b.y1
        hit = None
        for r in rows:
            for o in r.blocks:
                ho = o.y2 - o.y1
                small = min(hb, ho)
                limit = min(v_max, max(v_min, v_ratio * small))
                if _v_gap(b, o) <= limit and \
                        _h_near(b, o, max(v_min, h_gap_ratio * small)):
                    hit = r
                    break
            if hit is not None:
                break
        if hit is None:
            rows.append(OcrRow(blocks=[b]))
        else:
            hit.blocks.append(b)
    for r in rows:
        r.blocks.sort(key=lambda b: (b.y1, b.x1))
    rows.sort(key=lambda r: r.y1)
    return rows


def merge_rows_in_boxes(rows: List[OcrRow], boxes) -> List[OcrRow]:
    """把中心落在**同一个气泡盒**里的行合并成一条 —— 一个气泡/卡片 = 一条消息。

    ⚠️⚠️ 为什么必须有这一步（2026-10-13 真机实测，`tests/corpus` 语料）：

        切行只按**文字行距**归并，而卡片 / 长公告**内部的段落间距**（实测 33px）
        超过归并阈值 —— 于是一张卡片被切成好几条"消息"：
          · `wx_1323_miniapp` 的小程序卡片 -> **5 条**
          · `wx_1323_group141` 的活动卡片   -> **4 条**
          · `wx_1323_dark_group` 的分享卡片 -> 正文 + 平台名 **2 条**
        它们**本来各是一条**。症状看着像"消息变多了"，但那**不是好消息**：
        喂给 AI 的上下文会碎片化，口吻样本也会被这些半句话污染。

        盒是**可靠的边界**（它就是气泡/卡片的轮廓），所以：**盒内全归一条**。
        ⚠️ 只有**没有盒**时才退回按行距归并（浅色下对方气泡不成盒，见
           `rows_from_text`）—— 两条路是**互补**的，不是二选一。

    ⚠️ 只处理装了 **>= 2 行**的盒：装 1 行的盒什么都不用做，把它从"按行距
       归并的结果"里搬出来反而会丢掉 `pick_nickname` 需要的信息。
    ⚠️ 合并后的行**保留盒子**（`box` 字段）—— 身份判定要用"盒离哪边近"
       （`side_of_box`），不能用文字中心。
    """
    if not rows or not boxes:
        return rows
    used = [False] * len(rows)
    merged: List[OcrRow] = []
    for (x1, y1, x2, y2) in boxes:
        group = [i for i, r in enumerate(rows)
                 if not used[i] and x1 <= r.cx <= x2 and y1 <= r.cy <= y2]
        if len(group) < 2:
            continue
        row = OcrRow(blocks=[b for i in group for b in rows[i].blocks])
        row.box = (x1, y1, x2, y2)
        for i in group:
            used[i] = True
        merged.append(row)
    for i, r in enumerate(rows):
        if not used[i]:
            merged.append(r)
    merged.sort(key=lambda r: r.y1)
    return merged


def _color_count(a, box, q: int = 16) -> int:
    """一块区域里有多少种颜色（按 `q` 量化，滤掉抗锯齿的抖动）。"""
    x1, y1, x2, y2 = (int(v) for v in box)
    h, w = a.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 - x1 < 2 or y2 - y1 < 2:
        return 0
    sub = np.asarray(a[y1:y2, x1:x2]).astype(np.int32)
    key = ((sub[:, :, 0] // q) * 1024 + (sub[:, :, 1] // q) * 32
           + (sub[:, :, 2] // q))
    return int(np.unique(key).size)


def _ink_fraction(ca, bg, tol: int = 45) -> float:
    """这一块里"和背景色差得明显"的像素占多少（`bg` 就是这块的背景色）。

    ⚠️ 用途是**"这一屏到底画了东西没有"**：纯色图恒为 0，而真屏上气泡填充、
    文字、图片随便哪一样都远超千分之二（真机浅色那屏实测约 17%）。
    `read_chat` 用它当"这一档几何能不能用"的必要条件之一 ——
    没有它，几何完全错的时候 OCR 在纯色上编出来的那个块会让这一档被采纳，
    **"几何不对"这条告警就静默消失了**（见 `read_chat` 里那段注释）。
    """
    if ca is None or getattr(ca, "size", 0) == 0 or bg is None:
        return 0.0
    try:
        diff = np.abs(np.asarray(ca).astype(np.int32)
                      - np.asarray(bg, dtype=np.int32)).sum(axis=2)
        return float((diff > tol).mean())
    except Exception:
        return 0.0


def _looks_like_nickname(t: str, max_chars: int = 12) -> bool:
    """这段文字像不像**群昵称**（判据和 `pick_nickname` 的第二、三条一致）。

    昵称的特征：短、不是时间分隔、不是纯数字/符号。
    ⚠️ **单个汉字要保住**（'白' / '周' 是真昵称）—— 所以别按长度一刀切，
       只排除"空 / 超长 / 时间 / 纯数字符号"这几种。

    ⚠️⚠️ `max_chars` **必须传得很小**：真机群聊实测昵称是 **2 个字**
        （「细姨」「思翀」），而**真消息**可能只有 4 个字（「这就去」「皮韧咸香」）——
        单看长度本来就分不开。所以这条函数**只能当必要条件**，
        真正的判据是"它紧跟下面那条消息"（见 `read_chat` 里那段）。
    """
    s = (t or "").strip()
    if not s or len(s) > max_chars or is_time_text(s):
        return False
    return not re.match(r"^[\d\s:：./\\\-+]+$", s)


#: 昵称的**上限字数**：够放下「细姨」「思翀」「李四」这类，又能把
#: 「可以可以！没问题」（10 字）这种真消息挡在外面（真机实测）。
#: ⚠️ 它只是**必要条件**之一，不是充分条件。
_NICK_MAX_CHARS = 6


def _content_top(ca, bg, tol: int = 40, need: int = 3) -> int:
    """裁剪区里**第一行"画了东西"**的 y（整块同色时返回 -1）。

    ⚠️ 判据是「这一行里和 `bg` 差得明显的像素 >= `need` 个」——`bg` 就是这块的
    背景色，所以它回答"这儿开始有内容了没有"。
    ⚠️ 只看这一个数是为了**便宜**（逐行一次比较）。
    """
    if ca is None or getattr(ca, "size", 0) == 0 or bg is None:
        return -1
    try:
        diff = np.abs(np.asarray(ca).astype(np.int32)
                      - np.asarray(bg, dtype=np.int32)).sum(axis=2)
        hit = np.where((diff > tol).sum(axis=1) >= need)[0]
        return int(hit[0]) if hit.size else -1
    except Exception:
        return -1


def _top_cutoff_image(ca, mid_x: float, rows=None, bg=None, min_gap: int = 60,
                      edge: int = 14, boxes=None, blocks=None) -> list:
    """聊天区**顶部那截"贴着上边被切掉的图片/卡片"**的矩形（没有就返回 []）。

    ⚠️ 为什么需要（2026-10-12 真机实测）：右上那张**图片缩略图**（对方发的
    消息截图）里的字被 OCR 认成了 **3 条假消息**。另外几个想法实测都被否掉了：
      · 「盒内颜色数」—— 图里的文字小块**没连成盒**（每块 < 40×24），
        缩略图本身和聊天底色只差十几，也进不了 `bubble_boxes` 的 mask；
      · 「行内 ink（和行内底色比）」—— 图里的字是**浅灰压近白**（量化后差 8~24），
        判不出"这行有内容"，反而把**真消息**（气泡填充和底色只差 36）一起误杀；
      · 「顶部逐行数颜色」—— 这张缩略图在 q=24 下每行只有 6~27 种颜色，
        和正文行（6~22）**分不开**。

    判据（**结构性**，和配色、和图片内容都无关），两条**同时**满足：
      ① **内容是从裁剪区顶边开始的**（`_content_top <= edge`）——
         照片/截图消息被顶部切掉时就是这样：图片本色压在最上面。
         ⚠️ 用**内容**的起点而不是第一个文字块的 y1：图片的上沿常常在文字之上
         很多（真机实测图片从裁剪区第 5 行就开始，而它的文字在 y=25）。
      ② 它和下面那行文字之间有一个大空白（`>= min_gap`，默认 60px）。
         真机数据：假文字底边 `y≈103`、正文从 `y=241` 才开始（**138px**）；
         而同一段对话里消息之间的间隔只有 4~27px。

    ⚠️ 为什么不用"最大空白"：一屏里**底部**也可能有个更大的空白（真机实测：
        445~685 那段比顶部那段还大），取最大就会选错区间。只认**顶部贴边**的
        那种，就不用在这几个空白之间做比较。
    ⚠️ 已知代价：如果**真的**有一行消息正好贴着屏幕顶边、而且下面隔了 60px 空白，
        那一行会被当成图片丢掉 —— 但它本来就是被切掉半截的、读出来也不完整，
        这个取舍是有意的。
    """
    if ca is None or getattr(ca, "size", 0) == 0 or not rows or bg is None:
        return []
    h, w = ca.shape[:2]
    top = _content_top(ca, bg)
    if top < 0 or top > edge:
        return []                      # 顶上没内容 -> 不是被切掉的图片
    ys = sorted(int(r.y2) for r in rows)
    gaps = []
    prev = None
    for y in ys:
        if prev is not None and (y - prev) >= min_gap:
            gaps.append((prev, y))
        prev = y
    if not gaps:
        return []
    g0, _g1 = max(gaps, key=lambda p: p[1] - p[0])
    if g0 <= 0:
        return []
    # ⚠️⚠️ **"顶部贴边 + 下面有大空白"这条会恒真，必须再加一道证据。**
    #     （2026-10-13 用 `tests/corpus` 语料实测量出来的）
    #
    #     只按 gap 判会**恒真** —— 一屏正常聊天里，消息之间本来就隔着时间分隔
    #     和气泡间距（实测 `rows` 的 y2 间隔 70~148，而 `min_gap` 只有 60），
    #     于是**整块上半屏**被判成"被顶部切掉的图片"：
    #       · `printwindow_flags2`：5 条真消息一条不剩（语料验收召回 20%）
    #       · `wx_1323_dark_group`：连唯一一条真消息也被吃掉 -> `usable` 为空
    #         -> 几何直接判失败（`_geom_bad`，整屏 0 条）
    #     而这两屏在 160 条单测下**全绿**，因为合成用例里没有
    #     "时间分隔把行距撑开"这个形状。
    #
    #     ⚠️ 试过但**不够强**的补丁：在这块区域上再算"颜色数 >= 24" ——
    #        拦不住（多个气泡 + emoji + 抗锯齿叠起来照样几十种颜色）。
    #
    #     ✅ 现在这道证据用的是**结构**（与配色/字号/主题都无关）：
    #        区块 (0,0,w,g0) 里只要存在**"被真气泡盒装着的文字块"**，
    #        它就是正常消息区，不是被切掉的图片。
    #        这条判据能成立，靠的是项目里已实测的另一条事实：
    #        **图片上的字不成盒**（每块 < 40x24，进不了连通域）——
    #        所以"真图片带"里不会有任何文字被盒装着，它照样被裁掉。
    #     ⚠️ 盒与区块只要有**重叠**就算（不要求盒完全落在区块内）：
    #        实测 `wx_1323_dark_group` 那条绿气泡是 y=315..369，
    #        而 g0=358 —— 要求"盒底部 <= g0"会把唯一的证据整条漏掉。
    if boxes:
        for (bx1, by1, bx2, by2) in boxes:
            if by1 >= g0:
                continue                  # 盒子完全在区块之下，与它无关
            for b in (blocks or []):
                if (b.cy < g0 and bx1 <= b.cx <= bx2 and by1 <= b.cy <= by2):
                    return []             # 区块里有真气泡装着的字 -> 是消息区
    # ⚠️ 判据 B：区块里**成行的文字很多** -> 那是正常消息列表，不是图片带。
    #     2026-10-13 实测（`wx_1323_gongzhonghao`）：那是公众号消息列表，
    #     两个气泡盒都是**右侧封面图**（盒里一个字都没有）-> 判据 A 不成立，
    #     于是整块上半屏被当成图片，**12 段真文字全丢**（消息 3 条 -> 1 条）。
    #     而真正的"被顶部切掉的图片带"实测只产出 **3 条**假消息（图片上的字
    #     不成句、也不成行），所以门槛取 4 能把两边分开。
    if len(rows) >= 4:
        return []
    return [(0, 0, w, int(g0))]


#: 发给模型的图**压缩到长边多少**。
#: ⚠️ 再大对识别没帮助，只是烧 token（一整屏 1146x661 压到 1024
#:    约 1000~1500 token）。
_IMG_MAX_SIDE = 1024

#: 图片消息的占位文本。⚠️ 用**非空**文本是必须的：`store.save_new_only()`
#: 会跳过 `text` 为空的消息（`if not content: continue`），
#: 空文本的图片消息**入不了库**，等于没读出来。
_IMAGE_PLACEHOLDER = "[图片]"

#: 多大的一块才算"一条图片消息"（用来排除**头像**）。
#: ⚠️ 用**相对尺寸**而不是绝对像素：同一屏在 2 倍图上所有尺寸都翻倍
#:    （实测 `wechat_shot_20260923_005543_x2`：头像变成 108x108，
#    会被绝对阈值 100 误判成一条图片消息）。
#: 实测（2026-10-13，`tests/corpus`）：真图片消息占聊天区宽 **13.6%**
#: （123x85 橙色海报 / 123x270 表格截图），而头像只占 **6%**（54x54）。
_IMAGE_MIN_W_FRAC = 0.10
_IMAGE_MIN_H_FRAC = 0.12


#: 会话列表里"同一项"的竖直间隔上限（超过就开新项）。
#: 实测（1323x939 真机）：项内"会话名 / 时间"在同一行、预览在下一行
#: （间隔约 2px），而**项与项之间**间隔约 39px —— 取 20 能把两边分开。
_SESSION_ITEM_GAP = 20

#: 会话列表左边界占整窗宽度的比例（导航栏的右沿）。
#: 实测 1323 宽的图上是 x≈110（8.3%），这里取 7% 留余量。
#: ⚠️ 导航栏里只有图标，OCR 认不出有效文字，所以从它右边开始。
_SESSION_LEFT_FRAC = 0.07

#: 会话列表的竖向起点占比 —— 上面是搜索框（"搜索"两个字会被 OCR 认出来，
#: 变成一个假会话项）。实测搜索框底部在 10% 以内。
_SESSION_TOP_FRAC = 0.10


def group_session_blocks(blocks: List[OcrBlock],
                         max_gap: int = _SESSION_ITEM_GAP) -> List[List[OcrBlock]]:
    """把左侧列表的 OCR 块按**竖直间隔**分成一个个会话项。

    ⚠️ 为什么按"间隔"而不是按"固定行高"：会话项的高度**不是常数** ——
       带"消息免打扰"图标、未读红点、或"草稿"标记时都会长一点。
       而"两块之间隔了 20px 以上"在任何情况下都表示**换了一项**。
    ⚠️ 阈值要比切消息用的 `_v_gap` 那套**大**：会话项之间的间隔（实测 39px）
       比气泡内换行（约 12px）大得多，用切消息的阈值会把两项并成一项。
    """
    items: List[List[OcrBlock]] = []
    for b in sorted(blocks, key=lambda z: (z.y1, z.x1)):
        if items:
            bottom = max(x.y2 for x in items[-1])
            if b.y1 - bottom <= max_gap:
                items[-1].append(b)
                continue
        items.append([b])
    return items


#: 会话列表里"短日期"的形态（`10/06`）。`reader.TIME_RE` 覆盖了 `16:07` /
#: `星期四` / `昨天` / `X月X日`，但**不认这种** —— 而它在会话列表里很常见。
_SESSION_TIME_RE = re.compile(r"^\d{1,2}[/\-]\d{1,2}$")


def _looks_like_session_time(t: str) -> str:
    """这段文字像不像会话列表里的**时间**；像就返回**清理后**的文本，不像返回 `""`。

    ⚠️ 用**文本形态**判，**不用"靠右"** —— 实测"靠右"会被骗：
        `吃喝玩乐福利君@美团吃喝玩乐` 的**折行第二行** `@美团...` 也落在右侧，
        于是它被当成时间、真正的时间（`12:37`）掉进了预览。
        反过来时间也不一定贴边（有未读红点时会被挤开）。
    ⚠️ 前缀里可能混进 OCR 读出来的图标（未读圆点被读成 `Q` / `$`），
        实测 `Q14:05` —— 所以先把开头那些非数字符号剥掉再判，
        而且**返回剥好的**（顺手把这点噪声清掉）。
    """
    s = (t or "").strip().lstrip("Q$●○◎◉*·.").strip()
    if is_time_text(s) or _SESSION_TIME_RE.match(s):
        return s
    return ""


def session_item_from(blocks: List[OcrBlock], left_edge: int = 0,
                      right_edge: int = 0) -> Optional[SessionItem]:
    """把一个会话项的 OCR 块拼成 `SessionItem`（认不出会话名就返回 `None`）。

    实测（1323x939 真机）一个会话项长这样：

        公众号            (185,141)-(256,169)   <- 会话名：y 最小那一块
        16:07            (372,145)-(414,165)   <- 时间
        [28条]人民日报…    (187,171)-(407,198)   <- 预览：在会话名下面

    ⚠️ 时间和预览**靠文本形态**分（`_looks_like_session_time`），
       位置只在 OCR 的 y 有偏差时"补一刀"。
    ⚠️ `left_edge` / `right_edge` 不传（0）时位置判据不启用（老调用点行为不变）。
    ⚠️ ⚠️ 调用方传的边界必须是 **OCR 那批块的坐标系**（`img.crop(rect)` 之后的
       裁剪图坐标），不是原图坐标 —— 传错了位置判据会**永远不成立**。
    ⚠️ 时间**只取第一个**；多出来的按预览处理（宁可多留文字，也不丢）。
    """
    if not blocks:
        return None
    ordered = sorted(blocks, key=lambda z: (z.y1, z.x1))
    head = ordered[0]
    title = (head.text or "").strip()
    if not title:
        return None
    band = 0.0
    if right_edge and left_edge and right_edge > left_edge:
        band = (right_edge - left_edge) * 0.18
    time_text, previews = "", []
    for b in ordered[1:]:
        t = (b.text or "").strip()
        if not t:
            continue
        as_time = _looks_like_session_time(t)
        same_row = b.y1 < head.y2 and head.y1 < b.y2
        near_right = (not band) or (b.x2 >= right_edge - band)
        # ⚠️ 时间是**形态**说了算；位置只是"补一刀"（OCR 的 y 有时会偏，
        #    只按同行会漏），所以两条取**或**。
        if not time_text and as_time and (same_row or near_right):
            time_text = as_time
        else:
            previews.append(t)
    return SessionItem(title, " ".join(previews).strip(), time_text)


#: 标题末尾可能混进"图标被 OCR 读出来的**开括号类**符号"。
#: ⚠️ 只 rstrip 开括号类（`（`/`(`/`[`/`【`/`|`）和空白：闭括号、句号这些
#:    **可能是标题的正常结尾**（`某某（2024）` 这种），一并去掉会**改坏真标题**。
_TITLE_TAIL_JUNK = "（([【｜| \t\u3000"


def _clean_title(t: str) -> str:
    """把标题末尾的**图标噪声**切掉。

    ⚠️ 实测（2026-10-13，`wx_1567_group303`）：真标题是
       `示例群甲🏠(303)`，而 OCR 读成
       `示例群甲（303）（` —— 末尾那个 `（` 是标题栏右边的
       **免打扰铃铛图标**被认出来的。

    两条判据，**顺序不能反**：
        ① 标题里如果有**群成员数后缀** `(N)` / `（N）`，就**截到它结束**为止 ——
           那之后的东西必然属于标题栏右边的图标区；
        ② 否则只 rstrip 掉末尾的**开括号类**符号（见 `_TITLE_TAIL_JUNK`）。

    ⚠️ 这个函数**只切尾巴、不做归一化**：`session_key` 就是标题原文，
       改得越多越容易把两个会话串起来（见 `sessions.norm_title` 那边的说明）。
    """
    s = (t or "").strip()
    m = re.search(r"[（(]\d{1,5}[)）]", s)
    if m:
        return s[:m.end()].strip()
    return s.rstrip(_TITLE_TAIL_JUNK).strip()


#: 引用块的形态：`发言人: 被引用内容`。
#: ⚠️ 冒号**半角全角都认**（OCR 两种都会吐），发言人名字限 24 字以内。
_REFER_RE = re.compile(r"^[^:：\n]{1,24}[:：]\s*\S")

#: 引用块与它上面那个气泡之间的**最大**间隔（物理像素）。
#: 实测（`wx_1567_refer`）：引用块紧贴气泡盒底 **14px**，
#: 而"消息与消息之间"最近的也有 **24px** —— 阈值取 20 落在两者中间。
_REFER_GAP = 20


def _is_refer_of(msg: Optional[MessageItem], row: OcrRow, boxes,
                 mid_x: float) -> bool:
    """这一行是不是**上面那条消息的引用块**。

    ⚠️ 微信 **4.1.13.65** 的引用消息长这样（**正文气泡在上、引用块在下**，
       和某些老版本相反 —— 2026-10-13 用户实测截图确认）::

           ┌────────┐
           │  测试   │                  <- 绿气泡（正文）
           └────────┘
            αβγδεζηθικλμ: 什么时候来 │    <- 灰色小字（引用块）+ 右端竖线

       ⚠️ 那根竖线**不是输入光标**（两条引用块都有它，不可能有两个焦点），
          而是引用块的**右端视觉标记** —— 别拿"有没有光标"去判是不是输入区。

    ⚠️ **四条判据同时满足**才算（括号里是实测数据）：
        ① 文本形态是 `发言人: 内容`（`_REFER_RE`）；
        ② **紧贴**上一条气泡的盒底：间隔 <= `_REFER_GAP`（实测 14，消息间 24+）；
        ③ 与上一条**同侧**（都在中线右边或都在左边）；
        ④ 它**自己不在任何盒里**（引用块画在底色上，实测 `real_boxes` 里没有它）。
    ⚠️ 判据 ④ 在**浅色**下要小心（浅色里消息也不成盒）—— 所以它只是四条之一，
       不单独用；另外三条都是形态/几何的硬证据。
    """
    if msg is None or msg.kind == "image":
        return False
    if not _REFER_RE.match((row.text() or "").strip()):
        return False
    # ④ 引用块自己不在任何盒里
    for (x1, y1, x2, y2) in boxes:
        if x1 <= row.cx <= x2 and y1 <= row.cy <= y2:
            return False
    r = msg.rect or None
    if not r:
        return False
    # ② 紧贴**上方那个气泡盒的底**。
    #    ⚠️⚠️ **不能拿 `msg.rect[3]` 比**：`msg.rect` 是**文字行**的范围
    #       （实测 y2=416），而气泡盒画到 **423** —— 差这 7 个像素就把 14 算成了 21，
    #       阈值 20 直接被顶掉。症状是"**改了像没改**"（判据写了但一条都没合并），
    #       第一版就这么失败了 —— 所以这里必须找到**包住这条消息的那个盒**。
    bottom = r[3]
    for (x1, y1, x2, y2) in boxes:
        if x1 <= r[0] and r[2] <= x2 and y1 <= r[1] and r[3] <= y2:
            bottom = max(bottom, y2)
    gap = row.y1 - bottom
    if gap < 0 or gap > _REFER_GAP:
        return False
    # ③ 同侧
    return (row.cx < mid_x) == (((r[0] + r[2]) / 2.0) < mid_x)


def _merge_refer(msg: MessageItem, refer_text: str) -> None:
    """把**引用块**并进它上面那条消息。

    - `text` 变成 `[引用 发言人: 内容] 正文` —— 让 AI 知道"这句话引用了别的"。
      ⚠️ 不并的话，引用块**单独成条**时 AI 会把它当成对方说的一句话，那是错的。
    - `cls` 换成 `ChatReferItemView`，于是 `MessageItem.kind` 返回 **`refer`**
      （`reader.py` 里那个分支，2026-10-13 补的）。
    ⚠️ 引用内容放在**前面**（语义上是"我引用了这句、然后说了正文"），
       而屏幕上它是画在气泡**下面**的 —— 两者顺序相反是**有意的**。
    """
    ref = (refer_text or "").strip()
    body = (msg.text or "").strip()
    msg.text = ("[引用 %s] %s" % (ref, body)).strip() if ref else body
    msg.cls = "mmui::ChatReferItemView"


#: 「**分享卡片**」的判据：气泡盒里的**颜色种数**。
#: 实测（`wx_1567_file_link`，同一屏里三个卡片 + 一条普通消息对照）：
#:     小红书卡片 **441** · 文件卡片 **106** · bilibili 卡片 **77**   <- 都有缩略图
#:     普通文本消息 **21**（`测试`）
#:     头像 97（但它**块数为 0**，会被下面那条"≥3 块"排除）
#: 取 40：卡片最低的那个（77）还有一倍余量，而普通消息（21）差得远。
_CARD_MIN_COLORS = 40

#: 卡片至少装这么多块文字（标题 + 摘要 + 来源）。
#: 实测：三张卡片分别 **4 / 3 / 4** 块，而普通消息 **1** 块。
#: ⚠️ 这条同时把**头像**排除掉了（54x54 正方形、0 块）。
_CARD_MIN_BLOCKS = 3

#: 文件消息的**后缀**（必须和容量**同时**命中，见 `_card_kind_of`）。
_FILE_EXT_RE = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|7z|tar|gz|txt|csv|md|mp[34]|wav|avi|mkv"
    r"|png|jpe?g|gif|bmp|webp|exe|msi|apk|dmg|iso|psd|ai|py|java|cpp|json|xml"
    r"|html?)(?![0-9a-z])", re.I)

#: 文件消息的**容量**（实测 `17.1M` / `247.0K` / `9.1M`）。
_FILE_SIZE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:[KMGTP]B?|字节)", re.I)


def _find_box_of(row: OcrRow, boxes):
    """找**和这一行重叠最多**的那个气泡盒（没有重叠就返回 None）。

    ⚠️⚠️ 判据是**重叠**，不是"完全包含"：实测卡片的文字块会比气泡盒**高出几个
        像素**（`wx_1567_file_link` 里 row 的 `y1=10`、而盒的 `y1=15`），
        用"包含"会**找不到盒** —— 症状是那张卡片**没被分类**，而且**不报错**
        （第一版就这么漏掉了小红书卡片）。
    """
    best, best_area = None, 0
    for (x1, y1, x2, y2) in boxes:
        ox1, oy1 = max(x1, row.x1), max(y1, row.y1)
        ox2, oy2 = min(x2, row.x2), min(y2, row.y2)
        if ox2 <= ox1 or oy2 <= oy1:
            continue
        area = (ox2 - ox1) * (oy2 - oy1)
        if area > best_area:
            best, best_area = (x1, y1, x2, y2), area
    return best


def _card_kind_of(text: str) -> str:
    """卡片里的文字是**文件**还是**内容分享**（返回 `"file"` / `"link"`）。

    ⚠️ 文件判据是**后缀 + 容量同时命中**：
        - 只按后缀会误伤聊天里提到「我下了个 xxx.zip」；
        - 只按容量会误伤「我买了 8GB 内存」这种真消息。
        实测微信的文件卡片**两个都有**：`示例文件.zip` + `17.1M`。
    ⚠️ 剩下的都算「内容分享」，**别去匹配"来源"**（小红书 / 哔哩哔哩 / 任何 App
        都可能是来源，那是**开放集合**，硬编码必然漏）——
        真正的判据是**结构**（颜色种数 + 块数），不是字符串。
    ⚠️ 也**不是"含 URL"**：实测多数分享卡片**根本不显示 URL**
        （小红书 / bilibili 显示的都是「标题 + 摘要 + 来源」）。
        带 URL 的只有招聘报名那类卡片，属于少数。
    """
    s = text or ""
    if _FILE_EXT_RE.search(s) and _FILE_SIZE_RE.search(s):
        return "file"
    return "link"


def _looks_like_group_title(title: str) -> bool:
    """会话标题像不像**群名** —— 微信给群标题加成员数后缀。

    ⚠️ 为什么需要它（2026-10-13 实测）：现有的群判据是"**别人的消息上挂没挂
        昵称**"，而 `wx_1323_dark_group` 那一屏**全是我自己发的消息** ——
        结构上够不着，真群被判成私聊。而群标题的后缀是稳定的（实测
        `示例兴趣群(5)` / `示例群甲🏠(303)` / `示例群乙（141）`），
        私聊和服务号都没有（`张三` / `黑马资源AI助手19`）。

    ⚠️ 半角 `(5)` 和全角 `（141）` 都要认（微信两种都用过）。
    ⚠️ 只在**句尾**匹配 —— 标题中间出现括号不算数。
    """
    t = (title or "").strip()
    if not t:
        return False
    return bool(re.search(r"[（(]\d{1,5}[)）]\s*$", t))


def _is_overlay_row(row: OcrRow, blocks, boxes) -> bool:
    """这一行是不是**盖在消息上的浮层**（微信自己的提示条），而不是一条消息。

    ⚠️ 为什么需要它（2026-10-13 实测，`wx_1323_group303`）：
        那条长公告的右上角压着微信自己的「∧ 4条新消息」绿色胶囊，OCR 把它读成
        一条独立的消息 —— 因为它在盒外、5 个字、靠右，`side_of_row` 还判成了
        **「我」发的**（用户眼里就是凭空多出一条自己的话）。

    判据是**结构**的：**真消息之间不会在同一水平带上并排**（同一行会被
    `rows_from_text` 归并成一条），而浮层是**盖在消息之上**的 ——
    所以它与某个"盒内块"**竖直重叠、水平却分开**。

    ⚠️ 要求"水平完全分开"是刻意的：同一行的左右两段文字是**相邻或重叠**的，
       不会被这条误伤。
    ⚠️ 这一行自己必须在**盒外**；盒里的是正常消息（卡片/长文本）。
    """
    if not boxes:
        return False
    for (x1, y1, x2, y2) in boxes:
        if x1 <= row.cx <= x2 and y1 <= row.cy <= y2:
            return False                       # 自己在盒里 -> 是正常消息
    for b in blocks:
        if not (row.y1 < b.y2 and b.y1 < row.y2):
            continue                           # 竖直不重叠 -> 与浮层无关
        if not any(x1 <= b.cx <= x2 and y1 <= b.cy <= y2
                   for (x1, y1, x2, y2) in boxes):
            continue                           # 这个块也不在盒里
        if row.x2 < b.x1 or b.x2 < row.x1:
            # ⚠️ 还要求**这一行的大部分高度**都压在盒内块上：浮标是"整条盖在
            #    消息上"，而卡片内部的"左标题 + 右封面图"只是**部分**竖直重叠。
            #    实测（`wx_1323_gongzhonghao`）：真浮标 0.86、卡片标题 0.53
            #    -> 门槛 0.7 能把两边分开。⚠️ 不加这一条会把公众号文章卡片的
            #    标题整条误杀（实测丢 2 段）。
            ov = min(row.y2, b.y2) - max(row.y1, b.y1)
            if ov > 0 and ov >= 0.7 * (row.y2 - row.y1):
                return True                    # 整条压在消息上 -> 浮层
    return False


#: 微信自己的**固定文案** —— 它们画在聊天流里，但**不是对话内容**：
#:   · 悬浮按钮：实测「✓ 跳转到最新消息」（右下角绿色胶囊，2026-10-13 `wx_1567_group4`）；
#:   · 通话状态提示：实测「📞 已在其它设备拒绝」（2026-10-13 `wx_1567_single`）——
#:     ⚠️ 它**渲染成对方气泡**（左边一个灰气泡带电话图标），所以**像素上分不出**
#:     "这是提示"还是"对方说的话"，**只能靠文案认**。
#:
#: ⚠️ 这是**独立于 `textfilter`** 的一套判据，**别把那个拿过来用**：
#:    `textfilter.is_noise_text` 是"口吻样本"的筛子，它**刻意偏向多丢**
#:    （它自己的 docstring 就写着"别把这个函数拿去过滤正文"）——
#:    而消息过滤的铁律是**一条都不能丢**。
#: ⚠️ 所以这里**只认整条精确相等**：一旦放宽成"`in` 判子串"，
#:    真消息「我明天去看看跳转到最新消息」就会被误杀。
#: ⚠️ 也别往里加太短的词（`已取消` / `通话中`）—— 那几个用户真会发。
#: ⚠️⚠️ **更别加"像人话"的短语**：我一度把「对方无应答」「对方已取消」也列了进来，
#:    结果 `test_light_theme_reads_other_side` **立刻抓到** ——
#:    那个浅色合成用例里"我"发的那条消息文本**恰好就是「对方无应答」**。
#:    微信是把它当通话提示画的，但**用户也真可能发这五个字**，
#:    而消息过滤的铁律是"一条都不能丢" → 判据**只收"不像人话"的那些**
#:    （带"其它设备"这种术语的可以收，`对方无应答` 不行）。
_UI_ONLY_TEXT = (
    "跳转到最新消息",
    "回到最新消息",
    "以上是打招呼的内容",
    "已无更多消息",
    "查看更多消息",
    "已在其它设备接听",
    "已在其它设备拒绝",
)

#: 固定文案前面可能**粘着图标被 OCR 读出来的字符**：
#: 电话 `📞` -> `口` / `[1` / `Q`，未读圆点 -> `●`，免打扰铃铛 -> `Q`。
#: ⚠️ 只剥**这些符号**，绝不剥汉字 —— 否则「我已在其它设备拒绝」也会被当成提示。
_UI_ICON_PREFIX = "口[【(（]|1Q$●○◎◉*·.,，。:： \t\u3000"


def _ui_text_of(t: str) -> str:
    """剥掉前置的**图标字符**，用于和 `_UI_ONLY_TEXT` 精确比对。

    ⚠️ 为什么必须有它（2026-10-13 实测）：`limit_side_len` 从 736 提到 1536 之后，
       「📞 已在其它设备拒绝」前面那个电话图标**被认出来了**（读成 `口`），
       于是 `s in _UI_ONLY_TEXT` 的精确匹配**失效**、那条又漏进了正文。
    ⚠️ 只剥符号、**不剥汉字**：`我已在其它设备拒绝` 是**用户真可能说的话**，
       剥成提示就误杀了（消息过滤的铁律是"一条都不能丢"）。
    ⚠️ 剥完仍然要求**精确相等**，不放宽成子串匹配。
    """
    return (t or "").strip().lstrip(_UI_ICON_PREFIX).strip()


#: 微信的**系统提示里带变量**的那些 —— 不能精确匹配，只能按文本形态认。
#: ⚠️ 实测（2026-10-13，用户报）：「“洁”撤回了一条消息」被当成了
#:    一条「我」发的消息。它带**昵称变量**（任何人都可能撤回），
#:    所以 `_UI_ONLY_TEXT` 那种精确匹配用不上。
#: ⚠️ 只匹“回撤了一条消息”这个**固定部分**，而且**还要加一条
#:    位置判据（居中）兜底** —— 系统提示是居中的灰色小字，
#:    而用户真发这句话时通常在气泡里（靠左 / 靠右）。
_SYS_HINT_RE = re.compile(r"撤回了一条消息")

#: `_drop_reason()` 的返回值 -> 日志里怎么念（排查时这行是**唯一**能看到取舍的地方）。
_DROP_LABEL = {
    "below": "在最后一块文字下方",
    "image": "落在图片区域里",
    "overlay": "浮层（与消息竖直重叠且水平分开）",
    "ui": "界面按钮 / 系统提示的固定文案",
}


def _drop_reason(row: OcrRow, content_bottom, image_rects, blocks,
                 boxes, mid_x=None, area_width=0) -> Optional[str]:
    """这一行为什么**不该算消息** —— 返回 `None` 表示"留下"。

    ⚠️ 为什么抽成独立函数（2026-10-13 结构改进）：这三条判据原来**散在
       `read_chat` 的 70 行里**，中间夹着大段坐标说明；想加一条得在一堆注释
       中间找位置，还容易把**顺序**（就是优先级）搞错。现在它们是一张
       **顺序明确的清单**，而且能**单独测** —— 喂一个 `OcrRow` 加几个上下文
       参数就能验，不必起 OCR 引擎、不必准备整屏截图。

    返回值：
      · `"below"`   —— 在"最后一块文字"下方很多（输入框 / 工具栏）
      · `"image"`   —— 落在图片/卡片区域里（那是图上的字）
      · `"overlay"` —— 盖在消息上的浮层（微信自己的提示条）

    ⚠️ **时间分隔不在这个清单里**：它是 `snap.markers` 的来源，走的是另一条路
       （见调用点），而且**不计入"丢弃统计"**（它本来就不是消息）。
    """
    # ⚠️ "很多"要有余量：`content_bottom` 本身已经是最后一块的下沿 +12，
    #    所以这里再给 48 —— 太紧会把**最后一条真消息**一起丢掉
    #    （真机实测：绿气泡那条就是这么被吃掉的）。
    if content_bottom is not None and row.y1 > content_bottom + 48:
        return "below"
    # ⚠️ 落在图片/卡片区域里 -> 那是图片上的字，不是聊天消息
    #    （纯 OCR 路径本来分不出这两者）。
    if any(x1 <= row.cx <= x2 and y1 <= row.cy <= y2
           for (x1, y1, x2, y2) in image_rects):
        return "image"
    # ⚠️ 盖在消息上的浮层不是消息 —— 判据见 `_is_overlay_row`。
    if _is_overlay_row(row, blocks, boxes):
        return "overlay"
    # ⚠️ 微信自己的**悬浮按钮 / 系统提示**的固定文案不是消息：实测「✓ 跳转到最新消息」
    #    被读成一条「我发的」消息。判据**只认整条精确相等**（见 `_UI_ONLY_TEXT`）——
    #    消息过滤"一条都不能丢"，宁可漏认也不能误杀。
    #    ⚠️ 比对前用 `_ui_text_of()` **剥掉前置的图标字符**：实测电话图标 `📞`
    #       会被 OCR 读成 `口` / `[1`（`limit_side_len` 那次实验里就复现了）。
    _s = _ui_text_of(row.text())
    if _s in _UI_ONLY_TEXT:
        return "ui"
    # ⚠️ **带变量**的系统提示（撤回…）：文本形态 + **居中**。
    #    ⚠️ 两条**同时**要求 —— 只按文本会误杀「我刚刚撤回了一条消息」
    #       这种真消息；只按居中会误杀居中的正常气泡。
    #    ⚠️ 拿不到中线（旧调用点 / 单测）就**不敢用位置判据**，宁可放过。
    if _SYS_HINT_RE.search(_s):
        if mid_x and area_width and abs(row.cx - mid_x) < area_width * 0.25:
            return "ui"
        return None
    return None


def _image_regions(boxes, ca, mid_x: float, min_color: int = 24,
                   strip_frac: float = 0.16, blocks=None):
    """从气泡盒里挑出**图片 / 卡片**的区域（里面的字不算消息）。

    ⚠️ 为什么必须有它：切行改成按文字块之后，**图片上的字会变成假消息**
    （2026-10-12 真机实测：右上那张图片缩略图被读成 3 条）。而纯 OCR 路径
    **拿不到控件类名**，分不出「气泡里的字」和「图片里的字」——
    这是它的一条已知损失，原来靠"按盒切行"侥幸避开（图片盒里没字就不成消息），
    改成文字切行之后就避不开了。

    判据一：**盒内的颜色数**。
      · 文本气泡 = 底色 + 字色，量化之后只有个位数种颜色
      · 图片 / 照片 / 带缩略图的卡片 = 几十到几百种
    ⚠️ 这条判据**与主题无关**（不依赖色差），所以浅色深色都成立 ——
       和 `bubble_boxes` 那条"和底色差多少"的性质完全不同。

    判据二：**横跨聊天区大部分宽度的"顶部横条"**（`strip_frac`）。
      ⚠️ 为什么还要这条：真机实测那张图片缩略图**没有**被连通域完整圈住
      （只圈到了 54×54 的一角），而它上面的说明文字（"…的相关经历，想和您…"）
      落在盒子**外面**，于是照样当成了消息。图片在这一屏上是**置顶**的
      （`y` 从裁剪区最顶开始），而微信的消息流在滚动时本来就常常是
      "顶部一段被切掉的内容 + 下面跟着时间分隔"—— 所以「顶部横条 = 不完整
      的图片/卡片」这条判据很稳：**正文消息不会横跨这么宽**（气泡最多占
      六七成宽度，`find_input_top` 用的也是同一个事实）。

    返回 `[(x1,y1,x2,y2), ...]`（裁剪图坐标系）。
    """
    out = []
    W = ca.shape[1] if getattr(ca, "size", 0) else 0
    for b in (boxes or []):
        try:
            x1, y1, x2, y2 = (int(v) for v in b)
        except Exception:
            continue
        if x2 - x1 < BUBBLE_MIN_W or y2 - y1 < BUBBLE_MIN_H:
            continue
        # ⚠️⚠️ **装了"成行对齐的文字"的盒是卡片/文本消息，不是图片。**
        #     2026-10-13 真机实测补的：`wx_1323_dark_group` 里那张 bilibili
        #     分享卡片颜色数是 **77**（远超阈值 24），于是整盒被判成图片，
        #     **它装的 4 块卡片正文全被剔除** —— 而那 4 块**就是消息内容本身**
        #     （症状：那一屏召回 3/3 -> 1/3）。
        #     颜色数分不开"图文卡片"和"照片"（两者颜色都多），但**结构**能分：
        #     卡片正文**左边界对齐、成行排列**，而图片里的字位置随机。
        #     ⚠️ 容差取盒宽的 15%：实测卡片那 4 块左边界是 419/419/418/449
        #        （前三个完全对齐，最后一个是平台名），盒宽 378 -> 容差 57。
        #     ⚠️ 要求 >= 2 块：只有一块文字的盒（如头像旁一个短标签）
        #        证据太弱，继续按颜色数判。
        inside = [x for x in (blocks or [])
                  if x1 <= x.cx <= x2 and y1 <= x.cy <= y2]
        # ⚠️ 判据 A：装了 **>= 3 块**文字 —— 那就是图文卡片 / 长消息
        #     （实测小程序卡片 8 块、bilibili 卡片 4 块）。
        #     不会把纯图片也放进来：**照片/截图里的字识别不出块**
        #     （实测那张表格图片是 0 块）。
        if len(inside) >= 3:
            continue
        # ⚠️ 判据 B：装了 2 块**且左边界对齐**（卡片正文排成一整列）。
        if len(inside) >= 2:
            xs = sorted(x.x1 for x in inside)
            if (xs[-1] - xs[0]) <= 0.15 * (x2 - x1):
                continue                  # 文字排成一整列 -> 文本容器，不是图片
        if _color_count(ca, (x1, y1, x2, y2)) >= min_color:
            out.append((x1, y1, x2, y2))
            continue
        # 判据二：顶部横条（图片/卡片被顶部截断的那种）
        if W and (x2 - x1) >= strip_frac * W and y1 <= 4:
            out.append((x1, y1, x2, y2))
    return out


def _box_is_phantom(box, blocks, ca=None, span: float = 0.8) -> bool:
    """这个气泡盒是不是**假盒子**（其实是背景/一整片区域，不是一个气泡）。

    ⚠️ 为什么要有这条：浅色下真气泡不进 mask，而**能**进 mask 的反而是
    「主色就是聊天底色的那几块」—— 实测真机上一次冒出 4 个假盒子，其中最大的
    一个是 `794×831` 盖住整个聊天区。它会把所有文字块都"圈住"，
    于是身份判定、图片区域判定全被它带偏（而且它**不是**气泡，
    没有"离哪边近"可言）。

    ⚠️⚠️ 而且它有**两种**形态，两种都要挡：
      · **大盒子**：面积占聊天区一大块（`width_frac` / `height_frac`）——
        真气泡最多占六七成宽度（`find_input_top` 用的也是这个事实），
        高度更不可能占掉大半屏。⚠️ 低 `tol` 时特别容易大量冒出来
        （实测 `tol=30` 会生成一个盖住整屏的盒，把所有消息都吃掉）。
      · **横向分成两簇**：盒子里装着多个横向明显分开的文字块。
        ⚠️ `span` 2026-10-13 从 **0.6 放宽到 0.8**：浅色主题下
        `wx_1567_light` 那张**长标题卡片**（378 宽）里装着
        「示例推送标题」+ 图标上的「重要」「提醒」
        + 底部的「新华社」，横向跨度 **260 = 0.69*w**
        —— 被 0.6 判成假盒剔除，于是那张卡片**碎成两条**
        （标题一条、来源一条）而且**整张没被分类**。
        ⚠️ 敢放宽的理由：**真正要挡的假盒是“盖满整屏的背景连通域”**
        （实测 794x831 = 面积 120%、宽度 88%），那两条**面积/宽度判据已经拦住了**
        —— 横向这条只是兜底。
    """
    try:
        x1, y1, x2, y2 = (float(v) for v in box)
    except Exception:
        return True
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return True
    if ca is not None:
        try:
            ch, cw = ca.shape[:2]
            if cw and ch:
                # ⚠️⚠️ 判"盖住大半屏"要用**面积比**，不能用"单边超过一半"。
                #     2026-10-13 实测（`wx_1323_miniapp`）：那张小程序卡片是
                #     377x431，而聊天区只有 902x609 —— **高 431 > 0.5*609**，
                #     于是被这条判成"假盒"剔除，后果是**两头都坏**：
                #       · `_image_regions` 拿不到它（盒里的文字侥幸没被删）
                #       · `merge_rows_in_boxes` 也拿不到它 -> 卡片碎成 5 条
                #     而真正的假盒是"背景连通域"，它**几乎盖满整屏**
                #     （实测 794x831 = 聊天区面积的 1.2 倍）。
                #     换成面积比：真卡片 29.5% 放过，假盒 120% 拦住。
                if (w * h) > 0.7 * (cw * ch):
                    return True          # 面积盖住大半屏 -> 不是气泡
                if w > 0.8 * cw:
                    return True          # 横向横跨大半屏（细长条）也不是气泡
        except Exception:
            pass
    inside = [b for b in blocks
              if x1 <= b.cx <= x2 and y1 <= b.cy <= y2]
    if len(inside) < 3:
        return False
    xs = sorted(b.cx for b in inside)
    return (xs[-1] - xs[0]) > span * w


def rows_from_bubbles(blocks: List[OcrBlock], boxes,
                      mid_x: float, width: int):
    """把 OCR 块按气泡包围盒归成消息行（**气泡盒可用时的加分路径**）。

    返回 `(rows, free_blocks)`：
        rows        每个气泡一条（**没认到文字的气泡不产生消息**）
        free_blocks 落在所有气泡**外面**的块 —— 昵称、时间分隔都是这种，
                    由调用方再判一次（判别看 `pick_nickname` / `is_time_text`）

    归属判据：块的**中心**落在气泡盒里。用中心而不是"有重叠" ——
    昵称紧贴在气泡上沿，重叠判据会把它算进气泡（中心判据不会）。

    ⚠️ **别在浅色下依赖它**（见 `rows_from_text` 的说明）：浅色下对方的气泡
       压根不成盒，这里会返回 0 条 —— 而调用方以为"读到了，只是没有消息"。
    """
    buckets = [[] for _ in boxes]
    free = []
    for b in blocks:
        idx = None
        for i, (x1, y1, x2, y2) in enumerate(boxes):
            if x1 <= b.cx <= x2 and y1 <= b.cy <= y2:
                idx = i
                break
        if idx is None:
            free.append(b)
        else:
            buckets[idx].append(b)

    rows = []
    for box, bs in zip(boxes, buckets):
        if not bs:
            continue          # 头像一个字都没有、或者图里没字 -> 不是消息
        r = OcrRow(blocks=sorted(bs, key=lambda z: (z.y1, z.x1)))
        # 发送者按**气泡盒离哪边近**判，不按文字中心（见 side_of_box 的说明）
        r.is_self = side_of_box(box, width)
        # ⚠️ 把气泡盒记下来：`attach_free()` 判"昵称贴着气泡上沿"要用**盒子的顶边**，
        #    不能用行里文字的顶边 —— 气泡上沿到第一行文字之间还有一段内边距，
        #    实测 24px，而文字的顶边把它算进去之后"间隔太大"会把真昵称拒掉。
        r.box = box
        rows.append(r)
    rows.sort(key=lambda r: r.y1)
    return rows, free


def side_of_row(row: OcrRow, width: int,
                overlap: float = 0.15) -> Optional[bool]:
    """这条行是不是**我自己**发的 —— 按文字的横向位置判。返回 None 表示判不了。

    判据（**不用文字中心**，理由同 `side_of_box`：窄窗口上左右几乎重叠）：
        · 行整体落在右半边（`x1` 超过中线往右一点）-> 我
        · 行整体落在左半边（`x2` 不到中线）-> 对方
        · 横跨中线 -> **判不了，返回 None**（不瞎猜）

    ⚠️ 这是**没有气泡盒时**的兜底，比 `side_of_box` 弱：微信的气泡贴边，
       所以长消息也压不出中线太远；但**跨多行的长消息**有可能横跨中线 ——
       那种情况返回 None，由调用方按"保持上一次判定/整行按左边算"处理。
    """
    if width <= 0:
        return None
    mid = width / 2.0
    margin = width * overlap
    if row.x1 >= mid - margin:
        return True
    if row.x2 <= mid + margin:
        return False
    return None


def attach_free(row: OcrRow, free: List[OcrBlock], max_chars: int) -> bool:
    """把落在气泡外的块**认成这一行的群昵称**（认到返回 True）。

    条件：紧贴在气泡上方、水平方向压在气泡上、文本短、不是时间、不是纯数字。

    ⚠️ **"紧贴"要比的是气泡盒的顶边**（`row.box[1]`），不是行里文字的顶边 ——
        气泡上沿到第一行文字之间还有一段内边距（实测 24px），
        拿文字的顶边当基准、"间隔不超过半行高"当判据的话，
        **真昵称会被判成"离得太远"而拒掉**（第四十六轮实测踩到：
        真截图 `compare_uia_ocr.png` 上「李四」就是这么丢的）。

    ⚠️ 少了「水平压在气泡上」这条，左边那条昵称会被算到右边那条气泡头上
        （同一屏里左右两条气泡的 y 常常差不多）。
    """
    cand = [b for b in free
            if b.y2 <= row.y1 + 2 and b.x2 > row.x1 and b.x1 < row.x2]
    if not cand:
        return False
    top = max(cand, key=lambda b: b.y2)
    # 基准：有气泡盒就用盒子的顶边，否则退回文字的顶边
    base = row.box[1] if row.box else row.y1
    if top.y2 > base + 2:
        return False
    if base - top.y2 > max(12, (row.y2 - row.y1)):
        return False
    t = (top.text or "").strip()
    if not t or len(t) > max_chars or is_time_text(t):
        return False
    if re.match(r"^[\d\s:：./\\\-+]+$", t):
        return False
    row.nickname = t
    row.nick_block = top        # 正文里排除它
    return True


# ------------------------------------------------------------------ 聊天区几何

def find_input_top(a, bg, x1: int, x2: int, y_from: int) -> Optional[int]:
    """找**输入区上边那条分隔线**的 y（找不到返回 None）。

    为什么要检测而不是写死：输入框会随用户打字**变高**，
    写死一个高度的话，用户打了三行字就会把输入框的正文当成聊天消息读进库。

    判据：输入区和聊天区常常是**同一个底色**，唯一的分界就是那条 1px 分隔线。
    而分隔线是**横贯整个宽度**的：气泡最多占宽度的六七成，永远到不了 100%。

    做法：从窗口底部往上扫，找第一行「几乎整行都和聊天区底色不同、
    而且**它旁边那一行**又回到底色」的行。

    ⚠️ 加"旁边那一行要回到底色"这半条是实测补的：只看"整行都不是底色"的话，
        窗口最底下那条边框线会先被命中（它是最后一行，检测直接失败），
        结果整个输入区都被当成聊天内容 —— 实测把输入框里的「发送」
        读成了一条消息。分隔线的特征是**细**：上下相邻行必有一行是底色。
    """
    H = a.shape[0]
    sub = a[:, x1:x2]
    diff = np.abs(sub - bg).sum(axis=2)
    differ = (diff > 18).mean(axis=1)        # 每一行有多大比例"不是底色"
    for y in range(H - 3, y_from, -1):
        if differ[y] <= 0.9:
            continue
        if differ[y - 1] < 0.6 or differ[y + 1] < 0.6:
            # ⚠️⚠️ 这里**不要**再"往上扩展"去圈所谓的引用条 ——
            #    2026-10-13 试过，**已回退**：
            #    那次我以为 `wx_1567_file_link` 里那条
            #    「αβγδεζηθικλμ: 什么时候来」是**输入框上方的引用条**，
            #    其实它是**已经发出去的引用消息**（用户发了它）。
            #    判据是它的 x 范围 779..1006 **只占右侧一块** ——
            #    而**输入框里的文字是从左边内边距开始的**，不会出现在那儿。
            #    往上扩展会把它当输入区**排除掉 = 丢掉一条真消息**，
            #    正是这个项目最忌讳的错。
            #    📌 教训：**"紧贴输入框上方"这个位置既可能放引用条、
            #    也可能放最后一条消息** —— 光靠"连续非底色"分不开，别用这条判据。
            return y
    return None


def detect_chat_left(a, bg, tol: int = 30, need: float = 0.9,
                     min_run: int = 8, right_edge: float = 0.95,
                     left_edge: float = 0.10) -> Optional[int]:
    """**只靠像素**找聊天区左边界（会话列表和聊天区的分界）。认不出来返回 None。

    判据：聊天区那一侧是**贴着聊天区底色的**（气泡之间、气泡左右的留白都是），
    而会话列表那一侧**底色不是这个颜色** —— 所以逐列算「这一列有多大比例
    等于聊天区底色」，再从左往右找第一段**足够长的纯底色列**，它的左端就是分界。

    为什么需要它（而不是只靠 `ocr_read.chat_left`）：
        配置里那个 310 逻辑像素是**在某一台机器、某一个微信版本上量的**。
        换一台机器 / 换一个微信小版本，它就可能不对 —— 而判错了**不报错**，
        只是读出来全是会话列表里的字（或者一条都读不出来），
        和"这个后端坏了"完全分不开。

    ⚠️ `tol` 是「和聊天区底色差多少还算同色」（三通道曼哈顿距离）。**别调大**：
        实测聊天区底色 30、会话列表底色 52，差 66 —— 而 tol=60 就会把
        会话列表也当成"同色"，于是第一段纯底色列落在扫描起点、整幅被判成聊天区
        （我第一版就是 60，测出来 154 = `left_edge * 宽度`，一看就是没找到边界）。
        tol=30 时两边分得干干净净，而聊天区自己的留白和底色差是 0。

    ⚠️ 还要**真的观察到"由不是底色变成是底色"这个跳变**才算数：
        整幅都是底色（抓到空白图 / 两边同色）时返回 None，而不是返回扫描起点。

    ⚠️ 它**只用来兜底**：调用方是「配置值认不出气泡时才试它，
        而且试出来还得真能认出气泡才算数」。所以它认错也没有代价 ——
        这一条是刻意的，见 `OcrReader._geometry_candidates()`。
    """
    if bg is None or a.size == 0:
        return None
    H, W = a.shape[:2]
    if W < 200:
        return None
    diff = np.abs(a - bg).sum(axis=2)
    close = (diff <= tol).mean(axis=0)      # 每一列的"聊天区底色占比"

    lo = int(W * left_edge)
    run = 0
    for x in range(lo, int(W * right_edge)):
        if close[x] >= need:
            run += 1
            if run >= min_run:
                left = x - min_run + 1
                if left <= lo + 1:
                    # 从扫描起点就是底色 -> **没有观察到边界**，不可信
                    return None
                if left >= W * 0.8:
                    # 太靠右：聊天区不可能只占右边一小条，多半是误判
                    return None
                return left
        else:
            run = 0
    return None


def chat_rect(win_rect, scale: float, cfg, img_size) -> tuple:
    """算出聊天区在**位图里的**矩形 (x1, y1, x2, y2)。

    ⚠️ 用**逻辑像素**配 DPI 缩放来算，不按窗口宽度的百分比 ——
        微信的「导航栏 + 会话列表」是**固定宽度**（实测 310 逻辑像素），
        窗口一拉宽，它在总宽里的占比就变了，按比例算会直接错位。

    实测（2026-09-27，`out/printwindow_flags2.png`，1542×979 位图 / 1.5 倍）：
        会话列表和聊天区的分界在 x≈465 物理 = **310 逻辑** ✓
        聊天标题「张三」在 y=71..93 物理，落在标题栏里

    返回值会**钳到图像范围内**，并且保证 x2 > x1、y2 > y1。
    """
    W, H = img_size
    left = int(round(float(cfg.get("ocr_read.chat_left", 310)) * scale))
    top = int(round(float(cfg.get("ocr_read.chat_top", 70)) * scale))
    right_margin = int(round(float(cfg.get("ocr_read.right_margin", 0)) * scale))
    x1 = max(0, min(left, W - 2))
    y1 = max(0, min(top, H - 2))
    x2 = max(x1 + 1, W - right_margin)
    y2 = H
    return (x1, y1, x2, min(y2, H))


# ------------------------------------------------------------------ 读取器

class OcrReader:
    """和 `reader.WeChatReader` 同接口的**纯像素**后端。"""

    def __init__(self, cfg, logger: Callable[[str], None] = None, ocr=None):
        self.cfg = cfg
        self._log = logger or (lambda msg: None)
        self._ocr = ocr                 # 可以注入（测试用）；None 时懒加载真引擎
        self.last_miss = ""
        self.stats = {"reads": 0, "blank": 0, "ms": 0.0, "blocks": 0}
        #: 上一次读屏是不是**因为几何不对**而彻底失败（`run_watch` 用它给出
        #: 比"当前没有打开的会话"更有用的提示）
        self._geom_bad = False
        #: 上一次实际用的裁剪框 (x1, y1, x2, y2)
        self.last_crop = None
        #: 上一次**成功读屏**采用的聊天区左边界（物理像素，**不含** left_margin）。
        #: ⚠️⚠️ 标题栏必须和正文用**同一个**边界，见 `_title_left`。
        self._left_accepted = None
        #: 「左边界」那行日志**只报一次**用的（每拍都会走到，不节流会刷屏）。
        #: ⚠️ 必须在 `__init__` 里初始化！只在 `read_chat` 里赋值的话，
        #:    第一次读到它就 `AttributeError` —— 而那会让**记录器整个停摆**。
        #:    实测（2026-10-11）：`test_ocr_reader` 全绿也发现不了，
        #:    因为假配置走不到 `bx1 != x1` 那个分支，**只有真机才会踩**。
        self._left_logged = None
        #: 「丢了 N 段图片上的字」那行日志的节流标记（同上，每拍都会算）。
        self._image_rows_logged = None
        #: `_title_left` 自己检测的结果缓存，键是位图宽度
        #: （窗口一改宽度就重找 —— 那个边界本来就随宽度变）。
        self._title_left_cache = {}
        #: 探针要裁的那一块 `(x1, y2)`。**只在完整读屏之后更新**，
        #: 见 `_probe_geo` —— 探针里绝不做像素分析（实测会从 35ms 涨到 132ms）。
        self._probe_box = None
        #: 后台连续记录值不值得做 —— 见 `config.yaml` 的 `ocr_read.record`。
        #: ⚠️ 默认 **False**：一次读屏 ≈ 300ms（抓图 + OCR），
        #:    而 UIA 那条路一整拍才 ~100ms。快速档（0.2s）在这条路上
        #:    **根本跑不动**（一拍比间隔还长，会自由狂奔），
        #:    1s 一拍也要常驻三成的一个核。默认只在你要的时候读一屏。
        self.background_read_ok = bool(cfg.get("ocr_read.record", False))

    # ------------------------------------------------------------ 引擎

    def _engine(self):
        if self._ocr is None:
            from .ocr import get_engine
            self._ocr = get_engine(logger=self._log)
        return self._ocr

    def _scale(self) -> float:
        """位图像素 / 逻辑像素 的比值。

        ⚠️ 用 `dpi.current_dpi()/96` **而不是**拿窗口宽度去除某个数 ——
            配置里的尺寸（`chat_left: 310`）写的是**逻辑像素**，
            而 PrintWindow 的位图是**物理像素**，两者差一个 DPI 缩放。

        ⚠️ 进程**没有** DPI 感知时，Windows 会替我们把窗口坐标和位图
            一起虚拟化成逻辑像素，那时比值就是 1.0 —— 不能再乘缩放，
            否则会把聊天区切到会话列表里去（差 1.5 倍，正好切进去）。
        """
        try:
            from . import dpi
            if not dpi.is_aware():
                return 1.0
            return max(1.0, min(4.0, float(dpi.current_dpi()) / 96.0))
        except Exception:
            return 1.0

    # ------------------------------------------------------------ 窗口

    def find_window(self):
        """和 UIA 版**共用同一套挑选逻辑**（纯 Win32，本来就不依赖 UIA）。"""
        cands = list_windows(self.cfg)
        win = pick_window(cands, self.cfg)
        if win is None:
            why = ("有 %d 个候选但分不出主窗口：%s" % (
                len(cands), [c.get("title") for c in cands])) if cands else "没找到微信窗口"
            if why != self.last_miss:
                self._log("⚠ %s" % why)
            self.last_miss = why
        else:
            self.last_miss = ""
        return win

    # ------------------------------------------------------------ 抓图

    def grab(self, hwnd: int):
        """抓整窗位图。抓不到（空白）返回 None。"""
        img = capture.capture_window(hwnd)
        if img is None or capture.is_blank(img):
            self.stats["blank"] += 1
            return None
        return img

    # ------------------------------------------------------------ 标题

    def read_chat_title(self, hwnd: int) -> str:
        """OCR 标题栏，认出当前会话名。

        ⚠️ 这个方法的返回值是**会话主键**（`session_key`）——
            认错了会直接串会话、串设置、串历史。所以宁可为空也不要猜：
            认不出返回 ""，调用方会当作"这一步读不到"跳过。
        """
        img = self.grab(hwnd)
        if img is None:
            return ""
        return self._title_from(img)

    def _crop_left(self, x1: int) -> int:
        """真正该从哪一列开始裁 —— 在算出来的左边界上**再往左让一点**。

        ⚠️⚠️ 为什么不退这一点会掉字（2026-10-11 用户实测 + 复现）：

            用户报「**公众号**会被识别成**众号**，缺少一个字」。

            `probes/probe_title_clip.py` 在真机上把同一个标题栏用几种左边距
            各裁一次、各跑一次 OCR，结果：

                左边距  0（原来的）-> '众号'      ← 掉了「公」
                左边距  3           -> '公众号'    ✅
                左边距  6           -> '公众号'    ✅

            原因：`detect_chat_left()` 找的是「第一段足够长的纯底色列」，
            它的起点会**比真实边界晚几个像素**（实测真值 465、检测 467）。
            那几个像素正好是第一个字最左边那一竖 —— 少了它，
            OCR 就把「公」整个丢掉了（不是读错，是**根本没有这个字**）。

        ⚠️ 退多少是**有依据**的，不是拍脑袋：`ocr_read.left_margin` 默认 6 逻辑
            （1.5 倍屏上 9 物理）。实测往左让到 **24 物理** 都还能正确读出标题，
            而且「最左 30px 里有没有会话列表的文字」那一项在 0 / 6 / 16 三种
            左边距下都是**空** —— 也就是让出去的只是会话列表的底色，不吃它的字。

        ⚠️ 为什么不干脆多让一点（比如 40px）：再往左就真的会吃到会话列表的
            文字了，而 `_title_from` 取的是「**最靠左**的那一块」——
            一旦吃到，它会**赢过真标题**，标题就变成了会话列表里的某个名字。
            同理，聊天区的气泡盒也会多出左边一条非底色的窄条
            （不过那个宽度 < 40 会被 `bubble_boxes` 的尺寸过滤挡掉）。
        """
        m = int(round(float(self.cfg.get("ocr_read.left_margin", 6)) * self._scale()))
        return max(0, int(x1) - max(0, m))

    def _title_left(self, img) -> int:
        """标题栏该从哪个**物理像素**开始裁 —— 和正文用同一个左边界。

        ⚠️⚠️ 为什么必须有这个方法（2026-10-11 用户实测报的 bug）：

            先报「**公众号** → **众号**」（掉第一个字），加了 `left_margin: 6` 好了；
            又报「**广财**信息学院23级级群(499) → **财**信息学院23级级群(499)」
            —— **又是掉第一个字**。

            `probes/diag_chat_left.py` 在这台机器上量出来：

                窗口 1402 宽：**真边界 x=430**（428→0.00、430→1.00 那个跳变）
                而配置 `chat_left` = **465**

            **配置值偏右 35 物理像素，够吃掉一整个字。**

            根因不是边距不够，是**标题和正文用了两个不同的边界**：
              · 正文走 `_geometry_candidates()` —— **检测优先**，用的是 430 ✅
              · 标题走 `chat_rect()` —— 那个函数**只算配置值**，根本不调检测 ⚠️
            而 `_crop_left` 只往回让 9 像素，补不上这 35。

        ⚠️ 探针模式下尤其要紧：标题每 3 秒读一次，而 `read_chat` 只在
            **内容变化时**才跑 —— 所以 `_left_accepted` 可能**一直是 None**，
            不能只靠缓存，得自己找一次。

        ⚠️ 找一次就**缓存住**（按位图宽度）—— 包括"没找到"这个结果。
            不定死的话每 3 秒都要重算一次模式色，那是白花的开销。
        """
        scale = self._scale()
        cfg_x1 = int(round(float(self.cfg.get("ocr_read.chat_left", 310)) * scale))
        # ① 正文刚成功读过 —— 直接跟它走（最快，也最一致）
        if self._left_accepted is not None:
            return self._left_accepted
        # ② 关掉自动检测就老实按配置来
        if not bool(self.cfg.get("ocr_read.auto_geometry", True)):
            return cfg_x1

        W = getattr(img, "width", 0)
        if W in self._title_left_cache:
            cached = self._title_left_cache[W]
            return cfg_x1 if cached is None else cached

        det = None
        try:
            a = np.asarray(img.convert("RGB"))
            top = int(round(float(self.cfg.get("ocr_read.chat_top", 70)) * scale))
            # 底色取众数 —— 区间 x 用**配置值**起（它一定在聊天区内），
            # 和 `read_chat` 里算 bg 的方式一致。
            bg = self._bg(a, max(0, min(cfg_x1, a.shape[1] - 1)), a.shape[1],
                          max(0, min(top, a.shape[0] - 1)))
            det = detect_chat_left(a, bg)
        except Exception as e:
            self._log("标题左边界检测失败（沿用配置值 %d）: %s: %s"
                      % (cfg_x1, type(e).__name__, e))
            det = None

        if det is not None and 0 < det < (W or 1):
            self._title_left_cache[W] = det
            self._log("聊天区左边界：标题栏原本会用配置值 %d，实测真边界是 %d"
                      "（偏右 %d 像素，会把标题第一个字切掉）—— 已改用检测值"
                      % (cfg_x1, det, cfg_x1 - det))
            return det
        # 认不出来也记一笔（按宽度），免得每 3 秒白算一次
        self._title_left_cache[W] = None
        return cfg_x1

    def _title_from(self, img) -> str:
        """从已经抓好的位图里认标题（`read_chat` 也用它，别再抓一次图）。"""
        scale = self._scale()
        # ⚠️⚠️ 用 `_title_left` **不是** `chat_rect` —— 后者只算配置值，
        #    而配置值在窄窗口上偏右 35 像素（实测），会把第一个字整个切掉。
        x1 = self._title_left(img)
        _y1, x2 = 0, img.width
        top = int(round(float(self.cfg.get("ocr_read.chat_top", 70)) * scale))
        # ⚠️ 左边要**再让一点**（`_crop_left`）—— 不让的话第一个字会缺一笔，
        #    实测「公众号」被读成「众号」，而让 3 个物理像素就好了。
        strip = img.crop((self._crop_left(x1), 0, x2, max(top, 1)))
        blocks = self._recognize(strip)
        if not blocks:
            return ""
        # 标题是**最靠左的那一块**：标题栏右边只有图标按钮，认不出文字。
        b = min(blocks, key=lambda z: z.x1)
        t = (b.text or "").strip()
        # 太长的多半是误认（标题不会有 30 个字）
        if len(t) > 40 or is_time_text(t):
            return ""
        # ⚠️ 切掉末尾被认出来的图标符号（实测 `…饲养基地（303）（`）——
        #    详见 `_clean_title`：它**只切尾巴、不做归一化**。
        return _clean_title(t)

    # ------------------------------------------------------------ 正文

    def read_chat(self, hwnd: int) -> Optional[ChatSnapshot]:
        """抓一屏 + OCR，还原成 `ChatSnapshot`。

        **四步**（每一步都是一次调用，细节见各自的方法）：
            ① 抓图 + 几何：整窗位图、聊天区矩形、底色、输入区上边
            ② `_locate_and_recognize`：在几档候选左边界上试，定下能用的那一档并 OCR
            ③ `_build_messages`：切行 + 逐行定身份 / 昵称 / 过滤 -> 装进 snap
            ④ `_finish`：补图片消息 + 判群聊 + 标发言人

        ⚠️ 标题在这里自己补（`_title_from`）—— 它是 `session_key`，
           留空会让下游静默认为"当前没有打开的会话"（见那行注释）。
        """
        import time as _time

        t0 = _time.perf_counter()
        img = self.grab(hwnd)
        if img is None:
            # ⚠️ **抓不到图和几何不对是两回事，别混成一句话。**
            #    这里是 PrintWindow 拿到空白（窗口最小化 / 微信停在扫码登录窗），
            #    和 chat_left 对不对毫无关系 —— 不清掉的话，上一次几何失败留下的
            #    `_geom_bad=True` 会一直挂在那儿，界面就会报「几何可能不对，
            #    去跑 --calibrate」，把人往错的方向指。
            self._geom_bad = False
            return None
        W, H = img.width, img.height
        scale = self._scale()
        x1, y1, x2, _ = chat_rect((0, 0, W, H), scale, self.cfg, (W, H))

        a = np.asarray(img.convert("RGB")).astype(np.int16)

        # 输入区上边：**先检测**，检测不到就用配置里的兜底高度
        bg = self._bg(a, x1, x2, y1)
        input_top = self._input_top(a, bg, H, x1, x2, y1)        # 取更靠上的那个（y 更小）、以及为什么这么取，见 `_input_top`：
        #
        #    两种错法不对称，必须偏向保守：
        #      · 输入区**划小了**（分隔线找得太靠下）-> 把输入框里的字
        #        （你正在打的草稿、右边那个「发送」按钮的标签）当成消息读进库。
        #        这是**静默污染**，事后根本分不清哪条是聊天、哪条是草稿。
        #      · 输入区**划大了**（分隔线找得太靠上）-> 少读一两条消息。
        #        这是**看得见**的损失，而且回复目标本来就在最下面，
        #        真少了你一眼就知道。
        #    实测：老截图 `printwindow_flags2.png` 上检测出 961（几乎贴到底边），
        #    而兜底值是 754 —— 不取 min 的话「发送」会被读成一条消息。

        # ---- 几何候选：**检测优先，配置兜底** ----
        #
        # ⚠️ 第四十四轮这里是"配置优先"，第四十六轮**反过来**了 —— 因为实测
        #    会话列表宽度不是常数（1542 宽的窗口分界 465、1206 宽的是 430），
        #    而自动检测在三张真截图上是 467/467/467（真值 465）。
        #    **别再照这段旧理由改回去**，判据在 `_geometry_candidates` 的说明里。
        #
        # ⚠️⚠️ "什么才算这一档读出了东西"这条判据 2026-10-12 改过：原来要求
        #    **`boxes` 非空**（认出气泡盒），而那条在浅色下**必定失败**（对方气泡
        #    和底色只差 36）—— 于是整条路返回 None / 只出一条消息。现在按
        #    **文字块**判：OCR 认出文字才算这一档可用（OCR 在浅色下是好的）。
        #
        # ⚠️ 2026-10-13 结构改进：整段"逐个候选试 + 验收"搬进了
        #    `_locate_and_recognize()` —— `read_chat` 这里只剩一次调用。
        got = self._locate_and_recognize(img, a, bg, x1, y1, x2, input_top, scale)
        if got is None:
            return None
        _cl, _bx1, crop_box, area, blocks, real_boxes, ca, image_rects = got

        snap = ChatSnapshot()
        snap.source = "ocr"
        snap.container_rect = (0, 0, area.width, area.height)   # 裁剪区坐标
        # ⚠️⚠️ **这一条必须写**：截屏 + OCR **判不出「在不在底部」**。
        #
        #     它的 container_rect 就是"裁剪出来的那块可见聊天区"，而往上翻历史时
        #     那一屏照样填满这块区域 -> `at_bottom()` 里的 gap 恒 ≈0 -> **恒为真**。
        #     实测（2026-10-13 查库）：`screens.db` 97 屏，`at_bottom=1` 的 **97 屏**
        #     （100%），而其中的会话明明在滚动。
        #
        #     后果：`save_new_only` 在「这一屏一条都没记过」（cut=-1）时，
        #     本来靠 `at_bottom` 区分"新消息多于一屏（该写）"和"翻到了记录开始
        #     之前的旧消息（不能写）"—— 恒真就等于**永远走"全写"**，
        #     把翻上来的旧消息接到库尾（用户看到的就是"记录乱了"）。
        #     见 `reader.ChatSnapshot.bottom_known`。
        snap.bottom_known = False
        # ⚠️⚠️ **标题必须在这里自己补上，不能留给调用方。**
        #
        #    它是 `session_key`（会话主键）。而 `run_watch.do_generate()` 是
        #    「先 read_chat，再 `if not snap.title: return`」——
        #    这里留空的话，**按热键/点【生成建议】会直接静默返回"当前没有打开的会话"**，
        #    整条 OCR 路等于完全不能用（第一版就是这样，是真机上"完全无法使用"的根因）。
        #    而 UIA 那条路不用管这条：`reader.read_chat` 本来就把标题读进 snap.title。
        #    代价只是一条 60px 高的横条，很便宜；抓好的图直接复用，不再抓第二次。
        snap.title = self._title_from(img)

        # ---- 切行 + 逐行定身份 / 昵称 / 过滤 -> 装进 snap ----
        # ⚠️ 2026-10-13 结构改进：这 220 多行搬进了 `_build_messages()` ——
        #    它只吃 `blocks` + 真气泡盒 + 图片区域，不依赖抓图和几何，
        #    所以能**单独看、单独改、单独测**。
        # ⚠️ `ca`（与 `area` 同原点的 numpy 数组）也要传下去 ——
        #    "分享卡片"的判据要看**盒里的颜色种数**，那需要像素。
        self._build_messages(snap, area, blocks, real_boxes, image_rects, ca)
        self.stats["reads"] += 1
        self.stats["ms"] = (_time.perf_counter() - t0) * 1000

        # ---- 收尾：补图片消息 + 判群聊 + 标发言人（细节见 `_finish`）----
        self._finish(snap, area, image_rects)
        return snap

    def _crop_images(self, area, messages) -> List[str]:
        """把 `[图片]` 消息按 `rect` 裁出来、压缩、编码成 base64（**一张一条**）。

        ⚠️ **只发 `[图片]` 那几条，不发整屏**（2026-10-13 用户定）：
            · 字能被 OCR 认出来的图（付款记录那种白底长截图）
              **内容已经进上下文了**，不必重复发；
            · 而**认不出字**的图（表情包 / 照片 / 纯图）会产出 `[图片]`，
              那才是**真正需要看图**的情况。
        ⚠️ 裁剪坐标和 `message.rect` 同原点（都是**裁剪区坐标**，
           见 `_build_messages`）—— `area` 就是那一块。
        """
        out = []
        for m in messages:
            if m.kind != "image" or not m.rect:
                continue
            try:
                x1, y1, x2, y2 = (int(v) for v in m.rect)
            except Exception:
                continue
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(area.width, x2), min(area.height, y2)
            if x2 - x1 < 8 or y2 - y1 < 8:
                continue
            sub = area.crop((x1, y1, x2, y2))
            w, h = sub.size
            k = min(1.0, float(_IMG_MAX_SIDE) / max(w, h))
            if k < 1.0:
                sub = sub.resize((max(1, int(w * k)), max(1, int(h * k))),
                                 Image.LANCZOS)
            buf = io.BytesIO()
            sub.convert("RGB").save(buf, format="PNG")
            out.append(base64.b64encode(buf.getvalue()).decode("ascii"))
        return out

    def _finish(self, snap, area, image_rects) -> None:
        """收尾三件事（**原地改 `snap`**）：补图片消息、判群聊、标发言人。

        ⚠️ 为什么抽出来（2026-10-13 结构改进）：这三件事**不再依赖几何和 OCR
           的中间量**（只要聊天区尺寸和图片区域），却原来和读屏流程挤在一起。
           抽出来之后 `read_chat` 就是**四步** —— 抓图 → 定几何并 OCR →
           装消息 → 收尾，**每一步都是一次调用**。
        """
        # ---- 图片消息：给"够大的图片区域"补一条消息 ----
        #
        # ⚠️⚠️ 为什么必须补（2026-10-13）：`image_rects` 里的**文字**被丢掉，
        #    而这里原本**不产出任何东西** —— 用户的"图片消息"在库里就是
        #    **不存在**，连一句「[图片]」都没有（`MessageItem.kind` 的
        #    docstring 早就承诺了 `image`，只是从来没有代码产生它）。
        #
        # ⚠️ 判据用**尺寸**排除**头像**（它也是"颜色多的连通域"，实测
        #    54x54 / 54x24 都被算进 `image_rects`），真图片消息是 123x85
        #    和 123x270（见 `_IMAGE_MIN_W_FRAC`）。
        # ⚠️ 还要排除 `_top_cutoff_image` 那块"被顶部切掉的图片"（它精确地
        #    从 (0,0) 开始）：那是**半张**图，不是一条完整消息。
        for (ix1, iy1, ix2, iy2) in image_rects:
            if ix1 <= 0 and iy1 <= 0:
                continue
            # ⚠️ 用**相对尺寸**（见 `_IMAGE_MIN_W_FRAC`）：2 倍图上绝对阈值会失效。
            if ((ix2 - ix1) < _IMAGE_MIN_W_FRAC * area.width
                    and (iy2 - iy1) < _IMAGE_MIN_H_FRAC * area.height):
                continue
            snap.messages.append(MessageItem(
                text=_IMAGE_PLACEHOLDER,
                # 借用微信自己的类名，让 `MessageItem.kind` 判成 "image"
                cls="mmui::ChatImageItemView",
                rect=(ix1, iy1, ix2, iy2),
                sender_name=None,
                is_self=side_of_box((ix1, iy1, ix2, iy2), area.width),
            ))
        # ⚠️ 图片消息是**追加**上去的，得按 y 重排一次 —— 下游
        #    `window.stitch()` 靠顺序缝上下文，顺序错了上下文就错。
        snap.messages.sort(key=lambda m: (m.rect or (0, 0, 0, 0))[1])

        # ---- 群聊的第二条判据：标题带成员数后缀 ----
        # ⚠️ 只**能**把 is_group 置真、不会置假 —— 和"昵称"那条判据取**或**。
        if not snap.is_group and _looks_like_group_title(snap.title):
            snap.is_group = True

        # ⚠️ 群聊里把"认不出昵称的别人"标成 `群友`（不能叫「对方」——
        #    那词暗示只有两个人，群里会把多个人看成同一个）。
        #    和 UIA 那条路**共用同一个函数**，别各写一份。
        label_unknown_senders(snap)

        # ---- 图片本体（可选，见 `_crop_images`）----
        # ⚠️ 只有 `ocr_read.attach_image` 打开时才做 —— 关着时**一行都不走**
        #    （不裁剪、不压缩、不编码，零开销）。
        if self.cfg.get("ocr_read.attach_image"):
            snap.images = self._crop_images(area, snap.messages)
            if snap.images:
                self._log("附带 %d 张图片给模型看" % len(snap.images))

    # ------------------------------------------------------------ 几何

    def _build_messages(self, snap, area, blocks, real_boxes,
                       image_rects, ca=None) -> None:
        """切行 + 逐行定身份 / 昵称 / 过滤，**原地把消息装进 `snap`**。

        ⚠️ 为什么抽出来（2026-10-13 结构改进）：这一段有 220 多行、
           8 个局部状态（`in_box` / `free` / `pending_nick` / 三个 `dropped_*`
           计数器 / `content_bottom` / `_flush_pending` 闭包），但它**只吃**
           `blocks` + 真气泡盒 + 图片区域 —— 与抓图和几何无关。抽出来之后
           `read_chat` 只剩四步：抓图 → 定几何并 OCR → **装消息** → 收尾。

        ⚠️ 计时的 `reads` / `ms` **不在这里**：`t0` 是 `read_chat` 的
           （它还要算抓图的时间），所以由调用方写。
        """

        mid_x = area.width / 2.0
        max_nick = int(self.cfg.get("ocr_read.nickname_max_chars", 12))

        # ---- 切行：**以文字块为准**，气泡盒只用来加分 ----
        #
        # ⚠️⚠️ 2026-10-12 改（浅色读不到正文的修复）：
        #    原来「有盒就按盒切、没盒才退回归并」。而浅色下**对方的气泡根本不成盒**
        #    （色差 36 < 45），于是按盒切的结果是 0 条 —— 文字明明 OCR 认得出，
        #    却全落在所有盒之外。现在反过来：
        #      · 切行永远按**文字块归并**（`rows_from_text`）
        #      · 气泡盒只用来判「这条是谁发的 / 有没有昵称」——那是它的强项
        #    群昵称也顺带吃得下了：`attach_free` 用的 free 块不再被假盒子吸走。
        rows = rows_from_text(blocks, area.width)
        # ⚠️⚠️ 再按**气泡盒**把同一张卡片/长气泡内部的行合并成一条
        #     （一个盒 = 一条消息）。只按文字行距切会把一张卡片切成 4~5 条
        #     （实测 `wx_1323_miniapp` 5 条、`wx_1323_group141` 4 条）——
        #     那是碎片，不是消息。
        rows = merge_rows_in_boxes(rows, real_boxes)
        #: ⚠️ 调试开关（默认关）：`DSH_OCR_TRACE=1` 时每一条行**为什么留下 /
        #: 为什么丢掉**都会打一行日志。排查"某条消息不见了"**先开它，别靠猜** ——
        #: 这一轮就是靠它才看清：输入框的占位文字、时间分隔、顶部图片带三者
        #: 是怎么互相影响的（以及我原来把 `_top_cutoff_image` 的过滤写错了）。
        _trace = os.environ.get("DSH_OCR_TRACE") == "1"
        #: 哪些块**被某个真盒子圈住、而且不是昵称候选**。
        #: ⚠️⚠️ 昵称要**排除掉**：群昵称是画在聊天底色上、紧贴气泡上沿的，
        #:    它常常**正好落在气泡盒的范围内**（实测真机那张：昵称 y 比气泡上沿还高，
        #:    但盒是按"非底色像素"的连通域算的，昵称的笔画和气泡连在一起）。
        #:    如果把它算成"圈住了"，它就进不了 `free`，
        #:    于是 `attach_free()` / `pick_nickname()` 都拿不到它 ——
        #:    症状是**昵称变成一条独立消息**（实测单测里 `小明` 变成了「群友」）。
        in_box = [False] * len(blocks)
        for i, b in enumerate(blocks):
            for (x1, y1, x2, y2) in real_boxes:
                if x1 <= b.cx <= x2 and y1 <= b.cy <= y2:
                    in_box[i] = True
                    break
        # 昵称候选：同一行里**上面还压着别的块**的块（见 `pick_nickname` 的判据①）
        # ⚠️ **不再把它们塞回 `free`**：试过（想让挂在隔壁行的昵称也能被认出来），
        #    结果昵称会**同时**变成一条独立消息又被挂到隔壁 ——
        #    单测里的 `小明` 就这么变成了「群友」发的一句话。
        #    现在的取舍：**昵称如果自己占一行，就可能被当成一条消息**
        #    （已知损失，见 `OCR后端.md`），但绝不会重复出现。
        free = [b for i, b in enumerate(blocks) if not in_box[i]]

        #: ⚠️⚠️ **这一屏能不能判昵称 —— 只有群聊能判**。
        #:     判据是**标题带群成员数后缀 `(N)`**：微信给群标题**恒定**加这个后缀
        #:     （实测 `示例兴趣群(5)` / `示例群甲…(303)` / `示例群乙（141）`），
        #:     **私聊和服务号都没有**。
        #:     ⚠️ 2026-10-13 用户实测报的问题：**私聊里把消息内容当成了发言人** ——
        #:        显示成 `刚躺下: 头好痛`（对方连发「刚躺下」「头好痛」两句，
        #:        前一句只有 3 个字、被 `_looks_like_nickname` 当成昵称挂到了后一句上）。
        #:        更糟的是：**一旦误判出一个昵称就会 `is_group = True`**，
        #:        于是**同一屏其他人全变成「群友」**（用户看到的
        #:        `群友: 【广东职位…】.xlsx 247.0K` 就是这么来的）。
        #:     ⚠️⚠️ 这条判据**打断了那个循环依赖**：原来"先判出昵称 -> 才置 is_group"，
        #:        而"该不该判昵称"又要先知道是不是群。现在由**标题**一锤定音。
        #:     ⚠️ 代价：**不带 `(N)` 的群**（比如某些企业微信群）会退化成「对方」——
        #:        这比"私聊里编出假昵称"好得多（用户原话：「私聊是没有昵称显示的，
        #:        直接对方：或者我：就可以」）。
        _nick_ok = _looks_like_group_title(snap.title)

        #: 图片 / 卡片的区域 —— 里面的文字**不是消息**（见 `_image_regions`）。
        #: ⚠️ 这里**只用这一条**判据。试过另外三种都不成立（别再加回来）：
        #:   · 「行内 ink（和底色比）」—— 浅色下真消息的气泡填充和底色只差 36，
        #:     会把**对方每条真消息**都误杀（实测消息从 4 条掉到 3 条）；
        #:   · 「顶部最大空白 = 被切掉的图片」—— 单测的合成图上**误杀真消息**
        #:     （顶部两行被当成图片带）；
        #:   · 「顶部逐行数颜色」—— 缩略图每行只有 6~27 色，和正文行分不开。
        #: 真正的收口在下面：**行内主色占比**（照片 <92%、纯色/气泡 ≈100%）。
        _cut = [r for r in image_rects if r[0] == 0 and r[1] == 0]
        if _cut and _trace:
            self._log("[trace] 顶部被切掉的图片区域 = %s" % (_cut,))
        if _trace:
            self._log("[trace] image_rects=%s real_boxes=%s rows=%s"
                      % (image_rects, real_boxes,
                         [(r.y1, r.y2, r.x1, r.x2, (r.text() or '')[:8])
                          for r in rows]))
        # 气泡**外**的块：时间分隔走 markers，其余当昵称挂到下面那条消息上
        for b in free:
            t = (b.text or "").strip()
            if not t:
                continue
            if is_time_text(t) and abs(b.cx - mid_x) < area.width * 0.25:
                # ⚠️ 两条都要：只按文本筛，会把「对方刚好发了一句 16:19」
                #    也当成分隔符。
                snap.markers.append(TimeMarker(
                    text=t, rect=(b.x1, b.y1, b.x2, b.y2)))

        #: 已经被认成时间分隔的那些块 —— 下面装消息时要把它们**跳过**。
        #   ⚠️ 不跳的话同一段时间会既进 markers 又变成一条「我」的消息
        #      （2026-10-12 真机实测：17:42 出现两次）。
        #   ⚠️⚠️ **不能用 `id()` 做身份比较**（第一版就是这么错的）：
        #      `{id(b) for b in free}` 是个**临时集合**，表达式结束后那些
        #      `OcrBlock` 全被 GC 回收，`id` 会被后续对象**复用** ——
        #      于是 `id(r.blocks[0]) in marker_ids` 要么恒真要么恒假，**看运气**。
        #      真机表现：时间分隔那条没被丢掉，反而把 `content_bottom` 拉低，
        #      连带让输入框的占位文字活了下来（消息里多出 3 条假的）。
        #      → 改成**值判据**（这一行只有一块、文本是时间、位置在中线附近）。
        def _is_marker_row(row: OcrRow) -> bool:
            if len(row.blocks) != 1:
                return False
            t = (row.blocks[0].text or "").strip()
            return (is_time_text(t)
                    and abs(row.blocks[0].cx - mid_x) < area.width * 0.25)

        dropped_image = 0
        dropped_below = 0
        dropped_overlay = 0
        dropped_ui = 0
        #: "正文的最后一行" —— 消息只会出现在它附近（往上都是消息，往下是
        #: 输入框 / 工具栏）。真机实测：微信输入框的占位文字（`对方无应答`）
        #: 会被 OCR 认出来并当成一条「我」发的消息。
        #: ⚠️ 为什么用"最靠下的文字块"当锚点：`input_top` 那条检测在真机上给过
        #:    816（比输入框还低），配置兜底 708 也还是太低 —— 光靠几何拦不住，
        #:    而"哪些块真的认出来了"是同一批 OCR 结果里现成的信息。
        #: ⚠️ 方向别写反（第一版写反了）：是**丢掉比它低很多的行**，
        #:    不是丢掉比它高的行。
        content_bottom = None
        if blocks:
            content_bottom = max(b.y2 for b in blocks) + 12
        #: 上一行的**待领养昵称**（群里昵称自己占一行，见下面的处理）
        pending_nick = None
        #: 把一个"没被认领的昵称候选"**补吐成一条消息**（一个字都不许丢）。
        #   ⚠️ 这里**不能用 `_row.text()`** —— 它会把 `nick_block` 滤掉，
        #      而这个候选块自己**就是**被当成 `nick_block` 的那一个，
        #      于是 `text()` 返回空、这行文字**凭空消失**（真机实测踩到）。
        def _flush_pending(snap, _row) -> None:
            _t = " ".join(b.text.strip() for b in sorted(
                _row.blocks, key=lambda z: (z.y1, z.x1)) if b.text.strip()).strip()
            if not _t:
                return
            snap.messages.append(MessageItem(
                text=_t,
                cls="mmui::ChatTextItemView",
                rect=(_row.x1, _row.y1, _row.x2, _row.y2),
                sender_name=(_row.nickname or None),
                is_self=_row.is_self,
            ))
        for _idx, r in enumerate(rows):
            #: 下面还有没有"别的文字行" —— 昵称判定要用（昵称的定义是"挂在下面那条上"）
            _has_next = any(x.text().strip() for x in rows[_idx + 1:])
            txt = r.text()
            # ⚠️⚠️ **先看这一行是不是"上一条消息的引用块"** —— 是就并进去，不产新消息。
            #    微信 4.1.13.65 把引用画在气泡**下方**（见 `_is_refer_of` 的说明），
            #    不并的话**每条引用消息都会被拆成两条**（正文一条、引用块一条）。
            #    用户 2026-10-13 实测报的就是这个形状：
            #        `测试` 和 `αβγδεζηθικλμ: 什么时候来` 各占了一条。
            if _is_refer_of(snap.messages[-1] if snap.messages else None,
                            r, real_boxes, mid_x):
                _merge_refer(snap.messages[-1], txt)
                continue
            if not txt:
                continue
            # ⚠️ 只有一块、而且这一块已经被认成时间分隔 -> 这是分隔符，不是消息
            #    （⚠️ 它**不计入丢弃统计** —— 它本来就不是消息，见 `_drop_reason`）。
            if _is_marker_row(r):
                if _trace:
                    self._log("[trace] 丢掉（时间分隔）: %r y=%d"
                              % (txt[:20], r.y1))
                continue
            # ---- 其余三条丢弃判据集中在 `_drop_reason`（顺序即优先级）----
            why = _drop_reason(r, content_bottom, image_rects, blocks, real_boxes,
                               mid_x=mid_x, area_width=area.width)
            if why is not None:
                if why == "below":
                    dropped_below += 1
                elif why == "image":
                    dropped_image += 1
                elif why == "overlay":
                    dropped_overlay += 1
                else:
                    # ⚠️⚠️ **每个判据都要有明确分支，别图省事写 `else`**：
                    #    这里原来是 `else: dropped_overlay += 1`，于是 2026-10-13 新增的
                    #    `"ui"`（界面按钮）被**静默算成了"浮层"** —— 行为是对的，
                    #    但日志报"浮层 1 段"，**把我自己的排查方向带偏了一轮**
                    #    （trace 里明明写着"界面按钮的固定文案"）。
                    #    新增判据时**同时**在这里加分支。
                    dropped_ui += 1
                if _trace:
                    self._log("[trace] 丢掉（%s）: %r y=%d"
                              % (_DROP_LABEL[why], txt[:20], r.y1))
                continue
            if _trace:
                self._log("[trace] 留下: %r y=%d..%d x=%d..%d"
                          % (txt[:20], r.y1, r.y2, r.x1, r.x2))
            txt = r.text()
            if not txt:
                continue
            # 身份：① 有真气泡盒圈住这行的文字 -> 用盒子离哪边近判（最准）
            #       ② 否则用**文字的横向位置**判（`side_of_row`）
            is_self = None
            for (x1, _by1, x2, _by2) in real_boxes:
                if x1 <= r.cx <= x2:
                    is_self = side_of_box((x1, r.y1, x2, r.y2), area.width)
                    break
            if is_self is None:
                is_self = side_of_row(r, area.width)
            if is_self is None:
                # 横跨中线（多行长消息）-> 不瞎猜，按"文字起点在哪边"定
                is_self = r.x1 > mid_x
            r.is_self = is_self
            # ⚠️⚠️ **群里昵称自己占一行**（2026-10-12 真机群聊实测的形状：
            #    `思翀` / `细姨` 单独一行，压在气泡上方、和气泡**不重叠**，
            #    所以 `rows_from_text` 把它们切成了独立的一行）。
            #    规则三条，核心是**绝不吞掉真消息**：
            #      ① 只有"下面还有别的文字行"才可能当昵称（昵称挂在下面那条上）；
            #      ② **认领者必须是"别人的"消息**（`not is_self`）——
            #         如果是我的消息，这个候选就根本不是昵称；
            #      ③ 没被认领的候选**必须补吐出来**成一条消息（见循环之后），
            #         而且认领时是**把它拼进下一行**（`_merge_nick`），
            #         所以内容**一个字都不会丢**。
            if pending_nick is not None:
                if not is_self:
                    r.nickname = r.nickname or pending_nick[0]
                    snap.is_group = True
                    pending_nick = None
                else:
                    _flush_pending(snap, pending_nick[1])
                    pending_nick = None
            if (pending_nick is None and _nick_ok and not is_self and len(r.blocks) == 1
                    and _has_next and not r.nickname
                    and _looks_like_nickname(r.text(), _NICK_MAX_CHARS)):
                pending_nick = (r.text(), r)
                continue
            if not is_self:
                # ⚠️ 昵称只在**群聊**里判（见 `_nick_ok` 的说明）
                if _nick_ok:
                    attach_free(r, free, max_nick)
                    if not r.nickname:
                        # 兜底：只能靠"第一块压在第二块上方"，判据比气泡那条弱，
                        # 见 pick_nickname 的说明。
                        pick_nickname(r, mid_x, max_nick, boxes=real_boxes)
                txt = r.text()
                if not txt:
                    continue
                if r.nickname:
                    # ⚠️ 群聊判定的**唯一**线索在这个后端下就是"别人的消息上面
                    #    挂着昵称"：单聊不会有昵称行。
                    #    UIA 那边靠标题栏类名（`ChatTitleBarChatRoomView`），
                    #    像素这边拿不到。它只影响界面上的群标记，判错代价小，
                    #    所以够用就行、不做更复杂的推断。
                    snap.is_group = True
            snap.messages.append(MessageItem(
                text=txt,
                # 借用微信自己的类名，让 MessageItem.kind 判成 "text"。
                # ⚠️ 纯 OCR 路径**分不出**「气泡里的字」和「图片里的字」，
                #    也拿不到「[图片]」这种占位符 —— 这是一条已知的损失。
                cls="mmui::ChatTextItemView",
                rect=(r.x1, r.y1, r.x2, r.y2),
                sender_name=(r.nickname or None),
                is_self=is_self,
            ))
            # ⚠️⚠️ **分享卡片 / 文件分类**（2026-10-13）—— 判据是**结构**，不是字符串：
            #     盒里的**颜色种数**高（有缩略图）+ 装了 **>= 3 块**文字
            #     （标题 / 摘要 / 来源）。实测对照见 `_CARD_MIN_COLORS` 的注释。
            #     ⚠️ 用户 2026-10-13 纠正过我一次：**分享卡片通常不带 URL**
            #        （小红书 / bilibili 显示的是「标题 + 摘要 + 来源」），
            #        而"来源"是**开放集合**（任何 App 都可能）—— 两个都不能当判据。
            if ca is not None and len(r.blocks) >= _CARD_MIN_BLOCKS:
                _b = _find_box_of(r, real_boxes)
                if _b is not None and _color_count(ca, _b) >= _CARD_MIN_COLORS:
                    _m = snap.messages[-1]
                    _f = _card_kind_of(_m.text) == "file"
                    _m.text = "[%s] %s" % ("文件" if _f else "链接", _m.text)
                    # 借用微信自己的类名，让 MessageItem.kind 判成 file / link
                    _m.cls = ("mmui::ChatFileItemView" if _f
                              else "mmui::ChatLinkItemView")

        # ⚠️ **循环结束后要把"待领养昵称"补吐出来** —— 不能因为"它可能是昵称"
        #    就把一行文字丢掉。少了这一步，**末尾那条真消息会凭空消失**
        #    （真机实测：对方三句短话，最后一条被吞掉，消息 3 条 -> 2 条）。
        if pending_nick is not None and pending_nick[1] is not None:
            _flush_pending(snap, pending_nick[1])
        self.stats["image_rows"] = dropped_image
        # ⚠️ 丢过东西要说出来（这也是"静默损失"，别让它无声无息）。
        #    每拍都会走到这里 —— 所以只在**数值变了**的时候报（项目通用规则①）。
        dropped = dropped_image + dropped_below + dropped_overlay + dropped_ui
        if dropped and self._image_rows_logged != dropped:
            self._image_rows_logged = dropped
            self._log("本屏丢掉了 %d 段【文字】（图片/卡片上 %d 段、输入框那一带 "
                      "%d 段、浮层 %d 段、界面按钮 %d 段）；纯 OCR 后端分不出图片消息"
                      "和文本消息，这是已知损失"
                      % (dropped, dropped_image, dropped_below, dropped_overlay,
                         dropped_ui))

    def _locate_and_recognize(self, img, a, bg, x1: int, y1: int, x2: int,
                              input_top: int, scale: float):
        """在几档候选左边界上试，**返回第一档真能读出东西的**；全读不出返回 None。

        ⚠️ 为什么要有这个阶段（2026-10-13 从 `read_chat` 里抽出来）：
            「定几何 + 认一屏字」是一个**独立阶段** —— 输入是像素，输出是
            `(裁剪框, 裁剪图, OCR 块, 真气泡盒, 分析数组, 图片区域)`，
            与后面的"切行 / 定身份 / 装快照"没有耦合。抽出来之后
            `read_chat` 只剩四步：抓图 -> 定几何并 OCR -> 装消息 -> 收尾。

        返回 `(cl, bx1, crop_box, area, blocks, cand_boxes, ca, cand_img)`：
            cl / bx1    实际裁剪用的左边界 / 被采纳的候选值（日志要对比这两个）
            crop_box    裁剪框（探针按它裁图）
            area        裁剪后的 PIL 图
            blocks      OCR 出来的文字块
            cand_boxes  剔掉假盒之后的**真气泡盒**
            ca          与 `area` **同一块**的 numpy 数组（坐标系必须一致，见下）
            cand_img    图片 / 卡片区域（后面的过滤要用）

        ⚠️⚠️ **`ca` 必须和 `area` 同原点**（2026-10-12 血案）：`_recognize(area)`
            给的是**裁剪图坐标**，所以气泡分析也必须喂**同一块数组**
            （`a[y1:input_top, cl:x2]`）。原来喂的是 `a[y1:input_top, 0:x2]`，
            两者差了整片会话列表的宽度 —— 盒坐标和块坐标再也对不上，
            身份判定和图片区域判定**全都在看错地方**（当时只出 1 条消息）。
            ⚠️ 别想用"平移盒坐标"绕过去（试过：按 `y1` 平移会把盒推成负坐标）。

        副作用都是"每屏一次"的状态：`_left_accepted` / `_left_logged` /
        `_geom_bad` / `last_crop` / `_probe_box` / `stats["blocks"]`。
        """
        from .sender import _cv2

        has_cv2 = _cv2() is not None
        got = None
        tried = []
        for bx1 in self._geometry_candidates(a, bg, x1):
            tried.append(bx1)
            # ⚠️ 裁剪和气泡分析要用**同一个**左边界，而且都要**再往左让一点**
            #    （`_crop_left`）。
            cl = self._crop_left(bx1)
            crop_box = (cl, y1, x2, input_top)
            area = img.crop(crop_box)
            blocks = self._recognize(area)
            ca = a[y1:input_top, cl:x2]
            # ⚠️ tol 按**底色亮度**选：浅色主题下气泡/卡片的色差只有 34，
            #    用深色那档 45 会把它们整个判成背景（见 `bubble_tol`）。
            raw_boxes = (bubble_boxes(ca, bg, tol=bubble_tol(bg))
                         if has_cv2 else [])
            boxes = list(raw_boxes)
            # ⚠️ 整屏 OCR 会因为 `limit_type=max` **缩小**图像而漏掉小字
            #    （实测「嗯嗯」整屏认不出、把那个盒单独裁出来却是 0.99），
            #    所以对「装了 0 块、颜色数低」的**纯色气泡盒单独补一次**。
            blocks = self._retry_blank_boxes(area, ca, blocks, boxes)
            # ⚠️ 这一档到底"读出了东西没有"要**按产品后面要用的那套判据**判：
            #    先剔掉假盒子、算出图片区域，再看**有没有哪一块文字是能变成消息的**。
            #    为什么不能只看"boxes 或 blocks 非空"：
            #      · 只看 boxes —— 浅色下必定为空（气泡不成盒），整条路失效；
            #      · 只看 blocks —— **几何完全错**时 OCR 照样会报块（真机实测：
            #        一屏错几何上 OCR "编"出了块，于是"几何不对"这条静默消失，
            #        用户看到的是"没有消息"而不是"几何要重新标定"，
            #        而这两件事该做的事完全不同 —— 见 `_geom_bad`）。
            cand_boxes = [b for b in boxes if not _box_is_phantom(b, blocks, ca)]
            mid = area.width / 2.0
            cand_img = _image_regions(cand_boxes, ca, mid, blocks=blocks)
            cand_img += _top_cutoff_image(ca, mid, rows=rows_from_text(
                blocks, area.width), bg=bg, boxes=cand_boxes, blocks=blocks)
            usable = [b for b in blocks
                      if not any(r[0] <= b.cx <= r[2] and r[1] <= b.cy <= r[3]
                                 for r in cand_img)]
            # ⚠️⚠️ **还要有"这一屏真画了东西"的证据**，否则几何错的时候会静默通过：
            #    `bg` 就是这一块的背景色，所以"和 `bg` 差得明显的像素占比"天然
            #    回答"这儿有没有内容" —— 纯色图恒为 0，而**真屏**上气泡填充、
            #    文字、图片随便哪一样都远超 0.2%（实测真机浅色那屏约 17%、
            #    测试的深色合成图约 5%）。
            #    没有这一条会怎样（真机 + 单测都验过）：OCR 在一块**纯色**区域上
            #    报出一个块（它确实会），于是"能变成消息的文字"非空 ->
            #    这一档被采纳 -> **"几何不对"这条静默消失**，用户看到的是
            #    "没有消息"而不是"去重新标定几何"，而这两件事该做的事完全不同。
            ink = _ink_fraction(ca, bg)
            if usable and ink >= 0.002:
                got = (cl, bx1, crop_box, area, blocks, cand_boxes, ca, cand_img)
                break
        if got is None:
            self._geom_bad = True
            self._log("⚠ 截屏+OCR 后端这一屏既没认出文字、也没认出气泡（试过 "
                      "chat_left=%s）。多半是几何不对：跑 "
                      "`python probes/probe_ocr_read.py --calibrate` 重新标定，"
                      "或者把设置里的「读取方式」改回 UIA。" % tried)
            return None
        # 用的是检测值还是配置值？**记一行** —— 想把检测到的值固定下来就靠这个数。
        # ⚠️ 比的是**候选值**（`bx1`），不是实际裁剪用的左边界（`cl`）——
        #    后者已经减过 `left_margin`，永远不会等于配置值，
        #    拿它来比的话这行日志会**永远说"用了自动检测"**。
        bx1 = got[1]
        # ⚠️⚠️ **记下这个被采纳的左边界** —— 标题栏要跟它走。
        #    不记的话标题会退回 `chat_rect()` 的配置值，而那个值在窄窗口上
        #    偏右 35 物理像素，**会把标题第一个字切掉**（实测，见 `_title_left`）。
        self._left_accepted = bx1
        # ⚠️ **这行只报一次**（值变了才再报）—— 2026-10-11 用户实测它每秒刷一次，
        #    看起来像死循环（那一屏有个 GIF 在刷新，探针于是每拍都判"变了"）。
        #    ⚠️ 每拍都会发生的事，日志必须节流 —— 这个项目在这条上栽过好几次
        #       （跟随器 `_verify` 的"只报一次"也是同一个教训）。
        if bx1 != x1 and self._left_logged != bx1:
            self._left_logged = bx1
            self._log("聊天区左边界：自动检测 = %d 物理（约 %.0f 逻辑像素），"
                      "配置里的 chat_left 是 %d（约 %.0f 逻辑）—— "
                      "想固定的话把 ocr_read.chat_left 改成 %.0f"
                      "（这行以后不再重复）"
                      % (bx1, bx1 / scale, x1, x1 / scale, bx1 / scale))
        elif bx1 == x1 and len(tried) > 1 and self._left_logged != x1:
            self._left_logged = x1
            self._log("⚠ 检测到的 chat_left=%d 认不出气泡，退回配置值 %d"
                      "（这行以后不再重复）" % (tried[0], x1))
        if not has_cv2:
            self._log("⚠ 没有 cv2 —— 气泡盒这条路不可用（本来就只当加分项），"
                      "切行和身份判定都走文字块归并")
        self._geom_bad = False
        _cl, _bx1, crop_box, area, blocks, real_boxes, ca, image_rects = got
        #: 这次实际用的裁剪框（诊断用：探针要按它裁图看效果）
        self.last_crop = crop_box
        # ⚠️ **把几何喂给探针**（`_probe_geo` 会用它）—— 这样探针就不必
        #    每拍重算 `_bg` + `find_input_top`（那会让探针从 35ms 涨到 132ms，
        #    实测过）。探针只关心「裁哪一块」，不关心裁得对不对。
        self._probe_box = (crop_box[0], crop_box[3])
        self.stats["blocks"] = len(blocks)
        return got

    def _geometry_candidates(self, a, bg, configured_x1: int):
        """该试哪些 `chat_left`。**检测优先，配置兜底。**

        ⚠️ 顺序在 2026-09-27 第四十六轮**反过来了**，因为有实测证据：

            · 会话列表的宽度**不是常数**。同一个微信版本，窗口 1542 物理像素宽时
              分界在 465（310 逻辑），窗口 1206 宽时在 **430（287 逻辑）** ——
              差了 23 逻辑像素。逐列底色占比在 x=430 处正好从 0.00 跳到 0.91，
              所以 430 是真的边界，配置里那个 310 在窄窗口上偏右 23 逻辑。
            · 而 `detect_chat_left` 在三张真实截图上给出 467 / 467 / 467，
              真值 465 —— **它比那个常数准**。

        所以才改成"先信检测"。安全性不靠顺序，靠这两条：
            ① 检测**认不出来就返回 None**（两边同色 / 整幅同色 / 观察不到跳变）
            ② 认出来的值**还得真能认出气泡**才算数（见 `read_chat` 里的循环）

        顺序反过来还顺带省一次 OCR：正常情况一次就够了，不用先拿错的值白跑一遍。

        ⚠️ `ocr_read.auto_geometry: false` 时**只试配置值** —— 给"我就要它按我
            写的来"留一条路（排查几何问题时，自动纠偏反而会掩盖问题）。
        """
        if bool(self.cfg.get("ocr_read.auto_geometry", True)) and bg is not None:
            det = detect_chat_left(a, bg)
            # 和配置值差不多的就不必占一个候选（省一次 OCR）
            if det is not None and abs(det - configured_x1) > 20:
                yield det
        yield configured_x1

    # ------------------------------------------------------------ 内部

    def _bg(self, a, x1: int, x2: int, y1: int):
        """聊天区底色 = 那一块里出现最多的颜色。

        ⚠️⚠️ **算之前必须先转 int32**（2026-10-11 修，差点又是一个静默失效）：

            原来是 `q = (sub // 8)` 然后 `q[:, :, 0] * 1024`。`sub` 是 uint8 时
            `sub // 8` **还是 uint8**，而 `uint8 * 1024` 在 **NumPy 2.x（NEP 50）
            下直接抛 `OverflowError`**（旧版会静默回绕）。异常被这里的
            `except Exception: return None` 吞掉 —— 于是这个函数**恒返回 None**，
            而调用方全把它当"这次算不出来"，**不报错、不崩、只是静默降级**。

            实测（numpy 2.4.6）：
                `read_chat` 里 `a` 先 `.astype(np.int16)` 了 —— 所以**那条路径侥幸没事**；
                而任何**新**调用点只要直接把 PIL 图转成数组（uint8）就会踩到。
                我写 `_title_left` 时就是这么踩的：检测恒返回 None，
                于是又退回那个偏右 35 像素的配置值。

            → 所以修在**函数内部**：不管调用方给什么 dtype，进来先转 int32。
              别依赖"调用方记得转 int16"——那种约束没人会记得。
        """
        try:
            sub = a[y1:, x1:x2]
            if sub.size == 0:
                return None
            # ⚠️ 见上面：这一步是**必须**的，不是优化。
            q = (sub // 8).astype(np.int32)
            key = q[:, :, 0] * 1024 + q[:, :, 1] * 32 + q[:, :, 2]
            vals, counts = np.unique(key.ravel(), return_counts=True)
            k = int(vals[int(np.argmax(counts))])
            return np.array([(k // 1024) % 32 * 8 + 4,
                             (k // 32) % 32 * 8 + 4,
                             k % 32 * 8 + 4], dtype=np.float64)
        except Exception as e:
            # ⚠️ 别只 `return None` —— 这一类"静默降级"已经藏过一次 bug 了。
            self._log("算聊天区底色失败（这次当算不出来）: %s: %s"
                      % (type(e).__name__, e))
            return None

    def _recognize(self, img) -> List[OcrBlock]:
        """OCR 一张图 -> OcrBlock 列表。"""
        eng = self._engine()
        eng.load()
        raw = eng.recognize(img, min_score=float(
            self.cfg.get("ocr_read.min_score", 0.5)))
        out = []
        for r in raw:
            t = (r.get("text") or "").strip()
            if not t:
                continue
            out.append(OcrBlock(t, int(r["x1"]), int(r["y1"]),
                                int(r["x2"]), int(r["y2"]),
                                float(r.get("score") or 1.0)))
        limit = int(self.cfg.get("ocr_read.max_blocks", 400))
        return out[:limit]

    # ------------------------------------------------------------ 做不到的事

    # ------------------------------------------------------------ 便宜探针

    #: 「漏认的纯色文本气泡」最多补试几个 —— 一屏头像很多，不设上限会拖慢一拍。
    _RETRY_MAX = 3

    def _retry_blank_boxes(self, area, ca, blocks, boxes) -> List[OcrBlock]:
        """对「装了 0 块、颜色数又低」的气泡盒**单独放大重试 OCR**，把漏认的文本捞回来。

        ⚠️ 为什么需要（2026-10-13 实测，`wx_1567_single`）：那条「嗯嗯」的绿气泡
           **盒是存在的**（86x54、颜色数 21），但整屏 OCR **一块都没认出来** ——
           因为 `limit_type=max` 把 1146x661 的整屏**缩小**了（字从 14px 变 9px）。
           而把那个盒单独裁出来 OCR 是 **0.99** 的置信度。
           → 于是这条**普通文本消息静默消失**（不是切行丢的，是**根本没认出来**）。

        ⚠️ 判据两条**同时**满足才重试：
             · 盒里**一块文字都没有**；
             · 盒的**颜色数低**（< 24）。
           第二条把**头像**（实测 97 / 256）和**图片**排除掉了 —— 这正是它比
           "把整屏 OCR 的上限调大"高明的地方：那样会**把图片里的小字也翻出来**，
           而那种图**不成盒**、`_image_regions` 剔不掉（实测多出 4 条假消息）。
        ⚠️ **只放大、绝不缩小**（`图片消息OCR调研.md` 的结论：缩小不可逆且静默归零）。
        ⚠️ 补出来的块**用盒的中心**当坐标：身份判定走 `side_of_box`（不依赖块坐标），
           这样它既不会跑出这个盒，也不会把 `_box_is_phantom` 的"横向分散"判据带偏。
        """
        if not blocks or not boxes:
            return blocks
        out = list(blocks)
        tried = 0
        for (x1, y1, x2, y2) in boxes:
            if tried >= self._RETRY_MAX:
                break
            if any(x1 <= b.cx <= x2 and y1 <= b.cy <= y2 for b in out):
                continue                       # 盒里已经有字 -> 不用补
            if _color_count(ca, (x1, y1, x2, y2)) >= 24:
                continue                       # 头像 / 图片，不是"漏认的文本气泡"
            tried += 1
            sub = area.crop((x1, y1, x2, y2))
            if sub.width < 4 or sub.height < 4:
                continue
            sub = sub.resize((sub.width * 3, sub.height * 3), Image.LANCZOS)
            got = self._recognize(sub)
            txt = " ".join(b.text for b in got if b.text).strip()
            if not txt:
                continue
            # ⚠️ rect **用盒的范围**（不是 1 像素）：下游 `window.stitch()` 会看 rect，
            #    而"补出来的块"本来就代表这个盒里的文字（cx/cy 自然就是盒中心）。
            out.append(OcrBlock(txt, x1, y1, x2, y2))
        return out

    def _probe_geo(self, W: int, H: int):
        """探针要裁哪一块 -> `(x1, y2)`。

        ⚠️⚠️ **这里绝对不能做任何像素分析。** 第一版探针为了"和 read_chat
            用同一套规则"，在这里调了 `_bg()`（众数）和 `find_input_top()`
            （逐列扫描），还 `np.asarray(...).astype(np.int16)` 了一整张
            1402x1011 的图 —— **实测探针变成 132ms**（日志里那行
            「约 132 ms，空闲时只占 13.2% 一个核」），**比 UIA 一整拍还贵**，
            整个探针方案的经济性直接不成立。

            现在改成：几何**缓存上一次 read_chat 真实用的裁剪框**，
            没有缓存时**只用配置算术**（不碰像素）。于是探针里只剩
            `grab` + `crop` + `resize` + `tobytes`，没有 numpy。

        缓存的几何不会过时：它只在完整读屏之后更新，而窗口大小/微信改版
        这类变化会先在完整读屏里体现出来。⚠️ 万一切偏了也不会漏 ——
        探针只需要"**能发现变化**"，发现之后真读那次会用完整规则重新算。
        """
        if self._probe_box is not None:
            x1, y2 = self._probe_box
            if 0 <= x1 < W and 0 < y2 <= H:
                return x1, y2
        scale = self._scale()
        # ⚠️ 和 `read_chat` 一样要**往左让一点**（`_crop_left`），
        #    否则探针看的范围和真读看的范围差几个像素。
        #    差几个像素其实不影响"变了没"（两边都是同一个偏移），
        #    但保持一致能让排查时"探针看到的就是真读看到的"。
        x1 = self._crop_left(chat_rect((0, 0, W, H), scale, self.cfg, (W, H))[0])
        fb = H - int(round(float(self.cfg.get("ocr_read.input_fallback", 150))
                           * scale))
        return x1, max(1, min(fb, H))

    def probe(self, hwnd: int):
        """**便宜探针**：只抓图 + 缩略图哈希，不做 OCR、不做像素分析。

        为什么要它（2026-10-10，用户要求「OCR 频率能不能高点」）：

            一次完整读屏约 250ms，**绝大部分是 OCR 识别文字**；
            而"这一屏变了没"根本不需要认字。实测
            （`probes/bench_capture_hash.py`）：

                探针（抓图 ~32ms + 缩略图哈希 ~3ms）≈ 35 ms
                真读一屏（抓图 + OCR + 切行）        ≈ 250 ms
                => 便宜约 8 倍

            于是频率可以提上去而**空闲时几乎不花钱**：
                每 1 秒只探一次（~3.5% 一个核），**变了才付那 250ms**。
            对照：UIA 那条路一整拍就要 100ms（1 秒一拍 = 10%）。

        ⚠️ 探针范围是「**标题栏 + 聊天区**」，**不含会话列表和输入框**：
            · 含标题栏 -> 切换会话一定会让哈希变（标题变了），不会漏
            · 不含会话列表 -> 别的会话来新消息不会误触发
              （那是 Watcher 的事，而 Watcher 本来就没接线）
            · 不含输入框 -> **你自己打字不会触发 OCR**
              （否则每敲一个字都是"变了"）
        """
        img = self.grab(hwnd)
        if img is None:
            return None
        x1, y2 = self._probe_geo(img.width, img.height)
        area = img.crop((x1, 0, img.width, max(y2, 1)))
        # 缩到很小的灰度图：只用来判"变没变"，不需要任何精度。
        # ⚠️ 全程 PIL，**不转 numpy** —— 转一次 1400x1000 的 int16 就要几十毫秒。
        n = int(self.cfg.get("ocr_read.probe_thumb", 48))
        return area.convert("L").resize((n, n)).tobytes()

    def _input_top(self, a, bg, H: int, x1: int, x2: int, y1: int) -> int:
        """输入区上边 —— **探针和 `read_chat` 共用这一份实现**。

        ⚠️ 别在别处再写一遍：这是"取自下而上第一条横贯整宽的分隔线，
        再和兜底值取更靠上的那个"的规则，两份实现早晚会漂。
        """
        top = find_input_top(a, bg, x1, x2,
                             y1 + int(0.35 * H)) if bg is not None else None
        fb = H - int(round(float(self.cfg.get("ocr_read.input_fallback", 150))
                           * self._scale()))
        # 取更靠上的那个（y 更小），理由见 read_chat 里那段长注释
        top = fb if top is None else min(top, fb)
        return max(y1 + 1, min(top, H))

    # ------------------------------------------------------------ 做不到的事

    def _session_list_rect(self, img, a, bg) -> Optional[tuple]:
        """左侧**会话列表**在位图里的矩形（导航栏右沿 -> 聊天区左边）。

        ⚠️ 右边界用 `detect_chat_left()` —— 它找的**就是**"会话列表与聊天区的
           分界"，所以这里不依赖配置里那个 `chat_left`（那个值在窄窗口上偏右，
           实测 465 vs 430）。
        ⚠️ 左边界用**比例**（`_SESSION_LEFT_FRAC`）：导航栏宽度不必做成配置项，
           而它里面只有图标、OCR 认不出有效文字，从它右边开始最省事。
        ⚠️ 竖向从 `_SESSION_TOP_FRAC` 开始，避开搜索框。
        """
        W, H = img.width, img.height
        right = detect_chat_left(a, bg)
        if right is None:
            right = int(round(float(self.cfg.get("ocr_read.chat_left", 310))
                              * self._scale()))
        left = int(W * _SESSION_LEFT_FRAC)
        top = int(H * _SESSION_TOP_FRAC)
        if right <= left + 20 or top >= H:
            return None
        return (left, top, min(right, W), H)

    def read_sessions(self, hwnd: int) -> List[SessionItem]:
        """读左侧**会话列表**（纯像素 + OCR）。读不到返回 `[]`。

        ⚠️ 这条路以前是**明确不提供**的（`return []`），理由是"要还原它得再切
           一次行再 OCR 一次，成本翻倍，而它只用于「要不要提醒」（`Watcher`
           本来就没启用）"。但目标里"会话识别"是明确要求的一项，而实测成本
           可控：**左侧列表一次 OCR** 就能拿到会话名 / 预览 / 时间
           （实测 1323x939 上认出 26~27 个文本块，分成 8~9 个会话项）。

        ⚠️ 它**和 `read_chat` 读的是屏幕的不同区域**（左边 vs 右边），
           所以两次调用会各抓一次图 —— 调用方只在需要时才调它。
        """
        img = self.grab(hwnd)
        if img is None:
            return []
        a = np.asarray(img.convert("RGB")).astype(np.int16)
        W, H = img.width, img.height
        x1, y1, x2, _ = chat_rect((0, 0, W, H), self._scale(), self.cfg, (W, H))
        bg = self._bg(a, x1, x2, y1)
        if bg is None:
            return []
        rect = self._session_list_rect(img, a, bg)
        if rect is None:
            return []
        blocks = self._recognize(img.crop(rect))
        out: List[SessionItem] = []
        for col in group_session_blocks(blocks):
            # ⚠️⚠️ `blocks` 是**裁剪图坐标**（`img.crop(rect)` 之后才 OCR 的），
            #    所以右边界也必须换算成裁剪图坐标 —— 直接传原图的 `rect[2]`
            #    会让"贴右"判据**永远不成立**，于是所有时间都被当成预览
            #    （实测踩到过：time 列全空、时间挤进 preview）。
            item = session_item_from(col, 0, rect[2] - rect[0])
            if item is not None:
                out.append(item)
        return out

    def read_input_text(self, hwnd: int) -> str:
        """⚠️ 本后端**不提供**输入框内容（用户在打什么字）。

        要用就得再 OCR 一次输入区。返回 ""，调用方会当作"没在打字"。
        """
        return ""


def make_reader(cfg, logger=None, worker=None, ocr=None):
    """按配置挑一个后端，返回**同一个接口**的读取器。

    这是 `run_watch` 唯一该用的入口 —— 别在别处直接 `WeChatReader(...)`，
    否则降级逻辑会被绕过去。

    选法（`reader.backend`）：
        "uia"  —— 只用 UIA
        "ocr"  —— 只用像素 + OCR（读不到也不回落，用于排查）
        "auto" —— **默认**。先用 UIA；连续 `reader.fail_switches` 次
                  「窗口在、但一条消息都读不出来」就永久切到 OCR。

    ⚠️ 判据是「**窗口在、却读不出消息**」，不是「UIA 报错」——
        实测读不到的环境**不抛异常**，它只是返回一个空快照
        （2 个后代控件）。按异常判会永远不切换。
    """
    mode = str(cfg.get("reader.backend", "auto") or "auto").strip().lower()
    if mode == "ocr":
        return OcrReader(cfg, logger=logger, ocr=ocr)
    from .reader import WeChatReader
    if mode == "uia" or worker is None:
        return WeChatReader(cfg, worker, logger=logger)
    return AutoReader(cfg, worker, logger=logger, ocr=ocr)


class AutoReader:
    """先 UIA，读不到就自动降级到 OCR。**接口与两个后端完全一致。**

    ⚠️ 切换是**单向且永久**的。不做"过一会儿再试试 UIA"是因为：
        UIA 读不到是「机器 × 账号」的属性，不是偶发故障；
        来回试只会让一个 tick 里多花一次抓图 + OCR。
        想重新试就重启程序（或者在设置里把 backend 固定成 uia）。
    """

    def __init__(self, cfg, worker, logger=None, ocr=None):
        from .reader import WeChatReader

        self.cfg = cfg
        self._log = logger or (lambda msg: None)
        self.uia = WeChatReader(cfg, worker, logger=logger)
        self._ocr_cfg = cfg
        self._ocr_arg = ocr
        self.ocr = None                      # 懒建：没降级就永远不加载 OCR 引擎
        self.need = max(1, int(cfg.get("reader.fail_switches", 3)))
        #: 后代控件少于这个数就认为「UIA 树是空的」。实测正常 160、坏掉 2，
        #: 取 20 是为了留足余量（比如微信改版后控件变少也不至于误判）。
        self.min_controls = max(1, int(cfg.get("reader.min_controls", 20)))
        self._misses = 0
        self.switched = False
        self.last_miss = ""

    # ------------------------------------------------------------ 内部

    def _backend(self):
        if self.switched:
            if self.ocr is None:
                self.ocr = OcrReader(self._ocr_cfg, logger=self._log,
                                     ocr=self._ocr_arg)
            return self.ocr
        return self.uia

    def _note_failure(self, why: str) -> None:
        """记一次「UIA 树几乎是空的」。够多次就切。

        ⚠️ 只在**同一个原因连续出现**时累计；原因一变就清零 ——
            否则「用户切到一个没打开聊天的界面」攒的几次，
            会和后来真正的 UIA 故障连在一起，提前触发切换。
        ⚠️ 而「没打开聊天」根本不会走到这里：那种情况下控件树是满的，
            判据是**控件数量**，不是"标题为空"（见 `reader.uia_health`）。
        """
        if why != self.last_miss:
            self.last_miss = why
            self._misses = 0
        self._misses += 1
        if self._misses >= self.need and not self.switched:
            self.switched = True
            self._log("⚠ %s（连续 %d 次）—— **永久切换到「截屏 + OCR」后端**。"
                      % (why, self._misses))
            self._log("   这条路上：上下文只有当前一屏；不提供会话列表和输入框；"
                      "分不出「图片消息」和「文本消息」。")
            self._log("   嫌不对就在设置里把 reader.backend 固定成 uia（只用 UIA）"
                      "或 ocr（只用 OCR）。")

    # ------------------------------------------------------------ 对外

    @property
    def background_read_ok(self) -> bool:
        """跟着**当前后端**走 —— 切换之后立刻变成 False（除认 `ocr_read.record`）。"""
        return bool(self._backend().background_read_ok)

    @property
    def geom_bad(self) -> bool:
        """最后一次读屏是不是**因为几何不对**而彻底失败。

        `run_watch` 用它把「当前没有打开的会话」换成一条能指路的提示 ——
        这两种情况在界面上长得一模一样，但该做的事完全不同。
        """
        return bool(getattr(self._backend(), "_geom_bad", False))

    def find_window(self):
        return self._backend().find_window()

    def read_sessions(self, hwnd: int):
        return self._backend().read_sessions(hwnd)

    def read_input_text(self, hwnd: int) -> str:
        return self._backend().read_input_text(hwnd)

    def read_chat_title(self, hwnd: int) -> str:
        """读会话标题 —— **这里也是降级判定的入口**。

        为什么把判定放在这里而不是 `read_chat`：
            `Recorder.tick()` 的顺序是「先读标题，标题为空就整拍跳过」，
            所以 UIA 坏掉的环境里**根本走不到 `read_chat`**。
            判定挂在 read_chat 上的话，这个降级**永远不会触发**
            （我第一版就是那么写的，等于没做）。

        判据用 `uia_health()`（后代控件数量），**不是**"标题为空"：
            停在设置页 / 朋友圈时标题同样是空的，但控件树是满的 ——
            那种情况**不该**降级。
        """
        if self.switched:
            return self.ocr.read_chat_title(hwnd)

        title = self.uia.read_chat_title(hwnd)
        if title:
            self._misses = 0
            self.last_miss = ""
            return title

        n = self.uia.uia_health(hwnd)
        if 0 <= n < self.min_controls:
            self._note_failure("UIA 树几乎是空的（只有 %d 个后代控件）" % n)
        else:
            self._misses = 0
            self.last_miss = ""
        return title

    def read_chat(self, hwnd: int):
        """读一屏，交给当前后端。"""
        return self._backend().read_chat(hwnd)

    def probe(self, hwnd: int):
        """便宜探针（只抓图 + 哈希，不 OCR）。见 `OcrReader.probe`。"""
        return self._backend().probe(hwnd)
