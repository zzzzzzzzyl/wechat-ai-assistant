# -*- coding: utf-8 -*-
"""让悬浮窗贴着微信窗口站 —— 位置和高度都跟着微信走。

设计原则：**助手跟随微信，微信绝不动。**
本模块只做两件事：`GetWindowRect` 读微信窗口在哪、`IsIconic` 看它有没有最小化。
两个都是普通 Win32 调用，**不走 UIA**（所以不会卡死），也不改变微信的任何状态。

⚠️ 为什么绝不移动 / 置顶微信窗口
--------------------------------
搬动别的程序的窗口，跟"模拟点击"是同一类操作，在本项目的红线之外。
所以只能是助手去适配微信，反过来不行。

**代价说清楚**：微信最大化的时候屏幕两侧没有空位，面板就没地方站，
这时会退回原来的位置（不硬挤）。想要真正的并排，得把微信拖窄一点。

怎么做才不闪
------------
实测反馈过"动画很奇怪"。根因是**每一帧都调 `geometry()`**，
哪怕目标位置没变 —— 那会让 Tk 反复重排、重绘子控件。
所以这里先比对目标与当前值，**只有真的变了才动窗口**。
"""

import ctypes
import os
import time
from typing import Callable, Optional, Tuple

import win32api
import win32con
import win32gui
import win32process

# 面板与微信窗口之间留的空隙（基准像素，按 DPI 缩放由调用方传入）
DEFAULT_GAP = 8

# 兜底：多久没找到微信就认为它关了（秒）
LOST_AFTER = 3.0

# 死区：差不到这么多像素就**不动窗口**。
#
# ⚠️ 位置和尺寸要用**不同的**阈值，因为代价完全不同：
#   · 改位置 -> `geometry("+x+y")`，**不触发子树重排**，很便宜
#   · 改尺寸 -> 整棵控件树重排（卡片重新换行、圆角重画…），很贵
# 所以位置给 0（差 1px 也跟，才"跟手"），尺寸给 2px 吃掉噪声。
#
# 尺寸那边为什么需要死区：微信窗口的 rect 在拖动时会有 1~2px 抖动
# （DWM 隐形边框、取整），跟着它跑就是让面板来回伸缩，
# 而每次伸缩都要重排一遍 —— 又闪又费。
POS_DEADBAND = 0
SIZE_DEADBAND = 2

#: `ui.attach` 的三个取值（见 `config.yaml` 与 `settings.py` 的 SPEC）。
#:
#: ⚠️ **`dock` 和 `float` 的区别不是"跟不跟"，而是"位置从哪儿来"**：
#:   · `dock`  —— 位置是**算出来的**：每次重新算"站在微信哪一侧、紧贴它旁边"。
#:                用户手动拖过之后会被拉回边上（微信一动就拉回，见 `_apply`）。
#:   · `float` —— 位置是**你定的**：只记下"面板相对微信左上角的偏移"，
#:                之后微信怎么动，面板就按同一个偏移平移过去。
#: 这两个是用户先后明确要过的行为，**别把它们合并成一个**。
ATTACH_DOCK = "dock"
ATTACH_FLOAT = "float"
ATTACH_OFF = "off"


def _exe_of_pid(pid: int) -> str:
    """取进程的 exe 全路径。

    ⚠️ **不启子进程** —— 沙箱禁止管道，`subprocess.run(capture_output=True)`
    会 EPERM，所以只走纯 win32。
    """
    h = None
    try:
        h = win32api.OpenProcess(0x1000, False, pid)     # QUERY_LIMITED_INFORMATION
        return win32process.GetModuleFileNameEx(h, 0) or ""
    except Exception:
        return ""
    finally:
        if h is not None:
            try:
                win32api.CloseHandle(h)
            except Exception:
                pass


def find_wechat_window(cfg) -> int:
    """找微信主窗口（面积最大的那个 QWindowIcon）。

    用 `EnumWindows` 而不是 UIA —— 这里只需要一个 HWND，
    走 UIA 反而有 provider 卡死的风险。
    """
    proc = str(cfg.get("wechat.proc_name", "Weixin.exe") or "Weixin.exe").lower()
    hint = str(cfg.get("wechat.window_class_contains", "QWindowIcon") or "QWindowIcon")
    found = []
    name_ok = {"seen": False}

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            cls = win32gui.GetClassName(hwnd)
            if hint not in cls:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe = os.path.basename(_exe_of_pid(pid)).lower()
            if exe:
                name_ok["seen"] = True
                if exe != proc:
                    return True
            r = win32gui.GetWindowRect(hwnd)
            found.append((hwnd, (r[2] - r[0]) * (r[3] - r[1]), r))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return 0
    if not found:
        return 0
    found.sort(key=lambda t: -t[1])
    return found[0][0]


