# -*- coding: utf-8 -*-
"""Qt 版悬浮窗 —— 展示 AI 推荐回复，点一下就复制。

对 Tk 版（`wechat_assistant/ui.py`）的迁移
-----------------------------------------
**对外 API 一个都没改**：构造函数收同一批回调，`post_*` / `set_docked` /
`peek_intent` / `min_height` / `desired_size` … 方法名和语义都保持一致，
`run_watch.py` 和 `follow.py` 那边基本照旧调用。

⚠️ 所以迁移时**不能按"哪个方法看起来像 Tk 就删掉"来清理** ——
   `follow.WindowFollower` 会读 `min_height()` / `desired_size()` / `set_docked()`，
   `run_watch` 会读 `peek_intent()` / `post_*()`。少一个就是运行时 AttributeError。

Tk 那一整套补丁**全部消失**（这是迁移的理由，不是副作用）：

    Tk 的做法                                      Qt 的替代
    -------------------------------------------   ------------------------------
    `overrideredirect` + 透明色键 `#FF00FE`        `WA_TranslucentBackground`
    （色键撞色就在界面上穿个洞）
    自己画两层多边形当圆角 + PIL 位图抗锯齿        一次 `drawRoundedRect` + Antialiasing
    位图节流 `_Throttle`（间隔闸 + 帧闸）          不需要（原生绘制本来就是毫秒级）
    销毁子控件后手动 `update_idletasks()` 防残影    不需要（Qt 自己标脏区）
    `SCROLL_PAD` —— 内容右侧留 10px 给浮层滚动条    不需要（Qt 滚动条是独立部件）
    `wraplength` 手算（超宽静默裁字）               `QLabel.setWordWrap(True)`
    `place` 绝对像素 + 重建时重设                  `QHBoxLayout`（同一次布局里完成）

线程模型
--------
和 Tk 版**完全一致**，因为约束没变：触发源（热键 / 窗口事件 / 轮询）在子线程，
而界面只能由主线程碰。两边用 `queue.Queue` 通信：子线程 `post()`，
UI 线程由 `QTimer` 每 120ms `_pump()` 取。

⚠️ `_pump()` 的异常必须**按单条消息**捕获 —— 包在整个循环外面的话，
一条坏消息就中断排空循环，剩下的消息**已出队却不再处理**，界面静默失联。
这是从 Tk 版继承下来的血泪，别"优化"掉。

红线
----
本模块只做「显示」和「写剪贴板」。
**不注入微信、不模拟粘贴、不自动发送** —— 复制完由用户自己去微信里粘贴发送。
"""

import os
import queue
from typing import Callable, Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .. import theme
from .base import Style, init_app, px, qc
from .cards import Card
from .layout import Panel, ScrollArea, ShellDialog
from .inputs import InputBox
from .widgets import Pill

# 窗口基准尺寸（按 DPI 缩放；数值取「跟微信聊天区并排时不显突兀」的大小）
BASE_WIDTH = 340
#: 面板**默认高度**，也是「再矮不会矮过它」的下限。
#:
#: ⚠️ 2026-10-14 用户要求「默认高度最小改大一点，大概就是目前微信高度」——
#:     380 → **420**。实测他机器上微信常规窗口 **623 设备像素 = 415 基准**
#:     （1.5 倍缩放），所以 420 基准 ≈ 630 设备像素，和微信窗口基本齐平。
#:     之前 380 比"固定控件 + 三条回复"实际需要的 363 只高一点点，
#:     面板看起来是窄窄一条。
BASE_MIN_HEIGHT = 420
# 折叠态：一个**小方块**，不是细长条。
COLLAPSED_SIZE = 56

# 队列轮询间隔（和 Tk 版一致）
PUMP_MS = 120


# ================================================================ 标题栏

#: 齿轮图标资源（用户给的 SVG，`E:\Download\设置.svg` 拷进来的那一份）。
#: ⚠️ 它自带 `fill="#272636"`（深灰）—— 在深色主题上几乎看不见，
#:    所以**不能原样画**：见 `_gear_pixmap()` 的"拿形状、换颜色"。
GEAR_SVG = "gear.svg"

#: 齿轮画多大 = **按钮短边的多少倍**。
#: ⚠️ 这个数是**量出来的**，不是拍脑袋：同一排另外两颗是字形，
#:    「✕」的墨水占短边 **33%**、「—」占 50%（`QFontMetricsF.boundingRect`）。
#:    一开始按 0.94 画（几乎顶满按钮）→ 用户看到的第一句话就是「图标大了点」。
#:    0.68 画出来墨水约 72%，和「✕」是一个视觉重量（齿轮中间是空的，
#:    比实心「✕」略大一点才平衡）。对照图 `out/gear_sizes.png` 里 0.94/0.80/0.68/
#:    0.60/0.52 五档并排摆过。
GEAR_BOX = 0.68

#: 渲染好的齿轮位图缓存：`(宽, 高, 颜色)` -> QPixmap。
#: ⚠️ 必须缓存：`paintEvent` 在悬停/重绘时会被反复调到，而 SVG 光栅化比画一个
#:    QPainterPath 贵得多（实测这一颗要 ~1ms，二十次重绘就是肉眼可见的卡）。
_GEAR_CACHE: Dict[tuple, "QtGui.QPixmap"] = {}

#: SVG 这条路的状态（只探一次，失败就永久回落到自绘齿轮）。
#: `{"tried": bool, "ok": bool, "why": str}`
_GEAR_SVG_STATE = {"tried": False, "ok": False, "why": ""}


def gear_svg_ready() -> bool:
    """SVG 齿轮能不能用（探一次并缓存）。**测试和排查都点它**。"""
    if _GEAR_SVG_STATE["tried"]:
        return _GEAR_SVG_STATE["ok"]
    _GEAR_SVG_STATE["tried"] = True
    try:
        from .. import paths
        path = paths.asset_path(GEAR_SVG)
        if not os.path.exists(path):
            _GEAR_SVG_STATE["why"] = "资源不在：%s" % path
        else:
            from PySide6 import QtSvg          # ⚠️ spec 的 hiddenimports 里也有一份
            with open(path, "rb") as f:
                data = f.read()
            r = QtSvg.QSvgRenderer(data)
            if not r.isValid():
                _GEAR_SVG_STATE["why"] = "SVG 解析失败：%s" % path
            else:
                _GEAR_SVG_STATE["ok"] = True
    except Exception as e:                     # 缺 QtSvg / 读不了文件都走这里
        _GEAR_SVG_STATE["why"] = "%s: %s" % (type(e).__name__, e)
    if not _GEAR_SVG_STATE["ok"]:
        # ⚠️ **要说出来**（只报一次）：不然"打包后图标悄悄换成了自绘那个"
        #    谁都不知道为什么。
        print("齿轮图标：用不了 SVG（%s），回落到自绘齿轮" % _GEAR_SVG_STATE["why"])
    return _GEAR_SVG_STATE["ok"]


def _gear_pixmap(w: int, h: int, color: str, box: float = None):
    """把 `gear.svg` 渲染成 **当前主题色** 的位图（拿形状、换颜色）。

    ⚠️ 为什么不能直接画 SVG：它自带 `fill="#272636"`，深色主题上几乎看不见。
        做法是"先用 SVG 铺出 alpha 通道，再 `CompositionMode_DestinationIn` 用主题色填" ——
        这样**深浅两套主题 + 悬停态自动跟着走**，不用让用户准备两份图标。
    ⚠️⚠️ **全程必须在 `QImage(ARGB32_Premultiplied)` 上合成，不能图省事用 `QPixmap`**：
        `QPixmap(w, h)` 在 Windows 上建出来是 **RGB32（没有 alpha 通道）**，
        于是"透明"会被压成**不透明黑** —— 深色主题上看着没事（黑 ≈ 底色），
        **浅色主题上就是一块黑方块**。第一版就是这么写的，测试里"颜色是不是主题色"
        那条把它按住了（`#000000` 最多）。
    ⚠️ 尺寸按**短边**等比放进 0.94 的方框（viewBox 1084×1024 不是正方形，
        直接拉满会被横向拉扁）。
    """
    key = (int(w), int(h), str(color), round(float(box if box is not None else GEAR_BOX), 3))
    hit = _GEAR_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        from .. import paths
        from PySide6 import QtSvg
        with open(paths.asset_path(GEAR_SVG), "rb") as f:
            renderer = QtSvg.QSvgRenderer(f.read())
        side = min(int(w), int(h))
        box = side * (GEAR_BOX if box is None else float(box))
        x = (int(w) - box) / 2.0
        y = (int(h) - box) / 2.0

        # ① 先按 SVG 画出形状（颜色先不管）
        mask = QtGui.QImage(int(w), int(h),
                            QtGui.QImage.Format_ARGB32_Premultiplied)
        mask.fill(QtCore.Qt.transparent)
        pt = QtGui.QPainter(mask)
        pt.setRenderHint(QtGui.QPainter.Antialiasing, True)
        renderer.render(pt, QtCore.QRectF(x, y, box, box))
        pt.end()

        # ② 用主题色填，按遮罩的 alpha 裁（保留**底色** → DestinationIn）
        out = QtGui.QImage(int(w), int(h),
                           QtGui.QImage.Format_ARGB32_Premultiplied)
        out.fill(QtGui.QColor(color))
        pt = QtGui.QPainter(out)
        pt.setCompositionMode(QtGui.QPainter.CompositionMode_DestinationIn)
        pt.drawImage(0, 0, mask)
        pt.end()
    except Exception as e:
        print("齿轮图标：渲染失败（%s: %s），改用自绘齿轮" % (type(e).__name__, e))
        return None
    pm = QtGui.QPixmap.fromImage(out)          # ③ 最后才转成 QPixmap（带 alpha）
    _GEAR_CACHE[key] = pm
    return pm


class TitleButton(QtWidgets.QWidget):
    """标题栏上的小按钮（**齿轮 / — / ✕**）。

    ⚠️ 为什么不用 `QPushButton` 加样式表：这几个是**画出来的图形**不是普通文字，
        悬停底要圆角、线宽要跟标题栏一致，自绘几行就够了，
        而且能保证"悬停才显形"的行为和 Tk 版逐像素一致。

    ⚠️ 2026-10-14：设置那颗的**三代**——
        ① 最早是 emoji `"⚙"`(U+2699)：在 Windows 上会落到**彩色 emoji 字体**里，
           和旁边的「—」「✕」不是一套（用户：「不要用emoji」）；
        ② 改成文字「设置」：长度是 emoji 的两倍，和图标按钮不是一类东西
           （用户：「不要文字」）；
        ③ 现在 = **自己画一个单色齿轮**（`gear=True`，见 `_paint_gear`）：
           颜色跟着主题（`text` / `text_dim`），线形和「—」「✕」同一套。
        ⚠️ 所以它**回到了基准宽度**（和另外两颗等宽）—— `TitleButton` 仍然
           按字形实测宽度，但齿轮不传 glyph，走基准宽。

    对照 Tk 版的坑：那边整个标题栏是一个 Canvas，按钮靠 `_tb_hit`
    命中表 + 画布级 `<Motion>` 自己算命中 —— 因为 canvas item 的
    `<Enter>`/`<Leave>` 只在鼠标**压到字形上**才触发（可点区 24px 宽、
    实际只有字那么大，按钮"点不动"）。Qt 里每个按钮是**真部件**，
    命中区就是自己的矩形，这一整层补丁不需要了。
    """

    # ⚠️ 标题栏尺寸常量统一放这里（`TitleBar` 用它们定自己的高度和内边距，
    #    `TitleButton` 用它们定按钮大小）。散在各个类里迟早对不上。
    # ⚠️ **两个类里各有一份相同的常量，改一个必须改另一个** ——
    #    这里本来是"故意重复"（Tk 时代两个类互不引用），代价就是容易漏改。
    #    改高度时顺手把按钮也等比放大了一点（24 -> 26 高），
    #    否则在更高的标题栏里按钮会显得缩水。
    TITLEBAR_H = 36      # 基准像素（2026-09-27 用户要求"顶部高一点"，32 -> 36）
    TB_PAD_L = 12
    TB_PAD_R = 8
    TB_BTN_W = 28
    TB_BTN_H = 26

    # ---- 齿轮的几何（都是"按钮宽高里较短那一边"的比例，自适应缩放）
    # ⚠️ 8 齿是"一眼能认出是齿轮"的**最小值**：6 齿看着像六边形/雪花，
    #    10 齿以上又糊成一个小圆（按钮只有 26 基准像素高）。
    GEAR_TEETH = 8
    GEAR_R_OUT = 0.47     # 齿顶（最外那圈）
    GEAR_R_BODY = 0.30    # 齿根圆（齿轮的"身子"）
    GEAR_R_HOLE = 0.12    # 中心孔
    GEAR_TOOTH_W = 0.17   # 齿宽。⚠️ 必须小于"齿距"（周长/齿数 ≈ 0.236），
    #                       否则相邻齿会连成一片、看着像个带豁口的饼。

    def __init__(self, style: Style, glyph: str = "", tooltip: str = "",
                 parent=None, gear: bool = False):
        super().__init__(parent)
        self.style = style
        self._glyph = glyph
        self._gear = bool(gear)
        #: 齿轮占短边的比例；`None` = 用模块默认（`GEAR_BOX`）。
        #: ⚠️ 之所以做成实例属性：跑尺寸对照的探针要一次画好几档
        #:    （`.tmp/probe_gear_sizes.py`），改一个实例不该影响别的。
        self.gear_box = None
        self._hover = False
        self.setCursor(QtCore.Qt.PointingHandCursor)
        # ⚠️ 宽度**按字形实测**：单字符（— / ✕）就是基准宽，多字符自己撑开。
        #    写死宽度的话换成文字就会被裁掉（而且不报错）。
        #    ⚠️ 齿轮**没有字形**，所以就是基准宽（和其它图标按钮等宽）。
        _fm = QtGui.QFontMetricsF(style.font("label"))
        _text_w = int(round(_fm.horizontalAdvance(glyph))) if glyph else 0
        _w = max(style.px(TitleBar.TB_BTN_W), _text_w + style.px(10))
        self.setFixedSize(_w, style.px(TitleBar.TB_BTN_H))
        if tooltip:
            self.setToolTip(tooltip)

    def enterEvent(self, ev):
        self._hover = True
        self.update()

    def leaveEvent(self, ev):
        self._hover = False
        self.update()

    def paintEvent(self, ev):
        pt = QtGui.QPainter(self)
        pt.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p = self.style.p
        if self._hover:
            pt.setPen(QtCore.Qt.NoPen)
            pt.setBrush(qc(p["hover"]))
            pt.drawRoundedRect(QtCore.QRectF(0, 0, self.width(), self.height()),
                               self.style.px(6), self.style.px(6))
        _fg = qc(p["text"] if self._hover else p["text_dim"])
        if self._gear:
            # ⚠️ 齿轮**优先用 `gear.svg`**（用户给的图标），拿不到才回落到自绘那个。
            #    两条路都画成**单色**、都跟主题走 —— 见 `gear_svg_ready()` / `_paint_gear`。
            if gear_svg_ready():
                pm = _gear_pixmap(self.width(), self.height(),
                                  p["text"] if self._hover else p["text_dim"],
                                  self.gear_box)
                if pm is not None:
                    pt.drawPixmap(0, 0, pm)
                    return
            self._paint_gear(pt, _fg)
            return
        pt.setPen(_fg)
        pt.setFont(self.style.font("label"))
        pt.drawText(self.rect(), QtCore.Qt.AlignCenter, self._glyph)

    # ---- 齿轮（自己画）

    def _paint_gear(self, pt: QtGui.QPainter, color):
        """画一个**单色**齿轮：圆身子 + 8 个径向矩形齿 + 中心孔。

        ⚠️ 为什么不用字体里的齿轮字形：凡是"齿轮"码位（U+2699 等）都是 emoji
            码位，Windows 上会走彩色 emoji 字体 —— 这正是用户否掉的东西。
        ⚠️ 构造方式是**圆 ∪ 8 个矩形齿 − 中心圆**（`QPainterPath` 的布尔运算），
            不是"外圆/内圆交替取点连多边形"。**第一版就是取点连的，
            结果齿根弧重叠、多边形自交，画出来是"花+小 V 缺口"，一点都不像齿轮**
            （`out/gear_probe.png` 里那三张留了证据）。
        ⚠️ 中心孔用 `subtracted` 挖出来，不是"用背景色画一个圆" ——
            悬停时底色会变，拿底色去盖会露馅。
        """
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        size = float(min(w, h))
        r_out = size * self.GEAR_R_OUT
        r_body = size * self.GEAR_R_BODY
        r_hole = size * self.GEAR_R_HOLE
        tooth_w = size * self.GEAR_TOOTH_W
        n = self.GEAR_TEETH

        center = QtCore.QPointF(cx, cy)
        gear = QtGui.QPainterPath()                 # 圆身子
        gear.addEllipse(center, r_body, r_body)
        for i in range(n):
            tr = QtGui.QTransform()
            tr.translate(cx, cy)
            tr.rotate(i * 360.0 / n)
            # 齿在**局部坐标**里画：y 轴负方向朝外，根部伸进身子一点（保证并集连上）
            tooth = QtGui.QPainterPath()
            tooth.addRect(QtCore.QRectF(-tooth_w / 2.0, -r_out,
                                        tooth_w, r_out - r_body * 0.5))
            gear = gear.united(tr.map(tooth))

        hole = QtGui.QPainterPath()                 # 中心孔（真挖掉）
        hole.addEllipse(center, r_hole, r_hole)
        gear = gear.subtracted(hole)

        pt.setPen(QtCore.Qt.NoPen)                  # 纯填充：小尺寸下描边会发虚
        pt.setBrush(color)
        pt.drawPath(gear)


