# -*- coding: utf-8 -*-
"""设置窗口（Qt 版）—— 让用户自己配 API Key 和各种选项，不用去改 YAML。

这是 `wechat_assistant/settings_ui.py`（Tk 版，983 行）的 Qt 迁移。

对外 API 与 Tk 版**逐字一致**（`panel.py` 已经按这个签名在调用了）::

    SettingsWindow(cfg, store, style, logger=..., on_saved=...,
                   session_source=..., on_mode_change=..., preselect=...)
    win.open(group=None, preselect=None)
    win.set_style(style)
    win.close()

⚠️ 唯一的签名差异：**去掉了 Tk 版的第一个参数 `parent_root`**。
   Tk 需要它来建 `Toplevel` / 取 `winfo_rootx()` 定位；Qt 的窗口是独立顶层部件，
   定位和尺寸都自己说了算。`preselect` 按你说的放进了构造函数。
   `parent_root` 这个位置**不是"再塞一个 parent"** —— 少一个参数就是少一个，
   调用点（`panel.open_settings`）就是这么写的。

⚠️ **`.win` 属性保留着**：Tk 版判断"窗口还活着没"的写法是
   `self._settings_win.win is not None`，Qt 这边语义相同 ——
   `close()` 会把它置回 `None`。另外补了个 `is_open()` 方便新代码直接用。

界面是**按 `settings.py` 的 SPEC 表自动生成**的
----------------------------------------------
`SPEC` 里声明了几十个设置项（键、分组、类型、范围、说明、要不要重启），
这里只负责把它们画出来。**加一个设置项 = 在 SPEC 里加一行**，
这个文件一个字都不用改。

为什么不让用户直接改 config.yaml
--------------------------------
那份文件里每一段都在解释"这个值为什么是这个数"，是这个项目的教学价值所在。
而 YAML 写回会把**注释全部抹掉**。所以设置界面只写 `state/settings.json`
（覆盖层），config.yaml 只读。详见 `settings.py` 的模块说明。

"来源"和"恢复默认"为什么必须有
------------------------------
分层配置的老毛病是"我改了 config.yaml 却没反应"。所以每一项都标出
值是从哪来的（默认 / 你改过），并提供「恢复默认」—— 删掉覆盖项即可。

Tk 专属补丁全部消失（这是迁移的理由，不是副作用）
------------------------------------------------
    Tk 的做法                                        Qt 的替代
    ----------------------------------------------   ------------------------------
    `overrideredirect` + 透明色键 `#FF00FE` +          `qt_ui.layout.Panel`
      自绘两层多边形当圆角（色键撞色就穿洞）            （WA_TranslucentBackground +
    `_draw_shell()` 靠 `<Configure>` 每帧重画           一次 drawRoundedRect）
    `winchrome` 的 DWM 调用（系统标题栏跟主题走）     **这条路径上完全不引用** ——
                                                       Qt 窗口没有系统标题栏要染
    `widgets.SCROLL_PAD`（内容右侧留 10px 给浮层      不需要：Qt 的滚动条是
      滚动指示条，否则右边缘被压住）                    独立部件，不压内容
    `WrapGroup` + `<Configure>` 手算 `wraplength`       `QLabel.setWordWrap(True)`
      （写死会**静默裁字**，实测 13 条全被裁）
    `_grab_focus()` / `focus_force()` 抢焦点           不需要：Qt 的窗口管理器会把
      （`overrideredirect` 之后 Windows 不给焦点）      键盘焦点正常交给无边框顶层窗口
    `messagebox`                                     `QMessageBox`
"""

import threading
from typing import Callable, Dict, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .. import theme
from ..settings import (GROUPS, RESET_KEEP, RESET_KEEP_LABEL, SPEC, SPEC_BY_KEY,
                        SettingsStore, test_api)
from .base import Style, qc
from .cards import Switch
from .inputs import EntryBox, InputBox
from .layout import Panel, ScrollArea, WrapGroup
from .session_ui import SessionPanel
from .widgets import Pill

WIN_W = 760
WIN_H = 620

#: 不用重启的设置项：**改完自动生效**的轮询间隔（毫秒）。
#:
#: ⚠️⚠️ 为什么用**轮询 diff** 而不是给每个控件接"变了"信号（2026-10-11）：
#:     字段有 8 种 kind（text / secret / int / float / bool / choice /
#:     multiline / list），每种的信号都不一样，`_ChoiceField` / `_ListField`
#:     还是自绘的 —— 接一圈信号**漏一个就静默不生效**，
#:     而"静默不生效"正是这个项目里最贵的一类 bug。
#:     轮询一次 `_collect()` 只是几十个字典取值，代价可以忽略，而且**天然全覆盖**。
#: ⚠️ 400ms 是"人感觉是立刻"和"别太费"之间的折中：停下来不到半秒就生效了。
AUTO_APPLY_MS = 400

#: 「这个键还没有值」的哨兵。
#: ⚠️ 别用 `None`：配置项的值**本来就可以是 None**，那就分不清
#:    "没记过" 和 "记过的就是 None"，按钮该不该出现会判错。
_UNSET = object()
# ⚠️ 这几个数是**量微信设置窗口**得到的（`probes/qt/analyze_settings_nav.py`，
#    微信设置窗口 655x567 物理像素；本机缩放 1.5）：
#      导航栏 x 11~250 → **240 物理px = 160 基准px**
#      选中项 240x60   → **60 物理px = 40 基准px 高**，且**铺满**导航栏宽度
#      选中项文字 x 38~181 → 左边距 27 物理px ≈ **18 基准px**，右边距 69
#                          （右边距远大于左边距 = 左对齐，不是居中）
NAV_W = 160
NAV_ITEM_H = 40
NAV_TEXT_INSET = 18

# ⚠️ **这个常量已经不再驱动布局了**，留着只当"旧值"参照
#    （测试里用它断言"现在比以前更紧"）。
#    实际缩进由 `_SettingsFrame._content_inset()` 算：**外框线宽度 + 1**。
#    原来是固定 7（=10 物理px），那是在"内容层是方角、必须躲开圆角弧线"
#    的前提下定的安全值；现在内容层首尾部件自己会圆角（`fill_rounded` /
#    `round_corners_css`），就没有要躲的了 —— 用户要求「消除多余四周留白」。
SHELL_INSET = 7
# 圆角半径**从 theme 拿**（跟悬浮窗同一个值）—— 两边各写一个数的话，
# 并排看就是两个不同圆度。
SHELL_RADIUS = theme.SHELL_RADIUS
RESIZE_ZONE = 7          # 离边几像素算"抓到了边上"
MIN_W = 600
MIN_H = 420

# ============ 行高统一（2026-10-14 用户报「有些行高度没有统一」）============
# ⚠️ 起因：同一个分组里三种行高（实测浅色、默认窗口 1140×930 设备像素）：
#      · 输入框行 124 · **开关行 106** · 带额外按钮的行 160。
#    三个原因各修一处：
#      ① 每个控件自己的高度不一样（输入框 34 基准 / 开关 22 基准）→ `ROW_CTRL_H`
#         固定"控件带"高度，控件在里面垂直居中；
#      ② 头部带「恢复默认」按钮时是 **34 基准**（Pill 的高度）、不带时只有文字那么高
#         → `ROW_HEAD_H` 固定头部高度，并把那几颗 Pill 一起压到同一高度；
#      ③ 「测试连接」原来**单独占一行**（+36 设备像素）→ 挪进头部（见 `_build_row`）。
#    ⚠️ 例外是**故意的**：多行框（系统提示词）和它下面挂的两个查看入口本来就更高。
ROW_CTRL_H = 34      # 基准像素：= 输入框自己的高度（别改小，改小会挤到输入框）
#: **控件自带宽高的 kind** —— 它们的"控件带"高度**不能**被 `ROW_CTRL_H` 钉死，
#: 高度由 `spec.rows`（不填就 4~5 行）加底下那条可拖的把手决定。
#:
#: ⚠️⚠️ 2026-10-14 用户报「**黑名单底部被挡住了**」：`list`（`_ListField`）其实是
#:     `_TextAreaField(height=4)` —— **4 行高的文本框**，而做"行高统一"时只排除了
#:     `multiline`，于是它被 `holder.setFixedHeight(px(34))` 钉住，**底部整段被切掉**。
#:     ⚠️ 这是同一类 bug 的第二种长相：**"统一行高"的排除名单写窄了**。
#:     判据：`test_qt_settings_ui` §10d（holder 不许比控件矮）。
TALL_KINDS = ("multiline", "list")
ROW_HEAD_H = 30      # 基准像素：头部（标题 + 标记 + 小按钮）的统一高度
# ⚠️ **说明文字固定占几行** —— 这是"行高不统一"的**主因**：
#    最窄窗口（`MIN_W`）下量出来三种高度：
#      · 没有说明的行 120 · 说明 1 行的 143 · 说明 2 行的 163。
#    所以说明带高度**按最大行数固定**，没有说明的行也留同样高的一块空位。
#    ⚠️ 代价是每行多占一行小字的高度（≈20 设备像素）—— 用户要的就是"整齐"。
#    ⚠️ **谁也别写超过这个行数的说明**：超了会被 `setFixedHeight` **裁掉**
#       （不报错）。判据在 `test_qt_settings_ui` 里量"最窄窗口下没有任何说明超过 2 行"。
ROW_TIP_LINES = 2

#: **这些"打字的项"不自动生效**，攒到点「保存」（或关窗）再写 —— 2026-10-14 用户要求
#: 「有输入框的项目，也是按保存后进行保存吧」。
#: ⚠️ 理由两条，都是实测出来的：
#:   ① **边打边写盘**：防抖 400ms 只挡住"连续敲键"，你停下来想一下它就写一次；
#:   ② **写盘会连带重建界面**：`store.save()` → `_notify()` → 面板应用外观 →
#:      `settings_win.set_style()` → 整组重建 → **光标和滚动位置全丢**
#:      （用户原话：「打系统提示词，打一半就跳到顶部」）。
#: ⚠️ `restart=True` 那些**本来就攒着**（它们也不该边打边写），这一条管的是
#:    "不用重启、但也不该边打边写"的项。**bool / int / choice 仍然改完立刻生效** ——
#:    拨一下开关不该还要人再点一次保存。
DEFER_KINDS = ("text", "secret", "multiline")

# ⚠️ Tk 版这里有一个 `_KEY = "#FF00FE"`（透明色键）和一个 `SCROLL_PAD = 12`。
#    Qt 这边**两个都不需要**：
#      · 色键 -> `WA_TranslucentBackground`（真 alpha，不存在"撞色穿洞"）；
#      · SCROLL_PAD -> 滚动条是独立部件，不叠在内容上，也就不存在"右边缘被吃掉"。


# ================================================================ 小零件

def hline(style: Style) -> QtWidgets.QFrame:
    """一条发丝分割线。

    等价 Tk 版的 `widgets.hline()` —— 微信的设置列表就是靠这种 1px 线分的，
    不靠大色块对比。
    """
    ln = QtWidgets.QFrame()
    ln.setFixedHeight(1)
    ln.setStyleSheet("background:%s; border:none;" % style.p["border"])
    return ln


def label(style: Style, text: str, font_name: str = "body",
          color_key: str = "text", wrap: bool = False) -> QtWidgets.QLabel:
    """一行文字。

    ⚠️ `wrap=True` 时用的是 `QLabel` 原生的自动换行 —— Tk 版要拿
        `tk.Label` 的 `wraplength` 手算，而**算错不报错，只是静静裁字**
        （实测窗口拖到 600px 时 13 条帮助文字全被裁）。
    """
    lab = QtWidgets.QLabel(text)
    lab.setFont(style.font(font_name))
    lab.setStyleSheet("color:%s; background:transparent;" % style.p[color_key])
    if wrap:
        lab.setWordWrap(True)
    return lab


def _clear_layout(lay):
    """清空一个布局（部件用 deleteLater 走 Qt 自己的回收）。"""
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        sub = item.layout()
        if sub is not None:
            _clear_layout(sub)


# ---------------------------------------------------------------- 模态对话框
#
# ⚠️ 不用 `QMessageBox.question(...)` 这类静态便捷方法：**按钮文案由 Qt 的翻译
#    文件决定**，而这个程序没有加载 `qtbase_zh_CN.qm`，于是按钮会显示成英文的
#    "Yes / No" —— 而 Tk 版（`messagebox.askyesno`）在中文 Windows 上显示的是
#    "是 / 否"。这是迁移里很容易漏掉的一处"看起来没问题"的回归，
#    所以这里手工建 QMessageBox 并把按钮文字写死。

# ⚠️ 2026-09-27：这两个函数**原来是 `QMessageBox`**，现在换成 `ShellDialog`。
#    换的理由（用户提的）：
#      ① 「不要 python 的边框」—— `QMessageBox` 是**系统外观**：
#         系统标题栏、系统按钮、还有一个问号/叹号图标，
#         跟自己的悬浮窗/设置窗口完全不是一套。
#      ② 「看提示词与设置里面恢复默认的弹窗，风格也要一致」——
#         原来三个窗口三种样子（裸 QWidget / QMessageBox / 自绘 Panel）。
#    → 现在统一到 `ShellDialog`（无边框 + 真圆角 + 同一套外框线 + 同一套按钮）。
#
# ℹ️ 保留的历史说明（当时那个坑仍然成立）：不要用 `QMessageBox.question(...)`
#    这类**静态便捷方法** —— 按钮文案由 Qt 的翻译文件决定，而这个程序没有加载
#    `qtbase_zh_CN.qm`，于是会显示成英文的 "Yes / No"。
#    现在自己画按钮，这个问题自然消失了（文案是自己写的）。

