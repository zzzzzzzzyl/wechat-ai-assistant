# -*- coding: utf-8 -*-
"""Qt 基础层：QApplication 初始化、缩放、样式、小工具。

从 Tk 迁移过来的背景
--------------------
Tk 8.6 的 Canvas **不做抗锯齿**、也不支持 alpha，为此在旧实现里堆了一轮又一轮
补丁（自绘多边形 → 两层填充 → PIL 位图抗锯齿 → 位图节流），每修一个又带出新的。
实测（同一台机器、同一内容量）：

    · 改窗口尺寸：Tk 34~75 ms/帧（最差 281ms） → Qt **10~12 ms/帧**
    · 圆角抗锯齿：Tk 靠 4x 超采样位图（过渡色 516）→ Qt 一行
      `setRenderHint(Antialiasing)` → 过渡色 **1569**
    · 无边框圆角：Tk 要 `overrideredirect` + 透明色键 `#FF00FE`（撞色就穿洞）
      → Qt `WA_TranslucentBackground`（真 alpha）
    · 残影：Tk 销毁子控件后不标脏区，要手动 `update_idletasks()` → Qt 没这问题

**所以在 Qt 版里，位图抗锯齿 / 节流 / 色键 / 残影补丁全都不需要了。**

DPI 缩放：这里有个会毁掉整个布局的坑
------------------------------------
⚠️ **不能让 Qt 自动缩放和 Tk 那套手动缩放同时生效** —— 两边都缩放会让所有
    布局数字**翻倍**（实测 537 宽的窗口变成 805）。做法：
    · Qt 侧关掉自动缩放（`QT_ENABLE_HIGHDPI_SCALING=0` +
      舍入策略 `Floor`）；
    · 继续用 `theme.px()` **手动**缩放，和 Tk 版保持同一套布局常量。
    · 字号用 `QFont.setPixelSize()`（**不是** `setPointSize`），
      在 1.5x 下才是清晰的物理像素大小。

⚠️ 色板 / 字号仍然走 `theme.py`，本模块不写死任何色号。
"""

import os
from typing import Dict, Optional, Sequence

# ⚠️ 必须在 import QtWidgets **之前**设，否则不生效
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from .. import theme  # noqa: E402

# 逻辑 -> 物理的缩放系数，由 `init_app()` 按屏幕真实 DPI 设好
_SCALE = 1.0


def init_app(argv: Optional[Sequence[str]] = None) -> QtWidgets.QApplication:
    """建 QApplication 并定好缩放。整个程序只能调一次。

    ⚠️ 顺序要紧：舍入策略必须在 QApplication 构造**之前**设；
       `QT_ENABLE_HIGHDPI_SCALING=0` 更是必须在 **import QtWidgets 之前**
       设（所以它写在模块顶部，不是这里）。
    """
    QtCore.QCoreApplication.setAttribute(
        QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        list(argv or []))
    global _SCALE
    _SCALE = _detect_scale(app)
    apply_app_icon(app)
    return app


def apply_app_icon(app) -> str:
    """给应用设图标 —— 任务栏 / 标题栏 / Alt-Tab 都取它。

    ⚠️⚠️ **光设 `QApplication.setWindowIcon()` 在 Windows 上是不够的。**
        用 `python.exe 脚本.py` 启动时，任务栏按钮属于 **python.exe**：
        Windows 按「AppUserModelID」分组，而 python.exe 没有显式的那个，
        于是它走自己的默认 —— 图标显示成 **Python 的图标**，
        而且和别的 python 脚本挤成一组。
        → 所以还要 `SetCurrentProcessExplicitAppUserModelID`（见
          `_set_app_user_model_id`），两者**缺一不可**。
    ⚠️ 必须在**建任何窗口之前**调：Windows 是在窗口首次显示时决定它归到哪个
        任务栏按钮的，之后再改 AppUserModelID 不一定会重新分组。
    ⚠️ 设不上图标**不要抛异常**（图标是锦上添花，不该让程序起不来）——
        返回实际用上的路径（空串 = 没找到文件），调用方可以记一行日志。
    """
    path = ""
    try:
        from .. import paths

        path = paths.icon_path()
        if not os.path.exists(path):
            path = ""
        if path:
            ic = QtGui.QIcon(path)
            if not ic.isNull():
                app.setWindowIcon(ic)
            else:
                path = ""
    except Exception:
        path = ""
    _set_app_user_model_id()
    return path


def _set_app_user_model_id(app_id: str = "zzzyl.wechat-ai-assistant") -> bool:
    """让 Windows 把我们的窗口当成**一个独立应用**（而不是 python.exe）。

    不设的话任务栏上会是 Python 的图标、并且跟其他 python 脚本混在一起。
    ⚠️ 它必须在**窗口显示之前**设置。
    ⚠️ 非 Windows / 调用失败都只是返回 False，不影响程序运行。
    """
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(app_id))
        return True
    except Exception:
        return False


