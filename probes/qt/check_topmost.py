# -*- coding: utf-8 -*-
"""「总是置顶」生效条件体检（**真窗口 + 真 Win32 属性**）。

跑法：`python probes\\qt\\check_topmost.py`

为什么要有它
------------
「总是置顶」的语义是用户定的（2026-10-14）：**只有在「跟随微信 = 不跟随」时才置顶**。
跟随中由 `follow._sync_zorder()` 在微信切到前台那一刻把面板抬上去，
常年置顶会让你切到别的程序时它还压在最上面（很烦）。

这条语义有三个地方容易静默失效，**都只能靠真的量 `WS_EX_TOPMOST`** 才看得出来：
  ① 生效条件读了两份配置（`ui.topmost` + `ui.attach`），漏了后者 = 跟随中也置顶；
  ② 运行期切换走 `SetWindowPos(HWND_TOPMOST/NOTOPMOST)` ——
     ⚠️ **不设 argtypes 时这两个魔法值会被当 32 位 int 传**（它们是负数），
     内核收到无效句柄 → `SetWindowPos` **返回 0 且不报错** → "切了没反应"；
  ③ 运行期**不能**用 `setWindowFlag()`（窗口可见时会销毁重建原生窗口 = 闪一下）。

⚠️ 必须在**真实窗口平台**上跑（离屏平台没有真正的 `WS_EX_TOPMOST`）。
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from wechat_assistant import dpi                                    # noqa: E402
from wechat_assistant.qt_ui import base                             # noqa: E402
from wechat_assistant.qt_ui.panel import FloatingAssistant           # noqa: E402

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
user32 = ctypes.windll.user32


def is_topmost(hwnd):
    return bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)


class Cfg:
    def __init__(self, d):
        self.d = d

    def get(self, k, default=None):
        cur = self.d
        for p in k.split("."):
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur


class Store:
    def __init__(self, cfg):
        self._cfg = cfg

    def load(self):
        return self._cfg

    def get(self, k, d=None):
        return self._cfg.get(k, d)


def make(attach, topmost):
    cfg = Cfg({"ui": {"width": 340, "theme": "dark", "attach": attach,
                      "topmost": topmost, "opacity": 1.0},
               "wechat": {"window_title": "微信"}})
    ui = FloatingAssistant(cfg, logger=lambda m: None)
    ui.show()
    return ui, cfg


def main():
    app = base.init_app([])
    fails = []

    print("=" * 72)
    print("「总是置顶」= 只在「不跟随」时置顶（真窗口量 WS_EX_TOPMOST）")
    print("-" * 72)
    # (attach, topmost) -> 期望
    expect = [
        ("dock", False, False),
        ("dock", True, False),        # ⚠️ 跟随中**自动不生效**
        ("float", True, False),       # 同上
        ("off", False, False),
        ("off", True, True),          # 只有这一格真的置顶
    ]
    for attach, topmost, want in expect:
        ui, _ = make(attach, topmost)
        for _ in range(20):
            app.processEvents()
        got = is_topmost(int(ui.winId()))
        ok = got == want
        print("  [%s] attach=%-5s topmost=%-5s -> %-5s（期望 %-5s）"
              % ("通过" if ok else "失败", attach, topmost, got, want))
        if not ok:
            fails.append(("真值表", attach, topmost, got, want))
        ui.close()

    print("-" * 72)
    print("运行期切换**立刻生效**（用户报过「置顶状态得重启后才有用」）")
    ui, _ = make("off", True)
    for _ in range(20):
        app.processEvents()
    before = is_topmost(int(ui.winId()))
    ui._settings_store = Store(Cfg({"ui": {"width": 340, "theme": "dark",
                                          "attach": "dock", "topmost": True,
                                          "opacity": 1.0}}))
    ui._on_settings_saved()
    for _ in range(20):
        app.processEvents()
    after = is_topmost(int(ui.winId()))
    ok = before and not after
    print("  [%s] 「不跟随+置顶」改成「贴边跟随」：%s -> %s（期望 True -> False）"
          % ("通过" if ok else "失败", before, after))
    if not ok:
        fails.append(("运行期 un-topmost", before, after))

    ui._settings_store = Store(Cfg({"ui": {"width": 340, "theme": "dark",
                                          "attach": "off", "topmost": True,
                                          "opacity": 1.0}}))
    ui._on_settings_saved()
    for _ in range(20):
        app.processEvents()
    got = is_topmost(int(ui.winId()))
    ok = bool(got)
    print("  [%s] 再改回「不跟随+置顶」：-> %s（期望 True）"
          % ("通过" if ok else "失败", got))
    if not ok:
        fails.append(("运行期 topmost", got))
    ui.close()

    print("-" * 72)
    print("`SetWindowPos` 的 argtypes（**不设就静默失败**，见 `follow.set_window_pos`）")
    from wechat_assistant import follow
    follow.set_window_pos(0, follow.HWND_TOPMOST, 0)      # 空句柄：只为了让 argtypes 上号
    ok = user32.SetWindowPos.argtypes is not None
    print("  [%s] 调用过之后 argtypes 已设置：%s"
          % ("通过" if ok else "失败", user32.SetWindowPos.argtypes))
    if not ok:
        fails.append(("argtypes", None))

    print("=" * 72)
    print("不符项：%s" % (fails if fails else "无"))
    return 1 if fails else 0


if __name__ == "__main__":
    dpi.enable()
    sys.exit(main())
