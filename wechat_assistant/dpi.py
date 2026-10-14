# -*- coding: utf-8 -*-
"""让进程具备 DPI 感知 —— 一个必须在**创建任何窗口之前**调用的开关。

不设它会怎样（实测）
--------------------
本机是 2240x1400 物理 / 1493x933 逻辑（150% 缩放）。进程若**没有** DPI 感知：

    GetSystemMetrics(0)  -> 1493      （Windows 给的"虚拟化"后的值）
    GetDpiForSystem()    -> 96        （谎报 96）
    界面框架             -> 按 96 DPI 排版

然后 Windows 会把整个窗口**按 1.5 倍位图拉伸**再显示 —— 于是**文字明显发虚**。
（用户实测反馈："文字的分辨率看起来很模糊"，就是这个。）

更麻烦的是：这个属性原来是被**意外**获得的
------------------------------------------
`uiautomation` 这个库在被 import 时会自己调 `SetProcessDpiAwareness`，
而项目里的它是在 `uia_worker.py` 的**函数内部**懒加载的（跑在 worker 线程里）。
于是实际时序变成：

    1. 进程启动，DPI 不感知
    2. **悬浮窗在这时创建** -> 按 96 DPI 排版
    3. 之后 worker 第一次读 UIA -> import uiautomation -> 进程中途变成 DPI 感知
    4. 第 2 步那个窗口被按 1.5 倍拉伸 -> 模糊

「界面清不清晰取决于某个第三方库的懒加载时机」是绝对不能接受的依赖，
所以在这里显式设一次，让时序变成确定的。

为什么不放到 reader.py 里
-------------------------
因为 `reader` 本身也是懒加载 uiautomation 的，而且**探测脚本 / 预览脚本 /
测试**并不都会导入 `reader`。放在这个独立模块里，谁需要谁显式调用，
一眼就能看出"这个进程是不是 DPI 感知"。

⚠️ 必须在**创建任何窗口之前**调用（Qt 这边就是 `init_app()` / 建 QWidget 之前）。
窗口建好之后再改，Windows 不保证正确缩放（实测就是这个症状：窗口模糊）。
⚠️ 而且优先设的是 **V2**，理由见 `enable()`。
"""

import ctypes
import sys

# PROCESS_DPI_AWARENESS 的取值（shcore.dll）
_UNAWARE = 0
_SYSTEM_AWARE = 1
_PER_MONITOR_AWARE = 2

# DPI_AWARENESS_CONTEXT 的取值（user32.dll，Win10 1703+）
# ⚠️ `-4` 是 PER_MONITOR_AWARE_V2。**优先用它** —— 见 `enable()` 里的说明。
_PER_MONITOR_V2 = -4

_state = {"done": False, "mode": None}


def enable(verbose: bool = False) -> str:
    """让本进程 DPI 感知。**幂等**，可以放心重复调用。

    优先「每显示器感知 V2」，拿不到再退回老 API（每显示器 / 系统感知）。
    返回实际生效的模式名。

    ⚠️ **为什么把 V2 排在最前面**：进程的 DPI 感知方式**只能设一次**，
       设完就锁死。老 API 设的是 V1（`PROCESS_PER_MONITOR_DPI_AWARE`），
       而 Qt 6 一启动就要设 V2（`DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2`）——
       V1 已经锁住了，Qt 那次调用就拿到 `E_ACCESSDENIED`，并在 stderr 打一句
       `SetProcessDpiAwarenessContext() failed: 拒绝访问`。
       功能上不出错（V1 也是每显示器感知），但**每次启动都刷一条红字警告**，
       而人会对熟悉的警告脱敏 —— 真出问题那次就看不见了。
       先设 V2 之后 Qt 那次调用变成"已经是了"，警告消失。

    调用失败**不算错误** —— 某些环境（旧系统、已经是别的模式）会失败，
    此时程序照常运行，只是界面可能不如预期清晰。
    """
    if _state["done"]:
        return _state["mode"] or "already"

    mode = "none"
    try:
        # ① 每显示器感知 V2（Qt 6 自己的默认，先设掉它就不再有警告）
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(_PER_MONITOR_V2)):
            mode = "per-monitor-v2"
    except Exception:
        pass

    if mode == "none":
        # 已经设成每显示器感知了（比如 Qt 先启动、自己设过）——
        # 报告真相，别谎报成"设置失败"。
        # ⚠️ 不用 `AreDpiAwarenessContextsEqual` 去比 V1/V2：那个 API 要传
        #    `DPI_AWARENESS_CONTEXT` 句柄，而这里拿到的是**伪句柄**，
        #    直接比数值会误判。`GetAwarenessFromDpiAwarenessContext` 返回的是
        #    枚举值（2 = 每显示器），V1/V2 都是 2 —— 对我们够用：
        #    要区分的是"感知了没有"，不是"哪一版"。
        if _awareness_of(_current_context()) == _PER_MONITOR_AWARE:
            mode = "already-per-monitor"

    if mode == "none":
        try:
            hr = ctypes.windll.shcore.SetProcessDpiAwareness(_PER_MONITOR_AWARE)
            if hr == 0:
                mode = "per-monitor"
            else:
                # 0x80070005 = E_ACCESSDENIED，通常是已经设过了
                mode = "already-set(0x%08X)" % (hr & 0xFFFFFFFF)
        except Exception:
            try:
                if ctypes.windll.user32.SetProcessDPIAware():
                    mode = "system"
                else:
                    mode = "already-set(system)"
            except Exception as e:
                mode = "failed(%s)" % type(e).__name__

    _state["done"] = True
    _state["mode"] = mode
    if verbose:
        sys.stderr.write("DPI 感知: %s\n" % mode)
    return mode


def _current_context():
    """当前线程的 DPI_AWARENESS_CONTEXT 伪句柄（拿不到返回 None）。**只读。**"""
    try:
        return ctypes.windll.user32.GetThreadDpiAwarenessContext()
    except Exception:
        return None


def _awareness_of(ctx):
    """把 DPI_AWARENESS_CONTEXT 解成枚举值（0/1/2；解不出返回 -1）。**只读。**"""
    if not ctx:
        return -1
    try:
        return int(ctypes.windll.user32.GetAwarenessFromDpiAwarenessContext(ctx))
    except Exception:
        return -1


def state() -> str:
    """当前记录的状态（不触发设置）。"""
    return _state["mode"] or ("done" if _state["done"] else "not-set")


def is_aware() -> bool:
    """**实测**当前进程是否真的 DPI 感知（不看记录，直接问系统）。

    判据：拿实屏 DPI。未感知时 Windows 会谎报 96。
    """
    try:
        dc = ctypes.windll.user32.GetDC(0)
        try:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)   # LOGPIXELSX
        finally:
            ctypes.windll.user32.ReleaseDC(0, dc)
        return dpi > 96
    except Exception:
        return False


def current_dpi() -> int:
    """实测实屏 DPI（未感知时会是假的 96）。"""
    try:
        dc = ctypes.windll.user32.GetDC(0)
        try:
            return int(ctypes.windll.gdi32.GetDeviceCaps(dc, 88))
        finally:
            ctypes.windll.user32.ReleaseDC(0, dc)
    except Exception:
        return 96
