# -*- coding: utf-8 -*-
"""微信窗口定位与内容读取（只读）。

两条铁律（来自实测，见 README.md）：
  1. 定位窗口用 win32gui.EnumWindows 拿 HWND，**不要**用
     GetRootControl().GetChildren()（实测会挂死 60s+）。
  2. 取元素必须用 FindAll(TreeScope_Descendants, cond)，
     **不要**用 GetChildren() 递归 —— Qt 的 UIA provider 没实现 TreeWalker，
     逐级遍历会在自绘列表处断掉，看起来就像「微信不暴露消息」。

读取策略分两档：
  - 轻量 read_sessions(): 只按类名查会话列表项并读 Name，约 14.5ms，用于轮询检测新消息
  - 全量 read_chat():     扫全部后代，约 50ms，用于取当前聊天的详细内容
"""

import os
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import win32api
import win32con
import win32gui
import win32process

from .prompts import GROUP_UNKNOWN

# UIA PropertyId / TreeScope 常量
PROP_AUTOMATION_ID = 30011
PROP_CLASS_NAME = 30012
SCOPE_DESCENDANTS = 4

# 「列表是否滚到底部」的容差（像素）。
#
# ⚠️ 必须很小，别随手调大。列表是虚拟化的：最后一条滚出渲染区后会从 UIA 树里
# 消失，「最后一条」于是换成上一条 —— 设往上滚了 S、那一条高 h，
# 滚出前 gap = -S，滚出后 gap = h - S，**在 S = h 处归零**。
# 单聊消息高度都是 84，所以 S 落在 [84, 84+tolerance] 会被误判成「在底部」。
# 20 的容差意味着每 84px 有 20px 是误判窗口；改成 4 后只剩 4px。
# 实测在底部时 gap 正好是 0（不是「大约 0」），所以 4 足够。
BOTTOM_TOLERANCE = 4

#: 「UIA 树正常」的门槛（后代控件数）。正常环境 160 个，拿不到的环境 2 个。
#: ⚠️ 用在两处：后端选路（`ocr_reader.make_reader`）和**多候选时挑主窗口**
#:    （`WeChatReader._pick_by_health`）—— 后者是 2026-10-17 加的：
#:    「图片和视频」预览可能也叫「微信」，纯 Win32 分不开时用它兜底。
UIA_HEALTH_MIN = 20

# 会话名里可能出现的尾部标记
SESSION_FLAGS = ("消息免打扰", "草稿", "未读")

# ---------------------------------------------------------------- 时间分隔符
#
# 微信在消息之间画时间分隔（`mmui::ChatItemView`，Name 形如 `16:19` /
# `昨天 23:28` / `9月20日`）。它是聊天里**天然的分段线**。
#
# ⚠️ 两个坑（都实测过，2026-09-25）：
#   ① 它**没有 automationId** —— 现有的消息项条件是
#      `aid.startswith("chat_message_list")`，所以它一直被筛掉、从没被读过。
#   ② 它和「拍一拍」这类系统提示**共用同一个类名**
#      （实测 `我拍了拍 "张三" 变成了鲫鱼饼饼` 也是 `mmui::ChatItemView`），
#      所以**不能只看类名**，必须再用「Name 长得像时间」筛一道。
TIME_RE = re.compile(
    r"^(\d{1,2}:\d{2}"
    # ⚠️ `昨天` 后面的空格**必须是可选的**：微信自己画的是 `昨天 23:46`，
    #    而 OCR 常常把那个空格吃掉（真机实测读出 `昨天23:46`）——
    #    少了 `\s*` 这一条，整段时间分隔就会被当成一条「我」的消息。
    r"|昨天(\s*\d{1,2}:\d{2})?"
    r"|前天(\s*\d{1,2}:\d{2})?"
    r"|星期[一二三四五六日天]"
    r"|\d{1,2}月\d{1,2}日"
    r"|\d{4}年\d{1,2}月\d{1,2}日"
    r"|(上午|下午|晚上|凌晨)\s*\d{1,2}:\d{2})$")

# 「日期级」的分隔 = 跨天了。纯时间（16:19）只是同一天内的锚点，不算断层。
DATE_LEVEL_RE = re.compile(r"(昨天|前天|星期|\d+月|\d+年)")


def is_time_text(s: str) -> bool:
    """这段文本是不是微信的时间分隔（而不是正巧同类的系统提示）。"""
    return bool(TIME_RE.match((s or "").strip()))


# ------------------------------------------------------------------ 数据结构

