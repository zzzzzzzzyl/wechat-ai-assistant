# -*- coding: utf-8 -*-
"""系统托盘图标（右下角通知区域）。

为什么要有它
------------
⚠️ **窗口藏起来的时候，任务栏按钮也会跟着消失**（Windows 的行为，不是 bug）。
而本程序的 ✕ 就是"藏起来" —— 藏完之后这个窗口**既没有任务栏条目、alt-tab 也切不到**，
只剩热键一条路能叫回来（`CLAUDE.md` 里记着这条实测反馈）。
托盘图标补上的就是这条路：右下角一直有个图标，点一下/右键菜单就能把面板叫回来。

⚠️ Qt 侧两个必须记住的点（跟 `QTimer` 存局部变量只响一次是同一类问题）：
  · `QSystemTrayIcon` 和 `QMenu` **都必须存成实例属性**。写成局部变量会被 GC ——
    症状是"图标在、右键没反应"，或者图标过一会儿自己消失。
  · **没有托盘的环境**（离屏平台、某些精简桌面）`isSystemTrayAvailable()` 是 False，
    这时要**静默跳过**，不能让程序起不来。
"""

from typing import Callable, List, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

__all__ = ["tray_menu", "tray_tooltip", "TrayIcon"]


# ---------------------------------------------------------------- 纯逻辑（可脱开界面测）

def tray_menu(visible: bool) -> List[Tuple[str, str]]:
    """托盘右键菜单的 (动作 id, 文案)。

    ⚠️ 「显示/隐藏」的**文案要跟着当前状态变** —— 写死成"显示悬浮窗"的话，
        面板已经开着时那一项看起来是坏的（点了没反应，因为它本来就在）。
    ⚠️ 2026-10-14：**「只看对话」从菜单里撤掉了**（用户要求：「把只看对话和看提示词
        隐藏起来，可以放到设置里面」）。托盘是"面板藏起来之后唯一的入口"，
        该只留**找得回面板 / 看设置 / 退出**这三件事 —— 查看类动作去设置里点。
    """
    return [
        ("toggle", "隐藏悬浮窗" if visible else "显示悬浮窗"),
        ("settings", "设置…"),
        ("quit", "退出"),
    ]


def tray_tooltip(session: str) -> str:
    """鼠标停在托盘图标上显示的文字。

    ⚠️ 没有会话时**只说程序名**，不要写成「微信 AI 助手 · —」那种带破折号的
        （`"—"` 是"当前没打开会话"的内部占位，给人看没有意义）。
    """
    name = (session or "").strip()
    if not name or name == "—":
        return "微信 AI 助手"
    return "微信 AI 助手 · %s" % name


# ---------------------------------------------------------------- Qt 外壳

class TrayIcon(QtCore.QObject):
    """托盘图标。

    ⚠️ 和别的界面对象不同，它**不挂在某个窗口上**（`parent` 传的是 QObject），
        所以窗口隐藏/重建都不影响它 —— 这正是要它的理由。
    """

    def __init__(self, icon: QtGui.QIcon, on_action: Callable[[str], None],
                 parent: QtCore.QObject = None):
        super().__init__(parent)
        self._on_action = on_action
        self._visible = False
        self._session = ""

        # ⚠️ 存成实例属性（`self.`）—— 局部变量会被 GC，图标就没了。
        self.icon = QtWidgets.QSystemTrayIcon(icon, self)
        self.menu = QtWidgets.QMenu()
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        self._items = {}
        self._build_menu()

    # ---- 可用性

    @staticmethod
    def available() -> bool:
        """这台机器/这个平台有没有系统托盘。

        ⚠️ **离屏平台（`QT_QPA_PLATFORM=offscreen`）永远是 False** ——
            测试里必须容忍这一点，不能断言"一定建得出来"。
        """
        try:
            return bool(QtWidgets.QSystemTrayIcon.isSystemTrayAvailable())
        except Exception:
            return False

    # ---- 菜单

    def _build_menu(self):
        self.menu.clear()
        self._items = {}
        for act_id, text in tray_menu(False):
            # 分三组：显示/隐藏 · 只看对话 ｜ 设置 ｜ 退出
            if act_id in ("settings", "quit"):
                self.menu.addSeparator()
            a = self.menu.addAction(text)
            # ⚠️ 用**默认参数**把 act_id 绑住。写成 `lambda: self._fire(act_id)` 的话
            #    闭包捕获的是变量本身，循环结束后全都指向最后一个 id
            #    （经典的晚绑定坑 —— 那样四个菜单项会全变成"退出"）。
            a.triggered.connect(lambda _checked=False, i=act_id: self._fire(i))
            self._items[act_id] = a

    def _fire(self, act_id: str):
        try:
            self._on_action(act_id)
        except Exception:
            pass

    def _on_activated(self, reason):
        """点托盘图标本身。

        Windows 上左键单击 = `Trigger`，双击 = `DoubleClick` —— 两个都当"切换"。
        ⚠️ 右键是**弹菜单**（`Context`），这里必须放过，否则菜单刚弹出来
            又被我们切了一次显示状态。
        """
        if reason in (QtWidgets.QSystemTrayIcon.Trigger,
                      QtWidgets.QSystemTrayIcon.DoubleClick):
            self._fire("toggle")

    # ---- 状态

    def refresh(self, visible: bool, session: str = ""):
        """更新菜单文案与悬停提示。状态没变时不做任何事（避免无谓的重绘）。"""
        if visible == self._visible and session == self._session:
            return
        self._visible = bool(visible)
        self._session = session or ""
        item = self._items.get("toggle")
        if item is not None:
            item.setText(tray_menu(visible)[0][1])
        self.icon.setToolTip(tray_tooltip(session))

    def show(self):
        try:
            self.icon.setToolTip(tray_tooltip(self._session))
            self.icon.show()
        except Exception:
            pass

    def stop(self):
        """拆掉（退出时调）。⚠️ 顺序：先摘菜单再删，否则会有悬空引用。"""
        try:
            self.icon.hide()
            self.icon.setContextMenu(None)
            self.icon.activated.disconnect()
        except Exception:
            pass
        try:
            self.menu.clear()
            self.menu.deleteLater()
        except Exception:
            pass
        try:
            self.icon.deleteLater()
        except Exception:
            pass
