# -*- coding: utf-8 -*-
"""发送者判定：这条消息是谁发的。

════════════════════════════════════════════════════════════════════
为什么必须靠截图（实测结论）

微信 4.x 的 UIA **不提供**发送者信息：
    - 消息项 BoundingRectangle 是整行宽度，没有气泡左右信息
    - ItemStatus / ItemType / HelpText / AriaRole 全为空
    - 消息项不支持 TextPattern
    - 群聊昵称是自绘 canvas 上的像素，UIA 树里根本没有对应元素

判定方法（按可靠性排序）：

  ① 像素分析 —— 主力判据，单聊 / 群聊通用
     对方的消息气泡靠左，自己的靠右。按行截图后统计「内容像素」
     （头像+气泡，即与背景色差异大的像素）落在左半还是右半：
         左半占比 > 0.75 -> 别人
         左半占比 < 0.25 -> 自己
     实测区分度极大（对方 0.94~1.00 / 自己 0.00），且不受主题影响。
     辅助判据：自己发的是绿色气泡，绿像素占比 > 0.03 可兜底。

  ② 群聊昵称 —— 只知道「谁」，不能用来判断「是不是自己」
     别人的消息上方多一行昵称，行高明显更大（实测 115 vs 84）。
     只对高行做 OCR 读行内左上部即可拿到昵称。

  ⚠️ 两个踩过的坑，别再犯：

     - **行高在单聊里毫无意义**。单聊不显示昵称，对方和自己的短消息都是 84。
       早期版本用「行高 < 100 就判为自己」，结果把对方发的「哦哦」判成了用户发的。

     - **群聊里同一人连续发消息时，微信只在第一条显示昵称**，
       后续几条行高同样是 84 —— 同样不能据此判成自己。
       定性只能靠像素分析，昵称则从该人前一条继承（中间夹了自己的消息就断开）。

取图方式与遮挡（2026-09-24 修正，重要）
--------------------------------------
最初用屏幕截图（`ImageGrab.grab`），代价是**窗口被遮挡就截到别人的内容**。
实测按快捷键时聊天区常常同时被浏览器和本项目自己的悬浮窗（TkChild）盖住，
屏幕截图拿到的是**别的窗口**——OCR 出来是终端里的文字，判定整体失效：
`sender.resolve()` 返回 0 条 -> 所有消息 `is_self=None`
-> 「同一个人连发的几句话合并成一个回复目标块」这条逻辑完全失效
（表现为回复目标块永远只有 1 条，用户要的「一段话一起思考」从未生效）。

现在默认改用 `PrintWindow(PW_RENDERFULLCONTENT)`：让微信窗口**自己**把内容
画进位图，不受遮挡影响，也不用把微信临时置顶去打扰用户。
实测同一时刻对照：PrintWindow 命中 UIA 消息 **5/5**（连最新一条都在，说明是
实时内容），屏幕截图 **0/5**。

仍会放弃判定的情况：窗口最小化（PrintWindow 只拿到空白）。
此时把 is_self 留成 None（未知），不瞎猜，由上层决定怎么处理。
可用 `sender.capture_mode: screen` 退回旧行为（需窗口可见且不被遮挡）。
════════════════════════════════════════════════════════════════════
"""

import re
import time
from typing import Callable, List, Optional

import numpy as np
import win32api
import win32con
import win32gui
import win32process
from PIL import ImageGrab

from .capture import capture_window, crop_window_region, is_blank
from .reader import ChatSnapshot, MessageItem

# 明显不是昵称的 OCR 噪声
_RE_NUMERIC = re.compile(r"^[\d\.\s\-:：/\\]+$")

# 像素分析参数（实测调过）
BG_MARGIN = 8              # 用左右各多少像素估计背景色
DIFF_THRESH = 45           # 与背景色的曼哈顿距离超过它算「内容像素」
LEFT_RATIO_OTHER = 0.75    # 左半占比 > 它 -> 别人
LEFT_RATIO_SELF = 0.25     # 左半占比 < 它 -> 自己
GREEN_RATIO_SELF = 0.03    # 绿像素占比 > 它 -> 自己（兜底）


def looks_like_noise(t: str) -> bool:
    t = (t or "").strip()
    if not t:
        return True
    if _RE_NUMERIC.match(t):
        return True
    if len(t) == 1 and not t.isalnum():
        return True
    return False


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "") if not ch.isspace())