def work_area() -> Tuple[int, int, int, int]:
    """桌面可用区域 (left, top, right, bottom)，**已排除任务栏**。

    比 `GetSystemMetrics` 好：后者会把任务栏那几十像素也算进去，
    面板贴底时会被任务栏挡住。
    """
    try:
        rect = win32gui.SystemParametersInfo(win32con.SPI_GETWORKAREA)
        if rect:
            return tuple(rect)
    except Exception:
        pass
    try:
        return (0, 0, win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1))
    except Exception:
        return (0, 0, 1920, 1080)


def window_box(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """微信窗口的屏幕矩形；窗口没了返回 None。"""
    if not hwnd:
        return None
    try:
        if not win32gui.IsWindow(hwnd):
            return None
        return tuple(win32gui.GetWindowRect(hwnd))
    except Exception:
        return None


def is_minimized(hwnd: int) -> bool:
    try:
        return bool(win32gui.IsIconic(hwnd))
    except Exception:
        return False


def foreground_is(hwnd: int) -> bool:
    """当前前台窗口是不是（或属于）这个窗口。"""
    if not hwnd:
        return False
    try:
        fg = win32gui.GetForegroundWindow()
        if not fg:
            return False
        if fg == hwnd:
            return True
        # 前台窗口可能是微信的某个子窗口，往上找根再比
        import ctypes

        root = ctypes.windll.user32.GetAncestor(fg, 2)      # GA_ROOT
        return root == hwnd
    except Exception:
        return False


# ---------------------------------------------------------------- 界面适配
#
# ⚠️ 这一层原本是"Tk / Qt 两版通用的适配口"（这个模块原先到处直接摸
#    `ui.root.winfo_xxx()`）。**2026-09-26 Tk 版删掉之后，只剩 Qt 一条路** ——
#    但函数名保留着，因为调用点（`run_watch` / 测试）用的是它们。
#    新增访问界面几何/句柄的地方**统一走这两个函数**，别直接摸 `ui.xxx`
#    （这样将来再换界面框架时，要改的只有这里）。

def ui_native_handle(ui) -> int:
    """界面窗口的**顶层** HWND。

    ⚠️ `QWidget.winId()` 给的可能是**内层**窗口。能拿到 `QWindow` 就直接问它
        （那已经是顶层）；拿不到再退回部件句柄 + `GA_ROOT` 往上找。
        实测症状：句柄用错时 `SetWindowPos` **静默失败**（返回 0），
        面板不报错，只是**永远压在微信下面**。

    ⚠️ 拿到句柄后**必须验一次 `IsWindow`**。Qt 在 offscreen 平台下
        `winId()` 返回的是个假句柄（实测恒为 `0x1`），直接喂给 `SetWindowPos`
        是在赌运气。验不过就返回 0，调用点会把它当"拿不到"处理（静默跳过）。
    """
    cand = 0
    try:
        wh = ui.windowHandle()
        if wh is not None:
            cand = int(wh.winId())
    except Exception:
        cand = 0
    if not cand:
        try:
            cand = int(ui.winId())
        except Exception:
            cand = 0
    if not cand:
        return 0
    try:
        user32 = ctypes.windll.user32
        top = user32.GetAncestor(cand, 2)      # GA_ROOT
        if top:
            cand = int(top)
        if not user32.IsWindow(cand):
            return 0
        return cand
    except Exception:
        return 0


def ui_rect(ui):
    """界面窗口的实际几何 (x, y, w, h)。"""
    return int(ui.x()), int(ui.y()), int(ui.width()), int(ui.height())


def raise_to_top(ui) -> bool:
    """把我们自己的窗口提到**普通窗口堆的最上面**（不动 z 序之外任何东西）。

    ⚠️ 这个函数的存在理由是一条实测出来的毛病（2026-09-27，用户报
        「为什么生成建议时窗口会闪两下？」）：

        原来「唤醒」是靠 `QWidget.setWindowFlag(WindowStaysOnTopHint, True)`
        再在 300ms 后设回去。而 Qt 的 `setWindowFlag()` 在窗口**已经可见**时
        **会销毁并重建原生窗口**（官方文档：它会调 `setParent()`，
        部件因此被隐藏，必须再调 `show()`）—— 于是每调一次就闪一下，
        两个来回 = **闪两下**，而且正好发生在你点「生成建议」的瞬间。

    → 现在只改 **z 序**：`SetWindowPos(self, HWND_TOP, 0,0,0,0,
      SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE)`，**不重建窗口、不移动、
      不改大小、不抢焦点**。`SWP_NOACTIVATE` 尤其不能少 ——
      抢焦点的话你正在微信里打的字就跑进面板输入框了。

    ⚠️ 动的是**我们自己的**窗口，没碰微信（红线允许的唯一一种 `SetWindowPos`）。
    """
    hwnd = ui_native_handle(ui)
    if not hwnd:
        return False                       # 拿不到真句柄（如 offscreen）＝静默跳过
    HWND_TOP = 0
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    return set_window_pos(hwnd, HWND_TOP,
                          SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)


#: `SetWindowPos` 的 `hWndInsertAfter` 魔法值（**指针宽度**，不是 int）
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2


def set_window_pos(hwnd: int, insert_after, flags: int) -> bool:
    """`SetWindowPos` 的**唯一入口**（带明确的 `argtypes`）。

    ⚠️⚠️ 2026-10-14 实测踩到的坑（用户报「置顶状态得重启后才有用」的根因之一）：
        **不设 `argtypes` 时，`HWND_*` 这类"指针宽度"的参数会被当成 32 位 int 传**，
        而 `HWND_TOPMOST(-1)` / `HWND_NOTOPMOST(-2)` 是**负数** ——
        x64 调用约定下高 32 位不保证符号扩展，内核看到的是
        `0x00000000FFFFFFFE` 这种**无效句柄**，于是 `SetWindowPos`
        **返回 0（失败）而且不报任何错**。
        症状：运行期"取消置顶 / 加置顶"**毫无反应**。
        ⚠️ 传**真实窗口句柄**时侥幸能用（句柄值本来就小、正数），
           所以这个坑**只在 `HWND_*` 那几个魔法值上暴露** ——
           实测：`SetWindowPos(h, -2, ...)` 返回 0；设了 argtypes 并传
           `wintypes.HWND(-2)` 之后返回 1，`WS_EX_TOPMOST` 正确翻转。
    """
    if not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_uint]
        user32.SetWindowPos.restype = wintypes.BOOL
        return bool(user32.SetWindowPos(
            wintypes.HWND(hwnd), insert_after, 0, 0, 0, 0, flags))
    except Exception:
        return False


