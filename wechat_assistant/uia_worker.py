# -*- coding: utf-8 -*-
"""UIA 基础设施：常驻工作线程 + 超时保护。

为什么需要单独的工作线程：
    Qt 的 UIA provider 偶尔会无响应。同步调用一旦卡住，
    整个程序就死了（实测过 GetRootControl().GetChildren() 能挂 60 秒以上）。
    所以所有 UIA 调用都丢进这个线程串行执行，主线程最多等 timeout 秒。

设计取舍：
    超时后旧线程可能仍然卡在 COM 调用里（无法强杀），
    这里采取「换个新线程继续干活」的策略，牺牲一个泄漏的线程换取程序可用。
"""

import queue
import threading
from typing import Any, Callable


class UiaError(RuntimeError):
    """UIA 调用失败。"""


class UiaTimeout(UiaError):
    """UIA 调用超时（微信进程可能无响应）。"""


class UiaWorker:
    """把 UIA 调用串行化到一个常驻线程，并加超时保护。"""

    def __init__(self, timeout: float = 20.0, logger: Callable[[str], None] = None):
        self.timeout = float(timeout)
        self._log = logger or (lambda msg: None)
        self._queue: "queue.Queue" = queue.Queue()
        self._ready = threading.Event()
        self._init_error = None
        self._restart_lock = threading.Lock()
        self._generation = 0
        self._auto = None
        self._uia = None
        self._start()

    # ------------------------------------------------------------ 内部

    def _start(self) -> None:
        self._ready.clear()
        self._init_error = None
        threading.Thread(target=self._run, name="uia-worker", daemon=True).start()
        if not self._ready.wait(15.0):
            raise UiaError("UIA 工作线程初始化超时（15s）")
        if self._init_error:
            raise UiaError("UIA 初始化失败: %s" % self._init_error)

    def _run(self) -> None:
        try:
            import comtypes

            comtypes.CoInitialize()
            import uiautomation as auto
            from uiautomation import uiautomation as core

            ctx = auto.UIAutomationInitializerInThread(debug=False)
            ctx.__enter__()
            self._auto = auto
            self._uia = core._AutomationClient.instance().IUIAutomation
        except Exception as e:  # pragma: no cover - 取决于运行环境
            self._init_error = "%s: %s" % (type(e).__name__, e)
            self._ready.set()
            return
        self._ready.set()

        while True:
            job = self._queue.get()
            if job is None:
                return
            fn, box, gen = job
            if gen != self._generation:
                # 该任务属于被重建前的旧线程，直接丢弃
                box["error"] = "任务已被作废（工作线程已重建）"
                box["event"].set()
                continue
            try:
                box["result"] = fn(self._auto, self._uia)
            except Exception as e:
                # ⚠️ **必须带上函数名和栈尾。**
                #    原来只记 `"%s: %s" % (type(e).__name__, e)`，于是日志里
                #    只剩 `COMError: (-2147220991, '事件无法调用任何订户')` ——
                #    既不知道是哪一步（读标题？读消息？判定发送者？），
                #    也不知道谁调用的，**完全没法查**（实测踩过：
                #    用户每次点生成都报这个，但错误信息里一条线索都没有）。
                import traceback

                tb = [x.strip() for x in traceback.format_exc().strip().splitlines()]
                # 栈尾 3 行足够定位，又不至于把日志刷爆
                tail = " <- ".join(tb[-3:]) if tb else ""
                box["error"] = "%s: %s\n    位置: %s\n    栈尾: %s" % (
                    type(e).__name__, e,
                    getattr(fn, "__name__", "(匿名函数)"), tail)
            finally:
                box["event"].set()

    def _restart(self) -> None:
        with self._restart_lock:
            self._generation += 1
            self._log("重建 UIA 工作线程（generation=%d）" % self._generation)
            self._start()

    # ------------------------------------------------------------ 对外

    def call(self, fn: Callable[[Any, Any], Any], timeout: float = None,
             label: str = "") -> Any:
        """在工作线程里执行 fn(auto, uia)。

        fn 内部**只能**使用传进来的 auto / uia 对象，
        不要捕获主线程的 COM 对象（跨线程使用不安全）。

        `label` 可选，用来标明"这一步在干什么"（例如"读会话标题"）。
        出错时它会出现在错误信息里 —— **UIA 的报错本身完全看不出上下文**，
        没有 label 就只能靠猜。
        """
        timeout = float(timeout or self.timeout)
        box = {"event": threading.Event(), "result": None, "error": None,
               "label": label}
        self._queue.put((fn, box, self._generation))
        if not box["event"].wait(timeout):
            self._log("UIA 调用%s 超过 %.1fs 未返回，判定为无响应并重建线程"
                      % (("（%s）" % label) if label else "", timeout))
            self._restart()
            raise UiaTimeout("UIA 调用超时 %.1fs" % timeout)
        if box["error"]:
            raise UiaError(("%s：%s" % (label, box["error"])) if label else box["error"])
        return box["result"]

    def close(self) -> None:
        try:
            self._queue.put(None)
        except Exception:
            pass