class CollapseBox(QtWidgets.QWidget):
    """折叠态的那个小方块：一个圆角方块 + 「AI / 展开」。点一下展开。"""

    def __init__(self, style: Style, parent=None):
        super().__init__(parent)
        self.style = style
        self.setCursor(QtCore.Qt.PointingHandCursor)

    def paintEvent(self, ev):
        pt = QtGui.QPainter(self)
        pt.setRenderHint(QtGui.QPainter.Antialiasing, True)
        st, p = self.style, self.style.p
        w, h = self.width(), self.height()
        r = st.px(10)
        pen = QtGui.QPen(qc(p["border"]))
        pen.setWidth(1)
        pt.setPen(pen)
        pt.setBrush(qc(p["surface"]))
        pt.drawRoundedRect(QtCore.QRectF(0.5, 0.5, w - 1, h - 1), r, r)

        pt.setPen(qc(p["accent"]))
        pt.setFont(st.font("title_bold"))
        pt.drawText(QtCore.QRectF(0, 0, w, h - st.px(7) * 2),
                    QtCore.Qt.AlignCenter, "AI")
        pt.setPen(qc(p["text_dim"]))
        pt.setFont(st.font("small"))
        pt.drawText(QtCore.QRectF(0, h - st.px(11) * 2, w, st.px(11) * 2),
                    QtCore.Qt.AlignCenter, "展开")


class TitleBar(QtWidgets.QWidget):
    """自绘标题栏：左边会话名（群聊带标记），右边三个图标按钮。

    ⚠️ **光标只认两种**：默认箭头；只有悬停在按钮上才换手型。
        用户明确说过"不需要移动的鼠标"，所以标题栏空白处**不设** `SizeAllCursor`。
    """

    # ⚠️ 标题栏尺寸常量统一放这里（`TitleBar` 用它定自己的高度和内边距，
    #    `TitleButton` 用它定按钮大小）。散在各个类里迟早对不上。
    TITLEBAR_H = 36      # 基准像素（2026-09-27 用户要求"顶部高一点"，32 -> 36）
    TB_PAD_L = 12
    TB_PAD_R = 8
    TB_BTN_W = 28
    TB_BTN_H = 26

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.style = panel.style
        self._drag = None
        self.setFixedHeight(self.style.px(self.TITLEBAR_H))
        self.setCursor(QtCore.Qt.ArrowCursor)
        self.setAutoFillBackground(False)

        lay = QtWidgets.QHBoxLayout(self)
        # ⚠️ 2026-09-27：边距/间距改成**档位常量**（用户第 2 条）。
        #    原来是写死的 12 / 8 / 2 —— 值没变，但从此不再是"随手写的数"。
        #    左 12 让会话名的起点跟下面正文的左边缘（`body` 也是 12）在一条竖线上，
        #    这是"标题跟内容对齐"的关键：**两个数必须来自同一个档位**。
        lay.setContentsMargins(self.style.S("lg"), 0, self.style.S("md"), 0)
        lay.setSpacing(self.style.S("xxs"))
        lay.addStretch(1)

        self.buttons = {}
        # 顺序 = 左→右：设置 / 折叠 / 隐藏。三个**等宽等高 + 垂直居中**，
        # 间距用 `xxs`(2)：微信的标题栏按钮是"挨得比较近"的一组，不是散开的。
        # ⚠️ 2026-10-14：设置那颗的三种写法都试过了 —— emoji `"⚙"`（会走彩色
        #    emoji 字体）→ 文字「设置」（太长、和图标按钮不是一类）→
        #    **自己画的齿轮**（用户最终要求：「变成齿轮图标吧，不要文字也不要emoji」）。
        #    所以它现在 `gear=True`、没有字形 → 宽度回到基准，**三颗又等宽了**。
        for glyph, action, tip, gear in (("", "settings", "设置", True),
                                         ("—", "collapse", "折叠", False),
                                         ("✕", "hide", "隐藏", False)):
            b = TitleButton(self.style, glyph, tip, self, gear=gear)
            b.mouseReleaseEvent = (          # 绑实例上，避免再立一个子类
                lambda ev, a=action: self._on_click(ev, a))
            self.buttons[action] = b
            lay.addWidget(b, 0, QtCore.Qt.AlignVCenter)

    def _on_click(self, ev, action):
        if ev.button() == QtCore.Qt.LeftButton:
            self.panel._titlebar_action(action)

    # ---- 绘制

    def _text(self) -> str:
        pnl = self.panel
        name = pnl._session_title or "—"
        if len(name) > 18:
            name = name[:17] + "…"
        return name

    def _left_width(self) -> int:
        """会话名左边要留出的宽度。

        ⚠️ 2026-09-27：**群标记已经不画了**（用户要求「不需要显示群标签」），
            所以这里不再为它留宽度，直接就是标题栏的左边距。
            保留成方法是为了跟 `paintEvent` 里算文字起点用同一个来源 ——
            两边各写各的迟早对不上（以前就是这样差过几像素）。
        """
        return self.style.S("lg")           # 跟 `lay` 的左边距同一个档位

    def paintEvent(self, ev):
        pt = QtGui.QPainter(self)
        pt.setRenderHint(QtGui.QPainter.Antialiasing, True)
        st, p = self.style, self.style.p
        h = self.height()
        # ⚠️ **上面两个角要圆**，别用 `fillRect`。
        #    标题栏是内容层最上面那个不透明部件，它会一直铺到 `(0,0)`；
        #    不圆角的话就把窗口自绘的右上/左上圆角盖成直角 ——
        #    而缩进量为了"贴紧"已经压到外框线那么窄，躲不开弧线了。
        #    半径取 `_content_radius()`，正好接在窗口圆角的弧线上。
        from .layout import fill_rounded
        fill_rounded(pt, QtCore.QRectF(self.rect()), p["surface_alt"],
                     self.panel._content_radius(), top=True, bottom=False)

        # ⚠️ 这里原来会画一个绿色的「群」圆标（`_session_is_group` 为真时）。
        #    用户 2026-09-27 明确要求：「**不需要显示群标签**」——已删。
        #    `_session_is_group` 本身**还留着**：它由
        #    `Recorder.on_session(title, is_group)` 喂进来（那条链路没动），
        #    以后想再显示时不用重新接线。
        x = self._left_width()

        pt.setPen(qc(p["text"]))
        pt.setFont(st.font("title_bold"))
        x_end = self.buttons["settings"].x() if self.buttons else self.width()
        rect = QtCore.QRectF(x, 0, max(0, x_end - st.px(6) - x), h)
        fm = QtGui.QFontMetrics(st.font("title_bold"))
        txt = fm.elidedText(self._text(), QtCore.Qt.ElideRight, int(rect.width()))
        pt.drawText(rect, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, txt)

    # ---- 拖动

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self._drag = (ev.globalPosition().toPoint(),
                          self.panel.pos())
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            p0, w0 = self._drag
            d = ev.globalPosition().toPoint() - p0
            self.panel.move_clamped(w0.x() + d.x(), w0.y() + d.y())
            ev.accept()

    def mouseReleaseEvent(self, ev):
        self._drag = None

    def mouseDoubleClickEvent(self, ev):
        self.panel.toggle_collapse()


# ================================================================ 意图卡片

class IntentCard(QtWidgets.QWidget):
    """「对方在说：…」那张卡片。

    单独一张卡片而不是一行灰字 —— 判错的时候一眼就能发现，
    比默默生成一堆立场反了的回复好排查。没有判断时整块不占地方（高度 0）。
    """

    def __init__(self, style: Style, text: str = "", parent=None):
        super().__init__(parent)
        self.style = style
        self._text = text or ""
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                           QtWidgets.QSizePolicy.Fixed)
        self.setFixedHeight(0)

    def set_text(self, text: str, width: int = 0):
        self._text = text or ""
        if not self._text:
            self.setFixedHeight(0)
            self.update()
            return
        w = width or self.width() or self.style.px(BASE_WIDTH - 20)
        self.setFixedHeight(self.height_for_width(w))
        self.update()

    def height_for_width(self, width: int) -> int:
        """按可用宽度算出这张卡片需要多高（文字要换行）。"""
        st = self.style
        pad = st.px(10)
        head = "对方在说："
        fm = QtGui.QFontMetrics(st.font("label"))
        hw = fm.horizontalAdvance(head)
        text_w = max(60, int(width) - pad * 2 - hw)
        flags = int(QtCore.Qt.TextWordWrap | QtCore.Qt.AlignLeft)
        rect = fm.boundingRect(QtCore.QRect(0, 0, text_w, 10000), flags, self._text)
        return int(rect.height()) + pad * 2

    def resizeEvent(self, ev):
        # 宽度变了要重新算高度（换行条数会变）
        if self._text:
            self.setFixedHeight(self.height_for_width(self.width()))

    def paintEvent(self, ev):
        if not self._text:
            return
        pt = QtGui.QPainter(self)
        pt.setRenderHint(QtGui.QPainter.Antialiasing, True)
        st, p = self.style, self.style.p
        w, h, pad = self.width(), self.height(), st.px(10)
        pt.setPen(QtCore.Qt.NoPen)
        pt.setBrush(qc(p["surface"]))
        pt.drawRoundedRect(QtCore.QRectF(0, 0, w, h), st.px(8), st.px(8))

        fm = QtGui.QFontMetrics(st.font("label"))
        head = "对方在说："
        pt.setFont(st.font("label"))
        pt.setPen(qc(p["text_dim"]))
        pt.drawText(QtCore.QRectF(pad, pad, fm.horizontalAdvance(head), h - pad),
                    QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop, head)
        pt.setPen(qc(p["text"]))
        pt.drawText(QtCore.QRectF(pad + fm.horizontalAdvance(head), pad,
                                  w - pad * 2 - fm.horizontalAdvance(head),
                                  h - pad * 2),
                    int(QtCore.Qt.TextWordWrap | QtCore.Qt.AlignLeft |
                        QtCore.Qt.AlignTop), self._text)


# ================================================================ 提示词窗口