def _detect_scale(app) -> float:
    """量出真实缩放（1.0 = 96 DPI）。夹在 [1.0, 4.0]，和 Tk 版一致。

    ⚠️ 两道来源，不能只信 Qt：实测 `QT_QPA_PLATFORM=offscreen` 时
       `logicalDotsPerInch()` 恒等于 96 —— 整个界面会按 1.0 排版。
       真实屏幕下 Qt 这一路是准的，系统 API 只在 Qt 报 96 时兜底。

    ⚠️ `GetDpiForSystem()` 在本机返回过不稳定值（Tk 版就是因此改用
       `winfo_fpixels("1i")` 的），所以它**只当兜底**，不当主路径。
       而且只在**真屏幕报不出 DPI** 时才问它 ——
       真实屏幕下 Qt 那一趟本来就准，多问一次只会把"没感知"时的假 96 引进来。

    ⚠️ 判"要不要兜底"要按 **`logicalDotsPerInch()` 是不是可疑的 96**，
        不能按平台名。第一版写成 `platformName() != "windows"` 就漏掉了
        offscreen —— 而 offscreen **恰恰是唯一会恒报 96 的那个平台**
        （真机上它是 144）。漏掉之后探针的缩放静默退成 1.0，
        所有 `px()` 断言跟着缩水一半，测试却还是"绿"的。
    """
    try:
        scr = app.primaryScreen()
        dpi = float(scr.logicalDotsPerInch()) if scr else 96.0
    except Exception:
        dpi = 96.0
    if abs(dpi - 96.0) < 0.5:
        dpi = max(dpi, _win_dpi())
    return max(1.0, min(4.0, dpi / 96.0))


def _win_dpi() -> float:
    """Windows 系统 DPI（拿不到就返回 96）。

    ⚠️ **只读，不要在这里设进程 DPI 感知。** 进程的感知方式**只能设一次**，
        而 Qt 6 一启动就自己设 V2；这里再设一次会拿到 `E_ACCESSDENIED`
        并在 stderr 打红字警告。感知由 `wechat_assistant/dpi.py` 在
        **import Qt 之前**统一负责（它现在优先设 V2，正是为了让 Qt 不抱怨）。
    """
    try:
        import ctypes

        return float(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        return 96.0


def scale() -> float:
    return _SCALE


def px(v: float) -> int:
    """基准像素 -> 实际像素（和 Tk 版 `Style.px()` 同语义）。"""
    return theme.px(v, _SCALE)


class Style:
    """一套配色 + 字号 + 缩放。

    保留这个类是为了**让 `theme.py` 继续当唯一色号出处**：
    界面代码只认语义键（`p["bg"]` / `p["text"]` …），不写死色号。
    """

    def __init__(self, cfg=None, hwnd: int = 0, name: Optional[str] = None,
                 logger=None):
        self.scale = _SCALE
        self.theme_name = name or theme.detect(cfg, hwnd, logger)
        self.p = theme.palette(self.theme_name)
        self.f = theme.fonts(self.scale)
        self._fonts: Dict[str, QtGui.QFont] = {}

    def px(self, v: float) -> int:
        return theme.px(v, self.scale)

    # ---- 设计档位（2026-09-27 新增，见 `theme.RADIUS` / `theme.SPACE`）
    #
    # ⚠️ **新增界面代码从这里取，不要再写 `st.px(13)` 这种手写的数**。
    #    改之前盘过：`setContentsMargins` 18 种组合、`setSpacing` 7 种、
    #    圆角 3/8/25 混用（`probes/qt/audit_spacing.py`）。
    #    用户的要求是「全局统一边距常量，全部复用，禁止写死随机 margin/padding」。

    def R(self, name: str) -> int:
        """圆角档位：`window` / `control` / `chip` / `pill` / `none`。"""
        return theme.radius(name, self.scale)

    def S(self, name: str) -> int:
        """间距档位：`xxs` / `xs` / `sm` / `md` / `lg` / `xl`。"""
        return theme.space(name, self.scale)

    def margins(self, *names: str):
        """按档位名给出四个边距元组：`st.margins("xl", "md")` 等。

        支持 1 / 2 / 3 / 4 个名字，语义跟 CSS 一致
        （1=四边相同，2=上下/左右，3=上/左右/下，4=上/右/下/左）。
        """
        vals = [self.S(n) for n in names]
        if len(vals) == 1:
            return (vals[0],) * 4
        if len(vals) == 2:
            return (vals[0], vals[1], vals[0], vals[1])
        if len(vals) == 3:
            return (vals[0], vals[1], vals[2], vals[1])
        if len(vals) == 4:
            return tuple(vals)
        raise ValueError("margins 只接受 1~4 个档位名，收到 %d 个" % len(vals))

    def font(self, name: str) -> QtGui.QFont:
        """QFont。用 **setPixelSize** —— 见模块头部关于 DPI 的说明。"""
        if name not in self._fonts:
            spec = self.f.get(name, self.f["body"])
            fam = spec[0]
            size = abs(int(spec[1]))       # theme 里是 Tk 的负数字号 = 像素
            bold = len(spec) > 2 and spec[2] == "bold"
            f = QtGui.QFont(fam)
            f.setPixelSize(max(8, size))
            f.setBold(bool(bold))
            self._fonts[name] = f
        return self._fonts[name]

    def __repr__(self):
        return "<Style %s scale=%.2f>" % (self.theme_name, self.scale)


# ---------------------------------------------------------------- 小工具

def qc(color: str) -> QtGui.QColor:
    return QtGui.QColor(str(color))


def hand_cursor() -> QtGui.QCursor:
    return QtGui.QCursor(QtCore.Qt.PointingHandCursor)


def rgba_css(color: str, alpha: int = 255) -> str:
    c = qc(color)
    return "rgba(%d,%d,%d,%d)" % (c.red(), c.green(), c.blue(), alpha)


def transparent(widget: QtWidgets.QWidget):
    """让控件底色透出父级（等价 Tk 版的 `inherit_bg()`）。"""
    widget.setAttribute(QtCore.Qt.WA_TranslucentBackground, False)
    widget.setStyleSheet("background:transparent;")
    return widget