# ---------------------------------------------------------------- 昵称判据
#
# ⚠️ 为什么需要这一整套（2026-09-27 实测血案）
#
# 原来读昵称的办法是「行高 >= 100 就裁这一行的**上 55%**，OCR 后取最靠上的短文本」。
# 这个判据是**缺的**，它靠巧合工作，实测已经把库污染了 ——
# `state/history.db` 里一个 5 人群记着 **40 个不同的人名**：
#
#     灵感出炉        ← 喜茶截图里的字
#     新民主主义革命基本思想  ← 马哲课本截图里的字
#     （中国社会备阶级的分析） ← 同上
#     就说时间上实在排不出来   ← **上一条消息的正文**
#
# 实测那条 55% 条带：OCR 出 23 块、22 个候选，**其中 21 个是图里的字**，
# 只因为真昵称恰好在最上面才赢了 —— 判据本身完全没在筛。
#
# 现在的三条判据（缺一不可，各自对应一类污染）：
#   ① 位置  —— 候选必须在**本条消息内容块的正上方**（干掉「图里的字」）
#   ② 底色  —— 候选两旁必须是**聊天区底色**（干掉「上一条消息的正文」，正文坐在气泡上）
#   ③ 复现  —— 同一个名字在**同一会话里被读出来 >= min_hits 次**才算数
#              （图片里的字每次都不一样，只出现一次；真昵称跟着人的每条消息反复出现。
#               它顺带把 `早9腌12` / `9晚12` 这类**误识的变体**也滤掉了。）

MIN_CONTENT_H = 24         # 「内容块」至少连续这么多行；昵称行只有十几像素，靠高度区分
BG_SAMPLE_TOL = 22         # 候选两旁的颜色离底色多近才算「坐在聊天区底色上」
NICK_BG_MARGIN = 8         # 用左右各多少像素估计底色
NICK_MIN_HITS = 2          # ③ 的门槛：读到几次才认


def estimate_bg(a):
    """用消息行左右两侧的留白估计聊天区底色。估不出来返回 None。"""
    m = NICK_BG_MARGIN
    if a.shape[1] < 4 * m:
        return None
    left = a[:, :m].reshape(-1, 3).mean(axis=0)
    right = a[:, -m:].reshape(-1, 3).mean(axis=0)
    return (left + right) / 2.0


_CV2 = None
_CV2_TRIED = False


def _cv2():
    """懒加载 cv2。

    ⚠️ 不在模块顶层 import：`sender.py` 被 recorder / run_watch 都引到，
    而 cv2 的 import 不便宜。openai 那条链上没有 OCR 也用不到它。
    （本机 opencv 是装好的，rapidocr 本来就依赖它，所以只是把代价挪到真正用时。）
    """
    global _CV2, _CV2_TRIED
    if not _CV2_TRIED:
        _CV2_TRIED = True
        try:
            import cv2
            _CV2 = cv2
        except Exception:
            _CV2 = None
    return _CV2


def content_blocks(a, bg, tol=DIFF_THRESH, min_h=MIN_CONTENT_H):
    """行内所有「内容块」—— 返回 [(top, left, right, area), ...]，按面积从大到小。

    内容块 = 气泡 / 图片本体。**左边那个头像也是一块**，所以调用方必须用
    `content_top_for()` 按候选的 x 范围去挑，不能直接拿最大的那块。

    ⚠️ 为什么不能用更朴素的写法（两条都实测踩过）：
      ① **「第一行有内容」不行** —— 昵称自己就是第一行内容。
      ② **「第一段连续 >= min_h 行的内容」也不行** —— 实测左边那个头像
         从 y=13 起就有 54x54 的连续块，于是顶边算成 13，把右侧 y=10~30 的
         **真昵称「李四」**一起挡掉了（误伤）。
    改用**面积最大的连通域**之后同一行给出 44（图片本体 63283 px vs
    头像 2859 px，差 22 倍，分得很开）。
    """
    if bg is None or a.size == 0:
        return []
    mask = (np.abs(a - bg).sum(axis=2) > tol).astype(np.uint8)

    cv2 = _cv2()
    if cv2 is not None:
        try:
            n, _labels, stats, _cent = cv2.connectedComponentsWithStats(mask, 8)
            out = []
            for i in range(1, n):
                left = int(stats[i, cv2.CC_STAT_LEFT])
                top = int(stats[i, cv2.CC_STAT_TOP])
                w = int(stats[i, cv2.CC_STAT_WIDTH])
                h = int(stats[i, cv2.CC_STAT_HEIGHT])
                area = int(stats[i, cv2.CC_STAT_AREA])
                # 滤掉 1px 宽的边缘伪影和「一行小字」这种矮东西
                if w < 8 or h < min_h:
                    continue
                out.append((top, left, left + w, area))
            out.sort(key=lambda x: -x[3])
            if out:
                return out
        except Exception:
            pass

    # 没有 cv2（或它出错了）时的兜底：列投影 + 找连续足够高的一段。
    # ⚠️ 它**不区分头像**，所以调用方拿到的是「整行级别」的近似，比有 cv2 时弱。
    has = mask.sum(axis=1) > 2
    start, run = None, 0
    for y in range(has.shape[0]):
        if has[y]:
            if start is None:
                start = y
            run += 1
            if run >= min_h:
                return [(int(start), 0, a.shape[1], int(mask.sum()))]
        else:
            start, run = None, 0
    return []