def is_topmost(hwnd: int) -> bool:
    """这个窗口现在是不是**置顶**的（`WS_EX_TOPMOST`）。

    ⚠️ 判断必须看**扩展样式**，不能凭 "我们在设置里开着 topmost" ——
        那个样式会在运行期被 Windows 自己改掉（见 `raise_above` 的说明）。
    """
    if not hwnd:
        return False
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                    & WS_EX_TOPMOST)
    except Exception:
        return False


def raise_above(hwnd_self: int, hwnd_ref: int) -> bool:
    """把自己的窗口插到参考窗口的**正上方**。

    ⚠️ 这个 `SetWindowPos` 动的是**我们自己的窗口**，参考窗口只当作插入位置：
        不移动它、不改变大小、不激活、不发送任何输入。
        微信本身一点没被碰到（只读了它的 HWND）。

    `SWP_NOACTIVATE` 是必须的 —— 否则每次微信切到前台都会把焦点抢到面板上，
    你打字就打进面板的输入框里去了。

    ⚠️⚠️ 2026-10-14：**置顶窗口直接跳过**（用户报「保持置顶有时候会出错」）。
        实测（`.tmp/probe_opacity_topmost.py`）：面板开着 `ui.topmost` 时，
        一次 `raise_above()` 之后 `WS_EX_TOPMOST` 从 `True` 变成 **`False`** ——
        `SetWindowPos` 的规则是"把窗口插到**非置顶窗口**旁边会清掉它的置顶样式"，
        而这个函数的 `hWndInsertAfter` 正是微信那个**普通窗口**。
        症状：「总是置顶」开着，一旦微信切到前台（`_sync_zorder` → 这里）
        置顶就没了，切到别的程序时面板又被盖住。
        **「有时候」= 取决于有没有切回过微信** —— 正是这类偶发的典型长相。
        → 置顶窗口**本来就在所有普通窗口之上**，插不插都一样，所以返回 True
        （"已经到位了"），让调用方把它记成"这拍做完了"，别每拍重试。
    """
    if not hwnd_self or not hwnd_ref:
        return False
    if is_topmost(hwnd_self):
        return True
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    return set_window_pos(hwnd_self, hwnd_ref,
                          SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)


