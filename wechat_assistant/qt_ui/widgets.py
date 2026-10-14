# -*- coding: utf-8 -*-
"""Qt 版自绘控件库。

和 Tk 版（`wechat_assistant/widgets.py`）**同名同语义**，方便逐个替换调用点。
但实现短得多 —— 圆角抗锯齿、悬停、滚动、文本换行在 Qt 里都是原生的。

⚠️ 对照 Tk 版**故意去掉**的东西（Qt 不需要）：
   `round_pts` / `round_rect`（自绘多边形）、`aa_round_image` /
   `aa_rect_item`（PIL 位图抗锯齿）、`_Throttle`（位图节流）、
   `_AA_CACHE` / `_rgb_rows`（位图喂 Tk）、`SCROLL_PAD` 的整套位图补丁。
   颜色、尺寸语义**保持不变**。
"""

from typing import Callable, Dict, List, Optional, Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from .base import Style, hand_cursor, px, qc


# ---------------------------------------------------------------- 通用

def _css_font(style: Style, name: str) -> str:
    """QSS 里的字体声明（族 + 像素字号）。"""
    f = style.font(name)
    return "font-family:'%s'; font-size:%dpx; font-weight:%s;" % (
        f.family(), f.pixelSize(), "bold" if f.bold() else "normal")


def repolish(w: QtWidgets.QWidget):
    """改了 stylesheet 之后强制重算样式（Qt 不会自动重刷）。"""
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


# ---------------------------------------------------------------- 胶囊按钮