@dataclass
class SessionItem:
    """会话列表里的一项。"""
    raw_title: str            # 原始会话名（Name 的第一行）
    preview: str = ""         # 最后一条消息预览
    time_text: str = ""       # 时间显示（如 00:38 / 昨天 23:28）
    flags: List[str] = field(default_factory=list)  # 消息免打扰 等

    @property
    def fingerprint(self) -> str:
        """用于检测「这个会话有没有变化」。"""
        return "%s|%s" % (self.preview, self.time_text)


@dataclass
class MessageItem:
    """一条消息。"""
    text: str
    cls: str                  # 控件类名，用来判断消息类型
    rect: Optional[tuple] = None
    sender_name: Optional[str] = None   # 群聊昵称（由 OCR 补齐，见 sender.py）
    is_self: Optional[bool] = None      # True=我发的 False=别人发的 None=未判定

    @property
    def kind(self) -> str:
        """消息类型：text / image / emoji / refer / unknown。"""
        if "ChatTextItemView" in self.cls:
            return "text"
        # ⚠️⚠️ **实测（2026-10-14）：微信 4.1.13.65 的图片消息复用了
        #    `mmui::ChatBubbleReferItemView` 这个类名**（不是 `ChatImageItemView`）**，
        #    只有**控件名恰好是「图片」两个字**。实测尺寸 1009x301（一张图）。
        #    不认它就会被当成普通引用消息，而且**图片发不出去**（用户报的就是这个）。
        #    ⚠️ 为什么敢靠名字认：**真正的引用消息不可能只有「图片」两个字**
        #       （它总带着被引用的正文）。
        #    ⚠️ 要同时认「图片」和已规范化的「[图片]」：
        #       `read_chat` 会**先把 text 改成 `[图片]`**，而 `kind` 是 property、
        #       **到那时才求值** —— 漏了这条就又变回 `refer`。
        if (self.cls.endswith("ChatBubbleReferItemView")
                and (self.text or "").strip() in ("图片", _IMAGE_PLACEHOLDER)):
            return "image"
        if "ChatBubbleReferItemView" in self.cls:
            return "refer"
        # ⚠️ `image` 一直在 docstring 的承诺里，但**从来没有代码产生它** ——
        #    2026-10-13 补上：OCR 路径现在会给"够大的图片区域"发一条
        #    `mmui::ChatImageItemView` 的消息（以前图片消息**彻底消失**，
        #    库里连一句「[图片]」都没有）。UIA 路径的图片类名如果也含
        #    `ImageItemView`，会一并从 `unknown` 变成 `image`（更准确）。
        if "ImageItemView" in self.cls:
            return "image"
        # ⚠️ **引用消息**（2026-10-13 补）：微信把引用画在气泡**下方**，
        #    OCR 后端会把正文和引用块**切成两行** —— 它归并之后用这个类名标记
        #    （见 `ocr_reader._is_refer_of`）。UIA 后端不会产生它。
        if "ReferItemView" in self.cls:
            return "refer"
        # ⚠️ **文件 / 分享卡片**（2026-10-13 补）：OCR 后端靠"盒里颜色种数 + 块数"
        #    认出**分享卡片**，再用"后缀 + 容量"把文件从那批里分出来
        #    （见 `ocr_reader._card_kind_of`）。UIA 后端不会产生它们。
        if "FileItemView" in self.cls:
            return "file"
        if "LinkItemView" in self.cls:
            return "link"
        return "unknown"

    @property
    def who(self) -> str:
        """展示用的发送者名字。

        ⚠️ 兜底名是「对方」—— **一对一里是对的**。群聊里那个名字由
            `label_unknown_senders()` 提前写成 `群友`（见 `prompts.GROUP_UNKNOWN`），
            所以走到这里时群聊不会掉进这个兜底。
        """
        if self.is_self is True:
            return "我"
        if self.is_self is False:
            return self.sender_name or "对方"
        return "?"      # 未判定（窗口不可见等）


