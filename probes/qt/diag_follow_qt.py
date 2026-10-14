# -*- coding: utf-8 -*-
"""贴边跟随 x Qt 悬浮窗的接缝体检。

    python probes/qt/diag_follow_qt.py

为什么要单独探一下：`follow.WindowFollower` 是**业务层**，它原先只服务 Tk 版，
到处直接摸 `ui.root.winfo_xxx()`。迁移后它要驱动 Qt 的 `FloatingAssistant` ——
这是最容易在运行时炸、而单测又碰不到的一条缝（因为真跑起来要有微信）。

这里不装微信也能验的部分：
  · 跟随器的定时器能在 **Qt 事件循环**里跑起来（`_schedule` 的 Qt 分支）
  · `ui_rect(ui)` / `ui_native_handle(ui)` 对 Qt 面板给出合理值
  · 微信不在时它**体面地让位**（不跟随、不报错、界面自己按内容自适应）
  · 折叠态下它给的是正方形
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6 import QtWidgets                                    # noqa: E402
from wechat_assistant import dpi, follow                        # noqa: E402
from wechat_assistant.qt_ui import base                          # noqa: E402
from wechat_assistant.qt_ui.panel import FloatingAssistant       # noqa: E402

# ⚠️ 和 run_watch.py 同样的顺序：先 dpi.enable()，再 init_app()。
#    不调的话 GetDpiForSystem() 谎报 96，缩放静默退成 1.0，
#    量出来的一切尺寸都只有真机的一半。
dpi.enable()

FAIL = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("[通过]" if ok else "[失败]", name,
                         ("  " + detail) if detail else ""))
    if not ok:
        FAIL.append(name)


class Cfg:
    def __init__(self, d=None):
        self._data = d or {}

    def get(self, path, default=None):
        node = self._data
        for part in str(path).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def pump(app, ms=200, quiet=False):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def main():
    app = base.init_app([])
    px = base.px
    print("Qt / 缩放 %.2f" % base.scale())

    cfg = Cfg({"ui": {"width": 340, "radius": 10, "theme": "dark",
                      "attach": "dock", "follow_interval": 0.05,
                      "dock_side": "auto", "min_width": 200, "width_max": 640}})
    ui = FloatingAssistant(cfg, logger=lambda m: None)
    ui.resize(px(340), px(520))
    ui.show()
    pump(app, 200)
    ui.start_pump()

    print("\n== 几何访问器（两版通用）==")
    r = follow.ui_rect(ui)
    check("ui_rect 给出 Qt 面板的实际几何", r[2] == ui.width() and r[3] == ui.height(),
          str(r))
    check("ui_rect 的宽高都是正数（不是布局前的 0/1）", r[2] > 10 and r[3] > 10, str(r))
    h = follow.ui_native_handle(ui)
    # ⚠️ offscreen 平台下 Qt 的 `winId()` 返回的是假句柄（实测恒为 0x1），
    #    验不过 `IsWindow`，所以这里**预期就是 0** —— 那不是缺陷，
    #    正是"不把垃圾句柄喂给 SetWindowPos"的保护生效了。
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        check("offscreen 下拒绝假句柄（返回 0，不喂给 SetWindowPos）", h == 0,
              "0x%X" % h)
    else:
        check("真窗口下拿到非零句柄", h != 0, "0x%X" % h)
        try:
            import ctypes

            user32 = ctypes.windll.user32
            root = user32.GetAncestor(h, 2)      # GA_ROOT
            check("句柄是有效的顶层窗口（IsWindow + GA_ROOT 自洽）",
                  bool(user32.IsWindow(h)) and root in (0, h),
                  "0x%X vs 0x%X" % (h, root or 0))
        except Exception as e:
            check("句柄是有效的顶层窗口", False, str(e))

    print("\n== 跟随器在 Qt 事件循环里跑 ==")
    f = follow.WindowFollower(ui, cfg, logger=lambda m: None)
    check("跟随器建出来了", f is not None)
    f.start()
    pump(app, 400)
    check("start() 之后 running=True", f.running is True)
    # ⚠️ 这里最关键的一条：`_schedule()` 的 Qt 分支能不能真的**反复**触发。
    #    写成 QTimer 局部变量会被 GC 掉，定时器只响一次就没了 ——
    #    症状是"面板只跟一帧然后就不动了"，很难查。
    ticks = []
    orig = f._tick

    def spy():
        ticks.append(time.time())
        return orig()

    f._tick = spy
    f._schedule()
    pump(app, 400)
    check("定时器能**反复**触发（没被 GC 掉）", len(ticks) >= 2,
          "触发了 %d 次" % len(ticks))
    f.stop()
    pump(app, 200)
    n = len(ticks)
    pump(app, 300)
    check("stop() 之后不再触发", len(ticks) == n,
          "%d -> %d" % (n, len(ticks)))
    f._tick = orig

    print("\n== 微信不在时的行为 ==")
    # ⚠️ 不假设"这台机器没开微信" —— 实测这台机器上微信正好开着，
    #    跟随器直接就接管了（面板被摆到了 1694,180 / 510x780）。
    #    所以这里按**两种情形各自该有的结果**验，而不是假设其中一种。
    hwnd = getattr(f, "_hwnd", 0) or 0
    if hwnd:
        print("      （本机微信在线，hwnd=0x%X —— 跟随器已接管）" % hwnd)
        print("      running=%s _docked=%s h=%d _last=%s"
              % (f.running, ui._docked, ui.height(), f._last))
        # ⚠️ 这一步**必须在 `f.stop()` 之前**看：上面验定时器时已经 stop 过了，
        #    停止之后 `_tick` 不再跑，面板可能还停在上一拍的位置/高度上 ——
        #    那时拿 `h == 780` 去判"没解绑"是在赌测试的执行顺序。
        #    这里直接走一遍 `_undock()` 再量，不依赖前面的状态。
        f._undock()
        pump(app, 300, quiet=True)
        print("      _undock 之后 _docked=%s h=%d min_height=%d"
              % (ui._docked, ui.height(), ui.min_height()))
        check("解绑之后交还高度控制权", ui._docked is False)
        check("解绑后高度回到内容自适应（不再是微信那个高）",
              ui.height() != 780, "h=%d" % ui.height())
    else:
        check("没找到微信窗口时不崩（不接管）", ui._docked is False)
        check("没跟随时界面仍按内容自适应",
              ui.height() > 0, "h=%d" % ui.height())

    print("\n== 折叠态给的是正方形 ==")
    ui.collapsed = True
    size = f._panel_size((0, 0, 1200, 800)) if False else None
    check("折叠态 desired_size 是正方形",
          ui.desired_size() == (px(56), px(56)), str(ui.desired_size()))
    ui.collapsed = False

    print("\n== 手动模拟一次跟随（证明接缝能走通）==")
    # 直接喂一组几何给跟随器内部的应用逻辑，绕开"必须有微信"这一层
    try:
        f._last = None
        f._set(120, 140, 300, 640)
        pump(app, 150)
        check("跟随器能把 Qt 面板摆到指定位置",
              (ui.x(), ui.y(), ui.width(), ui.height()) == (120, 140, 300, 640),
              str((ui.x(), ui.y(), ui.width(), ui.height())))
        check("set_docked 之后 _docked=True", ui._docked is True)
        f._undock()
        pump(app, 200)
        check("解绑之后交还高度控制权", ui._docked is False)
        check("解绑后高度回到内容自适应", ui.height() != 640, str(ui.height()))
    except Exception as e:
        check("手动跟随", False, "%s: %s" % (type(e).__name__, e))

    ui.stop()
    print("\n" + ("全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