class Pill(QtWidgets.QPushButton):
    """胶囊形按钮。微信主按钮就是这个形状。

    variant:
        "primary"   绿底白字（生成建议 / 保存）
        "secondary" 灰底（次要）
        "ghost"     透明底、淡描边
        "text"      **无底无边的纯文字按钮**（列表项未选中时就是这样）
        "selected"  **选中态**：淡绿底 + 绿字

    ⚠️ `selected` 和 `primary` **不是一回事**，别混用：
        微信里"被选中的列表项"是**淡色底 + 绿字**（一眼看出选中，但不抢眼）；
        "主操作按钮"才是**实心绿底 + 白字**。
        以前设置窗口左列写的是
        `set_variant("primary" if 选中 else "text")` ——
        选中项直接变成**实心绿胶囊**，摊在侧栏里非常突兀，
        那不是微信的样子（微信侧栏的选中项不是实心块）。
    """

    def __init__(self, style: Style, text: str, command: Optional[Callable] = None,
                 variant: str = "ghost", width: Optional[int] = None,
                 pad: Optional[int] = None, parent=None, expand: bool = False,
                 radius: Optional[int] = None, height: Optional[int] = None,
                 text_inset: Optional[int] = None,
                 **kw):
        super().__init__(text, parent)
        self.style = style
        self.variant = variant
        self._hover = False
        self._enabled = True
        self._expand = bool(expand)
        self._pad = px(34) if pad is None else px(pad)
        #: 非 None 时文字**左对齐**，并从这个左边距开始（物理像素）。
        #: 设置窗口左侧导航用（微信那边实测是左对齐的，见 `_apply`）。
        self._text_inset = text_inset

        self.setFont(style.font("button"))
        self.setCursor(hand_cursor())
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        # 高度：34 基准像素
        self._h = px(34) if height is None else int(height)
        self.setFixedHeight(self._h)
        # ⚠️ 圆角默认取 **`control` 档（6 基准像素）**，不是胶囊。
        #    原来是 `self._h // 2`（34 高的按钮 = 17，就是**胶囊**）——
        #    那是 Tk 版留下来的形状。用户 2026-09-27 明确要求
        #    「按钮、输入框、气泡使用**偏小圆角**，各处圆角数值不要混乱」，
        #    微信 PC 的按钮也确实是小圆角矩形而不是胶囊，所以收成 6。
        #    ⚠️ 要胶囊就显式传 `radius=style.R("pill")`；
        #       设置窗口左栏的选中块传的是 `radius=0`（实测微信那边是直角）。
        self._radius = style.R("control") if radius is None else int(radius)

        if width is not None:
            self.setFixedWidth(int(width))
        elif not self._expand:
            self._fit_width()
        else:
            # ⚠️ 拉伸型按钮**不能** `setFixedWidth` —— 那会把宽度锁死。
            #    Tk 版的主按钮是 `pack(fill="x")`（铺满整宽），
            #    Qt 版对应的是「水平方向 Expanding + 高度固定」。
            #    实测漏了这一步的后果：主按钮只有 **135px**（正好是
            #    "生成建议"四个字 + 内边距），孤零零贴在左边，
            #    而 Tk 版是整条横贯的 —— 一眼就看得出不是微信那种主按钮。
            self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                               QtWidgets.QSizePolicy.Fixed)
            self.setMinimumWidth(self._fit_width_value())

        if command is not None:
            self.clicked.connect(lambda: command())
        self._apply()

    # ---- 尺寸

    def _fit_width_value(self) -> int:
        fm = QtGui.QFontMetrics(self.font())
        return fm.horizontalAdvance(self.text()) + self._pad

    def _fit_width(self):
        self.setFixedWidth(self._fit_width_value())

    def _apply(self):
        p = self.style.p
        r = self._radius                     # 胶囊 or 直角（见 __init__）
        v = self.variant
        if not self._enabled:
            # ===== 禁用态（2026-09-27 用户报「生成中没有边框，颜色固定
            #       浅白色，只看对话也是」）=====
            # ⚠️ 原来这一行是 `bg, fg, bd = p["bg"], p["text_faint"], "transparent"`。
            #    两个后果叠在一起，出来的正好就是用户看到的样子：
            #      ① `p["bg"]` 是**面板自己的底色** —— 按钮于是和背景一模一样，
            #         边界消失，浅色下就是一片浅白（"颜色固定浅白色"）；
            #      ② 描边写死 `transparent` —— 连形状都不画了（"没有边框"）。
            #    而生成期间 `_apply_busy()` 会把主按钮和【只看对话】**一起**
            #    禁用，所以两颗同时变成没有边界的浅白色块 ——
            #    「只看对话也是」这条就是这么来的，不是它单独有问题。
            # ⚠️ 量出来的证据（改之前，`probes/qt/diag_buttons_state.py`）：
            #    浅色下禁用态渲染出来的顶边 = 洋红与 `#FAFAFA` 的 50% 混色
            #    = 描边区域**是透明的**，也就是**没有边框**。
            # → 现在：底色照变体褪色、**描边一律保留**、文字先保证读得清。
            if v == "primary":
                # 主按钮的禁用态＝"褪色的绿"，而不是"变成底色"。
                # 它仍然是绿的，所以一眼还认得出"这是生成按钮、正在忙"。
                bg = _mix(p["accent"], p["bg"], 0.55)
                fg = p["accent_text"]
                if _contrast_ratio(fg, bg) < _MIN_LABEL_CONTRAST:
                    # ⚠️ 浅色下把绿往浅底褪会得到**很亮**的薄荷绿，
                    #    白字压上去只有 1.6:1（`probes` 里那把尺子算的），
                    #    所以这里跟 `selected` 一样：不够读就把字压深成深绿。
                    fg = _darken_until_readable(p["accent"], bg)
                bd = "transparent"
            else:
                # 次按钮/幽灵按钮：底色退回表面色，但**描边跟着保留** ——
                # 有描边才看得出"这里有一个按钮，只是现在不能点"。
                bg = p["surface"]
                fg = p["text_faint"]
                bd = p["border"]
        elif v == "primary":
            bg = p["accent"] if not self._hover else _mix(p["accent"], "#FFFFFF", 0.12)
            fg, bd = p["accent_text"], "transparent"
        elif v == "navsel":
            # ===== 设置窗口左侧导航的**选中项**（2026-09-27 按微信实测）=====
            # ⚠️ 这一档跟 `selected` **不是一回事**，别合并：
            #    用户报「微信设置的颜色与字体高亮并不是微信设置的颜色」，
            #    实测微信设置窗口左侧的选中项是**一块实心绿**
            #    （`probes/qt/analyze_settings_nav.py` 量出来的）：
            #       底色 #0DA869（占该块 93.3% 的像素，就是 `accent_dim`）
            #       文字 #E7F6F0（近白、带一点绿）
            #       形状 **240x60 的直角矩形**，铺满导航栏宽度
            #    而 `selected` 是"淡绿底 + 绿字"的胶囊。
            #
            # ⚠️ **上一轮的判断是错的**：当时写的是"微信侧栏的选中项不是实心块"。
            #    那个印象来自**聊天窗口的会话列表**（那边确实是很淡的灰），
            #    但**设置窗口**是另一回事 —— 微信自己在两个界面用了两种选中样式，
            #    所以这里分成两个 variant 不是冗余，是在照抄它。
            bg = p["accent_dim"] if not self._hover else _mix(
                p["accent_dim"], "#FFFFFF", 0.10)
            fg, bd = p["nav_sel_fg"], "transparent"
        elif v == "selected":
            # 选中态：**淡绿底 + 绿字**。
            #
            # ⚠️ 混色比例是**量出来的**，不是拍的：
            #    0.15 时算出来是 #284537 —— 一个偏暗的墨绿，跟未选中的
            #    侧栏底色贴得太近，"选中"不够跳。0.30 之后是 #276142，
            #    绿味明确、又不至于像主按钮那样是**实心亮绿**。
            #    两个色板都验过（浅色下同理）。
            bg = _mix(p["surface"], p["accent"], 0.30)
            if self._hover:
                bg = _mix(p["surface"], p["accent"], 0.40)
            # ⚠️ 字色要**跟着底色选**，不能无脑用 `accent`。
            #    实测（`probes/qt/audit_qss.py` 第 6 节）：
            #      深色：accent #01C260 压在 #215B3E 上 = 3.38:1  → 能读
            #      浅色：accent #07C160 压在 #B5ECCF 上 = **1.80:1** → 读不清
            #    浅色下底色是"白 + 一点点绿"（很亮），而 accent 也是个亮绿，
            #    两个亮色叠在一起就没有对比了 —— 而这两套色板都要能用
            #    （`config.yaml` 的 `theme: auto` 会跟着微信切）。
            #    修法：对比度不够就把 accent 压深，得到一个**深绿**字。
            #    深色下不会走到这一支（3.38 已经够），所以观感不变。
            fg = p["accent"]
            if _contrast_ratio(fg, bg) < _MIN_LABEL_CONTRAST:
                fg = _darken_until_readable(fg, bg)
            bd = "transparent"
        elif v == "secondary":
            bg = p["surface"] if not self._hover else p["hover"]
            fg, bd = p["text"], "transparent"
        elif v == "ghost":
            bg = p["surface"] if not self._hover else p["hover"]
            fg, bd = p["text"], p["border"]
        else:  # text
            bg = "transparent" if not self._hover else p["hover"]
            fg = p["text_dim"] if not self._hover else p["text"]
            bd = "transparent"
        # ⚠️ **这里必须用单花括号 `{` `}`。**
        #    之前写的是 `{{` `}}`（`.format()` 风格的转义），但下面用的是
        #    **`%` 格式化**，`%` 不认识 `{{` —— 于是双括号**原样留在 CSS 里**，
        #    变成 `QPushButton {{`，Qt 的样式表解析器**整条失败**，
        #    静默退回系统默认外观（浅灰白按钮）。
        #    实测（把按钮渲染到红底上看）：
        #        双括号 → #FCFCFC（系统默认）      单括号 → #01C260（绿了）
        #    症状就是用户报的「生成建议不是绿的」——
        #    而**审计读 `styleSheet()` 里的 `#01C260` 是能读到的**（字符串在，
        #    只是解析失败），所以只查"字符串里有没有绿"会漏掉这个 bug。
        # ⚠️ 文字对齐：默认居中（`QPushButton` 的原生行为）。
        #    但**设置窗口左侧导航**是要左对齐的 —— 实测微信的设置导航
        #    （`probes/qt/analyze_settings_nav.py`，把"选中项里比绿底亮的像素"
        #    找出来得到文字横向范围）：
        #        绿块 x 11~250，文字 x 38~181
        #        → 左边距 **27 物理px（≈18 基准px）**，右边距 69
        #    右边距远大于左边距 = **明显左对齐**，不是居中。
        #    ⚠️ 不设的话文字会居中，一眼就能看出跟微信不一样。
        if self._text_inset is None:
            align_css = "text-align:center; padding:0 %dpx;" % px(10)
        else:
            align_css = ("text-align:left; padding-left:%dpx; padding-right:0px;"
                         % int(self._text_inset))
        self.setStyleSheet("""
            QPushButton {
                %s
                background:%s; color:%s;
                border:1px solid %s; border-radius:%dpx;
                %s
            }
        """ % (_css_font(self.style, "button"), bg, fg, bd, r, align_css))
        self.setCursor(hand_cursor() if self._enabled else QtCore.Qt.ArrowCursor)
        # ⚠️ 把算出来的三个色**存下来**。探针和测试要验"选中态的字读不读得清"，
        #    必须拿到**真正生效的那个色**，而不是在外面按同一套规则再推一遍 ——
        #    推的那份会跟这里漂移，然后一起错。
        self._fg_color, self._bg_color, self._bd_color = fg, bg, bd

    def colors(self):
        """返回当前生效的 `(bg, fg, border)`。给探针/测试读，别在界面代码里用。"""
        return self._bg_color, self._fg_color, self._bd_color

    # ---- 悬停 / 状态

    def enterEvent(self, ev):
        self._hover = True
        self._apply()

    def leaveEvent(self, ev):
        self._hover = False
        self._apply()

    def set_text(self, text: str):
        super().setText(text)
        # 拉伸型按钮的宽度由布局给，只需要保持一个够放下最小文案的下限
        if self._expand:
            self.setMinimumWidth(self._fit_width_value())
        elif self.minimumWidth() == self.maximumWidth():
            self._fit_width()
        self._apply()

    def text(self) -> str:                     # noqa: A003
        return super().text()

    def set_variant(self, variant: str):
        """运行时换样式（设置界面用它做「选中/未选中」的选项组）。"""
        self.variant = variant
        self._apply()

    def set_enabled(self, ok: bool):
        self._enabled = bool(ok)
        self.setEnabled(bool(ok))
        self._apply()

    def set_checked(self, on: bool):
        """选中态。

        ⚠️ 用 `selected`（淡绿底 + 绿字），**不是** `primary`（实心绿底 + 白字）——
            两者在微信里是分开的两种视觉：列表项选中 vs 主操作按钮。
            见类文档的说明。
        """
        self.set_variant("selected" if on else "text")


