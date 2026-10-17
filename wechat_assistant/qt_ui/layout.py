# -*- coding: utf-8 -*-
"""Qt 版布局类控件：滚动区 / 换行组 / 圆角面板。

⚠️ 这一层是 Tk 版补丁**删得最多**的地方：

· **滚动条**：Tk 那边是「Canvas + 自己 place 一条浮层指示条」，
  为此要给滚动区里的每张卡片右侧留 `SCROLL_PAD`，否则内容被盖住
  （用户报的「三条回复右边被吃了一部分」）。Qt 用 `QScrollArea`，
  滚动条是**独立部件**、不压内容，这一整套补丁没了。
· **换行**：Tk 的 `tk.Label` 不吃 `wraplength` 超宽就静默裁字，
  所以要 `WrapGroup` 在 `<Configure>` 里手算宽度。Qt 的 `QLabel`
  有 `setWordWrap(True)`，文字跟着可用宽度自己换。
· **圆角窗口**：Tk 要 `overrideredirect` + 透明色键 + 自绘多边形 + 位图抗锯齿；
  Qt 用 `WA_TranslucentBackground` + 一次 `drawRoundedRect`。
"""

from typing import Callable, List, Optional, Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from .base import Style, hand_cursor, px, qc


# ---------------------------------------------------------------- 滚动区

