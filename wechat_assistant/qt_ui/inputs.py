# -*- coding: utf-8 -*-
"""Qt 版输入控件：多行输入区 / 单行输入框（圆角描边）。

⚠️ 本文件开头的说明**曾经被编码事故毁掉**（整段中文变成了 `?` 串，
   在 git 历史里就已经是那样了，不是某一次编辑临时出的问题）。
   下面是按**代码本身**重新写的，不是还原原文 —— 原文已经不可考。
   教训：写中文文件一律用编辑工具 / UTF-8，**别用会按 GBK 转手的
   PowerShell `Get-Content` + `Set-Content`**（那一次就是这么坏的）。

和 Tk 版的区别：
   Tk 那边要「`Canvas` 画圆角 + 里面嵌一个无边框的 `tk.Text` / `tk.Entry`」，
   因为 Tk 的输入控件画不了圆角，得用 `create_window` 把控件塞进画布。
   Qt 的 QSS 直接支持 `border-radius`，连占位符都是原生的
   （`setPlaceholderText`）—— 整层补丁都不需要了。

⚠️ 选中文字的高亮（`selection-background-color`）**两个类都要写**：
   本文件里 `InputBox` 走 `_edit_qss()`，`EntryBox` 有**自己的一份**样式表，
   只改一处的话单行输入框会退回系统默认的蓝。
"""

from PySide6 import QtCore, QtWidgets

from .base import Style, px


def _edit_qss(style: Style, focused: bool) -> str:
    p = style.p
    bd = p["accent"] if focused else p["border"]
    # ⚠️ `selection-background-color` / `selection-color` **必须显式写**。
    #    不写的话 Qt 用**系统默认的选中高亮**（本机实测是 `#308CC6` 那个蓝）——
    #    深色浅色两套下都是蓝的，跟微信没关系。用户报的
    #    「字体高亮并不是微信设置的颜色」就是这条。
    #    见 `probes/qt/check_selection_color.py`（选中文字后量像素）。
    return """
        QPlainTextEdit, QLineEdit {
            background:%s; color:%s; border:1px solid %s;
            border-radius:%dpx; padding:%dpx %dpx;
            selection-background-color:%s; selection-color:%s;
        }
    """ % (p["surface"], p["text"], bd, style.R("control"), style.px(6),
           style.px(10), p["sel_bg"], p["sel_fg"])


class InputBox(QtWidgets.QPlainTextEdit):
    """带圆角描边的多行输入区，聚焦时描边变绿。

    ⚠️ 和 Tk 版的区别：Tk 那边是「Canvas 画圆角 + 嵌一个无边框 Text」，
       因为 `tk.Text` 画不了圆角。Qt 的 QSS 直接给 `border-radius`，
       连占位符都是原生的（`setPlaceholderText`）—— 少掉一整层补丁。
    """

    def __init__(self, style: Style, height: int = 60,
                 font_name: str = "body", placeholder: str = "",
                 parent=None, **kw):
        super().__init__(parent)
        self.style = style
        self._focused = False
        self.setFont(style.font(font_name))
        self.setFixedHeight(int(height))
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        if placeholder:
            self.setPlaceholderText(placeholder)
        self._restyle()

    def _restyle(self):
        self.setStyleSheet(_edit_qss(self.style, self._focused))

    def focusInEvent(self, ev):
        self._focused = True
        self._restyle()
        super().focusInEvent(ev)

    def focusOutEvent(self, ev):
        self._focused = False
        self._restyle()
        super().focusOutEvent(ev)

    # ---- 对外（和 Tk 版同名）

    def get(self) -> str:
        return self.toPlainText().strip()

    def set(self, value: str):
        self.setPlainText("" if value is None else str(value))

    def clear(self):
        super().clear()


class EntryBox(QtWidgets.QLineEdit):
    """**圆角描边**的单行输入框。

    ⚠️ Tk 版要自己画圆角（`tk.Entry` 只能靠 highlightthickness 画直角）；
       Qt 一行 QSS 就有，而且聚焦描边、占位符都是原生的。
    """

    def __init__(self, style: Style, text: str = "", placeholder: str = "",
                 parent=None, **kw):
        super().__init__(parent)
        self.style = style
        self._focused = False
        self.setFont(style.font("body"))
        self.setFixedHeight(style.px(34))
        self.setText("" if text is None else str(text))
        if placeholder:
            self.setPlaceholderText(placeholder)
        self._restyle()

    def _restyle(self):
        p = self.style.p
        bd = p["accent"] if self._focused else p["border"]
        # ⚠️ 选中高亮的两个键见 `_edit_qss` 的说明 —— 这里**也要写**，
        #    否则单行输入框（设置窗口里的那些）又会退回系统默认的蓝。
        #    "改了一个地方就以为全改了"是这类问题最容易漏的点：
        #    这个类有自己的样式表，不走 `_edit_qss`。
        self.setStyleSheet("""
            QLineEdit {
                background:%s; color:%s; border:1px solid %s;
                border-radius:%dpx; padding:%dpx %dpx;
                selection-background-color:%s; selection-color:%s;
            }
        """ % (p["surface"], p["text"], bd, self.style.R("control"),
               self.style.px(2), self.style.px(10), p["sel_bg"], p["sel_fg"]))

    def focusInEvent(self, ev):
        self._focused = True
        self._restyle()
        super().focusInEvent(ev)

    def focusOutEvent(self, ev):
        self._focused = False
        self._restyle()
        super().focusOutEvent(ev)

    # ---- 对外（和 Tk 版同名）

    def get(self) -> str:
        return self.text()

    def set(self, value: str):
        self.setText("" if value is None else str(value))