#: 选中态文字跟它自己底色的对比度下限（WCAG 对正文的推荐是 4.5，
#: 对"大号/粗体"是 3.0 —— 侧栏按钮的字够大，按 3.0 卡）。
_MIN_LABEL_CONTRAST = 3.0


def _lum(h: str) -> float:
    """WCAG 相对亮度。"""
    def f(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    except Exception:
        return 0.0
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast_ratio(c1: str, c2: str) -> float:
    """两个色号的对比度（1.0 ~ 21.0）。"""
    a, b = _lum(c1), _lum(c2)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _darken_until_readable(fg: str, bg: str, steps: int = 20) -> str:
    """把 `fg` 往黑里压，直到它压在 `bg` 上够读。

    ⚠️ **方向只能是"压深"**：选中态的底色在浅色主题里是"白 + 一点点绿"，
       很亮；亮色文字压上去没有对比。往白里压只会更糟。
    ⚠️ 压到极限还是不够就把最深的那个返回 —— 不抛异常、不死循环，
       剩下的交给探针去报（这里静默兜底，探针那里是有数字的）。
    """
    best = fg
    for i in range(1, steps + 1):
        cur = _mix(fg, "#000000", i / float(steps))
        if _contrast_ratio(cur, bg) >= _MIN_LABEL_CONTRAST:
            return cur
        best = cur
    return best


def _mix(c1: str, c2: str, t: float) -> str:
    """把两个 #RRGGBB 按 t 混合（t=0 取 c1）。"""
    try:
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        m = [int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3)]
        return "#%02X%02X%02X" % tuple(m)
    except Exception:
        return c1