class PromptWindow(ShellDialog):
    """「看提示词」窗口。

    为什么要有它：生成结果不对劲时，第一件想确认的就是「到底发了什么」——
    历史是不是取错了、口吻样本对不对、人设有没有生效、关系设定进没进。
    看不见就只能猜。

    ⚠️ 2026-09-27：**改成继承 `ShellDialog`**（无边框 + 真圆角 + 同一套外框线）。
        原来它是个裸 `QtWidgets.QWidget` —— 于是**吃系统的标题栏和边框**，
        跟悬浮窗/设置窗口完全不是一个样子。用户的原话是
        「不要 python 的边框」，以及"几个弹窗风格要一致"。
    ⚠️ 非模态（`modal=False`）：它只是给人看的，不该挡住主界面操作。
    """

    def __init__(self, style: Style, on_copy, parent=None):
        super().__init__(style, "本次提交给模型的提示词", parent, modal=False)
        self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, True)
        self.resize(px(820), px(660))

        head = QtWidgets.QLabel(
            "下面是实际发出的内容（system + user）与模型返回的思考过程，一字未改。")
        head.setFont(style.font("label"))
        head.setWordWrap(True)
        head.setStyleSheet("color:%s; background:transparent;"
                           % style.p["text_dim"])
        self.body.addWidget(head)

        self.txt = QtWidgets.QPlainTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setFont(style.font("mono"))
        self.txt.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        self.body.addWidget(self.txt, 1)

        # 底部两栏：左边「复制全文」（主要动作），右边「关闭」（次要）。
        # ⚠️ 用 `foot` 现成的：它已经带了左弹簧和其他弹窗一致的间距。
        self.foot.insertWidget(0, Pill(style, "复制全文", on_copy,
                                       variant="primary", parent=self))
        self.add_button("关闭", variant="ghost")

        self.set_style(style)

    def set_style(self, style: Style):
        self.style = style
        p = style.p
        # ⚠️ 这个「看提示词」窗口的 `QPlainTextEdit` **不走** `inputs.py` 的
        #    `_edit_qss` —— 所以选中高亮那两个键这里也得写一遍。
        #    `QPlainTextEdit` 里的提示词是用户会去**选中复制**的东西，
        #    不写的话选出来还是系统默认的蓝（见 `check_selection_color.py`）。
        # ⚠️ 这里**只给 QPlainTextEdit 写规则**，不要再写 `QWidget {}` ——
        #    `QWidget` 会连**所有后代**一起选中，是个很容易踩的坑
        #    （`QLabel` 是 `QFrame` 子类那件事是同一类问题）。
        self.txt.setStyleSheet(
            "QPlainTextEdit {"
            " background:%s; color:%s; border:1px solid %s;"
            " border-radius:%dpx; padding:%dpx;"
            " selection-background-color:%s; selection-color:%s; }"
            % (p["surface"], p["text"], p["border"], style.R("control"),
               style.S("md"), p["sel_bg"], p["sel_fg"]))


# ================================================================ 只看对话窗口

class ReadWindow(ShellDialog):
    """「只看对话」窗口 —— 把**程序这次读到的对话**单独显示在一个窗口里。

    ⚠️ 2026-09-27 从主面板搬出来的（用户提的：「我感觉只看对话也用单独窗口来吧」）。
    原来它把整段对话当成"回复"塞进主面板的滚动区 —— 两个后果：
      ① **把刚生成的建议顶掉了**，看一眼对话就得重新生成（白花 token）；
      ② 长对话把主面板撑成一大坨可滚动的东西，状态栏那行小字跟着变长，
         而**状态栏文字宽度就是面板的最小宽度** → 面板被顶着变宽
         （用户报的「鼠标放到【生成建议】上，右边框往右变宽」就是这个）。

    和 `PromptWindow` 一个shape：无边框自绘外壳、只读文本框、底部「复制全文 / 关闭」。
    ⚠️ 非模态：它只是给人看的，不该挡住主界面操作。
    """

    def __init__(self, style: Style, on_copy, parent=None):
        super().__init__(style, "只看对话（程序这次读到的内容）", parent,
                         modal=False)
        self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, True)
        self.resize(px(760), px(620))

        head = QtWidgets.QLabel(
            "下面是程序这一次从微信窗口里读到的对话，按时间从上到下排。"
            "「→」那几条是它准备用来生成建议的目标。"
            "**没有调用 AI、也没有花 token。**")
        head.setFont(style.font("label"))
        head.setWordWrap(True)
        head.setStyleSheet("color:%s; background:transparent;"
                           % style.p["text_dim"])
        self.body.addWidget(head)

        self.txt = QtWidgets.QPlainTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setFont(style.font("mono"))
        self.txt.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        self.body.addWidget(self.txt, 1)

        self.foot.insertWidget(0, Pill(style, "复制全文", on_copy,
                                       variant="primary", parent=self))
        self.add_button("关闭", variant="ghost")

        self.set_style(style)

    def set_style(self, style: Style):
        self.style = style
        p = style.p
        # ⚠️ 跟 `PromptWindow` 一样：这个 `QPlainTextEdit` **不走** `inputs.py`
        #    的 `_edit_qss`，选中高亮那两个键这里得**自己写一遍**
        #    （不写就是系统默认的蓝，见 `probes/qt/check_selection_color.py`）。
        self.txt.setStyleSheet(
            "QPlainTextEdit {"
            " background:%s; color:%s; border:1px solid %s;"
            " border-radius:%dpx; padding:%dpx;"
            " selection-background-color:%s; selection-color:%s; }"
            % (p["surface"], p["text"], p["border"], style.R("control"),
               style.S("md"), p["sel_bg"], p["sel_fg"]))


# ================================================================ 悬浮面板