class ScrollArea(QtWidgets.QScrollArea):
    """可滚动区域。

    ⚠️ **不再需要 `SCROLL_PAD`**：Qt 的滚动条是独立部件（占自己的宽度），
       不像 Tk 版那样浮在内容上面。内容不会被盖住。
       （对外仍保留 `content_width()` 等方法名，方便调用点少改。）
    """

    def __init__(self, style: Style, bg_key: str = "bg", parent=None, **kw):
        super().__init__(parent)
        self.style = style
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        p = style.p
        self.setStyleSheet("""
            QScrollArea { background:%s; border:none; }
            QScrollBar:vertical {
                background:transparent; width:%dpx; margin:0;
            }
            QScrollBar::handle:vertical {
                background:%s; border-radius:%dpx; min-height:%dpx;
            }
            QScrollBar::handle:vertical:hover { background:%s; }
            QScrollBar::add-line, QScrollBar::sub-line { height:0; }
            QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }
        """ % (p[bg_key], style.px(6), p["border"], style.R("chip"),
               style.px(24), p["text_faint"]))

        self.inner = QtWidgets.QWidget()
        self.inner.setStyleSheet("background:%s;" % p[bg_key])
        self._lay = QtWidgets.QVBoxLayout(self.inner)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        self._lay.addStretch(1)          # 末尾留一个弹簧，内容贴顶
        self.setWidget(self.inner)
        # 宽度跟死视口，别让内容横着溢出（见 `_sync_width`）
        self.viewport().installEventFilter(self)
        # ⚠️ **滚动条要钉死自己的光标**。
        #    不设的话 Qt 会**沿父链继承**窗口的光标：设置窗口是无边框的，
        #    鼠标移到窗口右边缘时它把自己的光标设成 `SizeHorCursor`（左右拉伸），
        #    滚动条没有自己的光标 → 跟着变成"左右拉伸"。
        #    用户报的「鼠标放在拉动条上时左右拉伸的样式」就是这条。
        #    （实测确认：窗口设成 SizeHorCursor 后，`sb.cursor().shape()`
        #      跟着变成 SizeHorCursor。）
        #    光标**只影响外观**，不影响拖动/拉伸功能。
        self.verticalScrollBar().setCursor(QtCore.Qt.ArrowCursor)
        self.viewport().setCursor(QtCore.Qt.ArrowCursor)

    # ---- 内容宽度（Tk 版那套 SCROLL_PAD 的替代品）
    #
    # ⚠️ **必须把内容宽度钉在视口宽度上。**
    #    实测踩到：卡片建出来是 **470px**，而视口只有 **461px** ——
    #    右边有 **9px（正好一个滚动条宽）挂在视口外面**。
    #    原因是 `setWidgetResizable(True)` 只保证"内容不窄于视口"，
    #    **不保证不宽于**：卡片的 minimumWidth 一顶，inner 就被撑到 470，
    #    于是出现横向可滚动区（策略是 AlwaysOff，所以**没有滚动条可拉**，
    #    就是纯丢失 9px，而且 `content_width()` 还报 461，两边对不上）。
    #
    #    这与 Tk 版踩的是**同一类**问题（"三条回复右边被吃了一部分"），
    #    只是成因从"浮层滚动条盖住"变成"内容被撑宽"。修法也更干净：
    #    不需要给卡片留 SCROLL_PAD，直接把 inner 钉死。

    def _sync_width(self):
        """把内容的**最小宽度**钉在视口宽度上。

        ⚠️ 用 `setMinimumWidth` 而**不是** `setFixedWidth`。
            一开始写的是 fixed，结果把自己卡死了：卡片建出来时视口是 470
            （滚动条还没出现），卡片的 `setMinimumWidth(470)` 就被传给 inner
            当最小宽度；等内容多起来滚动条一出现、视口缩到 461 时，
            **inner 已经被自己的最小宽度锁在 470，缩不回去了** ——
            实测表现是"卡片右边有 9px（正好一个滚动条宽）挂在视口外面，
            而且横向还滚不动"。

            最小宽度是可缩的（`min(w, 现有值)`），不会锁死；
            而"内容不窄于视口、也不宽于视口"两件事靠最小宽度就都够了。
        """
        vw = self.viewport().width()
        if vw <= 0:
            return
        cur_min = self.inner.minimumWidth()
        if self.inner.width() != vw or cur_min != vw:
            self.inner.setFixedWidth(vw)   # 先摆正（含宽度上限）
            self.inner.setMaximumWidth(16777215)
            self.inner.setMinimumWidth(vw)

    def eventFilter(self, obj, ev):
        if obj is self.viewport() and ev.type() in (QtCore.QEvent.Resize,
                                                    QtCore.QEvent.Show):
            self._sync_width()
        return super().eventFilter(obj, ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._sync_width()

    # ---- 对外（和 Tk 版同名，调用点少改）

    def can_scroll(self) -> bool:
        return (self.verticalScrollBar().maximum() > 0)

    def content_height(self) -> int:
        return int(self.inner.sizeHint().height())

    def content_width(self) -> int:
        """内容可用宽度。

        ⚠️ 这是**内容区**的宽度（已经扣掉滚动条），所以卡片直接铺满即可 ——
           Tk 版要额外减 `SCROLL_PAD`，这里不需要。
        """
        w = self.viewport().width()
        return int(w) if w > 1 else 0

    def scroll_by(self, dy_px: float):
        sb = self.verticalScrollBar()
        sb.setValue(int(sb.value() + dy_px))

    def scroll_pages(self, n: float):
        sb = self.verticalScrollBar()
        sb.setValue(int(sb.value() + n * self.viewport().height() * 0.9))

    def to_top(self):
        self.verticalScrollBar().setValue(0)

    def to_bottom(self):
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def bind_wheel_deep(self, widget):
        """兼容外壳：Qt 的滚轮事件本来就冒泡到 QScrollArea，不用逐个绑。"""
        return

    def add(self, widget: QtWidgets.QWidget, stretch: int = 0):
        """往内容区尾部加一个部件（弹簧永远保持在最后）。"""
        self._lay.insertWidget(self._lay.count() - 1, widget, stretch)
        return widget

    def add_layout(self, lay: QtWidgets.QLayout):
        self._lay.insertLayout(self._lay.count() - 1, lay)
        return lay

    def clear(self):
        """清空内容区（保留末尾弹簧）。

        ⚠️⚠️ **必须连"嵌套布局里的部件"一起删**（2026-09-27 修，这是个大坑）。
            回复卡片不是直接 `add(部件)` 进去的，而是
            `add_layout(小布局)` 包了一层 —— 于是那些条目
            `item.widget()` 是 **None**。原来只写了
                w = item.widget()
                if w is not None: ...
            那一支什么都不做：**卡片没被删、也没被隐藏**，
            只是从布局里摘掉了，仍然挂在 `inner` 上**按原几何继续画**。
            症状：用户报「换会话后顶部能识别，但气泡纹丝不动」——
            日志证明切换检测到了、`_render_replies([], ...)` 也执行了，
            可屏幕上的三张卡片一点没变。

        ⚠️ 更阴的是：**`content_height()` 此时会报 0**（孤儿已经不在布局里了），
            拿它当"清没清干净"的判据会得到一个**假绿** ——
            我上一版的测试就是这么过的。判据必须是
            "inner 上还挂着几个可见的 Card"（见 `diag_scroll_clear.py`）。
        """
        while self._lay.count() > 1:
            self._dispose_item(self._lay.takeAt(0))

    def _dispose_item(self, item):
        """把一个布局条目连同它里面的部件一起拆掉（递归）。"""
        if item is None:
            return
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
            return
        sub = item.layout()
        if sub is not None:
            # ⚠️ 递归：卡片外面那层 QVBoxLayout 就是走这一支
            while sub.count():
                self._dispose_item(sub.takeAt(0))
            sub.deleteLater()
        # 剩下的（弹簧等）没有部件，丢掉即可


# ---------------------------------------------------------------- 换行组

class WrapGroup:
    """一组**跟着容器宽度自动换行**的提示文字。

    ⚠️ 在 Qt 里这几乎是个空壳：`QLabel.setWordWrap(True)` 本来就是原生的，
       不像 Tk 要手算 `wraplength`（超宽还静默裁字）。
       保留同名同方法，是为了让现有调用点**一行都不用改**就能迁移。
    """

    def __init__(self, style: Style, container, inset: int = 18,
                 min_px: int = 160, **kw):
        self.style = style
        self.container = container
        self.inset = inset
        self.min_px = min_px
        self.labels: List[QtWidgets.QLabel] = []

    def width(self) -> int:
        st = self.style
        w = int(self.container.width()) if self.container is not None else 0
        if w <= 1:
            return st.px(360)
        return max(st.px(self.min_px), w - st.px(self.inset))

    def add(self, parent, text: str, font_name: str = "small",
            color_key: str = "text_faint", **kw) -> QtWidgets.QLabel:
        lab = QtWidgets.QLabel(text)
        lab.setFont(self.style.font(font_name))
        lab.setStyleSheet("color:%s; background:transparent;"
                          % self.style.p[color_key])
        lab.setWordWrap(True)          # ← Qt 原生，不用算 wraplength
        self.labels.append(lab)
        return lab

    def refresh(self, _=None):
        """兼容外壳：Qt 自己会重排，无事可做。"""
        return

    def clear(self):
        self.labels = []


# ---------------------------------------------------------------- 圆角面板

def corner_radius(style: Style, override: Optional[int] = None) -> float:
    from .. import theme
    if override is not None:
        return float(style.px(override))
    return float(style.px(theme.SHELL_RADIUS))


def repolish(w: QtWidgets.QWidget):
    """凡是要在 QSS 里用 `border-radius` 裁剪子部件的，得先开这个属性。

    ⚠️ 普通 `QWidget` 默认**不画** QSS 里的 background/border ——
       必须 `setAttribute(Qt.WA_StyledBackground, True)` 才会走样式表那条绘制路径。
       `QLabel` / `QPushButton` 这类自带绘制的控件不受影响。
    """
    w.setAttribute(QtCore.Qt.WA_StyledBackground, True)


def round_corners_css(name: str, radius: int, top: bool = True,
                      bottom: bool = True, bg: str = None) -> str:
    """生成"只圆上面/下面两个角"的 QSS（给内容层的首尾部件用）。

    ⚠️ 为什么需要**只圆两个角**（用户提的「四角漏底层底色」）：
        窗口是无边框 + 自绘圆角的（`Panel.paintEvent`）。内容层是**方形**的，
        它按根布局的边距内缩，但**内缩多少都补不平圆角** ——
        只要内缩量小于 `r`，内容自己的那个方角就会盖到窗口的圆角弧线外面，
        看起来就是"窗口的圆角被啃掉一块/漏出底色"。
        缩进到 `0.35r` 能躲开，但那就等于**四周留了一圈白**（用户也不想要）。

    ✅ 正解：让内容层**自己也有圆角**。
        四个窗口角里，内容真正会碰到"上面两个"和"下面两个" ——
        中间的部件左右边缘是直的，碰不到角。
        所以只要把**最上面那个**部件圆上两个角、**最下面那个**圆下两个角，
        缩进就能压到几何最小值（外框线宽度），既不漏底色、又贴得紧。
    """
    css = ""
    if bg:
        css += "background:%s;" % bg
    if top:
        css += "border-top-left-radius:%dpx; border-top-right-radius:%dpx;" % (
            radius, radius)
    if bottom:
        css += ("border-bottom-left-radius:%dpx;"
                " border-bottom-right-radius:%dpx;") % (radius, radius)
    return css


def fill_rounded(painter: QtGui.QPainter, rect: QtCore.QRectF, color: str,
                 radius: float, top: bool = True, bottom: bool = True):
    """在 `painter` 上按"只圆上面/下面两个角"铺一块底色（自绘部件用）。

    `round_corners_css` 的**自绘版**：`QLabel` 走样式表，而
    `TitleBar` 这类自己 `paintEvent` 的部件得用这个。

    ⚠️ 用 `QPainterPath` 手工拼角，别用 `addRoundedRect` ——
       那个只能四个角一起圆，做不到"上圆下不圆"。
    """
    path = QtGui.QPainterPath()
    r = max(0.0, float(radius))
    x0, y0 = rect.left(), rect.top()
    x1, y1 = rect.right(), rect.bottom()
    if r <= 0:
        path.addRect(rect)
    else:
        r = min(r, rect.width() / 2.0, rect.height() / 2.0)
        path.moveTo(x0 + (r if top else 0), y0)
        path.lineTo(x1 - (r if top else 0), y0)
        if top:
            path.arcTo(x1 - 2 * r, y0, 2 * r, 2 * r, 90, -90)
        path.lineTo(x1, y1 - (r if bottom else 0))
        if bottom:
            path.arcTo(x1 - 2 * r, y1 - 2 * r, 2 * r, 2 * r, 0, -90)
        path.lineTo(x0 + (r if bottom else 0), y1)
        if bottom:
            path.arcTo(x0, y1 - 2 * r, 2 * r, 2 * r, 270, -90)
        path.lineTo(x0, y0 + (r if top else 0))
        if top:
            path.arcTo(x0, y0, 2 * r, 2 * r, 180, -90)
        path.closeSubpath()
    painter.setPen(QtCore.Qt.NoPen)
    painter.fillPath(path, qc(color))


def paint_shell(widget, painter, color: str, radius: float, border_w: float,
                border_color: str):
    """画无边框窗口的圆角外壳 + 外框线。

    ⚠️ **抽出来是为了"风格一致"是结构性的，而不是把同一段抄两遍**
        （用户要求：看提示词的弹窗和"恢复默认"的弹窗风格要一致）。
        抄两遍的后果是：改了一处、另一处悄悄留在旧样子。
    ⚠️ `Antialiasing` 必须开 —— Tk 版为了这一行写过一整套 PIL 位图补丁，
        Qt 里就是这一句。
    """
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    w, h = widget.width(), widget.height()
    half = border_w / 2.0
    rect = QtCore.QRectF(half, half, w - border_w, h - border_w)
    path = QtGui.QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.fillPath(path, qc(color))
    if border_w > 0:
        pen = QtGui.QPen(qc(border_color))
        pen.setWidthF(border_w)
        painter.setPen(pen)
        painter.drawPath(path)


class ShellDialog(QtWidgets.QDialog):
    """**无边框 + 真圆角**的对话框：跟悬浮窗 / 设置窗口**同一套外壳**。

    ⚠️ 为什么要这个类（用户 2026-09-27 提的两件事）：
        ① 「不要 python 的边框」—— 原来的「看提示词」是个裸 `QWidget`，
           于是**吃系统的标题栏和边框**，跟自己的界面格格不入；
        ② 「看提示词与设置里面恢复默认的弹窗，风格也要一致」——
           那两个"恢复默认 / 报错"用的是 `QMessageBox`（系统外观 + 问号图标），
           第三个样子。
        → 统一到这里：`WA_TranslucentBackground` + `paint_shell()` 一次
          `drawRoundedRect`（抗锯齿白送）+ 跟面板一样宽的外框线。

    ⚠️ 它继承 `QDialog`（不是 `Panel`）是**为了 `exec()`**：
        调用点要的是"弹出来、等用户点、拿到结果"的同步语义，
        `QDialog.exec()` 是这个语义的原生实现，自己拿 QEventLoop 模拟容易出岔子。

    ⚠️ 内容层缩进到外框线以内（跟 `_SettingsFrame._content_inset` 同一个算法）。
        这里的内容基本都是**透明**的（文字、胶囊按钮），所以不存在
        "方角盖住圆角"的问题 —— 不用像标题栏那样再给内容层补圆角。
    """

    def __init__(self, style: Style, title: str = "", parent=None,
                 modal: bool = True, min_width: Optional[float] = None):
        super().__init__(parent)
        from .. import theme
        self.style = style
        self.radius = float(style.px(theme.SHELL_RADIUS))
        self.border_w = float(style.px(
            theme.shell_border_width(style.theme_name)))

        self.setModal(modal)
        self.setWindowTitle(title or "提示")
        self.setWindowFlag(QtCore.Qt.FramelessWindowHint, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self._drag = None

        # ⚠️ 2026-10-14：**最小宽度**（用户报「恢复默认窗口太窄了」）。
        #    弹窗默认是按内容 `sizeHint` 定的，而一句短文字 + 换行会让它缩成
        #    一个窄条（"把所有设置恢复为默认值？/（API Key 不会动）"两行都短）。
        #    ⚠️ 用 `setMinimumWidth` 而不是"show 之后再 resize" ——
        #       后者会在显示后改尺寸，无边框窗口的位置不会跟着重算，
        #       看起来就是"弹出来又跳了一下"。设成下限则首次布局就按它来。
        if min_width:
            self.setMinimumWidth(int(style.px(min_width)))

        ins = int(round(self.border_w)) + 1
        root = QtWidgets.QVBoxLayout(self)
        # 四周用档位常量（用户第 2 条：禁止写死随机 margin）。
        # ⚠️ `pad`(16 基准px) 远大于外框线宽度，所以**顺带就把边框让开了** ——
        #    这里不需要再单独加 `ins`（加了反而多出一圈）。
        pad = style.S("xl")
        root.setContentsMargins(pad, pad, pad, pad)
        root.setSpacing(style.S("sm"))

        self.title_label = None
        if title:
            self.title_label = QtWidgets.QLabel(title)
            self.title_label.setFont(style.font("body_bold"))
            self.title_label.setStyleSheet(
                "color:%s; background:transparent;" % style.p["text"])
            root.addWidget(self.title_label, 0)

        self.body = QtWidgets.QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(style.S("xs"))
        root.addLayout(self.body, 1)

        self.foot = QtWidgets.QHBoxLayout()
        self.foot.setContentsMargins(0, 0, 0, 0)
        self.foot.setSpacing(style.S("sm"))
        self.foot.addStretch(1)
        root.addLayout(self.foot, 0)

    # ---- 组装

    def add_text(self, text: str, wrap: bool = True):
        lab = QtWidgets.QLabel(text)
        lab.setFont(self.style.font("body"))
        lab.setWordWrap(wrap)
        lab.setStyleSheet("color:%s; background:transparent;"
                          % self.style.p["text"])
        self.body.addWidget(lab)
        return lab

    def add_button(self, text: str, variant: str = "ghost", on_click=None,
                   close: bool = True, accept: bool = True):
        """往右下角加一颗按钮（顺序是从左往右）。

        ⚠️ 默认样式跟设置窗口底部那排保持一致：
           次要 `ghost`（取消/关闭）、主要 `primary`（确定/保存）。

        ⚠️⚠️ **「取消」必须传 `accept=False`**（2026-10-17 血案）：
            原来这里**无论点哪颗都调 `self.accept()`**，于是
            `dlg.exec() == Accepted` 恒为真 —— **点「取消」等于点「确定」**：
              · 确认框（移除资料 / 删除分组 / 恢复默认）→ 点取消照样执行删除；
              · 输入框（新建分组）→ 点取消拿回空名字，然后弹一个"组名不能为空"的错。
            症状是"取消没反应/反而做事了"，而单测全绿（测试里关了弹窗，走不到这条路）。
            现在：`accept=False` → `reject()`，`exec()` 返回 `Rejected`。
        """
        from .widgets import Pill      # 放在函数里：避免模块级循环引用

        def _go():
            if on_click is not None:
                on_click()
            if close:
                if accept:
                    self.accept()
                else:
                    self.reject()
        b = Pill(self.style, text, _go, variant=variant, parent=self)
        self.foot.addWidget(b, 0, QtCore.Qt.AlignVCenter)
        return b

    # ---- 外壳绘制（跟 Panel 共用一份 `paint_shell`）

    def paintEvent(self, ev):
        pt = QtGui.QPainter(self)
        paint_shell(self, pt, self.style.p["bg"], self.radius, self.border_w,
                    self.style.p["shell_border"])

    # ---- 拖动（无边框窗口得自己实现）

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self._drag = (ev.globalPosition().toPoint(), self.pos())
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            p0, w0 = self._drag
            d = ev.globalPosition().toPoint() - p0
            self.move(w0.x() + d.x(), w0.y() + d.y())
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag = None
        super().mouseReleaseEvent(ev)


class Panel(QtWidgets.QWidget):
    """**无边框 + 真圆角 + 真透明**的面板（悬浮窗 / 设置窗口的外壳）。

    对比 Tk 那套：`overrideredirect(True)` + 透明色键 `#FF00FE`
    （色键撞色就穿洞）+ 自己画两层多边形 + PIL 位图抗锯齿 + 位图节流。
    Qt 这边：一个窗口属性 + 一次 `drawRoundedRect`，抗锯齿是白送的。
    """

    def __init__(self, style: Style, radius: Optional[float] = None,
                 border_px: Optional[int] = None, translucent: bool = True,
                 parent=None, **kw):
        super().__init__(parent)
        self.style = style
        from .. import theme
        self.radius = (float(radius) if radius is not None
                       else style.px(theme.SHELL_RADIUS))
        self.border_w = (float(border_px) if border_px is not None
                         else float(style.px(
                             theme.shell_border_width(style.theme_name))))
        self._bg_key = "bg"

        if translucent:
            # ⚠️ 这两个属性缺一不可：少了 NoSystemBackground，
            #    圆角外面会被系统涂成不透明色，看起来就是个直角窗口。
            self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
            self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)

    def set_bg_key(self, key: str):
        self._bg_key = key
        self.update()

    def paintEvent(self, ev):
        # ⚠️ 走**共用的** `paint_shell()`（`ShellDialog` 用的是同一个）——
        #    抄两遍的后果是"改了一处、另一处留在旧样子"，
        #    而用户要的正是"几个弹窗风格一致"。
        pt = QtGui.QPainter(self)
        paint_shell(self, pt, self.style.p[self._bg_key], self.radius,
                    self.border_w, self.style.p["shell_border"])