def content_top_for(blocks, x1, x2, y2):
    """本条内容块的顶边 = **在候选下方、且和它水平重叠**的那些块里最靠上的一块。

    返回 None 表示「候选下方没有任何内容块」。

    ⚠️ 这条选法用错了两次，别再改回去：
      · **只按「面积最大」挑** —— 上一条消息的气泡可能比本条还大，
        于是拿它的顶边当本条的，位置判据就形同虚设（实测：上一条的正文
        就这么漏过去了，只剩判据② 兜着）。
      · **只按「第一段连续内容」挑** —— 会中**头像**的招：头像和真昵称是
        **并排**的（都在 y≈10~30），实测把真昵称「李四」一起挡掉了。
      「下方最近 + 水平重叠」同时避开这两个坑 ——
      头像在候选左边（不重叠）或在候选上方（top < y2），上一条气泡也在上方。
    """
    best = None
    for top, left, right, _area in (blocks or []):
        if right <= x1 or left >= x2:       # 和候选没有水平交集
            continue
        if top < y2:                        # 这块在候选上方（或包着候选）
            continue
        if best is None or top < best:
            best = top
    return best


def nickname_ok_position(blocks, x1, x2, y2) -> bool:
    """判据①：候选是否在**本条内容块的正上方**。

    ⚠️ 认不出内容块时（blocks 为空）返回 True —— 那种情况交给判据②③，
    别一刀切掉所有昵称（底色估计失败、整行纯色都会走到这）。
    """
    if not blocks:
        return True
    return content_top_for(blocks, x1, x2, y2) is not None


def sits_on_background(a, bg, box, tol=BG_SAMPLE_TOL, pad=3, span=5):
    """候选文字**两旁**的颜色是不是就是聊天区底色。

    判据②：昵称画在聊天区底色上，两旁是平的；
        气泡里的正文两旁是气泡色，图片里的字两旁是图片像素。
    ⚠️ 采样点在候选**竖直中线的左右外侧**，不是候选内部（内部本来就是字）。
    """
    if bg is None or a.size == 0:
        return False
    h, w = a.shape[:2]
    cy = (int(box[1]) + int(box[3])) // 2
    if not (0 <= cy < h):
        return False
    samples = []
    for x in range(int(box[0]) - pad - span, int(box[0]) - pad):
        if 0 <= x < w:
            samples.append(a[cy, x])
    for x in range(int(box[2]) + pad, int(box[2]) + pad + span):
        if 0 <= x < w:
            samples.append(a[cy, x])
    if not samples:
        return False
    mean = np.asarray(samples, dtype=np.float64).mean(axis=0)
    return float(np.abs(mean - bg).max()) <= tol