class FloatingAssistant(Panel):
    """悬浮面板：会话名 + 三条建议 + 「我想说」 + 生成按钮。

    ⚠️ 继承 `Panel`（无边框 + 真圆角 + 真透明）。
        Tk 版这里是 `tk.Tk()` + `overrideredirect` + 透明色键 + 自绘外壳画布，
        Qt 版一层 `paintEvent` 就够 —— 而且圆角是真抗锯齿。
    """

    def __init__(self, cfg, logger: Callable[[str], None] = None,
                 on_regenerate: Optional[Callable[[str], None]] = None,
                 on_read: Optional[Callable[[], None]] = None,
                 on_clear: Optional[Callable[[], None]] = None,
                 on_settings: Optional[Callable[[], None]] = None,
                 session_source: Optional[Callable[[], dict]] = None,
                 on_mode_change: Optional[Callable[[str, str], None]] = None,
                 on_config_changed: Optional[Callable[[object], None]] = None):
        self.cfg = cfg
        self._log = logger or (lambda m: None)
        self._on_regenerate = on_regenerate
        self._on_read = on_read
        self._on_clear = on_clear
        self._on_settings = on_settings
        # 会话级设置：会话表由 run_watch 的 SessionRegistry 提供（界面自己拿不到）；
        # 模式改完要写回注册表并落盘，所以也走注入的回调。
        self._session_source = session_source
        # 「会话」列表按时间排要用的时间表（由 run_watch 注入，见 SessionPanel 的说明）。
        # 默认返回空 —— 没有它的话列表就退回按名字排，不会崩。
        self._session_seen_source = lambda: {}
        self._on_mode_change = on_mode_change
        # 设置保存后把新 cfg 交回 run_watch —— 生成时的人设/关系是从那里读的。
        # ⚠️ 不回调的话，改了「会话级人设」界面显示已保存、生成却还用旧的，
        #    要重启才生效（那正是这个项目最怕的"静默失效"）。
        self._on_config_changed = on_config_changed
        self._on_copy = None            # 复制后通知触发源进入冷静期
        self._on_moved = None           # 用户手动挪了窗口

        # ⚠️ QApplication 必须在任何 QWidget **之前**建好，而且全进程只能一个。
        #    `init_app()` 内部走 `instance() or QApplication(...)`，重复调是安全的。
        init_app([])

        style = Style(cfg, 0, logger=self._log)
        radius = style.px(int(cfg.get("ui.radius", theme.SHELL_RADIUS)
                              or theme.SHELL_RADIUS))
        super().__init__(style, radius=radius)
        # ⚠️ 外壳自绘的底色键必须和内容层一致。
        #    `Panel` 默认铺 `bg`；而**纯文字按钮是透明的**，会直接露出外壳，
        #    两者不一致的话标题栏/状态栏那一圈会是另一种颜色
        #    （Tk 版踩过同族的坑：70 个按钮的底色没跟父容器对齐）。
        self.set_bg_key("bg")
        self._log("界面主题: %s（缩放 %.2f）" % (style.theme_name, style.scale))

        # ---- 队列（子线程 -> UI 线程）----
        self._q: "queue.Queue" = queue.Queue()

        # ---- 界面状态（重建时靠这些把界面恢复回来）----
        self._session_title = "—"
        self._session_is_group = False
        #: 静止时那行字 —— 给一条**能立刻用上**的信息（热键），见 `_idle_status()`。
        self._status = self._idle_status()
        self._intent = ""            # AI 判断的「对方在说什么」
        self._target = ""            # 本次要回复的内容
        self._replies: List[str] = []
        #: 每个会话各自记住自己上一次的建议（**只在内存里**，用户选了不写磁盘）。
        #: 结构：会话名 -> (replies, error, intent)。切走时存、切回时取。
        self._replies_by_session: Dict[str, tuple] = {}
        self._error = ""
        self._prompt = {"system": "", "user": "", "reasoning": ""}
        self._busy = False
        self.collapsed = False
        self._intent_mirror = ""
        # 是否正被「贴边跟随」接管（由 follow.WindowFollower 设置）。
        # 接管期间高度跟着微信窗口走，界面自己不再按内容自适应。
        self._docked = False
        # 用户是否手动拖过窗口 —— 拖过之后这一轮不再跟他抢位置，
        # 下次生成时自动恢复跟随（见 _render_replies）。
        self._user_moved = False
        self._width_resizing = None
        self._min_h_cache = None     # min_height() 的缓存，_build 时失效
        self._size = (self.desired_width(), style.px(BASE_MIN_HEIGHT))
        self._settings_store = None
        self._settings_win = None
        self._prompt_win = None
        self._read_win = None
        #: 系统托盘图标（右下角）。`run()` 里才建，见 `build_tray()`。
        self.tray = None
        #: 这一次读到的对话全文（「只看对话」窗口显示它）。
        #: ⚠️ 它**不进主面板的滚动区** —— 那是 2026-09-27 从主面板搬出来的原因，
        #:    见 `_show_read()` / `ReadWindow` 的说明。
        self._read_text = ""

        # ---- 窗口属性 ----
        self.setWindowTitle("微信 AI 助手")
        # 图标：任务栏 / Alt-Tab / 标题栏都取它。
        # ⚠️ 应用级图标在 `base.apply_app_icon()` 里统一设了，这里**再显式设一次**
        #    是有意的：任务栏按钮归到哪个图标还取决于窗口自己有没有图标
        #    （尤其设置了 `SetCurrentProcessExplicitAppUserModelID` 之后）。
        try:
            from .. import paths

            _ic = QtGui.QIcon(paths.icon_path())
            if not _ic.isNull():
                self.setWindowIcon(_ic)
        except Exception as e:
            self._log("设置窗口图标失败: %s: %s" % (type(e).__name__, e))
        self.setWindowFlag(QtCore.Qt.FramelessWindowHint,
                           bool(cfg.get("ui.frameless", True)))
        # ⚠️ 走 `_apply_topmost()` 而不是在这里直接 `setWindowFlag(..., ui.topmost)`：
        #    生效条件不只看 `ui.topmost`，还要看 `ui.attach`（见 `_want_topmost`）。
        self._apply_topmost()
        self._apply_opacity()
        self.setMouseTracking(True)      # 左右边缘要能改光标

        # ---- 建界面 ----
        self._root_lay = QtWidgets.QVBoxLayout(self)
        self._root_lay.setContentsMargins(0, 0, 0, 0)
        self._root_lay.setSpacing(0)
        self._build()
        self._apply_size()
        self._log("悬浮窗已创建")

    # ================================================================ 构建

    def _clear_layout(self, lay):
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            sub = item.layout()
            if sub is not None:
                self._clear_layout(sub)

    def _content_inset(self) -> int:
        """内容层要缩进多少。

        ⚠️ 这个补丁**必须有**，不然圆角等于白画。实测（抓窗口自己的位图，
            逐像素看四个角）：根布局边距写成 0 时，标题栏是不透明子部件，
            它会一直铺到窗口的 `(0, 0)` —— 把外壳画的圆角整块盖掉。
            **四个角看起来就是直角**，而自绘代码明明画了圆角、也没有任何报错。

        ============ 2026-09-27 改：从 `0.35r` 压到**几何最小值** ============
        原来是 `max(2, int(r*0.35))`（r=15 时是 5px）。那是"方形内容层躲开
        圆角弧线"的**最小安全值**，代价就是四周白白留一圈 ——
        用户的原话是「内部内容尽量贴紧窗口内边界，消除多余四周留白」。

        现在改成**只留外框线那么宽**（`border_w`，本机 3px）：
        因为内容层的**首尾两个部件自己也圆了角**
        （见 `_content_radius` 和 `round_corners_css` / `fill_rounded`），
        方角不再存在，就没什么要躲的了。
        ⚠️ **别把它调成 0**：那会让内容盖住外框线本身（线画在 [0, border_w]）。
        """
        return int(round(self.border_w))

    def _content_radius(self) -> int:
        """内容层首尾部件自己的圆角。

        = 窗口半径 − 缩进量。这样内容层的圆角弧线**正好接在**窗口圆角的弧线上，
        外面看是一条连续的圆角，不会在中间露出窗口底色。
        ⚠️ 别直接用 `self.radius`：那会比窗口的还大，反而啃掉窗口一圈。
        """
        return max(0, int(round(self.radius)) - self._content_inset())

    def _build(self):
        """按当前样式把整个界面搭出来。折叠切换时整体重建。

        ⚠️ 布局顺序决定了空间不够时谁被牺牲。
            Tk 版的血泪：可伸缩的滚动区排在前面、固定尺寸的输入框和按钮排在后面，
            结果**按钮被压到 3px、状态栏根本不显示**。
            正确做法：固定尺寸的按顺序摆好，**可伸缩的滚动区 stretch=1
            吃掉剩下的全部空间**。Qt 的 QVBoxLayout 用 stretch 表达同一件事。
        """
        st = self.style
        self._clear_layout(self._root_lay)
        # 内容层缩进到圆角以内（见 `_content_inset`）
        ins = self._content_inset()
        self._root_lay.setContentsMargins(ins, ins, ins, ins)

        # ⚠️ **必须把最小尺寸约束清零**。重建时旧的内容层是 `deleteLater()`
        #    （删除被推迟到下一轮事件循环），而 Qt 的布局会把它算进窗口的
        #    `minimumSize`。不清的话，折叠成 56x56 会**被那个旧约束顶回去**
        #    —— 实测折叠后窗口卡在 168x270 完全不缩，点一下没反应。
        #    （Tk 版没有这个问题：`pack` 不参与窗口最小尺寸。）
        #
        # ⚠️ 而且要**等旧控件真的销毁之后**再清一次：同步清的这一下会被
        #    尚未销毁的旧布局立刻算回去（实测 `set_docked(300)` 摆出来是 324
        #    —— 正好是被顶到内容最小宽）。所以再排一个 0ms 的补清。
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        QtCore.QTimer.singleShot(0, self._reset_size_constraints)

        self.titlebar = None
        self.lbl_status = None
        self.btn_gen = None
        self.scroll = None
        self.intent_card = None
        self.input = None
        self._min_h_cache = None     # 界面重建 -> 最少高度要重算
        self._retint()

        if self.collapsed:
            self._build_collapsed()
            return

        # ---------------- 标题栏 ----------------
        self.titlebar = TitleBar(self, self)
        self._root_lay.addWidget(self.titlebar, 0)
        # ⚠️ 标题栏下面要留一点空，否则它**紧贴**意图卡，视觉上像一整块
        #    （实测间距 = 0，而左右留白是 18px，上下严重不对称）。
        #    微信自己的标题栏下面也有分隔留白，不是无缝贴住的。
        self._root_lay.addSpacing(st.S("sm"))

        # ---------------- 主体 ----------------
        #
        # ⚠️ 横向边距 12 基准像素（真机 18px）。
        #    微信自己的内容区左右留白就是 12~16px 这个量级；原来写的 10 偏窄，
        #    配合内容层缩进之后视觉左边距只有 15px，跟微信比显得"顶格"。
        # ⚠️ 2026-09-27：改用档位常量。**关键约束**：这里的左右值必须
        #    跟 `TitleBar` 的左边距、以及状态栏文字的左边距**来自同一个档位**，
        #    否则会话名 / 卡片 / 状态文字三者的左边缘对不齐（曾经差 5px，肉眼可见）。
        body = QtWidgets.QWidget(self)
        body.setStyleSheet("background:%s;" % st.p["bg"])
        blay = QtWidgets.QVBoxLayout(body)
        blay.setContentsMargins(st.S("lg"), 0, st.S("lg"), 0)
        blay.setSpacing(0)

        # 意图卡片（顶部）
        self.intent_card = IntentCard(st, self._intent, body)
        blay.addWidget(self.intent_card, 0)
        blay.addSpacing(st.S("sm"))

        # 滚动区（可伸缩，最后吃掉剩余空间）
        self.scroll = ScrollArea(st, "bg", body)
        blay.addWidget(self.scroll, 1)

        # 「我想说」输入区
        #
        # ⚠️ 高度从 58 降到 46 基准像素：58 在真机是 87px，
        #    比微信自己的输入框高出一截，把下面两条按钮挤得没地方。
        #    46 基准像素 = 三行正文的高度，够用又不臃肿。
        self.input = InputBox(st, height=st.px(46),
                              placeholder="我想说…（留空则由 AI 自己判断）", parent=body)
        blay.addSpacing(st.S("sm"))
        blay.addWidget(self.input, 0)
        self.input.textChanged.connect(self._mirror_intent)
        self.input.installEventFilter(self)   # Ctrl+Enter 提交

        # 底部动作区
        # ⚠️ 状态栏的文字左边距也要跟着主体走（12 + 内容层缩进），
        #    否则状态文字比上面的卡片**往左凸出去一截**，一眼就看得出没对齐。
        act = QtWidgets.QWidget(body)
        alay = QtWidgets.QVBoxLayout(act)
        # 输入框 -> 生成按钮：上下都给 `sm`(6)，比原来的 6/5 上下对称
        alay.setContentsMargins(0, st.S("sm"), 0, st.S("sm"))
        alay.setSpacing(st.S("xs"))
        # ⚠️ 主按钮要**整条横贯面板**（Tk 版是 `pack(fill="x")`）。
        #    Qt 版靠 `expand=True` 让布局把它拉满 —— 默认的自动适配宽度
        #    只有 135px，贴在最左边，完全不是微信那种主按钮的样子。
        #    另外**不要**再传死宽度：面板宽度是可拖的，传死就跟着变不回去了
        #    （Tk 版传的 `px(BASE_WIDTH - 40)` 其实就有这个毛病）。
        self.btn_gen = Pill(st, "生成建议", self._regen, variant="primary",
                            expand=True)
        alay.addWidget(self.btn_gen, 0)

        # ⚠️⚠️ 2026-10-14：**「只看对话」和「看提示词」两颗按钮从面板上撤掉了**
        #    （用户要求：「把只看对话和看提示词隐藏起来，可以放到设置里面」）。
        #    面板上现在只有「生成建议」这一颗主按钮 —— 那两个是排查用的，
        #    平时站着位置还容易被当成说明文字（它们原来是 `text` 变体，
        #    灰字、全透明底，看着就像"碰巧能点的标签"）。
        #    入口改到 **设置 → AI 接口 → 系统提示词那一项下面**（见
        #    `settings_ui.SettingsWindow._build_view_tools`）。
        #    ⚠️ `_read()` / `_show_prompt()` **两个方法保留** —— 设置窗口和
        #       托盘那几条路都还调它们，删了会静默失效。
        blay.addWidget(act, 0)

        self._root_lay.addWidget(body, 1)

        # ---------------- 状态行（唯一的状态出口）----------------
        # ⚠️ 左右内边距 = **主体那 12**，不要再加上内容层缩进。
        #    因为状态栏本身就是挂在窗口上、从缩进处开始的，再补一个缩进量
        #    会让文字比卡片左边缘**右偏 5px**（实测 28 vs 23）—— 肉眼能看出来。
        #    ⚠️ 所以这里必须跟 `blay` 用**同一个档位**（`lg`），改一个就得改另一个。
        # 状态小字（用户第 11 条）：上下从 4 收到 `xxs`(2)，
        # 让这一条更薄 —— 它是辅助信息，不该占掉和正文一样的高度。
        pad_h = st.S("lg")
        self.lbl_status = QtWidgets.QLabel(self._status, self)
        self.lbl_status.setFont(st.font("small"))
        # ⚠️⚠️ **状态栏必须允许换行**（2026-09-27 修）。不换行的 `QLabel`，
        #    `minimumSizeHint().width()` **就是整行文字的宽度**；而它在根布局里，
        #    于是**它直接变成了面板的最小宽度** ——
        #    状态文字一变长，窗口就被 Qt 顶着变宽，而缩不回去。
        #    实测（`probes/qt/diag_hover_width.py` [6]）：
        #        把窗口架到 300 → 状态换成「只看对话，未调用 AI（点【生成建议】
        #        或按热键才会生成）」→ 窗口**从 300 跳到 490**（布局下限 490），
        #        状态换回短的之后**还是 490，缩不回去**。
        #    用户看到的就是「鼠标放到【生成建议】上，右边框往右变宽」——
        #    hover 会触发一次重排，把"状态文字撑出来的那个下限"显形。
        #    ⚠️ 不是按钮样式表的问题：hover 前后 `ui.width` 一点没变（[1]~[3] 量过）。
        self.lbl_status.setWordWrap(True)
        # 换行了就**不该再拿文字宽度当下限**，显式把下限放开。
        self.lbl_status.setMinimumWidth(0)
        self.lbl_status.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                      QtWidgets.QSizePolicy.Preferred)
        self.lbl_status.setContentsMargins(pad_h, st.S("xxs"), pad_h, st.S("xxs"))
        # ⚠️ **下面两个角要圆**（`round_corners_css`）。
        #    状态行是内容层最下面那个不透明部件，不圆的话窗口左下/右下
        #    的圆角会被它的方角盖住 —— 就是用户说的「四角漏底层底色」。
        from .layout import round_corners_css
        self.lbl_status.setStyleSheet(round_corners_css(
            "status", self._content_radius(), top=False, bottom=True,
            bg=st.p["surface_alt"]) + " color:%s;" % st.p["text_dim"])
        self._root_lay.addWidget(self.lbl_status, 0)

        self._restore_state()
        self._log("界面已构建（主题 %s）" % st.theme_name)

    def _build_collapsed(self):
        """折叠态：一个小方块，点一下就展开。

        ⚠️ 这里**不绑拖动**。方块太小，拖动和点击会互相抢；
           而位置本来就由跟随器管（贴在微信顶端），不需要手动拖。
        """
        self.collapse_bar = CollapseBox(self.style, self)
        self.collapse_bar.mouseReleaseEvent = self._collapse_clicked
        self._root_lay.addWidget(self.collapse_bar, 1)

    def _reset_size_constraints(self):
        """把窗口的最小尺寸约束清掉（见 `_build()` 里的说明）。

        只在**明确需要小巧**的场合补一刀：折叠态、以及被跟随器接管时
        （跟随器给什么尺寸就得是什么尺寸，不能被内容的最小宽顶回去）。
        """
        try:
            if self.collapsed or self._docked:
                self.setMinimumSize(0, 0)
        except Exception:
            pass

    def _collapse_clicked(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self.toggle_collapse()

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        self._build()
        # 原地折叠/展开。跳回屏幕角落的话，贴边跟随就白做了
        # （实测反馈过"缩小后跑到右上角去"）。
        self._apply_size(reposition=False)
        if not self.collapsed:
            self.show()

    def _retint(self):
        """外壳（圆角 + 边框）的颜色在 `paintEvent` 里按 `style.p` 现读现画，
        所以换主题只要换 `self.style` 再 `update()` 就行 —— 这里只负责重刷。"""
        self.update()

    # ================================================================ 状态恢复

    def _restore_state(self):
        """把状态推回控件（切主题、折叠切换都会走这里）。"""
        self._sync_status()
        if self.intent_card is not None:
            self.intent_card.set_text(self._intent)
        self._refresh_target_label()
        if self._intent_mirror and self.input is not None:
            self.input.set(self._intent_mirror)
        self._render_replies(self._replies, self._error, self._intent)

    def _idle_status(self) -> str:
        """静止时状态行显示什么。

        ⚠️ 2026-10-13 改（用户要求"悬浮窗下面的提示精简一点"）：
            原来是 **「等待触发…」** —— "触发"是我们代码里的说法，
            用户看到的只是一个不懂的词，而且它**没告诉他下一步做什么**。
            现在改成给他**唯一需要知道的快捷键**；没配热键才退回一句动作提示。
        ⚠️ 保持短。状态行是**曾经把面板顶宽**的那一处
            （不换行 QLabel 的 `minimumSizeHint` 会变成窗口最小宽度，
            实测 300 → 490 缩不回去，见 `lbl_status` 的说明）。
        """
        try:
            hk = (self.cfg.get("trigger.on_hotkey", "") or "").strip()
        except Exception:
            hk = ""
        return ("按 %s，或点【生成建议】" % hk) if hk else "点【生成建议】"

    def _sync_status(self):
        # 折叠态没有状态栏（不是"还没建"，是这一版界面压根没有）—— 直接跳过
        if self.lbl_status is None:
            return
        try:
            self.lbl_status.setText(self._status)
            # ⚠️ **不要 `adjustSize()`**：状态栏现在是**换行**的，
            #    `adjustSize()` 会把它按 `sizeHint` 撑成一行那么宽，
            #    等于把上面好不容易放开的最小宽度又还回去了。
            #    （它换行 + 横向 sizePolicy=Ignored 之后，
            #      布局会自己把宽度定成"可用宽度"，不需要手动调。）
            self.lbl_status.updateGeometry()
        except Exception:
            pass

    def _refresh_target_label(self):
        """回复目标原本只画在「详情」抽屉里。抽屉已删除，这里保留成空实现，
        免得几处调用点都要改。**目标内容仍然会存进 self._target**，
        需要时可以再画出来。"""
        return

    def _content_width(self) -> int:
        """滚动区实际可用的宽度。

        ⚠️ Tk 版要在这里小心 `winfo_width() or 默认值` 的陷阱（布局前返回 1，
            而 1 是真值）。Qt 里控件没显示时 `width()` 也可能是 0 或 100，
            所以同样先判"太小就按面板宽度估一个"。
        """
        st = self.style
        w = self.scroll.content_width() if self.scroll is not None else 0
        if w <= 1:
            panel = int(self.cfg.get("ui.width", BASE_WIDTH) or BASE_WIDTH)
            w = st.px(panel) - st.px(20)
        return max(st.px(160), int(w))

    # ================================================================ 回复列表

    def _render_replies(self, replies: List[str], error: str, intent: str = ""):
        self._replies = list(replies or [])
        self._error = error or ""
        self._intent = intent or ""
        # 新内容来了 -> 恢复跟随（用户之前手动拖过窗口的话，这里把它拉回来）。
        # ⚠️ 这一句必须放在**所有早退之前**。原来它在 `not replies` 那一支的后面，
        #    于是「生成失败」「没有建议」「折叠态」这三种情况下 `_user_moved`
        #    永远不复位 —— 用户看到的是"拖动之后跟随再也回不来了"。
        self._user_moved = False

        # 折叠态是一个小方块，**没有**意图卡和回复列表。
        # 这里只把状态存下来，展开时 _restore_state() 会把它推回界面。
        if self.collapsed or self.scroll is None:
            return

        if self.intent_card is not None:
            self.intent_card.set_text(self._intent, self.intent_card.width())
        self.scroll.clear()

        st = self.style
        if error:
            lab = QtWidgets.QLabel("生成失败：%s" % error)
            lab.setFont(st.font("label"))
            lab.setWordWrap(True)
            lab.setStyleSheet("color:%s; background:transparent;" % st.p["danger"])
            self.scroll.add(lab)
            self._status = "生成失败"
            self._sync_status()
            return

        if not replies:
            # ⚠️ 2026-10-13 精简（用户要求"悬浮窗下面的提示精简一点"）：
            #    原来写「还没有建议 —— 点下面的「生成建议」」，把"没有建议"这件事
            #    说了两遍（空区域本身就是证据）。**句子越短越好** ——
            #    状态/空态文字曾经把面板顶宽过（见 `lbl_status` 那段说明）。
            lab = QtWidgets.QLabel("还没有建议 · 点下面的【生成建议】")
            lab.setFont(st.font("label"))
            lab.setWordWrap(True)
            lab.setStyleSheet("color:%s; background:transparent;" % st.p["text_faint"])
            self.scroll.add(lab)
            return

        w = self._content_width()
        for i, text in enumerate(replies, 1):
            card = Card(st, text, index=i, on_click=self._copy, width_px=int(w),
                        parent=self.scroll.inner)
            # ⚠️ 不需要 Tk 版的 `SCROLL_PAD`：Qt 的滚动条是**独立部件**，
            #    不压内容，卡片右边缘不会被盖住。
            lay = QtWidgets.QVBoxLayout()
            lay.setContentsMargins(0, 0, 0, st.px(6))
            lay.addWidget(card)
            self.scroll.add_layout(lay)
        self.scroll.to_top()
        # 卡片建好之后才知道内容多高，这时再决定窗口要不要长高/变矮
        # （被跟随接管时 _fit_height 会自己让位，不会跟微信高度打架）
        #
        # ⚠️ **必须是"稍后"再量，不能当场量。** 实测踩过：
        #    这里直接调 `_fit_height()`，它读到的 `scroll.content_height()`
        #    只有 **27px** —— 卡片刚 create、Qt 还没跑布局，`sizeHint` 是个
        #    半成品。于是窗口按"27px 内容"被缩到 420，
        #    而**之后再没有第二次机会**：内容后来长到 576，窗口却停在 420，
        #    滚动区只剩 68px，三条回复根本看不全。
        #    （实测症状：不管几条回复，面板高度都是 423 左右、恒定不变。）
        #    排到本轮事件循环末尾（布局跑完之后）再量，拿到的才是真实高度。
        QtCore.QTimer.singleShot(0, self._fit_height)

        self._status = "已生成 %d 条 · 点一下卡片就复制" % len(replies)
        self._sync_status()

    def _copy(self, text: str):
        try:
            QtWidgets.QApplication.clipboard().setText(text)
            self._status = "已复制，去微信粘贴发送"
            self._sync_status()
            self._log("已复制: %r" % text[:40])
            # 反馈要发生在手指点的地方，而不是屏幕底部
            for i in range(self.scroll._lay.count()):
                item = self.scroll._lay.itemAt(i)
                lay = item.layout() if item is not None else None
                if lay is None:
                    continue
                w = lay.itemAt(0).widget() if lay.count() else None
                if isinstance(w, Card) and w.text == text:
                    w.flash()
            # 通知外部「用户正在去粘贴」，触发源据此进入冷静期，避免打扰
            cb = getattr(self, "_on_copy", None)
            if cb:
                cb()
        except Exception as e:
            self._status = "复制失败: %s" % e
            self._sync_status()

    # ================================================================ 我想说

    def _mirror_intent(self, *_):
        """把「我想说」的内容镜像成普通字符串，供其它线程安全读取。

        Qt 控件只能由 UI 线程碰，而**热键触发跑在子线程**，
        所以让子线程读这份镜像（只读，不改控件）。
        """
        try:
            self._intent_mirror = self.input.get()
        except Exception:
            pass

    def peek_intent(self) -> str:
        """线程安全地读「我想说」的当前内容（**不清空**）。热键路径用它。"""
        return self._intent_mirror or ""

    def clear_intent(self):
        """请 UI 线程清空「我想说」——子线程用掉内容后调用它。"""
        self.post("clear_intent", None)

    def _take_intent(self) -> str:
        """取出「我想说」的内容并清空（只在 UI 线程调用）。

        用完即清是有意的：这句话是「这一次」要表达的意思。留着的话，
        下次你切到别的会话再点生成，上一句会被当成本会话的意图用 —— 那比不填还糟。
        """
        try:
            txt = self.input.get()
            self.input.clear()
            self._intent_mirror = ""
            return txt
        except Exception:
            return ""

    def eventFilter(self, obj, ev):
        """输入框里 Ctrl+Enter = 提交并生成。"""
        if (obj is self.input and ev.type() == QtCore.QEvent.KeyPress
                and ev.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter)
                and ev.modifiers() & QtCore.Qt.ControlModifier):
            self._regen()
            return True
        return super().eventFilter(obj, ev)

    # ================================================================ 动作

    def _regen(self):
        """主按钮：读取 + 调 AI 生成建议。"""
        # ⚠️ 没接回调时**要说话**，不能静默返回。
        #    界面预览时装假数据、不接生成功能 —— 以前点它什么反应都没有，
        #    看起来像"UI 没接上"（实测被问过）。
        if not self._on_regenerate:
            self._status = "生成功能未接入"
            self._sync_status()
            return
        # 明确要求内容 -> 面板归位、重新跟着微信。
        # ⚠️ 用户手动拖过窗口的话 `_user_moved` 是 True；这里是**明确意图**的入口
        #    （点按钮 / 按热键），归一一下位置是符合预期的。
        #    另一条恢复路径在 `follow._apply()`：微信窗口自己一动也会恢复。
        self._user_moved = False
        intent = self._take_intent()
        self.btn_gen.set_text("生成中…")
        self.btn_gen.set_enabled(False)
        self._status = "正在按你说的意思生成…" if intent else "正在生成建议…"
        self._sync_status()
        self._on_regenerate(intent)

    def _read(self):
        """次按钮：只读取当前会话并显示对话，不调用 AI（不花钱）。"""
        if not self._on_read:
            self._status = "读取功能未接入"
            self._sync_status()
            return
        self._user_moved = False        # 同上：明确意图 -> 归位
        self._status = "正在读取会话…"
        self._sync_status()
        self._on_read()

    def _clear(self):
        """清空当前会话的历史上下文。

        ⚠️ **目前界面上没有入口** —— 原来在详情面板里有个「清空上下文」按钮，
            但它是破坏性操作，放在随手能点到的地方不合适，用户反馈"用处不大"，
            已经删掉了。这个方法 + `_on_clear` 回调**保留着**，
            需要时可以放进设置窗口（那里有确认对话框的位置）。
        """
        if not self._on_clear:
            self._status = "清空功能未接入"
            self._sync_status()
            return
        self._status = "正在清空上下文…"
        self._sync_status()
        self._on_clear()

    def _stash_replies(self, title: str):
        """把当前会话的建议收进内存缓存（换走时调）。

        ⚠️ 用户要求「**保留着这个会话的建议**，切回去还有」，而且选的是
            **只在本次运行内保留**（不写磁盘）—— 所以这里就是一个普通 dict，
            进程退出即失，不碰 `state/`。
        ⚠️ `"—"` / `""` 是"当前没有打开会话"，不给它建条目。
        ⚠️ 缓存要有上限：一次运行里切过几百个会话就无限涨了。
        """
        if title in ("", "—"):
            return
        self._replies_by_session[title] = (list(self._replies), self._error,
                                           self._intent)
        # 超了就丢最旧的（dict 保序，直接删前面的）
        cap = 50
        if len(self._replies_by_session) > cap:
            for k in list(self._replies_by_session)[:len(self._replies_by_session) - cap]:
                del self._replies_by_session[k]

    def _load_replies(self, title: str):
        """取某个会话缓存的建议；没有就给一组空值。"""
        return self._replies_by_session.get(title, ([], "", ""))

    def _switch_session(self, old_title: str, new_title: str):
        """换会话：**把旧的收起来，把新的取出来**。

        ⚠️ 这里取代了早先"换会话就清空"的做法。用户先要求"换会话别留着"
            （怕把上一个会话的建议发到当前会话），看了之后改成
            "**每个会话各自记住**，切回去还在" —— 两个诉求都成立：
            清空解决的是"张冠李戴"，缓存既解决了它（B 会话看不到 A 的建议）、
            又不用把 A 的建议丢掉。
        ⚠️ 状态栏要**明说**这是上次的建议，不然用户会以为是自己刚生成的。
        """
        self._stash_replies(old_title)
        reps, err, intent = self._load_replies(new_title)
        self._render_replies(reps, err, intent)
        # ⚠️ 打一行日志：换会话没生效时，这行能区分
        #    "根本没收到会话变化" 和 "收到了但没恢复" —— 不然只能猜。
        self._log("会话切换: %r -> %r（新会话缓存建议 %d 条，共缓存 %d 个会话）"
                  % (old_title, new_title, len(reps),
                     len(self._replies_by_session)))
        if reps:
            self._status = "已切回「%s」（上次的结果）" % new_title
            self._sync_status()

    def _apply_busy(self, busy: bool, tip: str):
        # ⚠️ 折叠态**没有** btn_gen（小方块只有那一块），
        #    而且 `_build` 会先把这些引用清成 None —— 不判空的话
        #    这里会抛 `'NoneType' object has no attribute ...`，
        #    被 _pump 吞掉，表现是"忙闲状态不更新"（实测日志里出现过）。
        try:
            if self.btn_gen is not None:
                self.btn_gen.set_enabled(not busy)
                self.btn_gen.set_text("生成中…" if busy else "生成建议")
            if tip:
                self._status = tip
                self._sync_status()
        except Exception as e:
            self._log("按钮状态更新失败: %s" % e)

    # ================================================================ 看提示词

    def _show_prompt(self):
        """把最近一次**实际提交**给模型的提示词显示出来（一字未改）。"""
        d = self._prompt
        if not (d.get("system") or d.get("user")):
            self._status = "还没有提示词 · 点【生成建议】"
            self._sync_status()
            return

        if self._prompt_win is None:
            self._prompt_win = PromptWindow(self.style, self._copy_prompt)
        win = self._prompt_win
        win.set_style(self.style)

        sys_txt = d.get("system") or ""
        usr_txt = d.get("user") or ""
        rsn_txt = d.get("reasoning") or ""
        parts = ["========== system（%d 字）==========\n%s\n" % (len(sys_txt), sys_txt),
                 "========== user（%d 字）==========\n%s\n" % (len(usr_txt), usr_txt)]
        # 模型「先想再答」的那段。它照样计费（实测占输出 token 的八成），
        # 而且判错时从这里最容易看出「它为什么这么判」。
        if rsn_txt:
            parts.append("========== 模型的思考过程（%d 字，会计费）==========\n%s"
                         % (len(rsn_txt), rsn_txt))
        else:
            parts.append("========== 模型的思考过程 ==========\n（这次没有返回思考内容）")
        win.txt.setPlainText("\n\n".join(parts))
        win.show()
        win.raise_()
        win.activateWindow()
        return win

    def _copy_prompt(self):
        """把提示词全文写进剪贴板（方便贴给别人看或存起来排查）。"""
        d = self._prompt
        whole = ("========== system ==========\n%s\n\n"
                 "========== user ==========\n%s\n\n"
                 "========== 模型的思考过程 ==========\n%s"
                 % (d.get("system") or "", d.get("user") or "",
                    d.get("reasoning") or "（无）"))
        try:
            QtWidgets.QApplication.clipboard().setText(whole)
            self._status = "提示词已复制"
            self._sync_status()
        except Exception as e:
            self._status = "复制提示词失败: %s" % e
            self._sync_status()

    # ================================================================ 只看对话

    def _show_read(self):
        """把这一次读到的对话**单独开一个窗口**显示出来。

        ⚠️ 2026-09-27 从主面板搬出来的（用户提的：「我感觉只看对话也用单独窗口来吧」）。
            原来它 `show_replies(对话全文)` —— 把整段对话当成"建议"塞进主面板，
            于是①刚生成的建议被顶掉、②长对话把主面板撑成一坨可滚动的东西，
            状态栏那行小字跟着变长，而**状态栏文字宽度就是面板的最小宽度**，
            面板就被顶着变宽（用户报的「鼠标放到【生成建议】上右边框往右变宽」）。
        """
        txt = self._read_text
        if not txt:
            self._status = "还没有对话 · 设置里可看"
            self._sync_status()
            return None

        if self._read_win is None:
            self._read_win = ReadWindow(self.style, self._copy_read)
        win = self._read_win
        win.set_style(self.style)
        win.txt.setPlainText(txt)
        win.show()
        win.raise_()
        win.activateWindow()
        return win

    def _copy_read(self):
        """把对话全文写进剪贴板。"""
        try:
            QtWidgets.QApplication.clipboard().setText(self._read_text or "")
            self._status = "对话已复制"
            self._sync_status()
        except Exception as e:
            self._status = "复制对话失败: %s" % e
            self._sync_status()

    # ================================================================ 尺寸与位置

    def min_height(self) -> int:
        """固定内容最少需要的高度（**带缓存**）。

        跟随器每秒问 20 次，每次真算一遍要跑一遍布局，太贵了。
        缓存失效点在 `_build()`。
        """
        if self._min_h_cache is None:
            self._min_h_cache = self._min_height()
        return self._min_h_cache

    def _reply_area_floor(self) -> int:
        """滚动区**最少**要留多高。

        ⚠️ 以前这里是写死的 `px(150)`（真机 225px）。实测的问题是：
            **三条回复要 255px 才装得下**，而 150 基准像素只给了 225px ——
            于是"三条回复尽量一屏全见"这个目标**根本没达成**：
            面板永远卡在 465px，滚动区只有 119px，看第三条必须滚动。

        现在按"三条回复"算一个**实际**的下限：量第一条卡片的实际高度
        （它已经按当前宽度换行过了，所以是真实值），三条 + 卡片之间的间距。
        还没出回复时退回一个不带内容的估计值。

        ⚠️ **每次调用都要让 `min_height()` 的缓存失效。**
            实测踩过：卡片刚建出来时很矮（还没换行），这时算出的下限是 450；
            等布局跑完卡片长高了，**缓存还留着 450** ——
            于是 `_target_height()`（它内部 `max(min_height(), ...)`）
            拿到的是过期的 450，窗口就停在 450，而真实下限已经是 504。
            症状是"窗口比它自己声称的下限还矮"。
            这里是唯一会随内容变化的部分，所以失效点放这里最省事。
        """
        self._min_h_cache = None
        st = self.style
        cards = []
        if self.scroll is not None:
            lay = self.scroll._lay
            for i in range(lay.count()):
                sub = lay.itemAt(i).layout()
                if sub is None:
                    continue
                w = sub.itemAt(0).widget()
                if w is not None and w.__class__.__name__ == "Card":
                    cards.append(w)
        if cards:
            h = max(c.height() for c in cards)
            return max(st.px(120), h * 3 + st.px(6) * 3)
        # 还没生成过：按"一条两行的卡片"粗估
        return st.px(200)

    def _min_height(self) -> int:
        """装得下所有**固定部分** + 三条回复所需的高度。

        为什么要算：标题栏、意图卡、输入框、按钮、状态栏是硬需求，滚动区才是
        可伸缩的。窗口比硬需求还矮的话，滚动区会被压成 0
        —— 看起来像"下面的内容看不见"。

        ⚠️⚠️ 2026-10-14 用户要求「**默认高度最小改大一点，大概就是目前微信高度**」
            （他机器上量到微信常规窗口 **623 设备像素 = 415 基准**）：
            所以这里再夹一道 `BASE_MIN_HEIGHT`（420 基准 = 630 设备像素）——
            面板**再矮也不会矮过它**，不会缩成一条。
            ⚠️ 夹在这里而不是 `follow.py` 里另写一个数：`follow._panel_size()`
                本来就是 `max(st.px(260), ui.min_height())`，
                从**这个方法**抬，两个地方就只有**一个**真值来源。
        """
        st = self.style
        try:
            fixed = st.px(30)          # 各段上下间距的粗估
            for w in (self.titlebar, self.intent_card, self.input,
                      self.btn_gen, self.lbl_status):
                if w is not None:
                    fixed += self._widget_height(w)
            return max(st.px(BASE_MIN_HEIGHT), fixed + self._reply_area_floor())
        except Exception:
            return st.px(BASE_MIN_HEIGHT)

    def _widget_height(self, w) -> int:
        """控件的"需要高度"。

        ⚠️ 固定高度的控件用 `height()`，可换行的用 `sizeHint()` ——
            意图卡的文字还没排过版时 `height()` 是 0，直接加进去会算矮。
        """
        try:
            if w is self.intent_card:
                return int(w.sizeHint().height())
            return int(w.height()) or int(w.sizeHint().height())
        except Exception:
            return 0

    def desired_width(self) -> int:
        """当前该用多宽 —— 折叠态是**小方块**。跟随器也用这个值。"""
        if self.collapsed:
            return self.style.px(COLLAPSED_SIZE)
        return self.style.px(int(self.cfg.get("ui.width", BASE_WIDTH) or BASE_WIDTH))

    def desired_size(self):
        """当前该用多大 (宽, 高)。折叠态是正方形。"""
        if self.collapsed:
            s = self.style.px(COLLAPSED_SIZE)
            return s, s
        return self.desired_width(), None

    def _apply_size(self, reposition: bool = True):
        """定尺寸与位置，高度随后交给 `_fit_height()` 按内容决定。

        ⚠️ `reposition=False` 用于**折叠 / 切主题**这类"重建界面"的场合 ——
            这时必须**保持窗口原来的位置**。以前一律调 `_default_pos()`，
            结果一按折叠，窗口就从微信旁边跳回屏幕右上角（实测反馈过）。
        """
        st = self.style
        if self.collapsed:
            # 折叠态是个**正方形小方块**，不跟微信高度
            w = h = st.px(COLLAPSED_SIZE)
        else:
            w = self.desired_width()
            if reposition:
                fixed = int(self.cfg.get("ui.height", 0) or 0)
                h = st.px(fixed) if fixed > 0 else st.px(BASE_MIN_HEIGHT)
            else:
                h = self._size[1]
        self._size = (w, h)
        if reposition:
            x, y = self._default_pos(w, h)
        else:
            x, y = self.x(), self.y()
        self.setGeometry(int(x), int(y), int(w), int(h))
        if not self.collapsed:
            self._fit_height()

    def _default_pos(self, w, h):
        """默认落在屏幕右侧靠上（贴边跟随由 follow.py 接管后会覆盖它）。"""
        try:
            from ..follow import work_area

            gl, gt, gr, _gb = work_area()
            return (max(gl, gr - w - self.style.px(24)), gt + self.style.px(int(120)))
        except Exception:
            return (100, 100)

    def set_geometry(self, x: int, y: int, w: Optional[int] = None,
                     h: Optional[int] = None):
        """外部（贴边跟随）指定窗口位置与尺寸。"""
        cur_w, cur_h = self._size
        w = w or cur_w
        h = h or cur_h
        self._size = (int(w), int(h))
        self.setGeometry(int(x), int(y), int(w), int(h))

    def _target_height(self) -> int:
        """按内容算出来的目标高度（**只算，不动窗口**）。

        ⚠️ 单独抽出来是为了让 `clear_docked()` 能判"高度还需要纠正吗"。
            它如果自己算一遍，两处的公式迟早会漂移 ——
            那时"要不要纠正"和"纠正成多少"用的就不是同一个数了。
            跟 `_min_height()` 拆成 `min_height()` / `_min_height()` 同一个道理。
        """
        st = self.style
        fixed_cfg = int(self.cfg.get("ui.height", 0) or 0)
        if fixed_cfg > 0:
            return st.px(fixed_cfg)
        fixed = st.px(30)          # 各段上下间距的粗估
        for w in (self.titlebar, self.intent_card, self.input,
                  self.btn_gen, self.lbl_status):
            if w is not None:
                fixed += self._widget_height(w)
        content = max(st.px(60), self.scroll.content_height()
                      if self.scroll is not None else 0)
        h = fixed + content + st.px(20)
        return max(self._min_height(), min(h, int(self._screen_height() * 0.85)))

    def _fit_height(self):
        """按内容算窗口高度 —— **只在没被「贴边跟随」接管时**才用。

        跟随接管时（`ui.attach: dock` 且微信在）高度跟微信窗口对齐，
        这个方法直接让位，免得两边互相打架、高度来回跳。

        没接管时按内容算，目标是让三条回复尽量一屏全见：

            高度 = 固定部分 + 意图卡 + 回复内容
            下限 = 装得下固定部分 + 一块最小回复区
            上限 = 屏幕高度的 85%（**超过才开始滚动**）

        另外 `ui.height` 配了正数时优先用它（手动钉一个固定高度）。
        """
        if self.collapsed or self._docked:
            return
        h = self._target_height()
        w, _ = self._size
        self._size = (w, h)
        self.move(self.x(), self.y())
        self.resize(int(w), int(h))

    def _screen_height(self) -> int:
        try:
            scr = self.screen() or QtWidgets.QApplication.primaryScreen()
            return int(scr.availableGeometry().height())
        except Exception:
            return 900

    def resizeEvent(self, ev):
        """窗口尺寸被**外部**改过之后，重新按内容算一次高度。

        ⚠️ 为什么必须有它：`_fit_height()` 只在"我主动调它"的地方跑
            （`_render_replies` / `_apply_size` / `clear_docked`）。
            而窗口高度还可能被**别人**改：
              · 用户拖窗口边框 / 系统缩放
              · `resize()` 被 Qt 的 `minimumSize` 顶了一下（尺寸被夹过）
              · 跟随器刚解绑、尺寸约束刚被清掉的下一拍
            没有这个钩子的话，窗口就停在那个中间值上**再也不会自愈** ——
            实测踩过：正常 680 的窗口，被压一次之后停在 450，
            而 `min_height()` 明明说下限是 504（**比下限还矮**），
            手动调一次 `_fit_height()` 立刻回到 680。

        ⚠️ 排到事件循环末尾再算，不要在 `resizeEvent` 里同步调 ——
            那会在"resize → 重新布局 → 又 resize"之间来回，栈直接吃掉。
            延迟一拍之后布局已经稳定，算出来是准的，而且
            `_fit_height()` 是幂等的（目标一样就不动窗口），会自己收敛。
        """
        super().resizeEvent(ev)
        if not self.collapsed and not self._docked:
            QtCore.QTimer.singleShot(0, self._fit_height)

    # ================================================================ 贴边跟随
    #
    # 由 `follow.WindowFollower` 在 UI 线程上驱动（见 follow.py）。
    # 「面板跟着微信长度」就是在这里落地的：接管期间高度跟微信窗口对齐，
    # 界面自己不再按内容自适应。

    def set_docked(self, x, y, w, h):
        """跟随器指定的位置与尺寸。

        **尺寸没变就只挪位置。** 写成「先 resize 再 move」会让 Qt 重新跑一遍
        整棵布局（每张卡片都要重新换行、滚动条要重算），拖动微信窗口时就是
        一片卡顿（Tk 版实测反馈过）。

        ⚠️ `self.resize()` 之后**不要**再跟一个 `self.move()`：
            Qt 的 `resize` 会保留左上角，位置不会被带动；
            但反过来先 move 再 resize 在某些平台上会被 WM 拉回来。
            这里固定成 resize -> move 的顺序，和 Tk 版「尺寸变了就一起设」等价。

        跟随器那边已经做过「值没变就不调」的去重，这里不再重复判断。

        ⚠️⚠️ **判据必须是"窗口实际尺寸"，不能是 `self._size`**（2026-09-27 修）。
            原来写的是 `old_w, old_h = self._size`，而 `_size` 只是面板自己的
            **信念**，它和真实窗口会分叉：

              `resize()` 在某些时刻会被布局的最小宽度顶一下（那一刻状态栏/
              目标卡里正好有一条长文本，布局下限比请求值大），于是真实窗口
              比请求值宽，而 `_size` 记的还是请求值。

            一旦分叉，这里判 `(w, h) == _size` 成立 → **直接跳过 resize**，
            分叉就**永远修不回来**。症状正是用户报的那条日志：

                面板几何被外部改过（(..., 406, ...) -> (..., 463, ...)），重新跟随

            `_verify()` 清掉 `_last` → 重新下发 406 → 这里又跳过 → 实际还是 463
            → 一秒后再记一次……**无限循环**，而且每次重摆都要整棵布局重排
            （那是最贵的那条路），还会刷满日志。

            拿**实际尺寸**比就没有这个问题：只要真实窗口和请求不符就 resize。
            代价只是"尺寸确实没变"时多一次比较，没有额外开销。
        """
        x, y, w, h = int(x), int(y), int(w), int(h)
        old_w, old_h = self.width(), self.height()
        self._docked = True
        self._size = (w, h)
        if (w, h) != (old_w, old_h):
            # 跟随器说了算：内容的最小宽不许把它顶回去
            self.setMinimumSize(0, 0)
            self.resize(w, h)
        self.move(x, y)

    def clear_docked(self):
        """停止跟随（微信关了 / 配置关掉了），把高度控制权还给界面自己。

        ⚠️ **不能只判 `_docked` 就早退。** 实测踩到过一个**卡死**：
            面板已经停在微信那个高度（780），但 `_docked` 是 `False` ——
            于是这里直接 return，高度**永远**回不到内容自适应，
            窗口就一直保持一个跟内容无关的高度，而且看不出哪里不对。
            （探针里量到的原话：`_docked=False h=780 min_height=496`。）

            所以判据要落在**"高度还需要纠正吗"**上，而不是"我刚才有没有在跟随"：
            只要当前不是折叠态、且高度跟目标不符，就重算一次。
            `_fit_height()` 是幂等的（算出来一样就不动窗口），重复调用没有代价。
        """
        was = self._docked
        self._docked = False
        if self.collapsed:
            return
        if was or self.height() != self._target_height():
            self._fit_height()

    # ================================================================ 拖左右边缘调宽度
    #
    # 无边框窗口没有系统边框，得自己实现。
    # 内容层缩进了约 0.35R（圆角以内），所以面板最外那圈是"空白"的 ——
    # 热区放在那里，不会跟里面的卡片抢鼠标。

    def _edge_zone(self) -> int:
        return self.style.px(6)

    def _near_h_edge(self, x_local: int) -> int:
        """鼠标是否贴着窗口的左右边缘。返回 1=左缘 / -1=右缘 / 0=不在边缘。"""
        zone = self._edge_zone()
        if x_local <= zone:
            return 1
        if self.width() - x_local <= zone:
            return -1
        return 0

    def mouseMoveEvent(self, ev):
        edge = self._near_h_edge(int(ev.position().x()))
        self.setCursor(QtCore.Qt.SizeHorCursor if edge else QtCore.Qt.ArrowCursor)
        if self._width_resizing is not None:
            self._width_drag(ev)
        super().mouseMoveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            edge = self._near_h_edge(int(ev.position().x()))
            if edge:
                gp = ev.globalPosition().toPoint()
                self._width_resizing = (edge, gp.x(), self.width(), self.x())
                ev.accept()
                return
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._width_resizing is not None:
            self._width_resizing = None
            self._save_width()
        super().mouseReleaseEvent(ev)

    def _width_drag(self, ev):
        edge, x0, w0, rx0 = self._width_resizing
        dx = int(ev.globalPosition().x()) - x0
        st = self.style
        lo = st.px(int(self.cfg.get("ui.min_width", 200) or 200))
        hi = st.px(int(self.cfg.get("ui.width_max", 640) or 640))
        if edge > 0:
            # 拖左边缘：窗口左边跟着走，宽度反向变化 —— 右边那条边保持不动
            w = max(lo, min(hi, w0 - dx))
            x = rx0 + (w0 - w)
        else:
            w = max(lo, min(hi, w0 + dx))
            x = rx0
        self._size = (w, self._size[1])
        self.setGeometry(int(x), int(self.y()), int(w), int(self.height()))

    def _save_width(self):
        """把拖出来的宽度记住，跟随器下一拍就会用新宽度。

        同时更新**内存里的 cfg** —— 跟随器读的是同一个 cfg 对象，
        不更新的话它还会按旧宽度摆，下一次跟随就把你拖的结果覆盖回去。
        """
        try:
            w = int(round(self._size[0] / max(0.01, self.style.scale)))
            self.cfg._data.setdefault("ui", {})["width"] = w
            from ..settings import SettingsStore

            st = SettingsStore(logger=self._log)
            st.set("ui.width", w)
            st.save()
            self._log("面板宽度已记住: %d" % w)
            self._status = "宽度已记住：%d" % w
            self._sync_status()
        except Exception as e:
            self._log("记住宽度失败: %s" % e)

    # ================================================================ 标题栏交互

    def _titlebar_action(self, action: str):
        if action == "settings":
            self.open_settings()
        elif action == "collapse":
            self.toggle_collapse()
        elif action == "hide":
            self.hide()

    def _clamp_pos(self, nx: int, ny: int):
        """把 (nx, ny) 夹进桌面可用区，返回夹过的坐标。

        ⚠️ 为什么必须夹：无边框窗口**没有任务栏条目、alt-tab 也切不到**，
            一旦被拖到屏幕外，就只能靠热键找回来了（实测反馈过"移动之后点不了"）。
            竖直方向不留余量（标题栏必须能抓到），水平方向允许露出一部分。

        ⚠️ 抽成独立函数是因为它有**两个**调用点，而两者的副作用完全不同：
            `move_clamped()` 会标记"用户拖过"；`move_only()` **不能**标记
            （跟随器每一拍都在调它，标记了就变成"每拍都重新定基准"，永远不动）。
            夹取逻辑只有一份，免得两边漂移。
        """
        try:
            from ..follow import work_area

            gl, gt, gr, gb = work_area()
            w, _h = self._size
            keep = self.style.px(60)
            nx = max(gl - w + keep, min(nx, gr - keep))
            ny = max(gt, min(ny, gb - self.style.px(38)))   # 标题栏始终可见
        except Exception:
            pass
        return int(nx), int(ny)

    def move_only(self, x: int, y: int):
        """**只挪位置**：不动尺寸、不改"谁控制高度"、也**不算用户拖过**。

        固定悬浮模式（`ui.attach: float`）用：位置由跟随器按"你定的相对偏移"推，
        高度仍然由界面自己按内容自适应。

        ⚠️ **不要改写成调 `move_clamped()`** —— 它会把 `_user_moved` 置真，
           而跟随器看到那个标记就会"重新定基准"，于是每一拍都把基准
           重新定义成"面板现在的位置"，**窗口永远不动**（跟随静默失效）。
        """
        x, y = self._clamp_pos(x, y)
        self.move(x, y)

    def move_clamped(self, nx: int, ny: int):
        """把窗口挪到 (nx, ny)，夹在桌面可用区里。**用户拖动走这条。**"""
        x, y = self._clamp_pos(nx, ny)
        self.move(x, y)
        # 用户自己挪了位置 -> 记下来（dock 下"微信一动就拉回边上"，
        # float 下"重新定基准"，两条路都读这个标记）
        self._user_moved = True
        cb = getattr(self, "_on_moved", None)
        if cb:
            cb()

    # ================================================================ 显示 / 隐藏

    def show(self):
        """显示窗口。

        ⚠️ **不要在这里 `raise_()`。** 那会把自己抬到所有窗口之上 ——
            实测反馈过"我打开其他页面，他也弹出来"。层级由跟随器管：
            只在**微信是前台窗口**时才把面板插到微信正上方（见 follow）。
        """
        try:
            super().show()
        except Exception as e:
            self._log("显示窗口失败: %s" % e)
        self._sync_tray()

    def hide(self):
        try:
            super().hide()
        except Exception as e:
            self._log("隐藏窗口失败: %s" % e)
        # ⚠️ 藏起来之后**任务栏按钮也没了**（Windows 的行为）—— 托盘图标是唯一
        #    还留在屏幕上的入口，所以它的菜单文案必须跟着变。
        self._sync_tray()

    def is_visible(self) -> bool:
        try:
            return bool(self.isVisible())
        except Exception:
            return True

    def _on_ui_thread(self) -> bool:
        """当前是不是 UI（主）线程。

        ⚠️ Qt 和 Tk 在这一点上**完全相反**，这是迁移里最危险的一处差异：
            Tk 里从子线程调 `widget.method()` 会被 tkinter 自己排进主循环，
            "能用"（虽然不推荐）；**Qt 里没有这层保护** ——
            从子线程碰控件要么直接崩，要么**死锁**。
            所以凡是可能被子线程调到的入口，都必须自己判一下。
        """
        import threading

        return threading.current_thread() is threading.main_thread()

    def _invoke_on_ui_thread(self, name: str):
        """把一个**没有参数**的槽排到 UI 线程执行；已经在 UI 线程就直接调。

        跨线程用 `QMetaObject.invokeMethod(..., QueuedConnection)`：
        槽会根据**接收者所属线程**投递，也就是主线程的事件循环 ——
        这正是 `QTimer.singleShot` 做不到的事（那条定时器属于**当前**线程，
        子线程里调它永远不会触发，这个坑踩过）。
        """
        if self._on_ui_thread():
            getattr(self, name)()
            return
        QtCore.QMetaObject.invokeMethod(self, name, QtCore.Qt.QueuedConnection)

    def wake(self):
        """把窗口唤醒到最前（热键路径用）。找不回来是原版的老毛病。

        ⚠️ **必须保证在 UI 线程上执行**。这是实测踩出来的一个**硬死锁**：
            `wake()` 里那句 `setWindowFlag()` 会让 Qt 重建原生窗口，
            走 `SetWindowPos` 并**等窗口过程响应** —— 而窗口过程跑在主线程。
            触发路径是「主线程点【生成建议】→ 在 click 处理里
            `threading.Thread(...).start()` **阻塞等新线程起来**
            → 新线程第一条就 `ui.wake()` → 等主线程响应 → 两边互等」。

            实测栈（点一次就永久卡死，Qt 事件循环完全停摆）：
                主线程: on_trigger → threading.start   ← 等子线程起来
                子线程: do_generate → panel.py wake()  ← 等主线程响应

            Tk 版没有这个问题：那边 `-topmost` 是属性、子线程调它会被排进主循环。
        """
        self._invoke_on_ui_thread("_do_wake")

    @QtCore.Slot()
    def _do_wake(self):
        """`wake()` 的真正实现。**只能从 UI 线程调**（见 `wake()` 的说明）。

        ⚠️ 2026-09-27 改写。用户报「为什么生成建议时窗口会闪两下？」——
            原来是「`setWindowFlag(置顶, True)` → `show()` → 300ms 后
            `setWindowFlag` 换回原值」。
            **Qt 的 `setWindowFlag()` 在窗口可见时会销毁并重建原生窗口**
            （官方文档：它内部会调 `setParent()`，部件因此被隐藏，
            必须再 `show()`），于是每调一次闪一下，两个来回就是**闪两下**，
            而且正好落在点「生成建议」的那一刻。

        → 现在只**改 z 序**（`follow.raise_to_top`：`SetWindowPos` +
          `SWP_NOACTIVATE`，不重建窗口、不移动、不改大小、不抢焦点）。
          「临时置顶一下再换回来」这种做法整个删掉了：
          它要的效果只是"浮到最前"，而改 z 序就够了。
        """
        self.show()
        try:
            from .. import follow

            if not follow.raise_to_top(self):
                self.raise_()          # 非 Windows / 拿不到句柄时退回 Qt 自己的
        except Exception:
            try:
                self.raise_()
            except Exception:
                pass

    def toggle(self):
        self.hide() if self.is_visible() else self.show()

    # ------------------------------------------------ 第二次启动 exe -> 唤出来

    def wake_from_second_launch(self):
        """用户又双击了一次 exe：把面板显示出来并唤到最前。

        用户要求（2026-10-13）：「打包后让用户使用时，如果已经打开了程序，
        就不要重新开一个，再次打开 exe 就是把原来的跳出来」。
        信号由 `wechat_assistant.single_instance` 的等待线程送来。

        ⚠️ **它是从子线程调的**（等待线程），所以只能"投递" ——
            真正碰控件的动作必须排进 UI 线程（`_invoke_on_ui_thread`）。
        """
        self._invoke_on_ui_thread("_do_wake_from_launch")

    @QtCore.Slot()
    def _do_wake_from_launch(self):
        """`wake_from_second_launch()` 的真正实现。**只能在 UI 线程跑。**

        ⚠️ 跟热键路径共用 `_do_wake()`（它会先 `show()` 再抬 z 序）——
            所以用户按过 ✕（隐藏）也能被叫回来。
        ⚠️ 额外处理**最小化**：那条路热键不会遇到（点 ✕ 是隐藏、点 — 是折叠），
            但从任务栏右键最小化是可能的，只 `show()` 唤不回来。
        """
        try:
            if self.isMinimized():
                self.showNormal()
        except Exception:
            pass
        self._do_wake()

    def closeEvent(self, ev):
        """点窗口的系统关闭（有标题栏时）＝ 隐藏，不是退出程序。"""
        ev.ignore()
        self.hide()

    # ================================================================ 主题

    def set_theme(self, name: Optional[str] = None):
        """切换主题并**重建**界面。

        重建比重绘每个控件简单可靠，而界面元素很少，代价可以忽略
        （Qt 里整棵重建实测 10ms 量级，Tk 版是百毫秒级）。
        """
        new = name or theme.detect(self.cfg, 0, self._log)
        if new == self.style.theme_name:
            return
        self._log("主题切换: %s -> %s" % (self.style.theme_name, new))
        self.style = Style(self.cfg, 0, name=new, logger=self._log)
        # 外壳半径也要跟着新缩放重算
        self.radius = self.style.px(int(self.cfg.get("ui.radius", theme.SHELL_RADIUS)
                                        or theme.SHELL_RADIUS))
        self.border_w = float(self.style.px(
            theme.shell_border_width(self.style.theme_name)))
        self._build()
        self._apply_size(reposition=False)   # 切主题不该把窗口挪走

    # ================================================================ 设置窗口

    def open_settings(self, group: Optional[str] = None):
        """打开设置窗口。

        不依赖外部注入 —— 界面自己就能开。外部想接管的话传 `on_settings` 即可。
        `group` 可以指定先看哪一组（见 `open_session_settings`）。
        """
        try:
            from ..settings import SettingsStore
            from .settings_ui import SettingsWindow

            # 当前会话名当作预选：从悬浮窗点进来的多半就是想调**这一个**会话
            cur = self._session_title if self._session_title not in ("", "—") else None

            # ⚠️ 复用已开着的设置窗口，但要**先确认它真的还活着**。
            #    Qt 里被 `deleteLater()` 收走的部件是个悬空引用，再调它的方法
            #    直接崩在 C++ 侧（不是可以 try/except 的 Python 异常）。
            #    Tk 版对应的写法是 `self._settings_win.win is not None`。
            if self._settings_win is not None and self._settings_win_is_open():
                self._settings_win.open(group, preselect=cur)
                return
            if self._settings_store is None:
                self._settings_store = SettingsStore(logger=self._log)
            self._settings_win = SettingsWindow(
                self.cfg, self._settings_store, self.style,
                logger=self._log, on_saved=self._on_settings_saved,
                session_source=self._session_source,
                on_mode_change=self._on_mode_change,
                # ⚠️ 2026-10-14：面板上那两颗按钮撤掉之后，**查看入口搬到设置里** ——
                #    把面板自己的两个方法直接注入进去（它们本来就只依赖 `self`）。
                on_show_prompt=self._show_prompt,
                on_show_read=self._read,
                seen_source=self._session_seen_source,
                preselect=cur)
            self._settings_win.open(group, preselect=cur)
        except ImportError as e:
            # Qt 版设置窗口还没接上时**别静默** —— 明确告诉用户走哪条路
            self._log("设置窗口不可用: %s" % e)
            self._status = "设置窗口打不开"
            self._sync_status()
            if self._on_settings:
                self._on_settings()
        except Exception as e:
            self._log("设置窗口打不开: %s: %s" % (type(e).__name__, e))
            if self._on_settings:
                self._on_settings()

    def _settings_win_is_open(self) -> bool:
        """设置窗口对象还在、且没被 Qt 收走。"""
        win = getattr(self, "_settings_win", None)
        if win is None:
            return False
        for probe in ("is_open", "isVisible"):
            fn = getattr(win, probe, None)
            if callable(fn):
                try:
                    return bool(fn())
                except RuntimeError:
                    # RuntimeError: Internal C++ object already deleted
                    self._settings_win = None
                    return False
                except Exception:
                    return False
        return True

    def open_session_settings(self):
        """直接打开「会话」分组（针对当前这个会话）。"""
        self.open_settings(group="session")

    # ------------------------------------------------------------ 外观类应用
    def _apply_opacity(self):
        """把 `ui.opacity` 应用到窗口上。

        ⚠️ 2026-10-14 用户问「**为什么不透明度还得重启后才有用？**」——
            因为原来**只在建窗口时**读了它一次，而设置保存后的
            `_on_settings_saved()` 只重应用了主题和尺寸，**没重设不透明度**。
            值明明写进配置了、界面就是不变，看着就像"要重启"。
        ⚠️ `setWindowOpacity()` 是**运行期安全**的（不像 `setWindowFlag()` 会销毁
            重建原生窗口），所以可以随时调。
        ⚠️ 夹到 `[0.2, 1.0]`：0.2 以下会让面板几乎看不见（配置里的 `lo=0.5` 只是
            表单校验，这里防的是手改 `config.yaml`）。
        ⚠️ **`0` 当"没设"处理 → 1.0**（`v or 1.0` 这个老写法在这里是**故意**的）：
            "不透明度 0" = 窗口完全看不见、用户再也点不到它，属于误配置；
            往**看得见**的方向兜底。想改这个语义先想清楚"看不见的面板怎么找回来"。
        """
        try:
            v = float(self.cfg.get("ui.opacity", 1.0) or 1.0)
        except Exception:
            v = 1.0
        v = max(0.2, min(1.0, v))
        try:
            self.setWindowOpacity(v)
        except Exception:
            pass

    def _want_topmost(self) -> bool:
        """**「总是置顶」只在「不跟随」时才生效**（用户 2026-10-14 的口径）。

        为什么这么定：
          · **跟随中**（贴边 / 固定悬浮）由 `follow._sync_zorder()` 在**微信切到前台
            那时**把面板抬到微信上方 —— 这才对：你切到别的程序时它不该还压在最上面
            （`follow._apply` 里那段注释说的就是这个"很烦"）。
            所以跟随中这个开关**自动不生效**。
          · **不跟随**时（`ui.attach: off`）没人负责抬它，一被别的窗口盖住就找不回来
            （而它藏起来时既没任务栏条目、alt-tab 也切不到，只剩托盘图标）——
            这时候才真的需要常年置顶。

        ⚠️ 判据要读**两份配置**（`ui.topmost` + `ui.attach`），所以改「跟随微信」
            那一项时也要重算一次（`_on_settings_saved` 里两件事挨着调，已覆盖）。
        """
        if not bool(self.cfg.get("ui.topmost", False)):
            return False
        return str(self.cfg.get("ui.attach", "dock") or "dock") == "off"

    def _apply_topmost(self, runtime: bool = False):
        """把「总是置顶」应用到窗口上（**两种时机走两条完全不同的路**）。

        ⚠️⚠️ **运行期绝对不能用 `setWindowFlag()`** —— 窗口可见时它会销毁并重建
            原生窗口：用户眼里"窗口闪一下"，而且跟随器记的几何、我们插好的 z 序
            全都作废（见 `follow.raise_to_top` 那段血案）。
            运行期改置顶要走 `SetWindowPos(HWND_TOPMOST / HWND_NOTOPMOST,
            SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE)` —— 只动 z 序属性。
        ⚠️ 构造期（窗口还没显示）用 `setWindowFlag()`：那一刻重建是免费的，
            而且能一次把样式位设对。

        ⚠️⚠️ 2026-10-14 用户报「**好像置顶状态得重启后才有用**」——
            因为原来**只有建窗口时**读它一次，设置保存后从没重新应用过。
            现在 `_on_settings_saved()` 会调这里（`runtime=True`），切了立刻生效。
        """
        on = self._want_topmost()
        if not runtime:
            try:
                self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, on)
            except Exception as e:
                self._log("设置置顶失败: %s: %s" % (type(e).__name__, e))
            return
        hwnd = 0
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        if not hwnd:
            return
        # ⚠️⚠️ 走 `follow.set_window_pos()`（它有明确的 `argtypes`）——
        #    自己直接 `ctypes.windll.user32.SetWindowPos(h, -2, ...)` 会**静默失败**：
        #    不设 argtypes 时指针宽度的参数按 32 位传，而 `HWND_NOTOPMOST(-2)` 是负数，
        #    x64 下高 32 位不保证符号扩展 → 内核收到无效句柄 → 返回 0、不报错。
        #    实测过（见 `follow.set_window_pos` 的说明）。
        try:
            from .. import follow

            follow.set_window_pos(
                hwnd,
                follow.HWND_TOPMOST if on else follow.HWND_NOTOPMOST,
                0x0001 | 0x0002 | 0x0010)      # NOSIZE | NOMOVE | NOACTIVATE
        except Exception as e:
            self._log("运行期改置顶失败: %s: %s" % (type(e).__name__, e))

    def _on_settings_saved(self):
        """设置保存后：重读配置，并把**能立即生效**的那部分应用上。

        ⚠️ 热键和几个路径项改了要重启才生效（SPEC 里标了 `restart`），
            这里只处理外观类，其余留给下次启动。
        """
        try:
            self.cfg = self._settings_store.load()
        except Exception as e:
            self._log("重新加载配置失败: %s" % e)
            return
        # 把新 cfg 交回 run_watch —— 生成时的人设/会话关系是从它那里读的。
        # ⚠️ 少了这一步，会话级人设"保存成功但不生效"，要重启才认。
        if self._on_config_changed:
            try:
                self._on_config_changed(self.cfg)
            except Exception as e:
                self._log("回传新配置失败: %s: %s" % (type(e).__name__, e))
        # 外观：主题 + 尺寸，立即生效
        try:
            new_theme = theme.detect(self.cfg, 0, self._log)
            if new_theme != self.style.theme_name:
                self.set_theme(new_theme)
            else:
                self._apply_size(reposition=False)
        except Exception as e:
            self._log("应用外观设置失败: %s" % e)
        # ⚠️⚠️ 2026-10-14 用户报的两件事，都是"值写进去了、界面没变"：
        #   ① 「为什么不透明度还得重启后才有用？」—— 原来**只有建窗口时**读它；
        #   ② 「保持置顶有时候会出错」—— 除了下面 `raise_above` 那个坑，
        #      运行期也从来没应用过它（拨了开关要重启才认）。
        #   → 这两项都能**立刻生效**（都不需要重建原生窗口），补在这里。
        self._apply_opacity()
        self._apply_topmost(runtime=True)
        # 设置窗口自己也用同一套样式，主题变了要跟着重建。
        # ⚠️⚠️ 2026-10-14 修「改一个值之后**跳到顶部**」（用户报的）：
        #    原来这里是**无条件** `set_style()`，而它会整组重建 → 滚动条归零。
        #    而这条回调是**每次自动生效都会走**的（拨一下开关、改个数值都算），
        #    于是"改一下就被弹回顶部"。
        #    → 只有**主题真的变了**才重建（主题变了本来就要整扇窗换色）。
        try:
            _sw = self._settings_win
            if (_sw is not None and getattr(_sw, "style", None) is not None
                    and _sw.style.theme_name != self.style.theme_name):
                _sw.set_style(self.style)
        except Exception:
            pass
        self._status = "设置已保存"
        self._sync_status()

    # ================================================================ 线程安全入口

    def post(self, kind: str, data=None):
        """子线程通过它向 UI 线程投递消息。"""
        self._q.put((kind, data))

    def post_session(self, title: str, is_group: bool, note: str = ""):
        self.post("session", {"title": title, "is_group": is_group, "note": note})

    def post_status(self, text: str):
        self.post("status", text)

    def post_replies(self, replies: List[str], error: str = "", intent: str = ""):
        self.post("replies", {"replies": replies or [], "error": error, "intent": intent})

    def post_target(self, target_text: str):
        """把「本次要回复的内容」交给界面显示 —— 抓错了一眼能发现。"""
        self.post("target", target_text or "")

    def post_prompt(self, system: str, user: str, reasoning: str = ""):
        """把本次**实际提交**的提示词、以及模型返回的思考过程交给界面。

        reasoning 是模型「先想再答」的那段（`reasoning_content`）。
        它**照样计费**（实测占输出 token 的八成），所以值得让人看见。
        """
        self.post("prompt", {"system": system or "", "user": user or "",
                             "reasoning": reasoning or ""})

    def post_read(self, text: str):
        """把「这一次读到的对话」交给界面 —— 由它**单独开一个窗口**显示。

        ⚠️ 2026-09-27 新增。以前【只看对话】走的是 `post_replies(对话全文)`，
            把整段对话当成"建议"塞进主面板 —— 于是刚生成的三条建议被顶掉，
            而且长对话会把主面板撑成一坨可滚动的东西（连带把面板顶宽）。
        """
        self.post("read", {"text": text or ""})

    def set_busy(self, busy: bool, tip: str = ""):
        self.post("busy", {"busy": bool(busy), "tip": tip})

    # ================================================================ UI 线程

    def _pump(self):
        """把队列里的消息全部消费掉。

        ⚠️ 异常必须**在单条消息这一层**捕获，不能包在整个循环外面。
            包在外面的话，任何一条消息处理出错都会中断整个排空循环，
            队列里剩下的消息**已经出队却不会再被处理**，界面就从此静默地不再更新
            —— 而且看起来一切正常，极难排查（实测踩过：标题栏画错一次，
            后面所有回复都再也没显示出来）。

        ⚠️ 退出时要能停下来：`stop()` 是 `QTimer.singleShot(quit)`，主循环结束后
            这个定时器可能还会再响一次，那时窗口已经没了 —— 加一道 `_quit` 闸。
        """
        if getattr(self, "_quit", False):
            return
        while True:
            try:
                kind, data = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(kind, data)
            except Exception as e:
                self._log("UI 处理 %r 异常: %s: %s" % (kind, type(e).__name__, e))

    def _handle(self, kind, data):
        if kind == "session":
            new_title = data.get("title") or "—"
            # ⚠️ 2026-09-27 新增：**换了会话就把推荐气泡清掉**。
            #    用户的原话「如果用户换会话后，推荐气泡不应保存着吧」——
            #    对。那三条建议是针对**上一个会话**生成的，留在屏幕上：
            #      ① 会让人以为它们是当前会话的建议；
            #      ② 点「复制」就把上一个会话的话发到当前会话里去了。
            #    ⚠️ **只在标题真的变了时才清**：`on_session` 是记录器
            #       每 2 秒报一次的，不比就会变成"界面永远在被清空"。
            #    ⚠️ `"—"` 是"当前没有打开会话"，从它切到真会话、或反过来，
            #       都算变了 —— 但仍然只有当**屏幕上确实还挂着建议**时才动作，
            #       免得启动时白刷一次状态。
            # ⚠️ 2026-09-27：**每个会话各自记住自己的建议**（用户要求
            #    「保留着这个会话的建议，切回去还有」）。
            #    ⚠️ **只在标题真的变了时才动**：`on_session` 是记录器
            #       每 2 秒报一次的，不比就会变成"界面永远在被重建"。
            #    ⚠️ 缓存只在**内存**里（用户明确选了"不写磁盘"）。
            if new_title != self._session_title:
                self._switch_session(self._session_title, new_title)
            self._session_title = new_title
            # ⚠️ is_group 可能是 None —— 记录器为了"切会话就更新标题"，
            #    在只读到标题、还没读快照时就先报了一次。那种情况下
            #    **保留上一次的值**。
            #    ℹ️ 注意：「群」标记 2026-09-27 起**不再显示**了（用户要求），
            #       这个状态现在只是留着备用，链路没断。
            if data.get("is_group") is not None:
                self._session_is_group = bool(data.get("is_group"))
            if data.get("note"):
                self._status = data["note"]
                self._sync_status()
            # 折叠态没有标题栏，别在这里崩（_pump 的异常会被吞掉，
            # 表现就是"切了会话界面却没更新"）。
            if self.titlebar is not None:
                self.titlebar.update()
            # 托盘图标的悬停提示也跟着走（"微信 AI 助手 · 张三"）
            self._sync_tray()
        elif kind == "status":
            self._status = str(data)
            self._sync_status()
        elif kind == "replies":
            self._render_replies(data.get("replies") or [],
                                 data.get("error") or "",
                                 data.get("intent") or "")
        elif kind == "busy":
            self._apply_busy(bool(data.get("busy")), data.get("tip") or "")
        elif kind == "target":
            self._target = (data or "").strip()
            self._refresh_target_label()
        elif kind == "prompt":
            self._prompt = {"system": data.get("system") or "",
                            "user": data.get("user") or "",
                            "reasoning": data.get("reasoning") or ""}
        elif kind == "read":
            # 「只看对话」：存下来并**单独开窗口**（不进主面板的滚动区）
            self._read_text = data.get("text") or ""
            self._show_read()
        elif kind == "clear_intent":
            # 热键路径用掉了「我想说」的内容，这里在 UI 线程把它清掉。
            # 不清的话，下次切到别的会话再按热键，上一句会被当成本会话的意图。
            try:
                self.input.clear()
            except Exception:
                pass
            self._intent_mirror = ""
        elif kind == "theme":
            self.set_theme(data)
        elif kind == "visibility":
            self.show() if data else self.hide()

    # ================================================================ 托盘图标

    def build_tray(self):
        """建系统托盘图标（右下角）。

        ⚠️ **它解决的是"藏起来就找不回来"**：本程序的 ✕ 是"隐藏"，而 Windows
            在窗口隐藏时**连任务栏按钮一起收走** —— 于是没有任务栏条目、
            alt-tab 也切不到，只剩热键一条路。托盘图标一直在那儿。
        ⚠️ 没有托盘的环境（离屏平台 / 精简桌面）返回 None，**不报错、不影响启动**。
        ⚠️ **不在 `__init__` 里建**：那样每建一个界面对象就冒一个托盘图标
            （测试里会建几百个），而且真实运行时也只该有**一个**。
            所以只在 `run()`（真正的主循环）里建，测试要验就显式调它。
        """
        if self.tray is not None:
            return self.tray
        try:
            from .tray import TrayIcon

            if not TrayIcon.available():
                self._log("这台机器上没有系统托盘，跳过托盘图标")
                return None
            icon = self.windowIcon()
            if icon.isNull():
                from .. import paths

                icon = QtGui.QIcon(paths.icon_path())
            self.tray = TrayIcon(icon, self._on_tray_action, parent=self)
            self.tray.refresh(self.is_visible(), self._session_title)
            self.tray.show()
            self._log("托盘图标已就绪（右下角，点一下显示/隐藏面板）")
            return self.tray
        except Exception as e:
            self._log("托盘图标建不出来（不影响使用）: %s: %s"
                      % (type(e).__name__, e))
            self.tray = None
            return None

    def _on_tray_action(self, act_id: str):
        """托盘菜单的动作。⚠️ 菜单回调跑在 UI 线程，可以直接碰控件。"""
        if act_id == "toggle":
            self.toggle()
        elif act_id == "read":
            self._read()
        elif act_id == "settings":
            self.open_settings()
        elif act_id == "quit":
            self._log("从托盘菜单退出")
            self.stop()

    def _sync_tray(self):
        """让托盘菜单文案跟上当前状态（显示/隐藏、当前会话）。"""
        if self.tray is None:
            return
        try:
            self.tray.refresh(self.is_visible(), self._session_title)
        except Exception:
            pass

    # ================================================================ 生命周期

    def run(self):
        """在主线程跑 UI 主循环。

        ⚠️ 只有**第一个**调用会真的进入 `exec()`（QApplication 是进程级的，
            重复进事件循环没有意义）。所以要退出时用 `QApplication.quit()`。
        """
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._pump)
        self._timer.start(PUMP_MS)
        self.show()
        # ⚠️ 托盘图标**只在这里建**（真正的主循环），别挪进 `__init__`：
        #    测试里会建几百个面板，那样每个都冒一个托盘图标。
        #    另外`setQuitOnLastWindowClosed(False)` 也是为它：有托盘的程序
        #    "关掉最后一个窗口"不该等于退出（面板本来就是"隐藏"而不是"关闭"）。
        app0 = QtWidgets.QApplication.instance()
        if app0 is not None:
            app0.setQuitOnLastWindowClosed(False)
        self.build_tray()
        app = QtWidgets.QApplication.instance()
        return app.exec() if app is not None else 0

    def start_pump(self):
        """只装定时器不跑主循环 —— 测试用（主循环由测试自己转）。"""
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._pump)
        self._timer.start(PUMP_MS)
        return self._timer

    def pump_now(self, ms: int = 800):
        """在一段**有界**的事件循环里把队列排空，然后返回。

        给 `run_watch.py --demo` 那条路用：它是"立即触发一次就退出"，
        **不会进 `run()` 的主循环** —— 而 `do_generate()` 是跑在子线程里的，
        它 `post_replies()` 之后主线程已经走到 return 了，消息**永远没被 `_pump()` 取走**。
        结果是 `--demo` 只在命令行打印，窗口里什么都看不到
        （「验证链路用」的那条路自己看不见结果，等于没验证）。

        ⚠️ 用**嵌套的** `QEventLoop` 而不是一堆 `processEvents()`：
            `processEvents()` 会立刻返回，定时器那 120ms 的拍子根本到不了；
            嵌套事件循环才会让 `QTimer` 正常触发。
        """
        loop = QtCore.QEventLoop()
        QtCore.QTimer.singleShot(int(ms), loop.quit)
        loop.exec()
        return self

    def stop(self):
        self._quit = True
        # 托盘图标要先拆 —— 它不属于任何窗口，窗口没了它还留着会变成
        # 一个点不动的僵尸图标（要等鼠标扫过去才消失）。
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        try:
            t = getattr(self, "_timer", None)
            if t is not None:
                t.stop()
        except Exception:
            pass
        try:
            QtCore.QTimer.singleShot(0, QtWidgets.QApplication.quit)
        except Exception:
            pass
