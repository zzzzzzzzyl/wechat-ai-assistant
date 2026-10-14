# -*- coding: utf-8 -*-
"""Qt 版自绘控件库（续）—— 卡片 / 开关 / 输入框 / 滚动区 / 换行组 / 圆角面板。

和 Tk 版（`wechat_assistant/widgets.py`）**同名同语义**，方便逐个替换调用点。

⚠️ 对照 Tk 版**故意去掉**的东西（Qt 原生就有，不用再补）：
   `round_pts` / `round_rect`（自绘多边形）、`aa_round_image` /
   `aa_rect_item` / `aa_shell_image`（PIL 位图抗锯齿）、`_Throttle`（位图节流）、
   `_AA_CACHE` / `_rgb_rows`（喂 Tk 的位图数据）、
   `SCROLL_PAD` 那一整套「内容别被浮层滚动条盖住」的补丁。
   色号、尺寸、对外方法名**保持不变**。
"""

from typing import Callable, Dict, List, Optional, Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from .base import Style, hand_cursor, px, qc
from .widgets import _mix


# ---------------------------------------------------------------- 圆角卡片

class Card(QtWidgets.QFrame):
    """一条回复建议。**整块可点**（点一下就复制）。

    为什么点整块就能复制：微信里点气泡是本能动作，而原版要精确去点一个
    6 字符宽的小按钮。悬停时右侧才浮出「复制」两个字，静止时界面干净。
    """

    PAD_X = 12
    PAD_Y = 10

    def __init__(self, style: Style, text: str, index: int = 0,
                 on_click: Optional[Callable[[str], None]] = None,
                 width_px: int = 300, parent=None, **kw):
        super().__init__(parent)
        self.style = style
        self.text_value = text or "（空）"
        self.index = index
        self._on_click = on_click
        self._hover = False
        self._flash = False
        # ⚠️ 卡片（候选回复气泡）的圆角走 `control` 档（6 基准像素）。
        #    原来是 `px(8)` —— 用户要求「气泡使用偏小圆角，各处圆角不要混乱」，
        #    所以跟按钮/输入框统一到同一档。
        self._radius = style.R("control")

        self.setCursor(hand_cursor())
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                           QtWidgets.QSizePolicy.Minimum)

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(style.px(self.PAD_X + 26), style.px(self.PAD_Y),
                               style.px(self.PAD_X), style.px(self.PAD_Y))
        lay.setSpacing(style.px(6))

        self.lbl_num = QtWidgets.QLabel(str(index) if index else "")
        self.lbl_num.setFont(style.font("label"))
        self.lbl_num.setFixedWidth(style.px(20))
        self.lbl_num.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        lay.addWidget(self.lbl_num, 0, QtCore.Qt.AlignTop)

        self.lbl_text = QtWidgets.QLabel(self.text_value)
        self.lbl_text.setFont(style.font("body"))
        self.lbl_text.setWordWrap(True)          # ⚠️ 自动换行，不用手算 wraplength
        self.lbl_text.setTextInteractionFlags(QtCore.Qt.NoTextInteraction)
        # ⚠️ 给正文一个**小的**最小宽度就够了，别按卡片目标宽度去设。
        #    原因（实测）：卡片目标宽度是"建的那一刻的视口宽"，
        #    而滚动条一出现视口就会变窄 9px；把 `setMinimumWidth(目标宽)`
        #    写在卡片上等于给它钉了一个**再也缩不回去**的下限 ——
        #    表现是"卡片右边有 9px 挂在视口外面"。
        #    设成一个小值之后，卡片随布局自由伸缩，宽度永远等于可用宽。
        self.lbl_text.setMinimumWidth(style.px(60))
        lay.addWidget(self.lbl_text, 1)

        self.lbl_act = QtWidgets.QLabel("")
        self.lbl_act.setFont(style.font("label"))
        self.lbl_act.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom)
        lay.addWidget(self.lbl_act, 0, QtCore.Qt.AlignBottom)

        if width_px:
            # 兼容外壳：调用点还在传目标宽度，但**不要**拿它当最小宽度
            # （见上面 lbl_text 的说明）。宽度交给布局。
            self.setMinimumWidth(min(int(width_px), style.px(120)))
        self._apply()

    # ---- 对外

    @property
    def text(self) -> str:
        return self.text_value

    def set_width(self, w: int):
        """宽度由布局给（`fill="x"` 等价于横向拉伸）。

        ⚠️ 不能再写成 `setMinimumWidth(w)` —— 那会把卡片钉死在"调用时那一刻"
            的宽度上，之后视口变窄它缩不回去（实测右边溢出 9px）。
        """
        self.setMinimumWidth(min(max(120, int(w)), self.style.px(120)))
        self._apply()

    def flash(self, ms: int = 900):
        """复制成功后闪一下 —— 反馈要发生在手指点的地方，不是屏幕底部。"""
        self._flash = True
        self._apply()
        QtCore.QTimer.singleShot(ms, self._unflash)

    def _unflash(self):
        self._flash = False
        self._apply()

    # ---- 交互

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton and self._on_click:
            self._on_click(self.text_value)
        super().mousePressEvent(ev)

    def enterEvent(self, ev):
        self._hover = True
        self._apply()

    def leaveEvent(self, ev):
        self._hover = False
        self._apply()

    def _apply(self):
        p = self.style.p
        # ⚠️⚠️ **`QLabel` 规则里必须显式写 `border:none`**（2026-09-27 修）。
        #    这里踩的是 Qt 样式表的一个经典坑：
        #      `QLabel` **是 `QFrame` 的子类**，而不带点的类型选择器
        #      `QFrame` 会**把子类一起选中** —— 于是每条标签也被套上了
        #      上面那条规则的 `border:1px solid <card_line>`。
        #      第二条只把 `background` 改回透明，**border 没清**，
        #      所以每个标签外面都多出一圈灰框。
        #    症状就是用户报的「三个气泡文字有灰色框，复制也有」
        #    （「复制」是 `lbl_act`，同样是 QLabel）。
        #    ⚠️ 别改成 `.QFrame`（带点 = 只匹配 QFrame 自己）—— `Card` 本身
        #       就是 `QFrame` 的子类，那样连卡片自己的描边都没了。
        #    实测证据：`probes/qt/diag_card_boxes.py` 数卡片内部成行的
        #    `card_line` 像素，修之前 y=16/97/134 各有几百个。
        if self._flash:
            bg, fg, line = p["accent"], p["accent_text"], p["accent"]
        else:
            # 悬停时底色往文字色靠一点点（和 Tk 版 mix(surface, text, 0.06) 一致）
            bg = _mix(p["surface"], p["text"], 0.06) if self._hover else p["surface"]
            fg = p["text"]
            # ⚠️ 描边用 `card_line` 而不是 `surface`。
            #    Tk 版这里是 `border:1px solid surface` —— 在深色下等于**没有描边**
            #    （和底色同色）。浅色下卡片是白底铺在 #FAFAFA 上，
            #    没有一圈稍深的边就**看不出卡片边界**。
            line = p.get("card_line", p["surface"])
        self.setStyleSheet("""
            QFrame {
                background:%s; border:1px solid %s; border-radius:%dpx;
            }
            QLabel { color:%s; background:transparent; border:none; }
        """ % (bg, line, self._radius,
               fg if not self._flash else p["accent_text"]))
        self.lbl_num.setStyleSheet("color:%s; background:transparent;" %
                                   (p["accent_text"] if self._flash
                                    else p["text_faint"]))
        self.lbl_act.setStyleSheet("color:%s; background:transparent;" %
                                   (p["accent_text"] if self._flash
                                    else p["text_dim"]))
        self.lbl_act.setText("复制" if self._hover and not self._flash else "")


