# -*- coding: utf-8 -*-
"""触发管理器：决定「什么时候读取会话并（可选）生成建议」。

设计出发点：
    轮询本身不花钱，花钱的是它下游的 AI 调用。所以不该由程序擅自决定
    什么时候调 AI，而应该让用户自己选触发时机。

可选的触发源（配置里可任意组合，命中任意一个就触发）：

    on_hotkey            全局热键。程序不在前台也能响应。
                         用 RegisterHotKey 注册 —— 这是**接收**用户按键，
                         不模拟任何键鼠，符合项目红线。
    on_window_focus      微信窗口被切到前台时。用 SetWinEventHook 监听
                         EVENT_SYSTEM_FOREGROUND，**零轮询**。
                         注：实测最小化/恢复不会触发 SHOW/HIDE（对顶层窗口
                         那属于状态变化），所以只能挂 FOREGROUND。
    on_session_switch    切换到别的会话时。轻量轮询标题（约 12~70ms/次），
                         这是最贴合使用习惯的触发点：你切过去 = 你要处理它。
    on_new_message       检测到新消息时。需要轮询消息列表，可能频繁触发。

另外还有手动触发：fire("manual")，给悬浮窗按钮（P1）用。

防抖：任意两次触发之间至少间隔 min_interval 秒，避免刷屏。
"""

import ctypes
import re
import threading
import time
from ctypes import wintypes
from typing import Callable, Dict, Optional

import win32con
import win32gui

user32 = ctypes.windll.user32

# ---- 热键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

# ---- 窗口事件
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
PM_REMOVE = 0x0001

MOD_MAP = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN,
}

WinEventProc = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
)


def parse_hotkey(spec: str):
    """把 'Ctrl+Alt+Q' 解析成 (modifiers, virtual_key)。解析失败返回 (None, None)。"""
    if not spec:
        return None, None
    parts = [p.strip().lower() for p in re.split(r"[+\-]", spec) if p.strip()]
    if not parts:
        return None, None
    mods = 0
    key = None
    for p in parts:
        if p in MOD_MAP:
            mods |= MOD_MAP[p]
        else:
            key = p
    if not key:
        return None, None
    if len(key) == 1:
        vk = ord(key.upper())
    else:
        named = {
            "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
            "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
            "f11": 0x7A, "f12": 0x7B, "space": 0x20, "enter": 0x0D,
            "tab": 0x09, "esc": 0x1B, "insert": 0x2D, "home": 0x24,
            "end": 0x23, "delete": 0x2E,
        }
        vk = named.get(key)
    if vk is None:
        return None, None
    return mods | MOD_NOREPEAT, vk