def label_unknown_senders(snap) -> int:
    """群聊里把「认不出昵称的别人」标成 `群友`（**原地改** `snap.messages`）。

    为什么必须有这个（2026-10-11 用户实测报的）：

        群里出现 `对方: 好吃` 和 `对方: 这个叫做碱水包…` —— 用户以为是**同一个人**，
        其实是**两个不同的人**。因为群昵称要「同一会话里读到 ≥2 次」才认
        （判据③，防止把图里的字当人名），没认出来的一律掉进 `who` 的兜底 `对方`，
        而**「对方」在中文里暗示"只有两个人"**。模型也会被带偏，
        把三个人的发言当成一对一对话来回复。

    ⚠️ 在一对一里**不动**（那里「对方」是对的）—— 所以先看 `snap.is_group`。
    ⚠️ 只改 `is_self is False` 且**没有昵称**的那些：`is_self is None`（没判出来）
        保持原样，`who` 会显示 `?` —— 那是**不知道**，别伪造成知道。
    ⚠️ 写在 `sender_name` 上（不是只在展示时换名）：这样**存进库的也是 `群友`**，
        历史渲染（`window.who_of`，它拿不到群聊标记）才不会又退回「对方」。

    ⚠️⚠️ **还要有"这一屏真的认出了别人的昵称"这个证据**（2026-10-11 用户报）：
        用户的原话是「**如果是私聊，就是不会有上面的名称，就不要是群友**」。

        为什么光看 `is_group` 不够：**OCR 后端的 `is_group` 是"看到任何一条消息带
        昵称"就置真的**（`ocr_reader.read_chat`），而昵称判定本身会误判
        （判据①②③ 是几何 + 像素，不是铁证）。私聊里**误判一次**，
        整屏的"别人"就全变成「群友」—— 而私聊里那个词是**错的**。

        判据：**私聊永远不会有昵称行**。所以"一个别人的昵称都没认出来"时，
        我们**没有**任何证据说明这是群聊 → 保持 `对方`（宁可用那个更弱的词，
        也不要在一个可能是一对一的会话里说「群友」）。

    返回改了几条（诊断用）。
    """
    # ⚠️⚠️ 第二个守卫：**发言人不可能等于当前会话标题**
    #     （2026-10-12 用户实测贴出来的：`张三: 广东省教育厅原厅长罗伟其…`
    #      —— `张三` 就是当前会话标题）。标题栏那一带的字被扫进了聊天区，
    #     于是成了"发言人"。
    #
    #     ⚠️ **只在非群聊里清**（第一版没加这个限制，直接打断了
    #        `test_ocr_reader` 的「昵称挂到下面那条消息上」用例）：
    #        · 实测那个 bug 出现在**私聊**（`张三` 就是私聊的标题）；
    #        · 群里**标题通常是群名**，而某人真名恰好等于群名、并且他发言时
    #          昵称被读出来的概率高得多 —— 那会把**真名字**清掉，损失更大。
    #        · 而且私聊里"标题 = 对方名字"本来就很常见，所以这一条
    #          正好覆盖"私聊里读出一个和标题同名的假昵称"这个真实场景 ✓
    #     ⚠️ 代价（罕见）：私聊里对方真名恰好等于会话标题时会被清掉
    #        → 退回「对方」—— 而私聊里那个词本来就是对的 ✓ 几乎无损失。
    if not getattr(snap, "is_group", False):
        _t = (getattr(snap, "title", "") or "").strip()
        if _t:
            _cleared = 0
            for m in (getattr(snap, "messages", None) or []):
                if (m.sender_name or "").strip() == _t:
                    m.sender_name = ""
                    _cleared += 1
            if _cleared:
                snap._title_sender_cleared = _cleared     # 供诊断
        return 0
    msgs = list(getattr(snap, "messages", None) or [])
    # ⚠️ 群聊的**证据**：至少认出一个别人的昵称（私聊不可能有）。
    known = [m.sender_name.strip() for m in msgs
             if m.is_self is False and (m.sender_name or "").strip()]
    if not known:
        return 0
    n = 0
    for m in msgs:
        if m.is_self is False and not (m.sender_name or "").strip():
            m.sender_name = GROUP_UNKNOWN
            n += 1
    return n


@dataclass
class TimeMarker:
    """聊天区里的一条时间分隔（不是消息）。

    它标记的是「**它下面**的内容属于这个时间点之后」。`is_date_level` 为真
    表示跨天了（昨天 / 星期X / 9月20日），那才是真正的分段线。
    """
    text: str
    rect: Optional[tuple] = None

    @property
    def is_date_level(self) -> bool:
        return bool(DATE_LEVEL_RE.search(self.text or ""))