# ---------------------------------------------------------------- 开关

class Switch(QtWidgets.QAbstractButton):
    """微信风格的开关（绿=开）。设置界面用。

    和 Tk 版一样：**高度 22 基准像素、宽度 40**，胶囊形状。
    Qt 里用 `QAbstractButton` + `paintEvent` 自绘，抗锯齿是白送的。
    """

    def __init__(self, style: Style, value: bool = False, command=None,
                 parent=None, **kw):
        super().__init__(parent)
        self.style = style
        self._value = bool(value)
        self._command = command
        self.setCheckable(True)
        self.setChecked(self._value)
        self.setCursor(hand_cursor())
        self.setFixedSize(style.px(40), style.px(22))
        self._w = style.px(40)
        self._h = style.px(22)
        self.toggled.connect(self._on_toggled)

    def _on_toggled(self, on: bool):
        self._value = bool(on)
        self.update()
        if self._command:
            self._command(self._value)

    # ---- 对外（和 Tk 版同名）

    def get(self) -> bool:
        return self._value

    def set(self, v: bool):
        self._value = bool(v)
        if self.isChecked() != self._value:
            self.blockSignals(True)
            self.setChecked(self._value)
            self.blockSignals(False)
        self.update()

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(self._w, self._h)

    # ---- 绘制

    def paintEvent(self, ev):
        p = self.style.p
        pt = QtGui.QPainter(self)
        pt.setRenderHint(QtGui.QPainter.Antialiasing, True)   # ← Tk 里要位图才有
        r = self._h / 2.0
        track = QtCore.QRectF(0.5, 0.5, self._w - 1, self._h - 1)
        # ⚠️⚠️ 关态**不能填 `surface`、滑块更不能写死白**（2026-10-14 用户报
        #    「胶囊开关在浅色时显示不明显」）：浅色下 `surface` 就是 `#FFFFFF`，
        #    于是白轨道 + 白滑块 = 只剩 1px 边框，开关几乎看不见。
        #    现在走两个**语义键**（`theme.py` 里有注释和实测亮度差）：
        #      `switch_off` 轨道底色（浅色是一档灰）、`switch_knob` 滑块色。
        #    开态保持品牌绿 + 白滑块（两套主题都够对比）。
        _track = p["accent"] if self._value else p.get("switch_off", p["surface"])
        pt.setPen(QtGui.QPen(qc(_track), 1))
        pt.setBrush(qc(_track))
        pt.drawRoundedRect(track, r, r)

        d = self._h - self.style.px(4)
        pad = self.style.px(2)
        x = (self._w - d - pad) if self._value else pad
        pt.setPen(QtCore.Qt.NoPen)
        pt.setBrush(qc(p.get("switch_knob", "#FFFFFF")))
        pt.drawEllipse(QtCore.QRectF(x, pad, d, d))


# ---------------------------------------------------------------- 圆角输入框