def analyze_side(img):
    """按内容像素的左右分布判断气泡在哪一侧。

    返回 (is_self, left_ratio, green_ratio)：
        True  -> 自己发的（气泡靠右）
        False -> 别人发的（气泡靠左）
        None  -> 判不出来（整行没内容等）
    """
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    h, w, _ = a.shape
    if w < 4 * BG_MARGIN or h < 4:
        return None, 0.0, 0.0

    # 用左右边缘估计背景色（消息列表两侧都有留白）
    left_bg = a[:, :BG_MARGIN].reshape(-1, 3).mean(axis=0)
    right_bg = a[:, -BG_MARGIN:].reshape(-1, 3).mean(axis=0)
    bg = (left_bg + right_bg) / 2.0

    diff = np.abs(a - bg).sum(axis=2)
    mask = diff > DIFF_THRESH
    if int(mask.sum()) == 0:
        return None, 0.0, 0.0

    left_cnt = int(mask[:, : w // 2].sum())
    right_cnt = int(mask[:, w // 2:].sum())
    denom = left_cnt + right_cnt
    lr = (left_cnt / denom) if denom else 0.5

    # 绿色气泡（自己发的特征色）作为兜底
    r_, g_, b_ = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    green = (g_ - np.maximum(r_, b_)) > 20
    gr = float(green.sum()) / float(h * w)

    if lr > LEFT_RATIO_OTHER:
        return False, lr, gr
    if lr < LEFT_RATIO_SELF:
        return True, lr, gr
    if gr > GREEN_RATIO_SELF:
        return True, lr, gr
    return None, lr, gr


class SenderResolver:
    """给消息填上 sender_name / is_self。"""

    def __init__(self, cfg, ocr=None, logger: Callable[[str], None] = None):
        self.cfg = cfg
        self.ocr = ocr
        self._log = logger or (lambda msg: None)

        self.enabled = bool(cfg.get("sender.enabled", True))
        self.auto_topmost = bool(cfg.get("sender.auto_topmost", False))
        # 取图方式：printwindow（默认，不受遮挡影响）/ screen（屏幕截图，需窗口可见）
        self.capture_mode = str(cfg.get("sender.capture_mode", "printwindow")).strip().lower()
        if self.capture_mode not in ("printwindow", "screen"):
            self.capture_mode = "printwindow"
        # 行高阈值只用来决定「要不要做 OCR 读昵称」，**不再**用来判断是不是自己发的
        self.min_row_height = int(cfg.get("sender.nickname_min_row_height", 100))
        self.max_rows = int(cfg.get("sender.max_ocr_rows", 8))
        self.top_nick_ratio = 0.55
        self.use_nickname = bool(cfg.get("sender.read_nickname", True))
        # ③ 昵称表门槛：同一个名字在同一会话里读到几次才认（见文件上方那段说明）
        self.nickname_min_hits = max(1, int(cfg.get("sender.nickname_min_hits",
                                                    NICK_MIN_HITS)))
        # 昵称表：会话标题 -> {名字: 读到过几次}
        #
        # ⚠️ 只放内存、**不落盘**，重启就重新学。代价是重启后每个人的
        #    第一条消息没有名字（显示「对方」），第二条就回来了。
        #    有意的：为了这个再开一个库/改存储格式不值，而且它会自愈。
        # ⚠️ 会话数要有上限，否则长时间挂着一个活跃的微信会让它无限涨。
        self._name_hits = {}
        self._max_sessions = 20
        #: ⚠️ 「非 UIA 快照直接返回」那句日志**只报一次**用的。
        #: 每拍都会走到那里，不节流的话日志会被刷满（2026-10-11 用户实测：
        #: 每秒一行，看起来像死循环）。见 `resolve()` 里的说明。
        self._logged_skip_source = None

    # ------------------------------------------------------------ 窗口状态

    def _is_obstructed(self, hwnd: int, pid: int, rect) -> bool:
        """采样若干点看是否还属于微信进程 —— 判断窗口有没有被别的窗口盖住。

        比「是不是前台窗口」更准：窗口露在一边没被盖住时也能正常截图。
        """
        if not rect:
            return False
        x1, y1, x2, y2 = rect
        for x, y in ((x1 + 20, y1 + 20), ((x1 + x2) // 2, (y1 + y2) // 2), (x2 - 20, y2 - 20)):
            try:
                h = win32gui.WindowFromPoint((x, y))
                if not h:
                    continue
                _, p = win32process.GetWindowThreadProcessId(h)
                if p != pid:
                    return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------ 昵称

    def _pick_nickname(self, img, msg_text: str, row_arr=None, bg=None,
                       blocks=None) -> str:
        """从行内上部截图里挑昵称。

        row_arr / bg / blocks 是本条**整行**的像素和判据原料（见文件上方说明）。
        候选的坐标和整行一致（两者都从 gx1, gy1 开始裁，所以不用换算）。
        """
        if self.ocr is None:
            return ""
        try:
            items = self.ocr.recognize(img)
        except Exception as e:
            self._log("昵称 OCR 失败: %s: %s" % (type(e).__name__, e))
            return ""
        cands = []
        for it in items:
            t = (it["text"] or "").strip()
            if not t or len(t) > 16:
                continue
            if _norm(t) == _norm(msg_text):
                continue
            if looks_like_noise(t):
                continue
            # 判据① 必须在**本条内容块上方** —— 干掉「图里的字」
            if not nickname_ok_position(blocks, it["x1"], it["x2"], int(it["y2"])):
                continue
            # 判据② 两旁必须是聊天区底色 —— 干掉「上一条消息的正文」
            if row_arr is not None and bg is not None:
                if not sits_on_background(
                        row_arr, bg, (it["x1"], it["y1"], it["x2"], it["y2"])):
                    continue
            cands.append((it["y1"], it["x1"], t))
        if not cands:
            return ""
        cands.sort()
        return cands[0][2]

    def _accept_nickname(self, session_key: str, name: str) -> bool:
        """判据③ 昵称表：同一会话里复查够 `nickname_min_hits` 次才认这个名字。

        为什么这一条很值：图片里的字**每次都不一样**（每个名字只出现一次），
        而真昵称会跟着那个人的每条消息反复出现。所以「只出现一次」本身就是
        一个很强的噪声判据，而且它顺带滤掉了同一个名字的**误识变体**
        （实测 `群友甲` 被读出 `早9腌12` / `9晚12` / `月9腌12` / `目9陷12`，
        这些变体各自只出现一次 -> 全被丢掉，不再污染 prompt 和「连续消息」链）。
        """
        c = self._name_hits.get(session_key)
        if c is None:
            if len(self._name_hits) >= self._max_sessions:
                self._name_hits.pop(next(iter(self._name_hits)))
            c = {}
            self._name_hits[session_key] = c
        c[name] = c.get(name, 0) + 1
        return c[name] >= self.nickname_min_hits

    # ------------------------------------------------------------ 主流程

    def resolve(self, hwnd: int, pid: int, snap: ChatSnapshot,
                targets: Optional[List[MessageItem]] = None) -> int:
        """就地填充 sender_name / is_self，返回成功定性的条数。"""
        if not self.enabled:
            return 0

        # ⚠️⚠️ **OCR 后端读出来的快照不要走这里**（2026-09-27 第四十五轮）。
        #
        #    两个理由，任何一个都足以致命：
        #
        #    ① **坐标系不是一回事**。本方法拿 `m.rect` 去窗口位图上裁像素，
        #       而那串 rect 是**整窗屏幕坐标**。OCR 后端的 rect 是**裁剪区坐标**
        #       （相对聊天区左上角），拿去裁只会裁到无关的地方。
        #    ② **判定它已经有了**。`OcrReader` 是**从同一批像素**里直接看出
        #       气泡靠左还是靠右的，`is_self` 已经填好了 —— 这里再跑一遍
        #       只会用错误的裁剪结果**把它覆盖掉**。
        #
        #    放在这里而不是调用点：`resolve()` 有三个调用点（Recorder /
        #    do_generate / 探针），堵在入口才不会漏掉一个。
        if getattr(snap, "source", "uia") != "uia":
            # ⚠️ **只记一次**（2026-10-11 用户实测：这行每秒刷一次，
            #    看起来像死循环）。
            #
            # 它描述的是**结构性事实**（"OCR 快照不需要再判一次发送者"），
            # 不是"这一拍出了什么事" —— 每拍都报没有任何信息量，
            # 只是把日志刷满，真正要注意的东西反而被埋掉。
            # ⚠️ 这个项目在这条上栽过好几次（跟随器 `_verify` 的"只报一次"也是）：
            #    **每拍都会发生的事，日志必须节流。**
            src = getattr(snap, "source", "?")
            if self._logged_skip_source != src:
                self._logged_skip_source = src
                self._log("这屏来自 %s 后端，发送者判定在那边已经做过了"
                          "（同类信息以后不再重复）" % src)
            return sum(1 for m in snap.messages if m.is_self is not None)

        msgs = [m for m in (targets if targets is not None else snap.messages) if m.rect]
        if not msgs:
            return 0

        sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
        crect = snap.container_rect

        # ---------- 取图：默认 PrintWindow（窗口自己渲染，不受遮挡影响）
        win_img = None
        wrect = None
        if self.capture_mode == "printwindow":
            try:
                wrect = win32gui.GetWindowRect(hwnd)
                win_img = capture_window(hwnd)
            except Exception as e:
                self._log("PrintWindow 抓图异常: %s: %s" % (type(e).__name__, e))
                win_img, wrect = None, None
            if win_img is not None and is_blank(win_img):
                # 窗口最小化时 PrintWindow 只能拿到空白。不瞎猜，直接放弃判定。
                self._log("PrintWindow 抓到空白（微信窗口可能已最小化），本次不判定发送者")
                win_img = None
            if win_img is None:
                self._log("PrintWindow 不可用，退回屏幕截图（需窗口可见且不被遮挡）")

        topmost_done = False
        try:
            # ---------- 屏幕截图的守卫：只在真的要用屏幕截图时才需要
            if win_img is None and win32gui.IsIconic(hwnd):
                self._log("微信窗口已最小化，无法判定发送者（需要窗口可见才能截图）")
                return 0
            if (win_img is None and self._is_obstructed(hwnd, pid, crect)
                    and not self.auto_topmost):
                self._log("微信窗口被其它窗口遮挡，跳过发送者判定"
                          "（如需强制判定，把 config.yaml 的 sender.auto_topmost 设为 true）")
                return 0

            if (win_img is None and self.auto_topmost
                    and self._is_obstructed(hwnd, pid, crect)):
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
                topmost_done = True
                # 等窗口真正前置并完成重绘。踩过坑：等太短会截到别的窗口，
                # 把某设备管理窗口的标题当成了群昵称。
                time.sleep(0.6)
                if self._is_obstructed(hwnd, pid, crect):
                    self._log("临时置顶后窗口仍被遮挡，跳过发送者判定")
                    return 0

            def grab_box(box):
                """按**屏幕坐标**取图。

                PrintWindow 模式：从整窗位图里裁（坐标系换算在 capture 模块里做，
                越界自动收敛）；屏幕模式：直接抓屏幕。
                """
                if win_img is not None:
                    return crop_window_region(win_img, wrect, box)
                return ImageGrab.grab(bbox=box, all_screens=True)

            done = 0
            ocr_rows = 0
            last_nickname = None      # 群聊里同一个人连续发消息时，昵称沿用上一条

            for m in msgs:
                x1, y1, x2, y2 = m.rect
                row_h = y2 - y1

                gx1 = max(0, x1, crect[0] if crect else 0)
                gy1 = max(0, y1, crect[1] if crect else 0)
                gx2 = min(sw, x2, crect[2] if crect else sw)
                gy2 = min(sh, y2, crect[3] if crect else sh)
                if gx2 - gx1 < 30 or gy2 - gy1 < 10:
                    m.is_self = None
                    m.sender_name = None
                    continue

                try:
                    img = grab_box((gx1, gy1, gx2, gy2))
                except Exception as e:
                    m.is_self = None
                    self._log("取图失败: %s: %s" % (type(e).__name__, e))
                    continue
                if img is None:
                    m.is_self = None
                    continue

                # ---- ① 主力判据：像素分析（单聊 / 群聊通用）
                is_self, lr, gr = analyze_side(img)
                m.is_self = is_self
                m.sender_name = None

                if is_self is True:
                    last_nickname = None       # 自己发的会打断「连续消息」链
                    done += 1
                    continue
                if is_self is None:
                    continue

                # ---- ② 别人发的
                done += 1
                if not self.use_nickname or not snap.is_group or self.ocr is None:
                    continue

                if row_h >= self.min_row_height and ocr_rows < self.max_rows:
                    # 先在本条**整行**上算两个判据的原料（判据①②，见文件上方说明）
                    row_arr = bg = blocks = None
                    try:
                        row_arr = np.asarray(img.convert("RGB")).astype(np.int16)
                        bg = estimate_bg(row_arr)
                        blocks = content_blocks(row_arr, bg)
                    except Exception:
                        row_arr, bg, blocks = None, None, None

                    crop_h = max(12, int((gy2 - gy1) * self.top_nick_ratio))
                    ocr_rows += 1
                    try:
                        top_img = grab_box((gx1, gy1, gx2, gy1 + crop_h))
                    except Exception:
                        top_img = None
                    if top_img is not None:
                        nick = self._pick_nickname(top_img, m.text,
                                                   row_arr, bg, blocks)
                        if nick:
                            if self._accept_nickname(snap.title, nick):
                                last_nickname = nick
                                m.sender_name = nick
                            else:
                                # ⚠️ 读到了但还不够可信（判据③没过）-> **不要继承**
                                #    上一个人的名字，那等于凭空认错人。
                                #    留成「对方」，等下条消息复读一次就认了。
                                last_nickname = None
                            continue

                # 行高小（别人连续发的第 2 条起）或没读出来 -> 继承上一条昵称
                if last_nickname:
                    m.sender_name = last_nickname
            return done
        finally:
            if topmost_done:
                for _ in range(3):
                    try:
                        win32gui.SetWindowPos(
                            hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
                        break
                    except Exception:
                        time.sleep(0.2)