@dataclass
class ChatSnapshot:
    """当前聊天窗口的一次快照。"""
    title: str = ""
    is_group: bool = False
    messages: List[MessageItem] = field(default_factory=list)
    markers: List[TimeMarker] = field(default_factory=list)
    container_rect: Optional[tuple] = None
    #: 这一拍**能不能判断「在不在底部」**。
    #:
    #: UIA 能：容器矩形和消息矩形都在同一套屏幕坐标里，往上滚时 gap 变负。
    #: **截屏 + OCR 不能**：它的"容器"就是**裁剪出来的那块可见聊天区**
    #: （`ocr_reader` 里 `container_rect = (0, 0, area.width, area.height)`），
    #: 而往上翻时那一屏照样填满这块裁剪区 —— gap 恒 ≈0，
    #: 于是 `at_bottom()` **恒为真**（实测 97 屏 100% 判成"在底部"，2026-10-13）。
    #:
    #: ⚠️ 所以读取方（`MessageStore.save_new_only`）不能只看 `at_bottom` 的真假，
    #:    要看这个标记：**不知道就宁可不写**，别把翻上来的旧消息接到库尾。
    bottom_known: bool = True
    #: 这一屏是哪个后端读出来的："uia" / "ocr"。
    #: ⚠️ 两个后端的**能力不一样**（OCR 分不出图片消息、拿不到会话列表），
    #:    所以来源要能一路带到日志和界面上去，别让它变成一个看不见的隐变量。
    source: str = "uia"
    #: 这一屏里 `[图片]` 的**图片本体**（base64 PNG，**一张一条**，
    #: 按消息顺序）。给模型看用的。
    #: ⚠️ **只用于当次生成，不落库** —— `window.py` 的序列化里
    #:    没有它，否则 history / screens 两个库会迅速膨胀。
    #: ⚠️ 只有 `ocr_read.attach_image` 打开时才填；关着时**恒为空**（老行为）。
    #: ⚠️ UIA 后端不会填它（它不截图）。
    images: List[str] = field(default_factory=list)

    def at_bottom(self, tolerance: Optional[int] = None) -> bool:
        """消息列表是否滚动到了底部 —— 即「可见的最后一条」就是「真正的最新一条」。

        为什么必须判断：
            UIA 只能读到**当前可见**的消息。用户往上滚去看历史时，
            可见区域的最后一条是一条**中间**消息，不是对话真正的最新一条。
            若直接拿它当「待回复的消息」，就会为一条早就回过的旧消息生成建议。

        判据（gap = 容器底部 - 最后一条消息底部）：
            在底部          : gap ≈ 0（实测正好贴着，见 probes/diag_scroll.py）
            往上滚          : gap < 0  —— 内容下移，最后一条被推到底部**以下**，
                                        gap 为负，绝对值≈滚动距离
            消息不满一屏    : gap > 0  —— 本来就没更多可滚，同样算在底部

        所以只有「gap 明显为负」才代表翻历史中。
        ⚠️ 注意别写成 `gap <= tolerance` —— 那会把 -50 也判成在底部，恰好判反。

        ⚠️ **tolerance 必须很小（默认 4），原来给的 20 有系统性漏洞**
        （用户实测反馈）：
            列表是**虚拟化**的，最后一条滚出渲染区后会从 UIA 树里消失，
            「最后一条」于是换成上一条。设往上滚了 S、那一条高 h：
                滚出前：gap = -S
                滚出后：gap = h - S        ← 在 S = h 处**归零**！
            单聊消息高度都是 84，所以 S 落在 [84, 84+tolerance] 时会被
            **误判成「在底部」**。tolerance=20 时每 84px 就有 20px 是误判窗口
            （约四分之一），对记录器来说意味着把翻上来的旧消息接到库尾。
            实测在底部时 gap 正好是 0（不是「大约 0」），不需要留大余量。
        """
        if tolerance is None:
            tolerance = BOTTOM_TOLERANCE
        if not self.container_rect or not self.messages:
            return True
        last = self.messages[-1]
        if not last.rect:
            return True
        gap = self.container_rect[3] - last.rect[3]
        return gap >= -tolerance

    def bottom_gap(self) -> int:
        """容器底部 - 最后一条可见消息底部。
        约 0 = 正好在底部；负数 = 往上翻历史了；正数 = 消息不满一屏。
        """
        if not self.container_rect or not self.messages:
            return 0
        last = self.messages[-1]
        if not last.rect:
            return 0
        return self.container_rect[3] - last.rect[3]


# ------------------------------------------------------------------ 工具函数