def compute_dock(panel_w: int, panel_h: int, wrect, work, side: str = "auto",
                 gap: int = DEFAULT_GAP):
    """算出面板该放在哪。返回 (x, y)；**两边都没地方就返回 None**。

    优先右边（微信默认在屏幕左侧偏中，右边通常有位置），右边放不下再试左边。
    垂直方向与微信窗口顶端对齐，必要时光上下夹一下让它整个留在屏幕里。
    """
    wl, wt, wr, wb = wrect
    gl, gt, gr, gb = work

    # 垂直方向与微信顶端对齐，**上下都要夹住**，否则微信窗口有一部分在屏幕外
    # （`wt` 是负数）时，面板会跟着跑到屏幕外面去（测试抓到过）。
    #
    # ⚠️⚠️ 2026-10-11 用户实测报「跟随微信时微信拖到下面它会被拦着」——
    #     原来是 `min(wt, gb - panel_h)`，那个 `gb - panel_h` 会在
    #     "面板底边顶到屏幕底"时**把面板钉住不再往下**：
    #         屏幕高 1400、微信/面板都高 900 -> `gb - panel_h = 500`
    #         微信顶端拖到 520，面板算出来还是 500 —— **差 20px 跟不上**，
    #         微信越往下差得越多。
    #     ⚠️ 那个夹子本来是"保护面板不被推出屏幕"，**但用户明确选了同步**：
    #         宁可面板底边跟着越出屏幕，也不要它跟丢。
    #         代价（已知并接受）：**面板底部的复制/生成按钮会跟着藏到屏幕外**
    #         （微信越出去多少，就藏多少）。
    #     ⚠️ 所以这里**只剩上边界**：`gt`（不让它跑到可用区顶上）。
    #         别再往回收底部 —— 那正是用户要的那个行为。
    y = max(gt, wt)

    # ⚠️ side 是用户明确指定的偏好，不能"这边放不下就换另一边"。
    #    只有 auto 才允许右→左依次尝试（测试抓到过：配了 right 却跑去了左边）。
    if side == "right":
        order = ["right"]
    elif side == "left":
        order = ["left"]
    else:
        order = ["right", "left"]

    for s in order:
        if s == "right":
            x = wr + gap
            if x + panel_w <= gr:
                return (x, y)
        else:
            x = wl - gap - panel_w
            if x >= gl:
                return (x, y)
    return None


def dock_gap(wrect, work, side: str = "auto", gap: int = DEFAULT_GAP):
    """微信两侧各有**多少像素**可以站面板。返回 (可用宽度, "right"/"left")。

    为什么需要它
    ------------
    实测踩过：用户屏幕上微信宽 1593px、面板要 510px、屏宽 2240px ——
    两边各只剩 289 / 358px，于是 `compute_dock` 老老实实返回 None，
    面板就**完全不跟随**了。虽然"不硬挤"是设计好的降级行为，
    但当这种情况是常态时，等于这个功能白做。

    所以先问"能站多宽"，容不下就把面板**缩窄**，而不是干脆不站。
    """
    wl, wt, wr, wb = wrect
    gl, gt, gr, gb = work
    right = gr - (wr + gap)
    left = (wl - gap) - gl
    if side == "right":
        return max(0, right), "right"
    if side == "left":
        return max(0, left), "left"
    return (max(0, right), "right") if right >= left else (max(0, left), "left")