#: 确认框 / 报错框的**最小宽度**（基准像素，会按缩放折算）。
#: ⚠️ 2026-10-14 用户报「恢复默认窗口可以宽一点，现在看起来太窄了」——
#:    这类弹窗只有一两句短文字，不设下限就会缩成窄条。两个弹窗共用同一个值，
#:    免得"一个宽一个窄"（用户之前就要求过几个弹窗风格要一致）。
DIALOG_MIN_W = 320


def _ask_yes_no(parent, title: str, text: str, yes: str = "确定",
                no: str = "取消", style=None) -> bool:
    """无边框圆角的是/否确认框。返回用户是否点了「是」。"""
    from .layout import ShellDialog
    st = style or (getattr(parent, "style", None))
    if st is None:
        # 退路：拿不到样式就别弹了，按"取消"处理（不做破坏性动作）
        return False
    dlg = ShellDialog(st, title, parent, modal=True, min_width=DIALOG_MIN_W)
    dlg.add_text(text)
    # ⚠️ 顺序跟设置窗口底部那排一致：取消（左、次要）→ 确定（右、主要）
    dlg.add_button(no, variant="ghost")
    dlg.add_button(yes, variant="primary")
    return dlg.exec() == QtWidgets.QDialog.Accepted


def _error(parent, title: str, text: str, ok: str = "知道了", style=None):
    """无边框圆角的报错框（只有一个「知道了」）。"""
    from .layout import ShellDialog
    st = style or (getattr(parent, "style", None))
    if st is None:
        return
    dlg = ShellDialog(st, title, parent, modal=True, min_width=DIALOG_MIN_W)
    dlg.add_text(text)
    dlg.add_button(ok, variant="primary")
    dlg.exec()


# ================================================================ 各种编辑器
#
# 每个编辑器只要提供 `widget`（放哪）和 `get()`（取值）两个东西。
# 取值一律返回**原始字符串/布尔**，交给 `Setting.coerce()` 做类型转换和范围校验 ——
# 校验逻辑只写一份，界面不重复实现。

class _TextField:
    """单行文本。用 `EntryBox`（圆角描边）—— 原生框的边框只能画直角。"""

    def __init__(self, parent, style, spec, value, on_change=None):
        self.box = EntryBox(style, text="" if value is None else str(value),
                            parent=parent)
        self.widget = self.box
        # ⚠️ `textChanged` 连**程序**改文本也会发（比如「恢复默认」）——
        #    那不是坏事：重置本来也该立刻生效。见 `_TextAreaField` 的同一处说明。
        if on_change is not None:
            self.box.textChanged.connect(lambda *_: on_change())

    def get(self):
        return self.box.get()

    def set(self, value):
        """把值写回控件（**程序**设值，不是用户输入）。

        ⚠️ 2026-10-13 补：底部「放弃改动」要把还没生效的字段**拨回去**，
            而那时只剩 `_seen` 改是不够的 —— `_render_group()` 会先 `_remember()`，
            它是**从控件里读**的，只改 `_seen` 会被当场覆盖回去（实测过）。
        ⚠️ 每种字段类都必须有 `get` **和** `set`：只有 `get` 的话，
            想拨回去的调用点会抛 `AttributeError`，被 `except` 吞掉后
            **悄悄什么也没做**（第一版就是这么错的，判据见 `test_qt_settings_ui` §19f）。
        """
        self.box.set("" if value is None else str(value))


class _SecretField:
    """API Key：默认打码，可以点「显示」看明文。

    默认打码是因为设置窗口经常被截图分享。不加密存储 —— 那会让文件不能手工编辑，
    而且换台电脑就解不开了；项目里 `secrets.yaml` 本身也不进 Git。

    ⚠️ Tk 版要钻进 `EntryBox` 内部的 `tk.Entry` 去 `configure(show="•")`
        （因为 Tk 的 EntryBox 是个 Canvas + 内嵌 Entry 的复合控件）。
        Qt 的 EntryBox 就是 `QLineEdit` 本身，一行 `setEchoMode()` 解决。
    """

    def __init__(self, parent, style, spec, value, on_change=None):
        self.style = style
        self._real = "" if value is None else str(value)
        self.shown = False
        #: 见 `_TextField` —— 每种字段都必须能报告「我变了」
        self._on_change = on_change

        self.box = QtWidgets.QWidget(parent)
        lay = QtWidgets.QHBoxLayout(self.box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(style.px(6))
        self.field = EntryBox(style, text=self._real, parent=self.box)
        if on_change is not None:
            self.field.textChanged.connect(lambda *_: on_change())
        lay.addWidget(self.field, 1)
        self.btn = Pill(style, "显示", command=self._toggle, variant="ghost",
                        parent=self.box)
        lay.addWidget(self.btn, 0)
        self.widget = self.box
        self._apply_mask()

    def _apply_mask(self):
        try:
            self.field.setEchoMode(QtWidgets.QLineEdit.Normal if self.shown
                                   else QtWidgets.QLineEdit.Password)
        except Exception:
            pass

    def _toggle(self):
        self.shown = not self.shown
        self._apply_mask()
        self.btn.set_text("隐藏" if self.shown else "显示")

    def get(self):
        return self.field.get()

    def set(self, value):
        """见 `_TextField.set`。"""
        self._real = "" if value is None else str(value)
        self.field.set(self._real)


class _BoolField:
    def __init__(self, parent, style, spec, value, on_change=None):
        # ⚠️ `Switch.set()` **屏蔽了信号**（程序设值不该触发回调），
        #    所以这里用 `command` 而不是 `toggled` —— 前者只在**真的点了**时响。
        self.sw = Switch(style, bool(value), parent=parent,
                         command=(lambda _v: on_change()) if on_change else None)
        self.widget = self.sw

    def get(self):
        return self.sw.get()

    def set(self, value):
        """见 `_TextField.set`。⚠️ `Switch.set()` 屏蔽信号，不会触发回调。"""
        self.sw.set(bool(value))


class _HeightGrip(QtWidgets.QWidget):
    """多行框**底下那条能拖的带子**：按住上下拖，改那个框的高度。

    ⚠️ 为什么要自己做一个：Qt 的 `QPlainTextEdit` **没有**原生的
        "拖底边改高度"（`QSizeGrip` 是给**顶层窗口**用的，塞进表单里不合适）。
        2026-10-11 用户要求「设置里这个输入框高度大一些并可以上下拖动高度」。

    ⚠️⚠️ 光把默认高度调高**不够**：他要改的是一整段提示词，
        长文本永远可能更长 —— 得让他自己拉。默认高度只是"开局大概够看"。

    ⚠️⚠️ **它不画任何东西**（2026-10-11 用户：「框下面不需要那三横吧」）。
        第一版画了三条短横线当提示，用户不要。
        "这里能拖"改由**两个不可见的线索**表达：
          · 鼠标移上去变成上下箭头（`SizeVerCursor`）
          · tooltip「按住上下拖动，改这个框的高度」
        所以**别再往这里加装饰**（加回来就是又把用户明确去掉的东西塞回去）。

    ⚠️ 拖的是 `target.setFixedHeight(...)`：`InputBox` 本来就是定高的
        （见 `inputs.InputBox.__init__`），所以改了之后布局不会把它拉回去。
    ⚠️ 最小值不能是 0：拖到没有高度就**再也拖不回来**（带子自己也被压没了）。
    """

    #: 逻辑高度。⚠️ 别小于 8 —— 太窄的话鼠标很难点中。
    #: ⚠️ 它**不画东西**，所以这个高度就是框下面那段空白；
    #:    别为了"好看"调大（那会变成一块莫名其妙的空隙）。
    H = 10
    #: 最少留这么高（逻辑像素），免得拖到看不见
    MIN_H = 64

    def __init__(self, style, target, parent=None, min_h=None):
        super().__init__(parent)
        self.style = style
        self.target = target
        self.min_h = int(min_h if min_h is not None else style.px(self.MIN_H))
        self._drag = None
        self.setFixedHeight(style.px(self.H))
        # ⚠️ **不设 `paintEvent`** —— 见类文档：用户不要那三横。
        #    光标和 tooltip 就是全部的"能拖"提示。
        self.setCursor(QtCore.Qt.SizeVerCursor)
        self.setToolTip("按住上下拖动，改这个框的高度")

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            # ⚠️ 记**全局** Y：拖动时相对本控件的 Y 会跟着变（它就贴在框底下），
            #    拿它当基准量出来是错的。
            self._drag = (ev.globalPosition().toPoint().y(), self.target.height())
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag is None:
            return
        y0, h0 = self._drag
        dy = ev.globalPosition().toPoint().y() - y0
        self.target.setFixedHeight(max(self.min_h, h0 + dy))
        ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._drag is not None:
            self._drag = None
            ev.accept()
            return
        super().mouseReleaseEvent(ev)


class _TextAreaField:
    """多行文本 = 输入框 + 底下一条**可拖的把手**。

    ⚠️ **这里和 Tk 版有一个刻意的差异**：Tk 版写的是
        `InputBox(parent, style, height=style.px(height))`，而 `height` 默认 5 ——
        也就是**一个 5 像素高的输入框**。实测（真跑了一遍 Tk）：`winfo_reqheight()`
        就是 5px。全局人设、黑名单这两个多行项在 Tk 版里根本没法用。
        Qt 版按"行数"换成真实像素高度。这是**观感修复**，不涉及任何取值/写回逻辑。

    ⚠️ `widget` 现在是**一个容器**（框 + 把手），不是那个框本身 ——
        取值接口（`get` / `set`）没变，所以调用点不用动。
        （别图省事把把手塞进外面的 `holder` 里：那要改 `_build`，
          而且别的 kind 不需要把手。）
    """

    # 一行正文（body 14px）加上行距约 19px，留点内边距
    LINE_PX = 19

    def __init__(self, parent, style, spec, value, height=5, on_change=None):
        self.style = style
        self.box = InputBox(style, height=style.px(max(40, height * self.LINE_PX)),
                            parent=parent)
        self.box.set(self._to_text(value))
        # ⚠️ 连在**填完初值之后**：`set()` 自己会发一次 `textChanged`，
        #    连在前面会让每次重建分组都白跑一轮"自动生效"。
        if on_change is not None:
            self.box.textChanged.connect(lambda *_: on_change())
        self.grip = _HeightGrip(style, self.box, parent=parent)

        self.widget = QtWidgets.QWidget(parent)
        lay = QtWidgets.QVBoxLayout(self.widget)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.box, 0)
        lay.addWidget(self.grip, 0)

    @staticmethod
    def _to_text(value) -> str:
        """把配置里的值变成输入框里的**文本**。

        ⚠️ 子类可能改写（列表要"一行一个"，见 `_ListField`）——
            所以**别在 `__init__` 里直接 `str(value)`**，那对列表是错的。
        """
        return "" if value is None else str(value)

    def get(self):
        return self.box.get()

    def set(self, value):
        """见 `_TextField.set`（多行框：`InputBox.set`）。"""
        self.box.set(self._to_text(value))


class _ListField(_TextAreaField):
    """名单类（一行一个）。"""

    def set(self, value):
        """见 `_TextField.set`。

        ⚠️ 列表必须走 `_to_text`（**一行一个**），绝不能 `str(value)` ——
            那会把 `["张三","李四"]` 写成 Python repr 再存回去（**会损坏数据**，
            见 `_to_text` 的说明）。`_TextField.set` 那套 `str()` 对这里是错的。
        """
        self.box.set(self._to_text(value))

    def __init__(self, parent, style, spec, value, on_change=None):
        # ⚠️ `on_change` 要**显式**往下传：这个类有自己的 `__init__`
        #    （不继承父类那个），漏传的话名单类改了**不会自动生效**。
        super().__init__(parent, style, spec, value, height=4,
                         on_change=on_change)
        self.box.setPlaceholderText("一行一个，例如：张三\nXX 工作群")

    @staticmethod
    def _to_text(value) -> str:
        """⚠️⚠️ 列表必须**一行一个**，**绝不能 `str(value)`**（2026-09-27 修）。

        踩过的坑（**会损坏数据**，用户的 `settings.json` 里留着现场的指纹）：
            填充走 `str(value)`、保存走 `str(raw).splitlines()`，于是
              · `[]`            -> 输入框里是字面量 `[]` -> 存回 `["[]"]`
                （黑名单里凭空多了一条叫「[]」的会话）
              · `["张三","李四"]` -> 输入框里是 `['张三', '李四']`
                （Python repr）-> 存回**一整条**，两个会话全丢
        实测证据：`state/settings.json` 里出现过 `"disabled": ["[]"]`。

        ⚠️ 空列表给**空文本**（不是 `[]`）—— 空文本经 `coerce` 就是 `[]`，
            而且 `apply_form(only_changes=True)` 会因此**不建覆盖**，
            让这一项继续跟着 `config.yaml` 走。
        """
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return "\n".join(str(x) for x in value)
        return str(value)