def parse_session_name(full_name: str):
    """把会话项的 Name 拆成 (标题, 预览, 时间, 标记列表)。

    实测 Name 形如：
        '张三\\n[动画表情] 那个\\n00:38\\n'
        '示例24新生群\\n群友乙: 要拍照留存…\\n昨天 21:40\\n消息免打扰\\n'
    """
    parts = [p.strip() for p in (full_name or "").split("\n") if p.strip()]
    if not parts:
        return "", "", "", []

    title = parts[0]
    rest = parts[1:]

    flags = []
    for tail in SESSION_FLAGS:
        while rest and rest[-1] == tail:
            flags.append(rest.pop())

    if len(rest) >= 2:
        time_text = rest[-1]
        preview = " ".join(rest[:-1])
    elif len(rest) == 1:
        time_text = rest[0]
        preview = ""
    else:
        time_text = preview = ""
    return title, preview, time_text, flags


def _rect_of(elem):
    try:
        r = elem.CurrentBoundingRectangle
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


# ------------------------------------------------------------------ 定位窗口

# ⚠️ 2026-10-17：`list_windows` / `pick_window` 已搬到 `winlocate.py` ——
#    `follow`（跟随）也要用**同一套判据**挑窗口，而它不能 import 本模块
#    （本模块顶部 import uiautomation，而 follow 是每拍跑的）。
#    这里 re-export，保持老引用（probes/ 有十几处
#    `from wechat_assistant.reader import list_windows, pick_window`）继续可用。
from .winlocate import list_windows, pick_window  # noqa: E402,F401





# ------------------------------------------------------------------ 读取器

#: 图片消息的占位文本。
#: ⚠️ 和 `ocr_reader._IMAGE_PLACEHOLDER` **必须一致**（两边各一份：`ocr_reader`
#:    import 了 `reader`，反过来 import 会循环）。
#: ⚠️ 为什么非得规范化：UIA 读到的控件名就是“图片”两个字
#:    （**没有方括号**）—— 2026-10-14 用户报「李四: 图片」且图发不出去。
_IMAGE_PLACEHOLDER = "[图片]"

#: 发给模型的图压缩到长边多少（和 OCR 那边一致）。
_IMG_MAX_SIDE = 1024


def _crop_images_from_window(hwnd: int, rects) -> List[str]:
    """按**屏幕坐标**抓一张窗口位图，裁出图片消息的本体（base64）。

    ⚠️ UIA 后端**从来不抓图**（它只读控件树），而图片消息的
        `rect` 是**屏幕坐标** —— 所以要现抓一张才能裁。
    ⚠️ 只在 `ocr_read.attach_image` 打开时被调到（关着时**一行都不走**）。
    ⚠️ 和 OCR 那边同一套取舍：**只发图片消息**（不发整屏）、
        压缩到长边 1024、**一张一条**。
    """
    import base64
    import io as _io

    from . import capture

    try:
        wrect = win32gui.GetWindowRect(hwnd)
    except Exception:
        return []
    img = capture.capture_window(hwnd)
    if img is None or capture.is_blank(img):
        return []
    out = []
    for r in rects:
        sub = capture.crop_window_region(img, wrect, r)
        if sub is None or sub.width < 8 or sub.height < 8:
            continue
        w, h = sub.size
        k = min(1.0, float(_IMG_MAX_SIDE) / max(w, h))
        if k < 1.0:
            sub = sub.resize((max(1, int(w * k)), max(1, int(h * k))))
        buf = _io.BytesIO()
        sub.convert("RGB").save(buf, format="PNG")
        out.append(base64.b64encode(buf.getvalue()).decode("ascii"))
    return out