class TriggerManager:
    """汇总各个触发源，统一回调。"""

    def __init__(self, cfg, reader, logger: Callable[[str], None] = None,
                 on_trigger: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.reader = reader
        self._log = logger or (lambda m: None)
        self._on_trigger = on_trigger or (lambda reason: None)

        self.min_interval = float(cfg.get("trigger.min_interval", 3.0))
        self.hotkey_spec = (cfg.get("trigger.on_hotkey", "") or "").strip()
        self.on_window_focus = bool(cfg.get("trigger.on_window_focus", False))
        self.on_session_switch = bool(cfg.get("trigger.on_session_switch", False))

        self._stop = threading.Event()
        self._last_fire = 0.0
        self._last_title: Optional[str] = None
        self._hook = None
        self._cb_ref = None          # 回调要保引用，否则被 GC 掉会崩
        self.counts: Dict[str, int] = {}
        self._hwnd = None
        self._hotkey_id = 1

    def use_config(self, cfg) -> None:
        """设置保存后换一份新配置（**不重启就生效**）。

        ⚠️ 为什么需要（2026-10-17 审计查出来的最后一处漏网）：
            `min_interval` 是**构造时读进属性**的（`fire()` 每拍都用它），
            而 `run_watch.on_config_changed()` 原来既不换这个对象、
            更新 `cfg` 内容也到不了它 —— 于是「高级 → 两次触发最小间隔」
            改了**一点反应都没有**，要重启才认，且不报错。
            （`Config.update_from()` 治的是"调用时读"，治不了这种"构造时冻结"。）

        ⚠️ 这里**只刷新能让它当场生效的那些**：
            · `min_interval` —— `fire()` 每次都读属性，换了立刻管用；
            · `hotkey_spec` / `on_window_focus` / `on_session_switch` **不在这里刷**：
              热键是启动时 `RegisterHotKey` 注册的，改了得重新注册（SPEC 已标
              `restart=True`）；后两个要重新挂/摘窗口事件钩子，而它们在 SPEC 里
              本来就没有界面项（只在 `config.yaml` 里）。
        """
        self.cfg = cfg
        try:
            self.min_interval = float(cfg.get("trigger.min_interval", 3.0))
        except Exception as e:
            self._log("刷新触发间隔失败（沿用 %.1fs）: %s: %s"
                      % (self.min_interval, type(e).__name__, e))

    # ------------------------------------------------------------ 触发入口

    def fire(self, reason: str) -> bool:
        """触发一次（带防抖）。返回是否真的触发了。"""
        now = time.time()
        if now - self._last_fire < self.min_interval:
            self._log("[触发] %s 被防抖忽略（距上次 %.1fs < %.1fs）"
                      % (reason, now - self._last_fire, self.min_interval))
            return False
        self._last_fire = now
        self.counts[reason] = self.counts.get(reason, 0) + 1
        self._log("[触发] %s" % reason)
        try:
            self._on_trigger(reason)
        except Exception as e:
            self._log("触发回调异常: %s: %s" % (type(e).__name__, e))
        return True

    # ------------------------------------------------------------ 生命周期

    def start(self, hwnd: int) -> None:
        """启动所有触发源。

        ⚠️ 热键与窗口事件的**注册必须和消息循环在同一线程**：
        RegisterHotKey 产生的 WM_HOTKEY、SetWinEventHook 的回调，
        都只会投递到「调用注册的那个线程」的消息队列。
        若在主线程注册、却让子线程跑消息循环，就永远收不到 —— 实测踩过这个坑
        （程序显示"已启用"，但切窗口/按热键毫无反应）。
        所以这里把注册动作整个放进消息循环线程里做。
        """
        self._hwnd = hwnd

        threading.Thread(target=self._message_loop, name="trigger-msg", daemon=True).start()

        if self.on_session_switch:
            threading.Thread(target=self._session_loop, name="trigger-session", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self.hotkey_spec:
                user32.UnregisterHotKey(None, self._hotkey_id)
        except Exception:
            pass
        try:
            if self._hook:
                user32.UnhookWinEvent(self._hook)
        except Exception:
            pass

    # ------------------------------------------------------------ 各触发源

    def _message_loop(self) -> None:
        """消息循环线程：**在这里注册**热键与窗口事件，然后处理它们的消息。

        注册必须发生在当前线程（见 start() 的说明）。
        """
        started = []

        if self.hotkey_spec:
            mods, vk = parse_hotkey(self.hotkey_spec)
            if mods is None:
                self._log("热键格式无法解析: %r（应为 Ctrl+Alt+Q 这种）" % self.hotkey_spec)
            elif user32.RegisterHotKey(None, self._hotkey_id, mods, vk):
                started.append("热键 %s" % self.hotkey_spec)
            else:
                self._log("热键 %s 注册失败（可能被别的程序占用）" % self.hotkey_spec)

        if self.on_window_focus and self._hwnd:
            if self._install_winevent(self._hwnd):
                started.append("微信窗口切前台")

        if self.on_session_switch:
            started.append("切换会话")

        self._log("触发源已就绪: %s（防抖 %.1fs）"
                  % ("、".join(started) if started else "无", self.min_interval))

        msg = wintypes.MSG()
        while not self._stop.is_set():
            got = False
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                got = True
                if msg.message == WM_HOTKEY and msg.wParam == self._hotkey_id:
                    self.fire("hotkey")
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if not got:
                time.sleep(0.02)

    def _install_winevent(self, hwnd: int) -> bool:
        def cb(hook, event, h, idobj, idchild, thread, t):
            try:
                if h == hwnd and event == EVENT_SYSTEM_FOREGROUND:
                    self.fire("window_focus")
            except Exception:
                pass

        self._cb_ref = WinEventProc(cb)     # 保引用
        self._hook = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
            None, self._cb_ref, 0, 0, WINEVENT_OUTOFCONTEXT)
        if not self._hook:
            self._log("窗口事件监听注册失败")
            return False
        return True

    def _session_loop(self) -> None:
        """轻量轮询当前聊天标题，检测「切换会话」。"""
        interval = float(self.cfg.get("trigger.session_poll_interval", 1.5))
        while not self._stop.is_set():
            try:
                if self._hwnd:
                    t = self.reader.read_chat_title(self._hwnd)
                    if t and t != self._last_title:
                        first = self._last_title is None
                        self._last_title = t
                        if not first:
                            self.fire("session_switch")
            except Exception as e:
                self._log("会话检测异常: %s: %s" % (type(e).__name__, e))
            time.sleep(interval)