class _ChoiceField:
    """选项组：一排小胶囊，选中的是绿色。

    不用 QComboBox —— 那是系统原生控件，风格跟这一套自绘的对不上。
    选项都只有 2~4 个，平铺开反而一眼能看全。
    """

    def __init__(self, parent, style, spec, value, on_change=None):
        self.style = style
        self.spec = spec
        self._on_change = on_change
        self.value = "" if value is None else str(value)
        if not self.value and spec.choices:
            self.value = spec.choices[0][0]

        self.box = QtWidgets.QWidget(parent)
        lay = QtWidgets.QHBoxLayout(self.box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(style.px(6))
        self._btns = {}
        for val, text in spec.choices:
            b = Pill(style, text, command=lambda v=val: self._pick(v),
                     variant="selected" if val == self.value else "ghost",
                     parent=self.box)
            lay.addWidget(b, 0)
            self._btns[val] = b
        lay.addStretch(1)
        self.widget = self.box

    def _pick(self, val):
        changed = (val != self.value)
        self.value = val
        # ⚠️ 选中态用 `selected`（淡绿底 + 绿字），**不是** `primary`
        #    （实心绿底 + 白字）—— 后者是主操作按钮的样式，
        #    一排选项里出现实心绿块会跟"保存"按钮抢视觉。
        for v, b in self._btns.items():
            b.set_variant("selected" if v == val else "ghost")
        # ⚠️ 值没变就不报"变了"：连点同一个选项不该触发一次落盘
        if changed and self._on_change is not None:
            self._on_change()

    def get(self):
        return self.value

    def set(self, value):
        """见 `_TextField.set`。

        ⚠️ 必须走 `_pick()` —— 直接改 `self.value` 的话，那一排胶囊的**选中样式**
            不会跟着变（值对了、界面还是旧的，看起来像没生效）。
        """
        self._pick("" if value is None else str(value))


def make_field(parent, style, spec, value, on_change=None):
    """按 SPEC 建一个字段控件。

    ⚠️ `on_change` 是「这个字段的值变了」的回调 —— **每一种 kind 都必须接上它**，
        否则那一项改了**不会自动生效，而且不报错**
        （"静默不生效"是这个项目里最贵的一类 bug）。
        判据：`tests/test_qt_settings_ui.py` 里有一条「每一种 kind 都能被监听到」，
        新增 kind 忘了接会被它拦下。
    """
    kind = spec.kind
    if kind == "secret":
        return _SecretField(parent, style, spec, value, on_change=on_change)
    if kind == "bool":
        return _BoolField(parent, style, spec, value, on_change=on_change)
    if kind == "choice":
        return _ChoiceField(parent, style, spec, value, on_change=on_change)
    if kind == "multiline":
        # ⚠️ 初始行数由 SPEC 的 `rows` 给（不填就 5）。
        #    提示词那种长文本要**开局就看得见十几行**，别让人一进来先拖一下。
        #    ⚠️ 它只是**初始高度**：框底下有把手（`_HeightGrip`），可以自己上下拖。
        return _TextAreaField(parent, style, spec, value,
                              height=int(spec.rows or 5), on_change=on_change)
    if kind == "list":
        return _ListField(parent, style, spec, value, on_change=on_change)
    return _TextField(parent, style, spec, value, on_change=on_change)


# ================================================================ 标题条

class _TitleBar(QtWidgets.QWidget):
    """自绘标题条：左边「设置」，右边 ✕，整条可拖动。

    ⚠️ **它就是"系统标题栏"的替身**，不算重复画页头：Qt 这边窗口是
        `FramelessWindowHint`，系统标题栏压根不存在。CLAUDE.md 里
        "不要再画一条写着「设置」的页头"说的是**内容区**那条 ——
        内容区的页头给的是**当前分组的名字 + 说明**（见 `_build`），
        那才是用户当下需要的信息，不重复。

    ⚠️ **光标只认两种**：默认箭头；只有悬停在按钮上才换手型。
        用户明确说过"不需要移动的鼠标"，所以标题条空白处**不设** `SizeAllCursor`
        （Tk 版同款约定）。
    """

    def __init__(self, owner, style: Style, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.style = style
        self._drag = None
        self.setFixedHeight(style.px(38))
        self.setCursor(QtCore.Qt.ArrowCursor)

        lay = QtWidgets.QHBoxLayout(self)
        # ⚠️ 边距/间距一律走**档位常量**（用户第 2 条：禁止写死随机 margin/padding）。
        #    左 16 让标题跟下面内容页头的标题对齐；右只留 4，
        #    因为 ✕ 按钮自己还有内边距，再给 8 会显得右边空一块。
        lay.setContentsMargins(style.S("xl"), 0, style.S("xs"), 0)
        lay.setSpacing(style.S("sm"))
        self.lbl = label(style, "设置", font_name="body_bold", color_key="text")
        # ⚠️ 标题**显式垂直居中**（用户第 4 条：标签-控件强制垂直居中）。
        #    不给 flag 的话，QHBoxLayout 会按 sizeHint 顶端对齐 ——
        #    标题栏只有一行时看不出，一旦旁边有更高的部件就会偏。
        lay.addWidget(self.lbl, 0, QtCore.Qt.AlignVCenter)
        lay.addStretch(1)
        # ⚠️ `pad` 传的是**逻辑像素**（Pill 内部会自己 px() 一次）——
        #    写成 `pad=style.px(24)` 会被缩放两次，按钮凭空宽一倍（Tk 版踩过）。
        self.btn_close = Pill(style, "✕", command=owner.close, variant="text",
                              pad=20, parent=self)
        # ⚠️ 说清 ✕ 的行为：**关窗时还没生效的改动会自动保存**（见 `SettingsWindow.close`）。
        #    不说的话，用户改完直接点 ✕ 会以为"没点保存就是没改"。
        self.btn_close.setToolTip("关闭（还没保存的改动会自动保存）")
        lay.addWidget(self.btn_close, 0, QtCore.Qt.AlignVCenter)

    def paintEvent(self, ev):
        pt = QtGui.QPainter(self)
        pt.setRenderHint(QtGui.QPainter.Antialiasing, True)
        # ⚠️ **上面两个角要圆**（跟悬浮窗标题栏同一个道理）。
        #    标题栏是内容层最上面那个不透明部件；`SHELL_INSET` 压小之后
        #    它躲不开窗口的圆角弧线了，不圆角就会把圆角盖成直角
        #    （四个角看起来"漏底层底色"）。
        from .layout import fill_rounded
        # ⚠️ `owner` 是**逻辑对象**（`SettingsWindow`，QObject），真正的外壳是
        #    `parent`（`_SettingsFrame`）。圆角得问外壳要。
        #    用 `hasattr` 兜一层：万一被 reparent，宁可退成直角也别抛异常
        #    —— `paintEvent` 里抛异常是**每帧刷屏**的，很难看出真问题。
        frame = self.parent()
        rad = frame._content_radius() if hasattr(frame, "_content_radius") else 0
        fill_rounded(pt, QtCore.QRectF(self.rect()),
                     self.style.p["surface_alt"], rad, top=True, bottom=False)

    # ---- 拖动（无边框窗口得自己实现）

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self._drag = (ev.globalPosition().toPoint(), self.owner.win.pos())
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._drag is not None and self.owner.win is not None:
            p0, w0 = self._drag
            d = ev.globalPosition().toPoint() - p0
            win = self.owner.win
            x, y = self.owner._clamp(w0.x() + d.x(), w0.y() + d.y(),
                                     win.width(), win.height())
            win.move(int(x), int(y))
            ev.accept()

    def mouseReleaseEvent(self, ev):
        self._drag = None


# ================================================================ 窗口外壳

class _SettingsFrame(Panel):
    """设置窗口的外壳：无边框 + 真圆角 + 自绘拖动/缩放。

    Tk 版这里是 `overrideredirect(True)` + 透明色键 `#FF00FE` + 一个铺满窗口的
    Canvas 手画两层圆角多边形（`_draw_shell`，每个 `<Configure>` 重画一次），
    外加 `winchrome` 的 DWM 调用来给**退回系统标题栏**那条路染标题条。

    Qt 这边：一个 `Panel`（`WA_TranslucentBackground` + 抗锯齿 `drawRoundedRect`），
    DWM 那条路**整个不存在** —— 我们永远是无边框，也就**不需要**任何
    `winchrome` 调用。

    ⚠️ 拖动和 8 向缩放仍然是**自己实现**的（没有系统边框就没有系统行为）：
        外壳露出来的那一圈（内容层缩进 `SHELL_INSET`）是空的，热区就放在那里，
        手感跟真窗口的边框一样，而且不用往界面里塞把手。
    """

    # 8 向缩放的光标。Tk 版用字符串名（"size_nw_se"），Qt 用枚举。
    _CURSORS = {
        "l": QtCore.Qt.SizeHorCursor,
        "r": QtCore.Qt.SizeHorCursor,
        "t": QtCore.Qt.SizeVerCursor,
        "b": QtCore.Qt.SizeVerCursor,
        "lt": QtCore.Qt.SizeFDiagCursor,
        "rb": QtCore.Qt.SizeFDiagCursor,
        "rt": QtCore.Qt.SizeBDiagCursor,
        "lb": QtCore.Qt.SizeBDiagCursor,
    }

    def __init__(self, owner, style: Style):
        super().__init__(style,
                         radius=float(style.px(SHELL_RADIUS)),
                         border_px=float(style.px(
                             theme.shell_border_width(style.theme_name))))
        self.owner = owner
        self._rs = None
        self.setWindowTitle("设置")
        # ⚠️ **不给它父窗口**。Tk 版特意踩过"无边框 + `transient()` 在 Windows 上
        #    根本不会显示"（`winfo_viewable()` 是 0，矩形 1x1）。Qt 这边干脆
        #    让它做独立顶层窗口，也就不存在这个坑 —— 而且和 Tk 版一样
        #    不去跟悬浮窗抢层级。
        self.setWindowFlag(QtCore.Qt.FramelessWindowHint, True)
        self.setMouseTracking(True)

        inset = self._content_inset()
        self._root_lay = QtWidgets.QVBoxLayout(self)
        self._root_lay.setContentsMargins(inset, inset, inset, inset)
        self._root_lay.setSpacing(0)

    # ---- 内容层的缩进与圆角（跟悬浮窗同一套算法）

    def _content_inset(self) -> int:
        """内容层缩进：**只留外框线那么宽**，别再多留。

        ⚠️ 原来是 `SHELL_INSET = 7`，那是"方形内容层躲开圆角弧线"的安全值，
            代价是四周白留一圈（用户要求「消除多余四周留白」）。
            现在内容层**首尾部件自己也圆角**（见 `_TitleBar.paintEvent`
            和 `_build` 里的按钮条），方角不存在了，所以能压到几何最小值。

        ⚠️ **缩进还有个隐藏职责：它是无边框窗口的"拉伸抓取环"**
            （`_edges_at` 的热区）。压得太狠会让窗口不好拖边。
            所以这里给外框线宽度**再留 1px**，别压到 0。
        """
        return int(round(self.border_w)) + 1

    def _content_radius(self) -> int:
        """内容层首尾部件自己的圆角 = 窗口半径 − 缩进，正好接上窗口的弧线。"""
        return max(0, int(round(self.radius)) - self._content_inset())

    def apply_style(self, style: Style):
        self.style = style
        self.radius = float(style.px(SHELL_RADIUS))
        self.border_w = float(style.px(theme.shell_border_width(style.theme_name)))
        inset = self._content_inset()
        self._root_lay.setContentsMargins(inset, inset, inset, inset)
        self.owner._build()
        self.update()

    def _reset_constraints(self):
        """把窗口的最小尺寸钉回 `MIN_W`/`MIN_H`。

        ⚠️ **Qt 独有的坑**（`panel.py` 里也踩过同一个）：重建界面时旧内容走的是
            `deleteLater()`，删除被推迟到下一轮事件循环，而 Qt 会把**还没销毁的
            旧布局**算进窗口的 `minimumSize`。切分组时「会话」面板和 SPEC 表单的
            内容宽度差得多，不补这一刀的话窗口会被顶大、而且拖不回去。
            （Tk 版没有这个问题：`pack` 不参与窗口最小尺寸。）
        """
        try:
            st = self.style
            self.setMinimumSize(st.px(MIN_W), st.px(MIN_H))
        except Exception:
            pass

    # ---- 缩放（8 向，自绘）

    def _edges_at(self, x, y) -> str:
        """鼠标在哪个边/角上（返回 "l"/"r"/"t"/"b" 的组合，空串表示不在边上）。

        语义与 Tk 版一致，只是 `winfo_width()` 换成 `width()`。
        """
        st = self.style
        w, h = self.width(), self.height()
        z = st.px(RESIZE_ZONE)
        e = ""
        if x <= z:
            e += "l"
        elif x >= w - z:
            e += "r"
        if y <= z:
            e += "t"
        elif y >= h - z:
            e += "b"
        return e

    def mouseMoveEvent(self, ev):
        if self._rs is not None:
            self._resize_drag(ev)
            return
        e = self._edges_at(int(ev.position().x()), int(ev.position().y()))
        self.setCursor(self._CURSORS.get(e, QtCore.Qt.ArrowCursor))
        super().mouseMoveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            e = self._edges_at(int(ev.position().x()), int(ev.position().y()))
            if e:
                gp = ev.globalPosition().toPoint()
                self._rs = {"e": e, "x0": self.x(), "y0": self.y(),
                            "w0": self.width(), "h0": self.height(),
                            "px": gp.x(), "py": gp.y()}
                ev.accept()
                return
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._rs = None
        super().mouseReleaseEvent(ev)

    def _resize_drag(self, ev):
        """拖动缩放。

        ⚠️ 坐标换算一律走 `event.globalPosition().toPoint()` ——
            无边框窗口在拖动中会**跟着鼠标移动**，用窗口内的局部坐标
            （`ev.position()`）会把"窗口自己挪了多少"再算进位移里，
            结果是窗口越拖越飞。Tk 版用的是 `x_root`，同一个道理。
        """
        rs = self._rs
        if not rs:
            return
        st = self.style
        e = rs["e"]
        gp = ev.globalPosition().toPoint()
        dx, dy = gp.x() - rs["px"], gp.y() - rs["py"]
        x, y, w, h = rs["x0"], rs["y0"], rs["w0"], rs["h0"]

        if "l" in e:
            x += dx
            w -= dx
        if "r" in e:
            w += dx
        if "t" in e:
            y += dy
            h -= dy
        if "b" in e:
            h += dy

        minw, minh = st.px(MIN_W), st.px(MIN_H)
        if w < minw:
            # 从左边缩到头之后，x 要跟着贴着走，否则窗口会"漂"
            if "l" in e:
                x = rs["x0"] + rs["w0"] - minw
            w = minw
        if h < minh:
            if "t" in e:
                y = rs["y0"] + rs["h0"] - minh
            h = minh

        x, y = self.owner._clamp(x, y, w, h)
        self.setGeometry(int(x), int(y), int(w), int(h))


# ================================================================ 设置窗口

class SettingsWindow(QtCore.QObject):
    """独立设置窗口：左侧分组，右侧表单。

    用法：
        win = SettingsWindow(cfg, store, style, logger=log, on_saved=lambda: ...)
        win.open()

    ⚠️ 继承 `QObject` 只为一件事：**「测试连接」要跨线程回话**。
        网络请求必须放子线程（卡在 UI 线程上窗口就假死了），而子线程不能碰控件；
        用 Signal + 队列连接是 Qt 里唯一干净的写法（见 `_test`）。
        其余部分它仍是纯逻辑对象，跟 Tk 版一一对应。
    """

    # 「测试连接」的结果（子线程 emit -> UI 线程收）
    _test_done = QtCore.Signal(bool, str)

    def __init__(self, cfg, store: SettingsStore, style: Style,
                 logger: Optional[Callable[[str], None]] = None,
                 on_saved: Optional[Callable[[], None]] = None,
                 session_source: Optional[Callable[[], dict]] = None,
                 on_mode_change: Optional[Callable[[str, str], None]] = None,
                 preselect: Optional[str] = None,
                 seen_source: Optional[Callable[[], dict]] = None,
                 on_show_prompt: Optional[Callable[[], None]] = None,
                 on_show_read: Optional[Callable[[], None]] = None):
        super().__init__()
        self.cfg = cfg
        self.store = store
        self.style = style
        self._log = logger or (lambda m: None)
        self._on_saved = on_saved
        self._group = GROUPS[0][0]
        self._fields: Dict[str, object] = {}
        # ⚠️ `.win` 的语义和 Tk 版完全一样：**None = 窗口不存在**。
        #    Tk 那边判"还活着没"写的是 `self._settings_win.win is not None`。
        self.win: Optional["_SettingsFrame"] = None
        # 测试时可以关掉模态对话框（模态框会卡住无人操作的自动化测试）
        self.show_dialogs = True

        # ---- 会话面板（「会话」分组）----
        # `session_source` 返回注册表的会话表（`SessionRegistry.sessions`）；
        # `on_mode_change` 把模式写回注册表并落盘。两者都由 run_watch 注入 ——
        # 界面自己拿不到注册表（它归 run_watch 管）。
        self._session_source = session_source
        self._on_mode_change = on_mode_change
        # 「会话」列表**按时间排**的依据（`{会话key: 时间字符串}`），由 run_watch 注入。
        self._seen_source = seen_source
        # ---- 两个「查看」入口（2026-10-14 从悬浮窗搬进来的）----
        # 用户要求：「把只看对话和看提示词隐藏起来，可以放到设置里面」。
        # 由 `FloatingAssistant` 注入（那两个方法本来就在面板上，只依赖它自己）。
        self._on_show_prompt = on_show_prompt
        self._on_show_read = on_show_read
        self._preselect = preselect
        self._session_panel: Optional[SessionPanel] = None
        # ⚠️ **暂存必须跨分组切换存活**：改了会话设置又切到别的分组再点保存，
        #    不暂存的话改动会被静默丢掉，而界面上完全看不出来。
        self._session_stage = {"modes": {}, "personas": {}, "relationships": {}}
        self._session_sel: Optional[str] = preselect
        self._pending_preselect: Optional[str] = None

        # 界面部件的引用（窗口没建出来时都是 None）
        self.status = None
        self.desc_title = None
        self.desc = None
        self.scroll = None
        self.custom = None
        self.test_result = None
        self._tips = None
        self._nav_btns: Dict[str, object] = {}

        self._test_done.connect(self._on_test_done)

    # ------------------------------------------------------------ 生命周期

    def is_open(self) -> bool:
        """窗口是不是还开着（`win is not None` 的可读版本）。"""
        return self.win is not None

    def open(self, group: Optional[str] = None, preselect: Optional[str] = None):
        """打开窗口。`group` 可以指定先看哪一组（例如从悬浮窗点进来直接看「会话」）。

        ⚠️ `preselect=None` 是「**不指定**」，不是「清空选择」：
        这时保持上次选中的那个会话不动。因为从悬浮窗点进来时，
        如果当前没打开任何会话（微信刚启动、在登录页），
        留住上次编辑的那个反而更顺手 —— 比跳回列表第一项强。
        """
        # ⚠️ 顺序要紧：先把会话面板里还没提交的编辑收进暂存，
        #    再应用新的预选。
        #    `_stash_session()` 同时会把 `_session_sel` 更新成"刚才在看哪个"，
        #    放在后面执行的话，会把刚指定的预选**覆盖回上一次的选择** ——
        #    症状是"设置窗口已经开着时，切了会话再从悬浮窗点进来，
        #    面板还是停在原来那个会话上"（测试抓出来的）。
        self._stash_session()
        if preselect:
            self._preselect = preselect
            # ⚠️ 为什么还要一个 `_pending_preselect`：
            #    下面 `_switch()` 会再走一次 `_render_group()`，而它开头又会
            #    `_stash_session()` —— 那个面板此时还是**旧的**，
            #    于是 `_session_sel` 会被旧选择覆盖回去。
            #    挂起值在**真正建面板那一帧**才消费，绕开这次覆盖。
            self._pending_preselect = preselect
            self._session_sel = preselect
        if group and group in dict((g[0], g) for g in GROUPS):
            self._group = group

        if self.win is not None:
            self.win.show()
            self.win.raise_()
            self.win.activateWindow()
            self._switch(self._group)      # 已经开着也要能切到指定分组
            return self.win

        self.win = _SettingsFrame(self, self.style)
        self._build()
        st = self.style
        self.win.resize(st.px(WIN_W), st.px(WIN_H))
        self.win._reset_constraints()
        self._center()
        self.win.show()
        return self.win

    def close(self):
        """关掉窗口。`self.win` 置回 None —— 调用方就是靠这个判断死活的。

        ⚠️ 2026-10-13：**关窗时先把还没生效的改动存掉**（用户报「修改后不要按取消吧，
            交互起来很奇怪」）。理由很直接：改了「记录间隔」然后直接点 ✕，
            用户的本意显然是"我要这个改动"，把它静默丢掉才是奇怪的。
            想反悔有**明确**的入口：底部那颗「放弃改动」。
        ⚠️ 保存失败（值填得不对）**照样关** —— 那种情况状态行 / 对话框已经报过，
            在这里把窗口卡住只会让人以为程序坏了。
        """
        try:
            pend = self._pending_restart()
            if pend or self._session_dirty():
                names = [SPEC_BY_KEY[k].label for k in sorted(pend)
                         if k in SPEC_BY_KEY]
                self._log("关窗：先把还没生效的改动存掉（%s）"
                          % ("、".join(names) if names else "会话面板那批"))
                self._save()
        except Exception as e:
            self._log("关窗时保存失败: %s: %s" % (type(e).__name__, e))
        win = self.win
        # ⚠️ 定时器要先停：它挂在 `win` 上，窗口 `deleteLater()` 之后
        #    定时器对象也没了，还留着引用会变成**悬空对象**（Qt 侧崩，不是 Python 异常）。
        try:
            if self._auto is not None:
                self._auto.stop()
                self._auto = None
        except Exception:
            pass
        for name in ("_debounce", "_btn_net"):
            try:
                t = getattr(self, name, None)
                if t is not None:
                    t.stop()
                    setattr(self, name, None)
            except Exception:
                pass
        self.btn_save = None
        self.btn_cancel = None
        self.win = None
        self._fields = {}
        self._session_panel = None
        self.status = None
        self.desc_title = None
        self.desc = None
        self.scroll = None
        self.custom = None
        self.test_result = None
        self._nav_btns = {}
        try:
            if win is not None:
                win.hide()
                win.setParent(None)
                win.deleteLater()
        except Exception as e:
            self._log("关闭设置窗口失败: %s: %s" % (type(e).__name__, e))

    def set_style(self, style: Style):
        """换一套样式（悬浮窗切主题后 `panel.py` 会调它）。

        Tk 版没有这个方法（是迁移时按调用点的需要补的），
        语义就是"把整扇窗口按新样式重建一遍"，窗口和分组选择都保持不动。

        ⚠️⚠️ 2026-10-14：重建会**把滚动位置冲掉**（内容层是重建的），
            所以这里把它**存下来再放回去** —— 用户在「高级」里点个「恢复默认」、
            或者切主题，不该被弹回顶部。
            ⚠️ 光在 `apply_style()` 之后同步设一次**不够**：那一刻新布局还没算完，
                滚动条的范围还是旧的，设进去会被夹成 0 → 再补一次 0ms 的。
        """
        win = self.win
        if win is None:
            self.style = style
            return
        pos = self._scroll_pos()
        self.style = style
        # ⚠️ 重建前必须先收暂存：面板会被销毁重建（见 `_stash_session`）
        self._stash_session()
        win.apply_style(style)
        self._restore_scroll(pos)

    #: 重建之后**补几次**滚动位置（毫秒）。
    #:
    #: ⚠️⚠️ **一次不够**（2026-10-15 实测）：`_render_group()` 换掉内容部件之后，
    #:     Qt 会在**布局 / 焦点落定**时自己再把视图推下去 —— 0ms 那次放回去，
    #:     过一会儿又会被推走。三个时刻各补一次才稳。
    RESTORE_TICKS = (0, 50, 150)

    def _rebuild_keep_scroll(self) -> None:
        """重建**当前分组**，但滚动位置和焦点都不动。

        ⚠️ 用于"同组内的重建"：恢复默认 / 保存后 / 放弃改动。
            用户报过「设置一个后保存会跳到顶部去」—— 根因就在这些重建里
            （内容层是重建的，滚动条自然归零）。
        ⚠️⚠️ 用户又报「**修改 AI 接口后保存会跳到下面去**」（2026-10-15）——
            重建**同时**还会把视图推下去（实测 0 → 860/883，**光调 `_render_group()`
            就复现**，不用保存），而当时 `_restore_scroll` 对 `pos <= 0` 早退，
            所以"停在顶部"这种情况从来没被恢复过。见 `_restore_scroll`。
        ⚠️ **切分组不要用这个**（`_switch` 里直接调 `_render_group()`）：
            换一组就应该回到顶部，否则新分组一进来停在半中间，很怪。
        """
        pos = self._scroll_pos()
        key = self._focus_key()
        self._render_group()
        self._restore_focus(key)      # ⚠️ 顺序：先焦点、后滚动 ——
        self._restore_scroll(pos)     #    因为 `setFocus` 自己也会让滚动区滚过去

    def _scroll_pos(self) -> int:
        """当前滚动位置（拿不到就 0）。"""
        try:
            return int(self.scroll.verticalScrollBar().value())
        except Exception:
            return 0

    def _focus_key(self):
        """当前焦点在**哪个设置项**里（不在任何字段里 → None）。

        ⚠️ 重建会把原来的输入框删掉 → 焦点被 Qt 丢给滚动区（实测），
            用户接着打字就打到空气里了。所以要把焦点**放回同一个字段**。
        """
        try:
            fw = QtWidgets.QApplication.focusWidget()
        except Exception:
            return None
        if fw is None:
            return None
        for k, f in self._fields.items():
            try:
                if f.widget is fw or f.widget.isAncestorOf(fw):
                    return k
            except Exception:
                continue
        return None

    def _restore_focus(self, key) -> None:
        """把焦点放回**重建后的同一个字段**（拿不到就什么都不做）。"""
        if not key:
            return
        f = self._fields.get(key)
        if f is None:
            return
        # 文字类有 `.box`（真正的输入框）；开关 / 选项类就点它自己的部件
        # （焦点策略是 NoFocus 的话 `setFocus` 是空操作，无害）。
        w = getattr(f, "box", None) or f.widget
        try:
            w.setFocus()
        except Exception:
            pass

    def _restore_scroll(self, pos: int) -> None:
        """把滚动位置放回去（见 `RESTORE_TICKS`：补几次才稳）。

        ⚠️⚠️ 2026-10-15 用户报「修改 AI 接口后保存会跳到下面去」。根因是这里
            原来开头有一句 **`if pos <= 0: return`** —— "用户正停在顶部"
            （最常见的情形）**从来不恢复**，而 `_render_group()` 会把视图推到
            接近底部，于是保存完看到的是"跳到下面去了"。
            **0 是合法的滚动位置，必须一起恢复** —— 别再对 0 早退。
        """
        def _put():
            try:
                self.scroll.verticalScrollBar().setValue(int(pos))
            except Exception:
                pass

        _put()
        for ms in self.RESTORE_TICKS:
            try:
                QtCore.QTimer.singleShot(ms, _put)     # UI 线程，事件循环在跑，安全
            except Exception:
                pass

    # ------------------------------------------------------------ 定位

    def _center(self):
        """把窗口摆到**当前活动窗口的中间**，整扇窗必须留在屏幕里。

        ⚠️ Tk 版的 `_center` 只把左上角夹到 >=0，**完全没管右边和下边**。
        而悬浮窗是贴屏幕**最右**的（它跟着微信走），于是 760px 宽的设置窗口
        按它居中之后右边直接出屏 —— 用户看到的就是「页面右边有溢出」。
        这里从一开始就把四个方向都按桌面可用区域夹一遍（`_clamp`）。

        Tk 版是拿 `parent_root` 的 `winfo_rootx()` 居中的；
        Qt 版没了那个参数，改成问 `QApplication.activeWindow()`
        （从悬浮窗点进来时它就是悬浮窗），没有就退回屏幕居中。
        """
        try:
            win = self.win
            if win is None:
                return
            w, h = win.width(), win.height()
            act = QtWidgets.QApplication.activeWindow()
            px = py = None
            if act is not None and act is not win and act.isVisible():
                px = act.x() + (act.width() - w) // 2
                py = act.y() + (act.height() - h) // 2
            if px is None:
                scr = win.screen() or QtWidgets.QApplication.primaryScreen()
                geo = scr.availableGeometry()
                px = geo.center().x() - w // 2
                py = geo.center().y() - h // 2
            px, py = self._clamp(px, py, w, h)
            win.move(int(px), int(py))
        except Exception as e:
            self._log("设置窗口居中失败: %s: %s" % (type(e).__name__, e))

    @staticmethod
    def _clamp(x, y, w, h):
        """把窗口夹在**桌面可用区域**里。

        用 `follow.work_area()` 而不是 `GetSystemMetrics`：后者把任务栏算进去，
        窗口贴底时会被任务栏挡住。
        """
        try:
            from ..follow import work_area
            left, top, right, bottom = work_area()
        except Exception:
            return max(0, x), max(0, y)
        if right - left >= w:
            x = max(left, min(x, right - w))
        else:
            x = left
        if bottom - top >= h:
            y = max(top, min(y, bottom - h))
        else:
            y = top
        return x, y

    # ------------------------------------------------------------ 构建

    def _build(self):
        """搭骨架：标题条 + 左边分组栏 + 右边内容区 + 底部按钮条。

        布局上的几个决定（都是"跟微信一致"逼出来的）：

        · **不再单独画一条「设置」页头** —— 内容区的页头留给
          **当前分组的名字 + 说明**，那才是用户当下需要的信息。
          （窗口自己那条标题条不算：无边框窗口没有系统标题栏，见 `_TitleBar`。）
        · **底色分三层**：侧栏和底栏用 `surface_alt`（比正文深一档/浅一档），
          正文用 `bg`。靠发丝线分开，不靠大色块对比 —— 这是微信的做法。
        · ⚠️ 底部按钮条在 Tk 里**必须先 pack**，否则内容长起来会把它挤出去。
          Qt 里没有这个问题：`body` 带 `stretch=1` 吃掉全部剩余空间，
          其余按自然高度排，**空间不够时被牺牲的是带 stretch 的那个**。
        """
        st = self.style
        p = st.p
        win = self.win
        if win is None:
            return

        _clear_layout(win._root_lay)
        self._fields = {}
        self._nav_btns = {}
        # ⚠️ 旧面板是跟着窗口部件一起被销毁的（上面那行），引用必须一起清掉 ——
        #    否则 `_stash_session()` 会去碰一个**已经删掉的 C++ 对象**，
        #    抛的是 `RuntimeError: Internal C++ object already deleted`，
        #    被 try/except 吞掉之后表现成"暂存莫名其妙没了"。
        #    调用方（`set_style`）在这之前已经把暂存收好了。
        self._session_panel = None
        self.status = None
        self.desc_title = None
        self.desc = None
        self.scroll = None
        self.custom = None
        self.test_result = None

        # ⚠️ 重建时旧内容是 deleteLater（见 `_reset_constraints` 的说明），
        #    先松一刀，等它们真被销毁之后再钉回来。
        win.setMinimumSize(0, 0)
        QtCore.QTimer.singleShot(0, win._reset_constraints)

        # ---- 标题条 + 发丝线
        self._titlebar = _TitleBar(self, st, win)
        win._root_lay.addWidget(self._titlebar, 0)
        win._root_lay.addWidget(hline(st), 0)

        # ---- 底部按钮条（**先建好，等 body 加进布局之后再挂上去**）
        # ⚠️⚠️ **别在这里就 `addWidget(foot)`** —— 这个函数里
        #     `addWidget(body, 1)` 在**后面**才执行，谁先 add 谁在上面。
        #     之前就是先 add 了 foot，于是「恢复全部默认 / 取消 / 保存」
        #     整条跑到了**标题栏正下方**（实测 y=84，窗口高 930），
        #     用户报「恢复默认与保存放在下方吧」说的就是这个。
        #     → 现在只**建**，不 add；到底部再 add（见本函数末尾）。
        foot = QtWidgets.QWidget(win)
        # ⚠️ 按钮条是内容层**最下面**那个不透明部件，所以**下面两个角要圆** ——
        #    不然窗口左下/右下角的圆角会被它的方角盖住（「四角漏底层底色」）。
        from .layout import round_corners_css
        foot.setStyleSheet(round_corners_css(
            "foot", win._content_radius(), top=False, bottom=True,
            bg=p["surface_alt"]))
        flay = QtWidgets.QHBoxLayout(foot)
        flay.setContentsMargins(st.px(14), st.px(10), st.px(14), st.px(10))
        flay.setSpacing(st.px(6))
        flay.addWidget(Pill(st, "恢复全部默认", self._reset_all, variant="text",
                            parent=foot), 0)
        # 保存结果放在**状态行**里，不弹模态框 ——
        # 每次保存都要手动关一个对话框很烦，而且模态框会挡住后续操作。
        self.status = label(st, "", font_name="small", color_key="text_dim")
        flay.addWidget(self.status, 1)
        # ⚠️⚠️ **这颗键的文案必须跟着"有没有还没生效的改动"变**（2026-10-13 用户报
        #    「修改后不要按取消吧，交互起来很奇怪」）。原来写死「取消」，但它调的是
        #    `self.close` —— **一个字节都不会回退**：不用重启的项改完就落盘生效了。
        #    所以现在：
        #      · 没有待保存的改动 -> 「关闭」（真的只是关窗，见 `_sync_save_btn`）
        #      · 有待保存的改动   -> 「放弃改动」（它放弃的正是"还没生效"的那些）
        #    ⚠️ 别把它改成"整窗回退"：那要记住开窗那一刻的全部值再反向写盘
        #       （还会重建 AIClient），跟"改完立刻生效"直接冲突。
        self.btn_cancel = Pill(st, "关闭", self._on_cancel, variant="ghost",
                               parent=foot)
        self.btn_cancel.setToolTip("关闭设置窗口")
        flay.addWidget(self.btn_cancel, 0)
        # ⚠️ **保存按钮默认隐藏**，只有「需重启」的项被改过时才出现
        #    （2026-10-11 用户要求：「不用重启的可以马上就变，需要重启的才出现保存按钮」）。
        #    会话面板里那些暂存的改动也算"要保存" —— 它们写的是 `state.json`，
        #    不走 `_fields`，**不保存就等于没改**（这是很容易漏的一处）。
        self.btn_save = Pill(st, "保存", self._save, variant="primary", parent=foot)
        self.btn_save.setToolTip("立刻写盘（直接关窗也会保存）")
        self.btn_save.setVisible(False)
        flay.addWidget(self.btn_save, 0)

        # ---- 不用重启的项：**监听**变化 -> 防抖 -> 立刻生效
        #
        # 2026-10-11 用户要求：「不用重启的可以马上就变，需要重启的才出现保存按钮」，
        # 并问「可不可以监听状态是否有变化然后修改？」—— 可以，就按监听到做。
        #
        # ⚠️ 为什么还要**防抖**：文本框是**每敲一个字**都发一次信号，
        #    不防抖的话 `settings.json` 会一个字写一次、`AIClient` 也跟着重建一次
        #    （提示词那框有 600 多字）。
        #    400ms 的"停下来才动"在人看来就是立刻，又把连续输入合并成一次。
        #
        # ⚠️⚠️ **每种字段都必须接上 `on_change`** —— 漏一种就是"改了没反应"，
        #    而且**不报错**。所以 `tests/test_qt_settings_ui.py` 里有一条
        #    「每一种 kind 都能被监听到」的测试兜着（新增 kind 会被它拦下）。
        self._seen: Dict[str, object] = {}
        #: 已经落盘生效的值（自动生效和手动保存都会更新它）
        self._base: Dict[str, object] = {}
        #: 试过但不合法的值 —— 记下来，免得反复报同一个错
        self._auto_bad: Dict[str, tuple] = {}
        self._saving = False
        self._debounce = QtCore.QTimer(win)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(AUTO_APPLY_MS)
        self._debounce.timeout.connect(self._auto_apply)
        # ⚠️ 这条**慢速兜底**只干一件事：刷新保存按钮的可见性。
        #    为什么不用事件：会话面板里那批改动（模式/人设/关系）**没有统一的变化信号**
        #    （那一坨是 `SessionPanel` 自己的暂存），而它们**不走 `_fields`**，
        #    不保存就等于没改 —— 所以必须有个东西盯着"按钮该不该出现"。
        #    代价：每 1.2s 一次字典比较，而且不在「会话」分组时 `_stash_session()` 直接返回。
        self._btn_net = QtCore.QTimer(win)
        self._btn_net.setInterval(1200)
        self._btn_net.timeout.connect(self._sync_save_btn)
        self._btn_net.start()
        #: 「待保存的那批是什么」的指纹 —— 只在**它变了**的时候才更新状态行
        #: （`_sync_save_btn` 每 1.2s 被叫一次，每次都说话就成了复读机）。
        self._pend_sig = None

        # ---- 主体
        body = QtWidgets.QWidget(win)
        blay = QtWidgets.QHBoxLayout(body)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(0)
        win._root_lay.addWidget(body, 1)

        # ---- 按钮条挂到**最下面**
        # ⚠️ 顺序 = `_root_lay` 的 add 顺序：标题栏 → 发丝线 → body(拉伸 1)
        #    → 发丝线 → 按钮条。
        #    **`addWidget(body, 1)` 必须先执行** —— 之前就是先 add 了按钮条，
        #    于是「恢复全部默认 / 取消 / 保存」整条跑到了标题栏正下方
        #    （实测 y=84，窗口高 930）。用户报「恢复默认与保存放在下方吧」
        #    说的就是那个。
        win._root_lay.addWidget(hline(st), 0)
        win._root_lay.addWidget(foot, 0)

        # 左侧分组栏
        # ⚠️ 底色用 `nav_bg` 这个**专门的语义键**，别借 `surface`。
        #    实测（`probes/qt/analyze_settings_nav.py`，微信设置窗口）：
        #        深色导航栏 `#2F2F30`（正好等于 `surface`）
        #        浅色导航栏 `#EEEEF0`（正好等于 `surface_alt`）
        #    —— **两套映射到不同的键**。之前直接写 `surface`，浅色下导航栏
        #    就成了纯白 `#FFFFFF`，跟右栏 `#FAFAFA` 几乎分不开。
        #    右栏内容区两套都是 `bg`（深 #1E1E1F / 浅 #FAFAFA）。
        nav = QtWidgets.QWidget(body)
        nav.setStyleSheet("background:%s;" % p["nav_bg"])
        nav.setFixedWidth(st.px(NAV_W))
        nlay = QtWidgets.QVBoxLayout(nav)
        # ⚠️ 左右边距为 0：微信的选中项是**铺满导航栏宽度**的直角矩形
        #    （实测 x 11~250 正好是导航栏的整幅宽度），不是缩进的小胶囊。
        nlay.setContentsMargins(0, st.px(8), 0, st.px(8))
        nlay.setSpacing(0)
        for gid, gname, _desc in GROUPS:
            # ⚠️ 这里用 `navsel`（实心绿 + 近白字的**直角矩形**），
            #    不是 `selected`（淡绿底 + 绿字的胶囊）—— 见 widgets.py 的说明。
            #    初建时也要设对，不然一进来第一屏是错的。
            b = Pill(st, gname, command=lambda g=gid: self._switch(g),
                     variant="navsel" if gid == self._group else "text",
                     width=st.px(NAV_W), height=st.px(NAV_ITEM_H),
                     radius=0, pad=0, text_inset=st.px(NAV_TEXT_INSET),
                     parent=nav)
            nlay.addWidget(b, 0)
            self._nav_btns[gid] = b
        nlay.addStretch(1)
        # ⚠️ 2026-10-13 **删掉侧栏底部那句话**（原来是「改动只写 state/settings.json /
        #    config.yaml 不动」）。那是**分层配置的实现说明**，是给维护者看的 ——
        #    用户不需要知道值落在哪个文件里。用户视角只需要两件事：
        #      · 改完立刻生效（状态行会报「已生效」）
        #      · 标了「需重启」的项要点「保存」再重启
        #    这两件都已经由状态行和按钮本身表达了，不再占一块界面。
        blay.addWidget(nav, 0)

        # 右侧内容区
        right = QtWidgets.QWidget(body)
        rlay = QtWidgets.QVBoxLayout(right)
        rlay.setContentsMargins(st.px(10), 0, st.px(10), st.px(8))
        rlay.setSpacing(0)
        blay.addWidget(right, 1)

        head = QtWidgets.QWidget(right)
        hlay = QtWidgets.QVBoxLayout(head)
        hlay.setContentsMargins(st.px(16), st.px(12), st.px(16), st.px(10))
        hlay.setSpacing(st.px(3))
        self.desc_title = label(st, "", font_name="body_bold", color_key="text")
        self.desc = label(st, "", font_name="small", color_key="text_dim",
                          wrap=True)
        hlay.addWidget(self.desc_title, 0)
        hlay.addWidget(self.desc, 0)
        rlay.addWidget(head, 0)
        rlay.addWidget(hline(st), 0)

        # 提示文字的登记表。Qt 里换行是 QLabel 自己的事，留着它只为同名 API。
        self._tips = WrapGroup(st, right, inset=32)

        self.scroll = ScrollArea(st, "bg", right)
        rlay.addWidget(self.scroll, 1)
        # 「会话」分组用的容器：它**不能进 ScrollArea**（里面还有两个自己的
        # 滚动区，嵌套滚轮会互相打架）。两个容器按分组轮流显示。
        self.custom = QtWidgets.QWidget(right)
        self.custom.setStyleSheet("background:%s;" % p["bg"])
        self._custom_lay = QtWidgets.QVBoxLayout(self.custom)
        self._custom_lay.setContentsMargins(st.px(8), st.px(6), st.px(8), st.px(6))
        self._custom_lay.setSpacing(0)
        rlay.addWidget(self.custom, 1)

        self._render_group()

    def _switch(self, gid):
        self._group = gid
        # ⚠️ 左列分组的选中态用 **`navsel`（实心绿 + 近白字的直角矩形）**，
        #    未选中是 `text`（完全无边框、无底色）。
        #
        # ⚠️⚠️ **这里是改过两轮的，两轮的理由都记一下**：
        #   第一轮写的是 `primary` —— 选中项变成实心绿**胶囊**，放在侧栏里突兀。
        #   第二轮改成 `selected`（淡绿底 + 绿字胶囊），理由写的是
        #   "微信侧栏的选中项不是实心块"。
        #   **那个理由只对了一半**：它来自**聊天窗口的会话列表**（那边确实很淡），
        #   但**设置窗口**是另外一个界面 —— 实测微信设置窗口左侧的选中项
        #   就是**一块实心绿**（`#0DA869`，直角矩形，铺满导航栏），
        #   字是近白的 `#E7F6F0`。用户报「微信设置的颜色与字体高亮并不是
        #   微信设置的颜色」说的就是这个。
        #   → 所以现在用 `navsel`，形状和颜色都照实测走。
        #   ⚠️ **别再把这一档合并回 `selected`**：`selected` 还用在会话面板的
        #     模式选择上，那个界面没量过，不要连带改动。
        for g, b in self._nav_btns.items():
            b.set_variant("navsel" if g == gid else "text")
        self._render_group()

    def _render_group(self):
        """把当前分组画出来。

        两组形态：
          · 「会话」= 自定义面板（会话 × 属性，塞不进 SPEC 的一行一项模型）
          · 其余    = 按 SPEC 平铺的表单
        """
        st = self.style
        win = self.win
        if win is None:
            return

        # ⚠️ 换分组前先把会话编辑框里的内容收进暂存，否则切走就丢（见 __init__ 注释）
        self._stash_session()
        # ⚠️ **必须在这里收拢**：下一行就把 `_fields` 清空重建了，
        #    那时这一组的值已经取不到了 —— 而 `_seen` 是跨分组判断
        #    "保存按钮该不该出现"的唯一依据。
        self._remember()

        # 分组名 + 说明放到右侧内容区的页头（见 _build 里的说明）
        gname = dict((g[0], g[1]) for g in GROUPS).get(self._group, "")
        desc = dict((g[0], g[2]) for g in GROUPS).get(self._group, "")
        try:
            self.desc_title.setText(gname)
            self.desc.setText(desc)
        except Exception:
            pass

        # ⚠️ 提示标签是**每次重建**的，登记表必须跟着清 ——
        #    不清的话它会一直往一张越来越长的表里塞已经销毁的控件。
        try:
            self._tips.clear()
        except Exception:
            pass

        if self._group == "session":
            self.scroll.setVisible(False)
            self.custom.setVisible(True)
            self._render_session()
            return

        self.custom.setVisible(False)
        self.scroll.setVisible(True)
        self.scroll.clear()
        # 会话面板已经收进暂存了，这里直接把它拆掉（Tk 版是把引用置 None 让它留着，
        # 但那样在 Qt 里会留下一堆待销毁的部件、还会顶大窗口的最小尺寸）
        _clear_layout(self._custom_lay)
        self._session_panel = None
        self._fields = {}

        cfg_now = self.store.load()
        items = [s for s in SPEC if s.group == self._group]
        for i, spec in enumerate(items):
            self._build_row(spec, cfg_now)
            # 发丝线分隔相邻两项（微信的设置列表就是这么分的）。
            # 最后一项后面不加 —— 底部已经有一条了，两条会挨在一起变成双线。
            if i < len(items) - 1:
                self.scroll.add(hline(st))

    # ------------------------------------------------------------ 会话面板

    def _stash_session(self):
        """把会话面板里还没提交的编辑收进暂存。"""
        if self._session_panel is None:
            return
        try:
            self._session_stage = self._session_panel.collect()
            sel = self._session_panel.selected
            if sel:
                self._session_sel = sel
        except Exception as e:
            self._log("暂存会话设置失败: %s: %s" % (type(e).__name__, e))

    def _render_session(self):
        _clear_layout(self._custom_lay)
        # 挂起的新预选优先，用过就清（见 open() 的说明）
        sel = self._pending_preselect or self._session_sel or self._preselect
        self._pending_preselect = None
        self._session_panel = SessionPanel(
            self.custom, self.style, self.store,
            session_source=self._session_source,
            seen_source=self._seen_source,
            on_mode_change=self._on_mode_change,
            logger=self._log,
            preselect=sel,
            stage=self._session_stage,
        )
        self._custom_lay.addWidget(self._session_panel, 1)

    # ------------------------------------------------------------ 表单一行

    def _build_row(self, spec, cfg_now):
        st = self.style

        row = QtWidgets.QWidget(self.scroll.inner)
        rlay = QtWidgets.QVBoxLayout(row)
        # ⚠️ **左右都要留 16**。
        #    原来左右都是 0，理由写的是"Qt 的滚动指示条是独立部件、不叠在内容上面"。
        #    那句话**只是几何上对**（滚动条确实不压在内容上），但量出来是
        #    **内容最右边界 == 滚动条左边界**，也就是**贴着**滚动条、一点缝都没有
        #    （`probes/qt/diag_settings_scroll.py` 量到的），看起来就是右边被吃了。
        #    用户报「右边还是有部分被拉动条遮挡住」。
        # ⚠️ 取 16 是为了跟**页头**（`head` 的 hlay 也是 16）对齐 ——
        #    两边都是 16 的话，表单的标签跟页头标题在同一条竖线上。
        #
        # ⚠️ 2026-09-27 起改成**档位常量**（用户第 2 条）。
        #    上下也从 8/3 收到 8/4 这一对，配合下面那两级文字的间距，
        #    整行的垂直节奏变成：标题 → 4 → 控件 → 2 → 说明。
        rlay.setContentsMargins(st.S("xl"), st.S("md"), st.S("xl"), st.S("xs"))
        rlay.setSpacing(0)

        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(st.S("sm"))
        # ⚠️ 标题、来源标记、恢复默认**三者都显式垂直居中**（用户第 4 条）。
        #    它们字号不同（正文 / 小字 / 按钮），不给 flag 的话
        #    布局按各自的 sizeHint 顶端对齐，小字会明显偏上。
        head.addWidget(label(st, spec.label, font_name="body", color_key="text"),
                       0, QtCore.Qt.AlignVCenter)

        # 来源标记 + 恢复默认。
        # Tk 是逐个 `pack(side="right")` 摞出来的，视觉顺序（从右往左）是
        # 恢复默认 / 已修改 / 需重启；Qt 从左往右写就是反过来的那个顺序。
        head.addStretch(1)
        # ⚠️ 「测试连接」**原来在控件下面单独占一行**（用户 2026-10-14 报
        #    「有些行高度没有统一」+「是不是多了没必要的组件」）——
        #    它让 API Key 那一行比别人高 36 设备像素。现在挂进头部，
        #    跟「恢复默认」同一档；结果文字也放这儿（见 `_build_test_button`）。
        if spec.key == "ai.api_key":
            self._build_test_button(head, row)
        if spec.restart:
            head.addWidget(label(st, "需重启", font_name="small",
                                 color_key="text_faint"), 0,
                           QtCore.Qt.AlignVCenter)
        src = self.store.source(spec.key)
        if src == "default":
            head.addWidget(label(st, "默认", font_name="small",
                                 color_key="text_faint"), 0,
                           QtCore.Qt.AlignVCenter)
        else:
            head.addWidget(label(st, "已修改" if src == "user" else "来自密钥",
                                 font_name="small", color_key="accent"), 0,
                           QtCore.Qt.AlignVCenter)
            # ⚠️ 2026-10-14：**「恢复默认」不画在 `RESET_KEEP` 那几行上**
            #    （用户要求「恢复默认接口地址与模型不要改」）。
            #    这两样是"用哪家服务"的选择，不是调参 —— 要清空就直接删框里的内容。
            #    ⚠️ 画一颗按不动的按钮比不画更让人困惑，所以是**不画**。
            if spec.key not in RESET_KEEP:
                head.addWidget(self._head_pill(st, "恢复默认", variant="text",
                                               command=lambda k=spec.key: self._reset_one(k),
                                               parent=row), 0,
                               QtCore.Qt.AlignVCenter)
        # ⚠️ **头部必须固定高度**：带 Pill 的行头部是 34 基准像素、不带 Pill 的
        #    只有文字那么高 —— 同一列里就会"一行高一行矮"（用户报的就是这个）。
        #    固定成 `ROW_HEAD_H`，Pill 也一起压到那个高度（见 `_head_pill`）。
        headw = QtWidgets.QWidget(row)
        headw.setFixedHeight(st.px(ROW_HEAD_H))
        headw.setLayout(head)
        rlay.addWidget(headw)

        # 编辑器
        holder = QtWidgets.QWidget(row)
        hlay = QtWidgets.QVBoxLayout(holder)
        hlay.setContentsMargins(0, 0, 0, 0)
        hlay.setSpacing(0)
        value = cfg_now.get(spec.key, spec.default)
        # ⚠️ **切走再切回来，没生效的改动要还在**：分组字段是每次重建的，
        #    直接用配置里的值会把用户在别组改过、还没保存的东西**抹掉**。
        #    （`_seen` 里那个是"用户改成的值"，`_base` 里那个是"已生效的值"。）
        if (spec.key in self._base and spec.key in self._seen
                and self._seen[spec.key] != self._base[spec.key]):
            value = self._seen[spec.key]
        # ⚠️ `on_change` **必须传**：不传的话这一项改了不会自动生效，
        #    而且**不报错**（"静默不生效"是这个项目里最贵的一类 bug）。
        field = make_field(holder, st, spec, value,
                           on_change=self._on_field_changed)
        # ⚠️ **第一次见到这一项** -> 把"刚从配置填进去的"值当作**已生效的值**。
        #    必须在**建完控件之后**记（用 `field.get()`，不是 `value`）：
        #    配置里是 `2.0`、输入框里是字符串 `"2.0"`，两种类型混着比
        #    会**永远判成"变过"**，那就一进设置就白写一遍盘。
        if spec.key not in self._base:
            self._base[spec.key] = field.get()
        hlay.addWidget(field.widget, 0)
        # ⚠️ **控件带固定高度**（`ROW_CTRL_H`）：输入框 34 基准、开关只有 22 ——
        #    不归一化的话"开关那一行"比"输入框那一行"矮 18 设备像素，
        #    用户看到的就是"行高不统一"。控件在里面**垂直居中**。
        #    ⚠️ **自带宽高的 kind**（`TALL_KINDS` = multiline / list）例外：
        #       它们的高度由 `spec.rows` + 可拖的把手决定 ——
        #       ⚠️ 把 `list` 也钉成 34 的话，**黑名单底部会被切掉**（2026-10-14 用户报）。
        if spec.kind not in TALL_KINDS:
            hlay.setAlignment(QtCore.Qt.AlignVCenter)
            holder.setFixedHeight(st.px(ROW_CTRL_H))
        # 标题 -> 控件：用 `xs`(4)，比原来的 5 略紧，跟标题栏是一组
        rlay.addSpacing(st.S("xs"))
        rlay.addWidget(holder, 0)
        self._fields[spec.key] = field

        # 说明 + 单位
        tip = spec.help or ""
        if spec.unit:
            tip = ("单位：%s。" % spec.unit) + tip
        if spec.kind in ("int", "float") and spec.lo is not None:
            tip = "范围 %s ~ %s。" % (spec.lo, spec.hi) + tip
        if tip:
            # ⚠️ 换行交给 QLabel（`setWordWrap`），**不能写死宽度** ——
            #    Tk 那边写死的话窗口一拖窄就静静裁字（实测 13 条全中）。
            # 控件 -> 说明：用 `xxs`(2)，说明文字本来就要"贴着"它解释的控件，
            # 离太远会让人不确定它在说哪一项。
            rlay.addSpacing(st.S("xxs"))
            self._tips.add(row, tip)
            self._tip_lab = self._tips.labels[-1]
            rlay.addWidget(self._tip_lab, 0)
        else:
            # ⚠️ **没有说明的行也要占同样高的空位**（不然它比别人矮一行，
            #    用户报的就是"有的行高有的行矮"）。空标签不画东西。
            rlay.addSpacing(st.S("xxs"))
            self._tip_lab = QtWidgets.QLabel("")
            self._tip_lab.setFont(st.font("small"))
            rlay.addWidget(self._tip_lab, 0)
        # ⚠️ 说明带**固定高度**（`ROW_TIP_LINES` 行）：说明有一行、两行、还是压根没有，
        #    行高都完全一样。⚠️ 超长说明会被裁 —— 所以那条"没有说明超过 2 行"的
        #    测试是这条设计的**前提**，别删。
        self._tip_lab.setFixedHeight(self._tip_line_h() * ROW_TIP_LINES)

        # ⚠️ 「测试连接」不在这儿 —— 它挂在**头部**（见上面 `_build_test_button`），
        #    原来在这儿单独占一行，让这一行比同组别的行高 36 设备像素。
        # 系统提示词那一项下面挂「看提示词 / 只看对话」（2026-10-14 从面板搬来）
        if spec.key == "ai.system_prompt":
            self._build_view_tools(rlay)

        self.scroll.add(row)

    def _build_view_tools(self, rlay):
        """「看提示词」「只看对话」两个查看入口。

        用户要求（2026-10-14）：「把只看对话和看提示词隐藏起来，可以放到设置里面」。
        ⚠️ 为什么挂在**系统提示词**这一项下面：两颗都是"看程序到底读到了什么 /
        发给了什么"的排查动作，而「看提示词」看的就是这一项拼出来的最终结果。
        ⚠️ 工具提示里**不能有 `**` 或反引号**（设置界面不渲染 markdown）。
        """
        st = self.style
        bar = QtWidgets.QWidget()
        blay = QtWidgets.QHBoxLayout(bar)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(st.px(8))
        b1 = Pill(st, "看提示词", self._show_prompt, variant="ghost", parent=bar)
        b1.setToolTip("看最近一次实际发给模型的提示词（一字未改）")
        b2 = Pill(st, "只看对话", self._show_read, variant="ghost", parent=bar)
        b2.setToolTip("把程序这次读到的对话单独显示出来，不调用 AI")
        self.btn_prompt = b1
        self.btn_read = b2
        blay.addWidget(b1, 0)
        blay.addWidget(b2, 0)
        blay.addStretch(1)
        rlay.addSpacing(st.px(5))
        rlay.addWidget(bar, 0)

    def _tip_line_h(self) -> int:
        """说明文字**一行**的高度（设备像素）。

        ⚠️ 用 `QFontMetrics` 量字体，**不是写死 20** —— DPI 或字号一变，
            写死的值要么裁字要么留一道缝。
        """
        return QtGui.QFontMetrics(self.style.font("small")).height()

    def _head_pill(self, st, text, variant="text", command=None, parent=None):
        """头部里的小按钮（「恢复默认」/「测试连接」）。

        ⚠️ 高度**压到头部那一档**（`ROW_HEAD_H`）：Pill 自己高 34 基准，
            比不带按钮的那一行头部高出一截 —— 那样同一列里就是"有的行高有的行矮"
            （用户 2026-10-14 报的）。压平之后带不带按钮都一样高。
        """
        pill = Pill(st, text, variant=variant, command=command, parent=parent)
        pill.setFixedHeight(st.px(ROW_HEAD_H))
        return pill

    def _build_test_button(self, head, row):
        """API Key 那一行的「测试连接」——**挂在头部**，结果文字也放这儿。

        ⚠️ 2026-10-14 改：原来它在控件下面**单独占一行**（一个 ghost 按钮 +
            结果文字），于是 API Key 那一行比同组其它行高 36 设备像素
            （用户报「有些行高度没有统一」）。现在挂进头部，
            跟「恢复默认」同一档，**不再多占一行**。
        ⚠️ 结果文字初始为空、占了宽度但高度就是一行小字 —— 不改变行高。
        """
        st = self.style
        head.addWidget(self._head_pill(st, "测试连接", variant="text",
                                       command=self._test, parent=row), 0,
                       QtCore.Qt.AlignVCenter)
        self.test_result = label(st, "", font_name="small", color_key="text_dim")
        head.addWidget(self.test_result, 0, QtCore.Qt.AlignVCenter)

    # ------------------------------------------------------------ 动作

    def _reset_one(self, key):
        # ⚠️ `RESET_KEEP` 那几样**不许清**（用户 2026-10-14 要求）——
        #    界面上那颗按钮已经不画在这两行了（见 `_build_row`），
        #    这里是**第二道闸**：程序里再调到也不动它。
        if key in RESET_KEEP:
            self._log("「恢复默认」跳过 %s（接口地址/模型/Key 不动）" % key)
            return
        self.store.unset(key)
        self.store.save()
        # ⚠️ 同组重建：**滚动位置要保住**（用户报过"一改就被弹回顶部"）
        self._rebuild_keep_scroll()
        # ⚠️ **重建分组不会发"变了"的信号**（新控件是直接用初值建的），
        #    所以要显式提一次 —— 否则「恢复默认」之后那一项**不会自动生效**，
        #    看起来就是"点了没反应"。
        self._on_field_changed()
        self._notify()

    def _reset_all(self):
        if self.show_dialogs and not _ask_yes_no(
                self.win, "恢复默认",
                # ⚠️ 那句"不会动"的说明**从 `RESET_KEEP_LABEL` 拼**，别手写第二份
                #    （2026-10-14 用户要求「恢复默认接口地址与模型不要改」）。
                "把所有设置恢复为默认值？\n（%s 不会动）" % RESET_KEEP_LABEL,
                yes="恢复默认", no="取消", style=self.style):
            return
        self.store.reset_all()
        self.store.save()
        # 会话级人设/关系也在覆盖层里，一起被清掉了 —— 暂存必须同步清空，
        # 否则面板重建时会把刚清掉的值又填回去（看起来"恢复默认没用"）
        self._session_panel = None
        self._session_stage = {"modes": {}, "personas": {}, "relationships": {}}
        # ⚠️ 同组重建：保住滚动位置（用户报过"点一下就跳回顶部"）
        self._rebuild_keep_scroll()
        self._on_field_changed()
        self._notify()

    def _show_prompt(self):
        """把面板那条路叫起来（**它自己会开窗口**，见 `panel._show_prompt`）。"""
        if not self._on_show_prompt:
            return
        try:
            self._on_show_prompt()
        except Exception as e:
            self._log("看提示词失败: %s: %s" % (type(e).__name__, e))

    def _show_read(self):
        """「只看对话」同理：读取的动作在 run_watch 那边，这里只负责叫它。"""
        if not self._on_show_read:
            return
        try:
            self._on_show_read()
        except Exception as e:
            self._log("只看对话失败: %s: %s" % (type(e).__name__, e))

    def _collect(self) -> Dict[str, object]:
        return {k: f.get() for k, f in self._fields.items()}

    def _save(self):
        # ⚠️ **保存期间停掉自动生效**：不然轮询会在落盘中途把
        #    某些项当成"又变了"再写一次（尤其是我们刚 `_render_group()` 重建完之后）。
        self._saving = True
        try:
            self._save_inner()
        finally:
            self._saving = False
            # 保存完，已落盘的值成为新基准 -> 「需重启」那批不再算"待保存"，
            # 按钮自己就消失了（不需要另外写隐藏逻辑）。
            self._remember()
            for k, v in self._seen.items():
                self._base[k] = v
            self._sync_save_btn()

    def _save_inner(self):
        # ⚠️⚠️ **提交的是 `_seen`（看过的所有分组），不是 `_collect()`（只有当前分组）**。
        #
        #    2026-10-11 修的一个**会静默丢改动**的 bug：
        #        用户在第 1 组改了「记录间隔」（需重启），切到第 2 组，
        #        这时点「保存」—— 原来只提交第 2 组的字段，
        #        **第 1 组那个改动根本没写进去**，而按钮却消失了
        #        （因为保存后我把 `_base` 全更新了），用户以为已经保存。
        #    → `_seen` 是"用户看过的每一项的当前值"，提交它才覆盖得到。
        #      ⚠️ 没看过的分组不在 `_seen` 里 —— 但那些用户也没改过，
        #        `apply_form(only_changes=True)` 本来也不会给它们建覆盖。
        self._remember()
        values = dict(self._seen)
        secrets = [k for k in values
                   if k in SPEC_BY_KEY and SPEC_BY_KEY[k].secret]
        form = {k: v for k, v in values.items() if k not in secrets}
        # 记住"这次保存里有哪几项是要重启才生效的" —— 保存完它们就不算"待保存"了
        # （`_base` 被刷新），后面那句话得靠这里攒下来的名字（见 `_save_inner` 收尾）。
        restart_names = [SPEC_BY_KEY[k].label
                         for k in sorted(self._pending_restart())
                         if k in SPEC_BY_KEY]

        errs = self.store.apply_form(form)
        if errs:
            # 这个用对话框 —— 必须让用户明确看到"一个都没保存"
            self._say("有设置填得不对，一个都没保存", ok=False)
            if self.show_dialogs:
                _error(self.win, "有设置填得不对",
                       "\n".join(errs) + "\n\n（一个都没保存，改完再点保存）",
                       style=self.style)
            return

        # 密钥单独写 secrets.yaml（行级替换，保住注释）
        for k in secrets:
            new = str(values[k] or "").strip()
            cur = str(self.store.get(k, "") or "").strip()
            if new and new != cur:
                try:
                    self.store.set_secret(k, new)
                except Exception as e:
                    self._say("保存密钥失败：%s" % e, ok=False)
                    return

        # 会话级设置：人设/关系进 settings.json，和上面一起落盘
        self._apply_session_maps()

        try:
            self.store.save()
        except Exception as e:
            self._say("保存失败：%s" % e, ok=False)
            return

        # ⚠️ 模式写的是**另一个存储**（`state.json`，归 SessionRegistry 管），
        #    所以要单独应用。放在 save() 之后 —— 万一前面失败，模式不会被带偏。
        merrs = self._apply_session_modes()

        self._log("设置已保存")
        # 保存完把暂存清空，并让面板重建（否则会把刚存过的值又当成"待提交"）
        self._session_panel = None
        self._session_stage = {"modes": {}, "personas": {}, "relationships": {}}
        # ⚠️ **这就是"保存之后跳到顶部"那一处**（L1806 附近）：
        #    保存要走这里重建（把刚存过的值不再当"待提交"），
        #    所以必须保住滚动位置 —— 用户在「高级」里点保存不该被弹回顶部。
        self._rebuild_keep_scroll()
        self._notify()
        if merrs:
            self._say("已保存，但有会话模式没写成功：" + "；".join(merrs), ok=False)
        elif restart_names:
            # ⚠️ 说清"哪些要重启"（用户问过「有什么需要重启的」）——
            #    比原来那句"标注「需重启」的项要重启程序才生效"具体得多。
            self._say("已保存。重启程序后生效（%d 项）：%s"
                      % (len(restart_names), "、".join(restart_names)), ok=True)
        else:
            self._say("已保存", ok=True)

    # ------------------------------------------------------------ 会话设置落盘

    def _session_cfg(self) -> dict:
        self._stash_session()
        return self._session_stage

    def _apply_session_maps(self):
        """人设/关系：整张映射写回 settings.json 的覆盖层。

        ⚠️ 用**整张映射**而不是 `persona.per_session.<会话名>` 这种点分路径：
        `dotted_set` 是按 `.` 切的，而会话名里**完全可能有句点**
        （「张三.工作」这种），一点就切坏。整张写没有这个问题。

        ⚠️ 空值 = **删掉那个键**（恢复用全局）。不删的话，用户想恢复全局
        就只能去手改 JSON —— 而"留空"在界面上看起来已经生效了。
        """
        st = self._session_cfg()
        for name, dotted in (("personas", "persona.per_session"),
                             ("relationships", "persona.relationships")):
            cur = dict(self.store.get(dotted, {}) or {})
            new = dict(cur)
            for k, v in (st.get(name) or {}).items():
                v = (v or "").strip()
                if v:
                    new[k] = v
                else:
                    new.pop(k, None)
            if new != cur:
                # 全空时传 None = 删掉这一层的覆盖（不留 `{}` 空壳）
                self.store.set(dotted, new or None)

    def _apply_session_modes(self) -> list:
        st = self._session_stage
        errs = []
        for k, m in (st.get("modes") or {}).items():
            if not self._on_mode_change:
                continue
            try:
                self._on_mode_change(k, m)
            except Exception as e:
                errs.append("%s（%s）" % (k, e))
        return errs

    def _say(self, text: str, ok: bool = True):
        """在状态行里说一句话（**不弹对话框**）。"""
        try:
            self.status.setText(text)
            self.status.setStyleSheet(
                "color:%s; background:transparent;"
                % (self.style.p["accent"] if ok else self.style.p["danger"]))
        except Exception:
            pass

    # -------------------------------------------- 不用重启的项：改完立刻生效

    def _on_field_changed(self):
        """**任何**字段一变就调这里（各字段类在值变化时回调它）。

        ⚠️ 只负责**重新计时**，不直接落盘 —— 见 `_debounce` 的说明。
        """
        if self._saving or self.win is None:
            return
        try:
            self._debounce.start()
        except Exception as e:
            self._log("启动自动生效定时器失败: %s: %s" % (type(e).__name__, e))

    def _remember(self):
        """把**当前这一组**字段的值并进 `_seen`。

        ⚠️ `_fields` 只是当前分组那一批（`_render_group` 每次重建并清空），
            所以要**在切换分组之前**收拢，否则换一组回来就忘了。
        ⚠️ **不在这里给 `_base` 兜底**（2026-10-11 修的真 bug）：
            第一版写的是"没见过这个键就把**当前值**记进 `_base`"——
            而那是在 `_auto_apply` 里调的，那时用户**已经改过了**，
            于是"改了需重启的项"被判成"没变"，**保存按钮根本不出现**。
            → `_base` 的初始化搬到了建控件的地方（那里拿到的才是**从配置读出来的**值）。
        """
        try:
            for k, f in (self._fields or {}).items():
                self._seen[k] = f.get()
        except Exception as e:
            self._log("收拢字段值失败: %s: %s" % (type(e).__name__, e))

    def _auto_apply(self):
        """把**不需要重启**的项的改动立刻写盘并通知程序。

        ⚠️⚠️ **逐项** `apply_form({k: v})`，不是一次提交整张表单：
            `apply_form` 是"有一个不合法就**整批不写**" ——
            一次提交的话，你在某个 int 框里输到一半（空串）
            会把**别的项的自动生效一起卡住**，而且看不出来为什么。
        ⚠️ 失败的值记进 `_auto_bad`，**同一个值只报一次** ——
            不记的话每 400ms 弹一次同样的错，日志和状态行都会被刷爆
            （"每拍都会发生的事必须节流"）。
        """
        if self._saving or self.win is None:
            return
        self._remember()
        done = []
        for k, v in list(self._seen.items()):
            spec = SPEC_BY_KEY.get(k)
            if spec is None or spec.restart:
                continue                      # 需重启的留给「保存」按钮
            # ⚠️ 打字的项（text / secret / multiline）**也不自动生效** ——
            #    见 `DEFER_KINDS` 的说明（边打边写盘 + 写盘会重建界面把光标顶掉）。
            if spec.kind in DEFER_KINDS:
                continue
            if self._base.get(k, _UNSET) == v:
                continue                      # 和已生效的一样，不用动
            if self._auto_bad.get(k, (None,))[0] == v:
                continue                      # 这个值刚试过、不合法，别再刷屏
            try:
                if spec.secret:
                    cur = str(self.store.get(k, "") or "").strip()
                    new = str(v or "").strip()
                    if new != cur:
                        self.store.set_secret(k, new)
                else:
                    errs = self.store.apply_form({k: v})
                    if errs:
                        raise ValueError(errs[0])
            except Exception as e:
                self._auto_bad[k] = (v, str(e))
                self._say("「%s」还没生效：%s" % (spec.label, e), ok=False)
                continue
            self._auto_bad.pop(k, None)
            self._base[k] = v
            done.append(spec.label)
        if done:
            try:
                self.store.save()
            except Exception as e:
                self._say("自动保存失败：%s" % e, ok=False)
                return
            self._notify()
            self._say("已生效（不用重启）：" + "、".join(done), ok=True)
        self._sync_save_btn()

    def _pending_save(self) -> Dict[str, object]:
        """**还没生效**的项 —— 既要重启的，也包括"打字的项"（`DEFER_KINDS`）。

        ⚠️ 和 `_pending_restart()` 的区别只有一个：后者只算"需重启"的，
            用来**写状态行那句话**（"要重启才生效（N 项）"）。
            判断"保存按钮该不该出现 / 「放弃改动」该撤哪些"必须用这个宽的那个 ——
            否则打字的项改了之后**按钮不出现**，用户等不到保存（静默不落盘）。
        """
        self._remember()
        out = {}
        for k, v in self._seen.items():
            spec = SPEC_BY_KEY.get(k)
            if spec is None:
                continue
            if not (spec.restart or spec.kind in DEFER_KINDS):
                continue
            if self._base.get(k, _UNSET) != v:
                out[k] = v
        return out

    def _pending_restart(self) -> Dict[str, object]:
        """哪些**需重启**的项被改过、还没保存。"""
        self._remember()
        out = {}
        for k, v in self._seen.items():
            spec = SPEC_BY_KEY.get(k)
            if spec is None or not spec.restart:
                continue
            if self._base.get(k, _UNSET) != v:
                out[k] = v
        return out

    def _session_dirty(self) -> bool:
        """会话面板里有没有还没落盘的改动。

        ⚠️ 它们写的是 `state.json`（归 `SessionRegistry` 管），**不走 `_fields`** ——
            所以自动生效那条路完全覆盖不到它们，**必须**算进"要保存"，
            否则用户改了会话模式、按钮不出现，改动就**静默丢了**。
        """
        self._stash_session()
        st = self._session_stage or {}
        return any(st.get(d) for d in ("modes", "personas", "relationships"))

    def _on_cancel(self):
        """底部那颗「关闭 / 放弃改动」。

        ⚠️ 2026-10-13（用户报「修改后不要按取消吧，交互起来很奇怪」）：
            它**不是"取消"**。不用重启的项改完就已经写进 `settings.json` 并生效了，
            **撤不回来** —— 所以"取消"这个名字本身就是在骗人。现在：
              · 没有待保存的改动 -> **关窗**（和标题栏那个 ✕ 等价）
              · 有 -> 把那些**还没生效**的改动拨回去
        """
        # ⚠️ 用 `_pending_save()`（宽的那个）：打字的项也**还没生效**，
        #    「放弃改动」同样得把它们拨回去 —— 只算"需重启"的话，
        #    用户在提示词框里改了一堆再点「放弃改动」，会以为撤了、其实还在框里。
        pend = self._pending_save()
        dirty = self._session_dirty()
        if not pend and not dirty:
            self.close()
            return
        names = [SPEC_BY_KEY[k].label for k in sorted(pend) if k in SPEC_BY_KEY]
        for k in list(pend):
            # 拨回"已生效的那个值"。
            # ⚠️ **两处都要拨**：`_seen`（跨分组判断"要不要保存"的依据）+ **控件本身**
            #    —— 因为下面那句 `_render_group()` 会先调 `_remember()`，
            #    而 `_remember()` 是**从控件里读**的，只改 `_seen` 会被它当场覆盖回去
            #    （第一版就只改了 `_seen`，实测"放弃"之后字段里还是那个新值）。
            if k in self._base:
                self._seen[k] = self._base[k]
                fld = self._fields.get(k)
                if fld is not None:
                    # ⚠️ 用 `set()`；**不要**在这里吞异常 —— 字段类少一个 `set`
                    #    就会变成"点了放弃、界面没变"（第一版正是这么错的）。
                    fld.set(self._base[k])
            else:
                self._seen[k] = _UNSET
        # 会话面板那批（写 `state.json` 的）也一起丢掉（它们**不保存就等于没改**）
        self._session_stage = {"modes": {}, "personas": {}, "relationships": {}}
        self._session_panel = None
        self._rebuild_keep_scroll()
        self._sync_save_btn(force_say=True)
        self._say("已放弃还没生效的改动" + ("：" + "、".join(names) if names else ""))

    def _sync_save_btn(self, force_say: bool = False):
        """保存按钮：**只有真的需要它的时候才出现**；底部那颗键的文案也跟着变。

        ⚠️ 2026-10-13 加了两件事（用户问「修改后是否有马上实施？有什么需要重启的」）：
            ① 「关闭 / 放弃改动」的文案与提示**跟着状态走**（见 `_on_cancel`）；
            ② 把**要重启的是哪几项**写进状态行 —— 用户不用去猜、也不用翻文档。
        """
        if self.btn_save is None:
            return
        # ⚠️ 判断"要不要出现"用**宽**的那个（含打字的项），写状态行那句话
        #    才用"只算需重启"的那个 —— 不然打字之后按钮不出现（见 `_pending_save`）。
        pend_all = self._pending_save()
        pend = self._pending_restart()
        dirty = self._session_dirty()
        need = bool(pend_all) or dirty
        self.btn_save.setVisible(need)
        try:
            self.btn_cancel.set_text("放弃改动" if need else "关闭")
            self.btn_cancel.setToolTip(
                "放弃这些还没生效的改动（已经生效的那些撤不回来）" if need
                else "关闭设置窗口（已经改过的项都已生效）")
        except Exception:
            pass
        # 「要重启的是哪几项」只在**集合变了**的时候说一次 ——
        # 这个是 1.2s 的定时器叫的，每拍都 `_say` 会把状态行刷成复读机。
        sig = (tuple(sorted(pend_all)), tuple(sorted(pend)), dirty)
        if sig != self._pend_sig or force_say:
            self._pend_sig = sig
            if pend:
                names = [SPEC_BY_KEY[k].label for k in sorted(pend)
                         if k in SPEC_BY_KEY]
                # ⚠️ 不写"点「保存」"：直接关窗也会保存（见 `close`），
                #    而那几句话只会让状态行变长。要重启的是**哪几项**才是重点。
                self._say("要重启才生效（%d 项）：%s"
                          % (len(names), "、".join(names)))
            elif pend_all:
                # 「打字的项」不是要重启，只是还没保存 —— 说清楚它不会自己生效
                names = [SPEC_BY_KEY[k].label for k in sorted(pend_all)
                         if k in SPEC_BY_KEY]
                self._say("还没保存（%d 项）：%s" % (len(names), "、".join(names)))
            elif dirty:
                self._say("会话面板的改动还没保存")

    def _notify(self):
        if self._on_saved:
            try:
                self._on_saved()
            except Exception as e:
                self._log("应用设置时出错: %s: %s" % (type(e).__name__, e))

    # ------------------------------------------------------------ 测试连接

    def _test(self):
        """测试连接：发一次最小请求验证 Key / 地址 / 模型。

        ⚠️ 必须放在子线程 —— 网络请求会阻塞，卡在 UI 线程上窗口就假死了。
        ⚠️ 但子线程**不能碰控件**，结果要经 Signal 排队回 UI 线程
            （见类文档里关于 QObject 的说明）。Tk 版走的是 `win.after(0, ...)`，
            Qt 里没有对应的线程安全入口，`QTimer.singleShot(0, ...)` 在子线程里
            根本不会被执行（那个线程没有事件循环）。
        """
        vals = self._collect()
        base = str(vals.get("ai.base_url") or self.store.get("ai.base_url", "") or "")
        key = str(vals.get("ai.api_key") or "").strip()
        model = str(vals.get("ai.model") or self.store.get("ai.model", "") or "")
        if not key:
            key = str(self.store.get("ai.api_key", "") or "")

        self._set_test_result("正在测试…", ok=None)

        def work():
            try:
                ok, msg = test_api(base, key, model)
            except Exception as e:          # test_api 自己承诺不抛，兜一层
                ok, msg = False, "%s: %s" % (type(e).__name__, e)
            try:
                self._test_done.emit(bool(ok), str(msg))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, ok: bool, msg: str):
        """子线程的结果回来了（这个槽在 UI 线程上跑）。"""
        self._set_test_result(msg, ok=ok)

    def _set_test_result(self, text: str, ok: Optional[bool] = None):
        lab = self.test_result
        if lab is None:
            return
        p = self.style.p
        color = p["text_dim"] if ok is None else (p["accent"] if ok else p["danger"])
        try:
            lab.setText(text)
            lab.setStyleSheet("color:%s; background:transparent;" % color)
        except Exception:
            pass