class WeChatReader:
    """封装微信窗口定位与内容读取。所有 UIA 调用都走 worker 线程。"""

    #: 后台连续记录值不值得做。UIA 一次读屏 ~100ms，值得；
    #: OCR 后端一次 ~300ms，不值得 —— 见 `ocr_reader.OcrReader`。
    #: `Recorder` 每拍都会读它（后端可能在运行期切换，所以不能只在启动时看一次）。
    background_read_ok = True

    def count_controls_by_class(self, hwnd: int, cls_names) -> dict:
        """数几个 ClassName 在窗口里各有多少个控件（**诊断用**，只读）。

        用途：回答「为什么 UIA 在某个页面上读不到东西」。UIA 是**按控件类型**
        读的 —— 非聊天页面（公众号 / 微信支付 / 设置 / 朋友圈）上
        `mmui::ChatTextItemView` 这类**消息控件根本不存在**，于是
        `read_chat()` 返回 0 条消息，`Recorder.tick` 直接跳过（**不报错**）。
        数一下就能把这个机制**量出来**，而不是靠猜
        （见 `probes/probe_uia_vs_nonchat.py`）。

        ⚠️ 每个类名一次全量 FindAll —— 只在诊断时调，**别放进每拍**。
        """
        names = list(cls_names)

        def job(auto, uia):
            el = auto.ControlFromHandle(hwnd).Element
            out = {}
            for cn in names:
                try:
                    cond = uia.CreatePropertyCondition(PROP_CLASS_NAME, cn)
                    out[cn] = int(el.FindAll(SCOPE_DESCENDANTS, cond).Length)
                except Exception as e:
                    out[cn] = "查询失败: %s" % type(e).__name__
            return out

        return self.worker.call(job)

    def probe(self, hwnd: int):
        """便宜探针 —— **UIA 后端不需要它，永远返回 None**。

        `ocr_reader.OcrReader.probe` 是「抓图 + 缩略图哈希」（约 32ms），
        用来在"读一屏要 250ms"的后端上做到「**变了才真读**」。

        UIA 一屏才 ~100ms，而且它本来就有 `_screen_changed()` 那层文本去重 ——
        再加一层像素探针只会白花抓图那 32ms。所以这里明确返回 None，
        含义是「**这个后端不用探针，照旧每拍真读**」。
        """
        return None

    def use_config(self, cfg) -> None:
        """设置界面保存后换一份新配置（**不重启就生效**）。

        ⚠️⚠️ 为什么必须有这个方法（2026-10-17 真 bug，用户报「打开了识别图片，
        模型还是说看不到图」）：`run_watch.on_config_changed` 当时只换了自己那个
        `cfg` 变量 + 重建 `AIClient`，**没换读取器手里这份** —— 而
        `ocr_read.attach_image` 是在读屏时用**读取器自己的 cfg** 判断的。
        于是开关"显示已打开"、图片却一张都不裁，**重启才正常**，且不报错。
        """
        self.cfg = cfg

    def __init__(self, cfg, worker, logger: Callable[[str], None] = None):
        self.cfg = cfg
        self.worker = worker
        self._log = logger or (lambda msg: None)
        #: 上一次 find_window 挑不出来时的原因（给上层做降级判断用）
        self.last_miss = ""
        #: 「多候选分不出主窗口」时按 UIA 控件树挑出来的结果缓存：
        #: (候选 hwnd 集合, 选中的 hwnd)。见 `_pick_by_health`。
        #: ⚠️ 必须**按候选集合**缓存 —— 窗口换了要重判，否则会一直用旧的那个。
        self._health_pick = None

    # -------------------------------------------------- 1. 定位窗口（纯 Win32）

    def find_window(self) -> Optional[dict]:
        """用 EnumWindows 找到微信主窗口。找不到返回 None。

        ⚠️ 2026-10-17 加了一层兜底：`pick_window()` 是纯 Win32 的，
           而**「图片和视频」预览可能也叫「微信」**（同名同进程同类名）——
           那时 owner/caption 也不一定分得开。这种情况改用
           「哪棵 UIA 树最丰富」来挑（`_pick_by_health`，**有缓存**）。
        """
        cands = list_windows(self.cfg)
        win = pick_window(cands, self.cfg)
        if win is None and len(cands) > 1:
            win = self._pick_by_health(cands)
        if win is None:
            # ⚠️ 这条每拍都会走到，而 find_window 每个 tick 都调 ——
            #    不节流的话日志会被同一句话刷满（实测过一次类似的：
            #    跟随器每秒一行「面板几何被外部改过」）。
            #    只在**原因变了**的时候记一次。
            why = ("有 %d 个候选但分不出主窗口：%s" % (
                len(cands), [c.get("title") for c in cands])) if cands else "没找到微信窗口"
            if why != self.last_miss:
                self._log("⚠ %s" % why)
            self.last_miss = why
        else:
            self.last_miss = ""
        return win

    def _pick_by_health(self, cands):
        """多个候选分不出时，挑**UIA 树最丰富**的那个（主窗口）。

        ⚠️ 为什么需要：实测「图片和视频」预览可能也叫「微信」，
           纯 Win32 的 owner / `WS_CAPTION` 判据不一定能分开。
           而 UIA 能：主窗口有会话列表 + 消息控件（正常 160 个后代），
           图片预览那棵几乎是空的（2 个）—— 门槛用 `UIA_HEALTH_MIN`。

        ⚠️ 为什么不能每拍都算：一次 `uia_health` 是一次全量 FindAll（~50-90ms），
           而 `find_window` 每拍都调。所以按**候选 hwnd 集合**缓存：
           集合不变就直接用上次的结论；窗口变了才重判。

        ⚠️ 挑不出来（全都是空树 / 体检失败 return -1）就返回 None ——
           宁可**等待**（`run_watch` 启动时会等），也不瞎读。
        """
        key = tuple(sorted(c["hwnd"] for c in cands))
        cached = self._health_pick
        if cached and cached[0] == key:
            hwnd = cached[1]
            return next((c for c in cands if c["hwnd"] == hwnd), None)
        best, best_n = None, -1
        for c in cands:
            try:
                n = self.uia_health(c["hwnd"])
            except Exception:
                n = 0
            if n > best_n:
                best, best_n = c, n
        if best is not None and best_n >= UIA_HEALTH_MIN:
            self._health_pick = (key, best["hwnd"])
            self._log("按 UIA 控件树挑主窗口：%r（%d 个控件）"
                      % (best.get("title"), best_n))
            return best
        return None

    # -------------------------------------------------- 2. 轻量：会话列表

    def read_sessions(self, hwnd: int) -> List[SessionItem]:
        """只查会话列表项并读 Name（约 14.5ms），用于轮询检测新消息。"""
        cls_cell = self.cfg.get("wechat.cls.session_cell", "mmui::ChatSessionCell")

        def job(auto, uia):
            el = auto.ControlFromHandle(hwnd).Element
            cond = uia.CreatePropertyCondition(PROP_CLASS_NAME, cls_cell)
            arr = el.FindAll(SCOPE_DESCENDANTS, cond)
            items = []
            for i in range(arr.Length):
                try:
                    name = arr.GetElement(i).CurrentName or ""
                except Exception:
                    continue
                title, preview, time_text, flags = parse_session_name(name)
                if title:
                    items.append(SessionItem(title, preview, time_text, flags))
            return items

        return self.worker.call(job)

    # -------------------------------------------------- 3. 轻量：当前聊天标题

    def read_chat_title(self, hwnd: int) -> str:
        """只读当前聊天标题（轻量）。

        注意：标题元素的 AutomationId 是一长串路径
        （content_view...current_chat_name_label），**不是**精确等于配置值，
        所以先用 ClassName 缩小范围再做子串匹配，不能直接做等值条件查询。
        """
        cls_text = self.cfg.get("wechat.cls.text_view", "mmui::XTextView")
        id_title = self.cfg.get("wechat.node.chat_title", "current_chat_name_label")

        def job(auto, uia):
            el = auto.ControlFromHandle(hwnd).Element
            cond = uia.CreatePropertyCondition(PROP_CLASS_NAME, cls_text)
            arr = el.FindAll(SCOPE_DESCENDANTS, cond)
            for i in range(arr.Length):
                try:
                    e = arr.GetElement(i)
                    if id_title in (e.CurrentAutomationId or ""):
                        return e.CurrentName or ""
                except Exception:
                    continue
            return ""

        return self.worker.call(job)

    # -------------------------------------------------- 3.5 轻量：输入框内容

    def read_input_text(self, hwnd: int) -> str:
        """读取聊天输入框里当前的文字（用户正在打的字）。

        注意：`chat_input_field` 的 **Name 属性是会话名，不是输入内容**，
        真正的输入内容必须用 ValuePattern 读 —— 这个坑实测踩过。
        """
        id_input = self.cfg.get("wechat.node.input_field", "chat_input_field")

        def job(auto, uia):
            el = auto.ControlFromHandle(hwnd).Element
            cond = uia.CreatePropertyCondition(PROP_AUTOMATION_ID, id_input)
            arr = el.FindAll(SCOPE_DESCENDANTS, cond)
            for i in range(arr.Length):
                e = arr.GetElement(i)
                try:
                    if (e.CurrentAutomationId or "") != id_input:
                        continue
                    ctl = auto.Control.CreateControlFromElement(e)
                    vp = ctl.GetValuePattern()
                    return (vp.Value or "") if vp else ""
                except Exception:
                    continue
            return ""

        return self.worker.call(job)

    # -------------------------------------------------- 4. 全量：当前聊天

    def uia_health(self, hwnd: int) -> int:
        """这个窗口的 UIA **后代控件数量**。

        这是判断「UIA 到底能不能读到微信」的**唯一可靠指标**
        （2026-09-27 实测，见 CLAUDE.md）：

            正常环境            160 个后代控件
            拿不到的环境        **2 个**

        ⚠️ 它和「会话标题为空」是两回事，**别把两者混起来**：
            用户停在设置页 / 朋友圈 / 文件传输助手时，标题也是空的，
            但那是**正常的**，控件树满满当当。
            只按"标题为空"去选路，会把「用户看了一眼设置」判成 UIA 坏了。

        ⚠️ 这是一次全量 FindAll（约 50ms），**只在该用的时候调**
            （`AutoReader` 只在标题为空时才体检），别塞进每拍。

        出错返回 -1（当作"查不出来"，不据此选路）。
        """
        def job(auto, uia):
            el = auto.ControlFromHandle(hwnd).Element
            arr = el.FindAll(SCOPE_DESCENDANTS, uia.CreateTrueCondition())
            return int(arr.Length)

        try:
            return int(self.worker.call(job, label="UIA 体检"))
        except Exception:
            return -1

    def read_chat(self, hwnd: int) -> Optional[ChatSnapshot]:
        """读取当前打开聊天的标题、消息，以及时间分隔符（约 50ms）。"""
        id_title = self.cfg.get("wechat.node.chat_title", "current_chat_name_label")
        id_msglist = self.cfg.get("wechat.node.message_list", "chat_message_list")
        tb_group = self.cfg.get("wechat.cls.titlebar_group", "mmui::ChatTitleBarChatRoomView")
        cls_time = self.cfg.get("wechat.cls.time_item", "mmui::ChatItemView")

        def job(auto, uia):
            el = auto.ControlFromHandle(hwnd).Element
            arr = el.FindAll(SCOPE_DESCENDANTS, uia.CreateTrueCondition())
            snap = ChatSnapshot()
            raw_markers = []        # 容器 rect 可能还没读到，先攒着，循环后再筛
            image_rects = []        # 图片消息的 rect（后面按它裁图）
            for i in range(arr.Length):
                e = arr.GetElement(i)
                try:
                    cls = e.CurrentClassName or ""
                    aid = e.CurrentAutomationId or ""
                    name = e.CurrentName or ""
                except Exception:
                    continue
                if id_title in aid:
                    snap.title = name
                elif cls == tb_group:
                    snap.is_group = True
                elif aid == id_msglist:
                    snap.container_rect = _rect_of(e)
                elif aid.startswith(id_msglist) and cls.endswith("ItemView"):
                    _r = _rect_of(e)
                    # ⚠️ 和 `MessageItem.kind` 里那条**必须一致**（同一个事实两处用）。
                    _is_img = ("ImageItemView" in cls
                               or (cls.endswith("ChatBubbleReferItemView")
                                   and name.strip() == "图片"))
                    # ⚠️ 图片消息的控件名就是“图片”两个字（没有方括号），
                    #    统一成 `[图片]` 和 OCR 后端一致 —— 否则界面上看到的就是
                    #    「李四: 图片」而不是「李四: [图片]」。
                    snap.messages.append(MessageItem(
                        text=(_IMAGE_PLACEHOLDER if _is_img else name),
                        cls=cls, rect=_r))
                    if _is_img and _r:
                        image_rects.append(_r)
                elif cls == cls_time and is_time_text(name):
                    # ⚠️ 走到这里说明它不是消息项（消息项在上面那条分支被收走了）。
                    #    同一个类名还承载「拍一拍」等系统提示，靠 is_time_text 区分。
                    raw_markers.append(TimeMarker(text=name.strip(), rect=_rect_of(e)))

            # 只留落在消息容器竖直范围内的 —— 别处也可能有同名控件
            if snap.container_rect:
                top, bottom = snap.container_rect[1], snap.container_rect[3]
                snap.markers = [m for m in raw_markers
                                if m.rect and m.rect[3] > top and m.rect[1] < bottom]
            else:
                snap.markers = raw_markers
            # ⚠️ 群聊里把"认不出昵称的别人"标成 `群友`（不能叫「对方」——
            #    那词暗示只有两个人，群里会把多个人看成同一个）。
            #    见 `prompts.GROUP_UNKNOWN` 那段说明。
            # ⚠️ 图片本体（可选）：只有 `ocr_read.attach_image` 打开时才走，
            #    关着时**一行都不走**（不抓图、不编码）。
            #    ⚠️ UIA 本来是**不抓图**的，这是它唯一会抓图的地方。
            if image_rects and self.cfg.get("ocr_read.attach_image"):
                snap.images = _crop_images_from_window(hwnd, image_rects)
            label_unknown_senders(snap)
            return snap

        return self.worker.call(job)