class WindowFollower:
    """定时把悬浮窗摆到微信旁边，高度与微信对齐。

    ⚠️ 必须跑在 **UI 线程**：它调 `ui.set_docked()` 改 tk 窗口。这里用
    `root.after` 做定时器，不用另起线程，天然满足这个约束。
    """

    def __init__(self, ui, cfg, logger: Optional[Callable[[str], None]] = None):
        self.ui = ui
        self.cfg = cfg
        self._log = logger or (lambda m: None)
        self.interval = float(cfg.get("ui.follow_interval", 0.4) or 0.4)
        self.side = str(cfg.get("ui.dock_side", "auto") or "auto")
        self.follow_minimize = bool(cfg.get("ui.follow_minimize", True))
        self._hwnd = 0
        self._job = None
        self._hid_by_us = False
        self._lost_at = 0.0
        self._last = None            # 上一次应用的位置尺寸，用来去重
        #: 用户手动拖过面板时，**记下那一刻微信窗口的矩形**。
        #: 之后只要微信的矩形跟它不一样（挪了 / 缩放了），就判定"这次拖动过期"，
        #: 恢复贴边跟随。见 `_apply()` 里那段说明。
        self._moved_ref = None
        #: `float`（固定悬浮）模式的两块状态：
        #:   `_float_off`  = 面板相对微信**左上角**的偏移 (dx, dy)，用户拖一次就重定一次；
        #:   `_float_last` = 上一次真正下发过的位置，用来去重（值没变就别动窗口）。
        #: ⚠️ 用**独立**的去重字段，不共用 `_last` —— `_last` 是贴边那条路的几何记录，
        #:    `_verify()` 靠它做自愈；而 float 下窗口位置本来就随时可能被用户改掉，
        #:    共用会让 `_verify()` 每隔一秒把用户刚放好的位置判成"被外部改过"。
        self._float_off = None
        self._float_last = None
        self._mode = None            # 当前 attach 模式，用来发现"模式切换了"
        self._warned_pair = None     # `_verify()` 已经报过的那一对（避免刷屏）
        self._no_room = False        # 已经提示过"两侧没空间"了吗
        self._last_note = ""         # 上一次记过的宽度提示，避免刷屏
        self._fg_was = False         # 上一拍微信是不是前台
        self._z_done = False         # 这一轮层级是否已经摆好
        self.running = False

    # ------------------------------------------------------------ 生命周期

    def use_config(self, cfg):
        """设置窗口保存后换了**新的配置对象**，跟随器要跟着换。

        ⚠️ 不换的话就是「**改了设置完全没反应**」：
            跟随器每一拍读的是**构造时传进来那个对象**，而
            `run_watch.on_config_changed` 写的是 `cfg = new_cfg`
            —— 那是**换了个新对象**，不是原地改旧对象。
            症状：设置里把「跟随微信」从贴边改成固定悬浮，跟随行为一点不变，
            而且不报任何错。

        ⚠️ `side` / `follow_minimize` / `interval` 都是**构造时缓存**成实例属性的
            （`side` 每拍都要用、`interval` 每拍都要读），所以这里要一起刷新。
        """
        self.cfg = cfg
        self.side = str(cfg.get("ui.dock_side", "auto") or "auto")
        self.follow_minimize = bool(cfg.get("ui.follow_minimize", True))
        try:
            self.interval = float(cfg.get("ui.follow_interval", 0.4) or 0.4)
        except Exception:
            pass

    def start(self):
        if self.running:
            return
        self.running = True
        self._tick()

    def stop(self):
        self.running = False
        if self._job is not None:
            # job 一直是 QTimer 对象，`stop()` 一句就够
            try:
                self._job.stop()
            except Exception:
                pass
            self._job = None

    def _schedule(self):
        if not self.running:
            return
        try:
            from PySide6 import QtCore

            # ⚠️ **必须把 QTimer 存到实例上**（`self._job`）：写成局部变量会被
            #    GC 掉，定时器只响一次就没了 —— 症状是"面板只跟一帧然后不动"。
            timer = QtCore.QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(self._tick)
            timer.start(int(self.interval * 1000))
            self._job = timer
        except Exception:
            self._job = None

    # ------------------------------------------------------------ 每一拍

    def _tick(self):
        self._job = None
        if not self.running:
            return
        try:
            self._apply()
            self._verify()
        except Exception as e:
            self._log("跟随微信失败: %s: %s" % (type(e).__name__, e))
        self._schedule()

    # 每隔多少拍核对一次窗口实际几何（16ms × 60 ≈ 1 秒）
    VERIFY_EVERY = 60

    def _verify(self):
        """定期核对"窗口实际几何"与"我记着的值"是否一致，不一致就强制重新下发。

        防的是**状态卡住**：
        `_set()` 会拿 `_last` 去去重（值没变就不动窗口），这本来是对的 ——
        但如果窗口因为**别的原因**被改过（主题重建、折叠展开、用户拖过），
        而 `_last` 还记着旧值，那么去重就会让它**永远不再纠正**，
        面板就一直停在错误的位置和高度。

        实测踩过：**程序在微信最小化时启动**，之后即便把微信调出来，
        面板也一直停在启动那一瞬间的位置（高 650 而微信是 896）。

        自愈很简单：发现实际值和记录不符就把记录清掉，下一拍自然会重新摆。

        ⚠️ **但同一对值反复出现时不要再刷屏**（2026-09-27 加）。
            真出现"窗口就是给不了那个尺寸"（布局最小宽度顶着）的话，上面这套
            会变成**每秒一次的无限循环**：清记录 → 重新下发 → 还是不符 → 再清，
            而每次重摆都要整棵布局重排（那是最贵的那条路）。
            用户看到的就是日志里同一条消息无限重复（原话：
            「没有修改但是会重复出现」）。
            → 同一对（记录值, 实际值）**只记一次**，之后照样继续自愈但不再打日志。
        """
        if self._last is None:
            return
        self._verify_count = getattr(self, "_verify_count", 0) + 1
        if self._verify_count < self.VERIFY_EVERY:
            return
        self._verify_count = 0
        try:
            actual = ui_rect(self.ui)
        except Exception:
            return
        if actual == self._last:
            self._warned_pair = None      # 对上了，下次不符可以再报一次
            return
        pair = (self._last, actual)
        if pair != getattr(self, "_warned_pair", None):
            self._warned_pair = pair
            self._log("面板几何被外部改过（%s -> %s），重新跟随"
                      % (self._last, actual))
        self._last = None

    def _apply(self):
        cfg = self.cfg
        mode = str(cfg.get("ui.attach", ATTACH_DOCK) or ATTACH_DOCK)

        # 模式切换：两条路用的状态不一样，混着用会互相拆台
        # （比如 float 的"位置是用户定的"和 dock 的"位置是算出来的"）。
        if mode != self._mode:
            self._log("跟随模式: %s -> %s" % (self._mode, mode))
            self._mode = mode
            self._float_off = None
            self._float_last = None
            self._moved_ref = None
            # 清掉上一条路留下的几何记录，并把高度控制权交还界面
            # （`off` 就靠这一下收尾；之后每拍直接返回，不做任何事）。
            self._undock()

        if mode == ATTACH_OFF:
            return

        if not self._hwnd or not win32gui.IsWindow(self._hwnd):
            self._hwnd = find_wechat_window(cfg)

        wrect = window_box(self._hwnd)
        if wrect is None:
            # 微信没了 / 关了。**不要把面板藏起来** —— 用户可能还要复制刚才的回复，
            # 而且藏起来之后这个窗口没有任务栏条目，很难找回来。
            #
            # ⚠️ 但**不能忘掉 `_hid_by_us`**：微信最小化期间句柄可能失效
            #    （窗口被重建、或缩到托盘时 `IsWindowVisible` 为假导致
            #    `find_wechat_window` 返回 0），于是走到这里。此时面板还是我们
            #    自己藏起来的状态，一旦清了标记就**再也没人负责把它显示回来** ——
            #    用户看到的就是「微信最小化再打开，悬浮窗没跟着出现」。
            #    所以只清 `_hwnd`，标记留给下面「微信恢复」那段消费。
            self._hwnd = 0
            self._lost_at = self._lost_at or time.time()
            self._undock()
            self._restore_if_hid()
            return
        self._lost_at = 0.0

        # 最小化：跟着藏起来；恢复：跟着露出来。
        # 只在自己藏过的情况下才主动显示，避免跟用户手动「隐藏」打架。
        if is_minimized(self._hwnd):
            if self.follow_minimize and self.ui.is_visible():
                self.ui.hide()
                self._hid_by_us = True
            return
        self._restore_if_hid()

        # 层级：微信在前台时把面板插到**它正上方**。
        #
        # 不这么做的话，微信一升到前面面板就被盖住了（实测反馈"打开微信它就在底层"）。
        # 也**不能**简单地把面板设成总在最前 —— 那样你切到别的程序时它还是压在
        # 最上面，很烦。所以只在"微信是前台窗口"时才抬，其余时候不管。
        self._sync_zorder()

        # ---- 固定悬浮（`ui.attach: float`）：位置**听用户的**，但跟着微信一起平移
        #
        # 跟贴边是两条完全不同的路，所以在用户拖动那段**之前**就分叉。
        if mode == ATTACH_FLOAT:
            self._float_apply(wrect)
            return

        # 用户自己拖过窗口 -> 位置暂时听他的。
        #
        # ⚠️ **但只在"微信窗口没动"的时候听他的。**（2026-09-27 用户反馈后改）
        #
        # 原来这里是无条件 `_apply_height()` 然后 return，写的是"这一轮不跟他抢位置"。
        # 用户实测反馈了两件事，其实是**这同一个写法的两个侧面**：
        #   ① 「拖动后拖动微信窗口，他可能宽度会错误变化」
        #      —— `_apply_height()` 的 docstring 说"只把高度对齐微信"，
        #         但它内部是 `_panel_size()` + `_set(cur[0], cur[1], w, h)`，
        #         **把宽度一起写回去了**。于是「位置冻结、宽高还在变」：
        #         微信一挪，可用空隙变了 → 面板在原地被拉宽/压窄。
        #   ② 「拖动微信上边框高度，他反而是下边框变化」
        #      —— 拖微信上边框时微信**上边变、下边不动**，而这里位置被冻住，
        #         只有高度跟着变 → 面板的下边框往下跑。
        # 合起来用户看到的就是「跟随失效」。
        #
        # → 现在的规则（用户选的）：**拖动只影响你放下的那一下。**
        #   微信窗口自己一动（挪位置 / 改大小 / 上边框下边框随便哪个），
        #   这次拖动就算"过期"，立刻恢复贴边跟随。
        if getattr(self.ui, "_user_moved", False):
            if self._moved_ref is None:
                self._moved_ref = wrect        # 记住"用户拖完时微信在哪"
            if wrect == self._moved_ref:
                # 微信一点没动 -> **什么都别碰**（连高度都别写）。
                # ⚠️ 别在这里调 `_apply_height()`：它会把宽度一起覆盖掉（见上）。
                #    而且这一拍真的无事可做 —— 微信没动就意味着高度也没变。
                return
            self.ui._user_moved = False
            self._log("微信窗口动了，面板恢复跟随（之前的位置是你手动拖的）")

        self._moved_ref = None
        self._apply_pos(wrect)

    def _restore_if_hid(self):
        """**我们**之前为了跟微信最小化而藏起来的面板，现在把它显示回来。

        单独抽成方法是因为它有**两个**调用点：正常的「微信恢复」那一拍，
        以及「微信最小化期间句柄失效、走了 `wrect is None` 那条早退」之后。

        ⚠️ 判据是 `_hid_by_us`（我们藏的），**不是** `not ui.is_visible()`
        （窗口是藏着的）。后者会把用户自己点的「隐藏」也一起掀开 ——
        `test_follow.py` 有一条专门守着这个区别。
        """
        if not self._hid_by_us:
            return
        self._hid_by_us = False
        self.ui.show()

    def _sync_zorder(self):
        """微信在前台时，把面板插到它正上方；离开前台就什么都不做。"""
        fg = foreground_is(self._hwnd)
        if not fg:
            self._fg_was = False
            return
        # 只在"刚切到微信前台"、"或者刚摆好位置"时抬一次，避免每拍都调
        if self._fg_was and self._z_done:
            return
        hwnd_self = ui_native_handle(self.ui)
        if raise_above(hwnd_self, self._hwnd):
            self._fg_was = True
            self._z_done = True

    def _panel_size(self, wrect):
        """算面板的目标宽高。

        折叠态：**一个小方块**，贴在微信旁边/顶端，不跟微信高度。
        展开态：宽度由界面决定（`ui.desired_width()`），空间不够时缩窄；
                高度跟随微信窗口，但**不低于固定内容需要的高度**，
                否则输入框和「生成建议」会被压没。
        """
        ui = self.ui
        st = ui.style
        work = work_area()

        if getattr(ui, "collapsed", False):
            size = int(getattr(ui, "desired_width", lambda: st.px(56))())
            return size, size

        if hasattr(ui, "desired_width"):
            w = int(ui.desired_width())
        else:
            w = st.px(int(self.cfg.get("ui.width", 340) or 340))

        avail, _ = dock_gap(wrect, work, self.side, st.px(DEFAULT_GAP))
        min_w = st.px(int(self.cfg.get("ui.min_width", 200) or 200))
        if avail < min_w:
            self._warn_no_room()
            return None
        if avail < w:
            w = max(min_w, int(avail))
            self._log_once("空间有限，面板宽度自动缩到 %dpx" % w)

        fixed = int(self.cfg.get("ui.height", 0) or 0)
        if fixed > 0:
            h = st.px(fixed)
        else:
            wl, wt, wr, wb = wrect
            gl, gt, gr, gb = work
            h = min(wb - wt, gb - gt)

        floor = st.px(260)
        try:
            # ui.min_height() 带缓存，不会每秒重排 20 次
            floor = max(floor, int(ui.min_height()))
        except Exception:
            pass
        return w, max(floor, h)

    def _warn_no_room(self):
        """两侧空隙连最小宽度都放不下 —— 告诉用户**为什么**没跟随。

        不说的话用户只会觉得"这个跟随坏了"（实测反馈过）。

        ⚠️ 2026-10-14：试过"退化成固定悬浮"那一版（提示改成"已改成固定悬浮"），
            用户说 **「那旧行为就没问题了，暂不跟随」** → 行为和提示都撤回了。
        """
        if getattr(self, "_no_room", False):
            return
        self._no_room = True
        self._log("微信窗口两侧空间不足，面板暂时不跟随。"
                  "把微信拖窄一点、或者把 ui.width / ui.min_width 调小即可并排。")
        try:
            # ⚠️ 2026-10-13 精简（用户要求"悬浮窗下面的提示精简一点"）：
            #    详细的排查办法留在上面那行**日志**里（`_log`），
            #    状态行只说结论 + 用户能做的一件事。
            self.ui.post_status("空间不够，暂不跟随（把微信拖窄点）")
        except Exception:
            pass

    def _log_once(self, msg):
        if getattr(self, "_last_note", "") != msg:
            self._last_note = msg
            self._log(msg)

    def _apply_pos(self, wrect):
        ui, st = self.ui, self.ui.style
        size = self._panel_size(wrect)
        if size is None:
            return
        w, h = size
        pos = compute_dock(w, h, wrect, work_area(), self.side, st.px(DEFAULT_GAP))
        if pos is None:
            # 两边都没地方（微信多半最大化了）。不硬挤，退回原来的位置。
            #
            # ⚠️ 2026-10-14 试过一版"退化成固定悬浮"（位置听用户的、跟着微信平移），
            #    用户看过之后说 **「那旧行为就没问题了，暂不跟随」** → 撤回了。
            #    所以这里就保持 `_apply_height()`：高度对齐微信、位置一个数不写。
            #    别再自作主张改成 float —— 想改先问。
            self._apply_height(wrect)
            return
        self._no_room = False
        self._set(pos[0], pos[1], w, h)

    def _apply_height(self, wrect):
        """只把高度对齐微信，x 不动（用户拖过位置、或没地方站时用）。"""
        ui, st = self.ui, self.ui.style
        size = self._panel_size(wrect)
        if size is None:
            return
        w, h = size
        cur = ui_rect(ui)
        self._set(cur[0], cur[1], w, h)

    def _set(self, x, y, w, h):
        """应用位置尺寸 —— **只有变了才动窗口**。

        这是"不闪"的关键：每一帧都无条件调 `geometry()` 会让 Tk 反复重排
        并重绘所有子控件，肉眼就是一片抖（实测反馈过）。

        另外还带**死区**：差不到 `DEADBAND` 像素就当作没变。
        微信窗口拖动时 rect 会有 1~2px 的抖动，跟着它跑就是来回伸缩。
        """
        x, y, w, h = int(x), int(y), int(w), int(h)
        last = self._last
        if last is not None:
            # 位置用 POS_DEADBAND（0 = 差 1px 也跟，移动才跟手）；
            # 尺寸用 SIZE_DEADBAND（贵，给点容差吃掉抖动）
            if (abs(x - last[0]) <= POS_DEADBAND and abs(y - last[1]) <= POS_DEADBAND
                    and abs(w - last[2]) < SIZE_DEADBAND
                    and abs(h - last[3]) < SIZE_DEADBAND):
                return
        self._last = (x, y, w, h)
        # 位置一变就把层级标记清掉，下一拍重新把面板插到微信正上方 ——
        # 否则拖动微信的过程中面板会被压到后面去
        self._z_done = False
        self.ui.set_docked(x, y, w, h)

    def _float_apply(self, wrect):
        """固定悬浮（`ui.attach: float`）：记住**你放的那个相对位置**，跟微信一起走。

        用户原话（2026-09-27）：「固定悬浮模式就记住你放的那个相对位置，
        以后跟着微信一起移动」。

        跟 `dock` 的根本区别是**位置从哪儿来**：
          · `dock`  —— 位置是**算出来的**（站在微信哪一侧、紧贴它旁边），
                       所以用户拖过之后会被拉回边上（微信一动就拉，见上面那段）。
          · `float` —— 位置是**你定的**：只记下"面板相对微信左上角的偏移"，
                       微信怎么动，面板就按同一个偏移平移过去。

        高度**不跟微信**（界面按内容自适应）—— 要跟高度就用 `dock`。
        这正是"固定悬浮"的意思：窗口大小是它自己的，只是跟着微信挪。

        ⚠️ 用户又拖了一下 = 他要改这个相对位置，所以拿**当前位置**重定基准。
           这是 float 和 dock 在"用户拖过之后"的另一个区别：
           dock 是"微信一动就把你拉回边上"，float 是"你放哪儿就记住哪儿"。
        ⚠️ 用户的新位置会被 `move_clamped` 夹在桌面可用区里，所以基准一定是合法的。
        """
        if self._float_off is None or getattr(self.ui, "_user_moved", False):
            cur = ui_rect(self.ui)
            self._float_off = (cur[0] - wrect[0], cur[1] - wrect[1])
            self.ui._user_moved = False
            # 基准就是"面板现在待的地方"，所以把去重值也设成它 ——
            # 否则下面会立刻下一次"挪到它已经在的位置"，白白多调一次 move。
            self._float_last = (cur[0], cur[1])
        dx, dy = self._float_off
        x, y = wrect[0] + dx, wrect[1] + dy
        if self._float_last == (x, y):
            return
        self._float_last = (x, y)
        self.ui.move_only(x, y)

    def _undock(self):
        """不跟随了，把控制权还给界面自己（退回按内容自适应高度）。"""
        if self._last is not None:
            self._last = None
        self.ui.clear_docked()
