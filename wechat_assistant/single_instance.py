# -*- coding: utf-8 -*-
"""单实例：同一个程序只跑一个；第二次启动**去把已经开着的那个唤出来**。

用户要求（2026-10-13）：

    打包后让用户使用时，如果已经打开了程序，就不要重新开一个，
    再次打开 exe 就是把原来的跳出来。

机制（纯 Win32，不需要 Qt、也不需要额外依赖）：

    第一个实例                         第二个实例（用户又双击了一次 exe）
    ─────────────────────────          ────────────────────────────────
    CreateMutexW(名字)                  CreateMutexW(同一个名字)
      -> 新句柄（err != 183）              -> err == 183（ALREADY_EXISTS）
      -> 我是"主实例"                      -> 说明有人在跑
    CreateEventW(唤醒事件)               CreateEventW(同名 -> 等于打开已有的)
    起一条等待线程                         SetEvent(...)
      -> 收到信号 = ui 显示 + 唤到最前      -> 打印一行提示 -> **直接退出**
      -> 自动复位，继续等下一次

⚠️ **两个内核对象的名字必须和已发布的版本保持一致** ——
   改了名字等于"旧版本跑着的那个不算数"，会出现两个实例。

## 实测记下来的坑（本机 Windows / 2026-10-13）

1. ⚠️ **不要用 `OpenEventW`**：本机实测它返回 NULL、`GetLastError()=5
   (ACCESS_DENIED)`（大概是权限/沙箱对"按名字打开"的限制），
   而 `CreateEventW` **同名再建一次**是好的（返回有效句柄 + `err=183`）。
   → 统一用"建或打开"的写法，别用 Open。
2. ⚠️ **事件要用"自动复位"**（`bManualReset=False`）：等到了内核会自动清，
   不用自己 `ResetEvent`（手动复位 + 自己清之间会**漏掉**一次信号）。
3. ⚠️ **句柄必须留着引用**（模块级全局）：被 GC 掉就等于释放了，
   守卫形同虚设、事件也收不到信号。
4. ⚠️ **拿不到内核对象时一律"当自己是主实例"**（fail-open）：
   宁可多开一个，也不要因为一个 API 失败就让程序**打不开**
   （这是个研究工具，用户还可能故意 `--allow-multi`）。
"""

import threading

#: 互斥体名：判断"有没有人在跑"。
#: ⚠️ 用 **`Local\`（每登录会话）而不是 `Global\`（整台机器）**：
#:    · 两个 Windows 用户（或两个远程会话）**各开一个是对的** ——
#:      用 `Global\` 的话第二个用户会被"已经有人在跑"挡住，
#:      而那个在跑的窗口还在**别人的会话**里，唤也叫不过来，程序等于打不开；
#:    · `Global\` 命名对象在某些受限令牌下要 `SeCreateGlobalPrivilege`，
#:      拿不到就直接失败（实测沙箱里就出现过 `CreateMutexW` 返回 NULL）。
#: ⚠️ 改了名字 = 老版本跑着的那个不算数（会出现一瞬两个实例），可以接受。
MUTEX_NAME = "Local\\wechat_ai_assistant_single"
#: 唤醒事件名：第二次启动用它通知第一个实例"把窗口跳出来"。
EVENT_NAME = "Local\\wechat_ai_assistant_wake"

_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258

#: 句柄活到进程结束（**故意不关**：它们就是"我在跑"的凭据）。
_mutex_handle = None
#: 已经挂了监听的名字（**同名只挂一条**）与它们的句柄。
_watched = set()
_event_handles = []
_watch_started = False
_lock = threading.Lock()


def _kernel32():
    import ctypes

    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateEventW.restype = ctypes.c_void_p
    k32.SetEvent.restype = ctypes.c_int
    k32.WaitForSingleObject.restype = ctypes.c_ulong
    return k32


def is_primary(log=None, name: str = MUTEX_NAME) -> bool:
    """我是不是**第一个**实例。

    ⚠️ 返回 False 只表示"有人在跑"，**不**表示失败（调用方该做的是
       去 `signal_primary()` 然后退出）。
    ⚠️ 任何异常都返回 True（fail-open，见模块说明第 4 条）。
    """
    global _mutex_handle
    try:
        k32 = _kernel32()
        handle = k32.CreateMutexW(None, False, name)
        if not handle:
            if log:
                log("单实例检查拿不到互斥体（忽略，继续启动）")
            return True
        if k32.GetLastError() == _ERROR_ALREADY_EXISTS:
            return False
        _mutex_handle = handle          # 留住引用，别被 GC（见模块说明第 3 条）
        return True
    except Exception as e:
        if log:
            log("单实例检查失败（忽略，继续启动）: %s: %s" % (type(e).__name__, e))
        return True


def signal_primary(name: str = EVENT_NAME) -> bool:
    """通知**已经开着的那个**实例把窗口跳出来。第二个实例调它，然后自己退出。"""
    try:
        k32 = _kernel32()
        # ⚠️ 用"建或打开"（同名再建一次会拿到已有那个句柄，err=183）——
        #    **不要**用 OpenEventW（见模块说明第 1 条）。
        handle = k32.CreateEventW(None, False, False, name)
        if not handle:
            return False
        ok = bool(k32.SetEvent(handle))
        k32.CloseHandle(handle)         # 这个句柄可以关：它只是用来置位
        return ok
    except Exception:
        return False


def is_running(name: str = EVENT_NAME):
    """**只"看一眼"**：现在有没有实例在跑。不持有任何东西，也不打扰在跑的那个。

    用途：测试里判「另一个实例在跑吗」（那种"仓库文件被动过"的断言
    在有别人写文件时不能算失败）。

    ⚠️ 实现是"建一次唤醒事件再看 `GetLastError`"：
        `183 (ALREADY_EXISTS)` = **已经有人建过** = 有人在跑。
        对象本来就存在时 `CreateEventW` 只是"打开"，我们**马上关掉**自己这个句柄，
        所以在跑的那个不受影响；对象不存在时我们建的会被自己关掉，不留残留。
    ⚠️ **不能用 `OpenEventW`/`OpenMutexW` 来判**：本机实测它们返回 NULL +
        `err=5 (ACCESS_DENIED)`（大概是权限/沙箱对"按名字打开"的限制），
        而 `CreateXxxW` 的"建或打开"是好的 —— 用它才不会**静默失效**
        （静默失效的后果是"有实例在跑"永远判成 False，测试于是会**假红**）。
    ⚠️ 判不了就返回 `None`（"不知道"），调用方按保守方式处理。
    """
    try:
        k32 = _kernel32()
        handle = k32.CreateEventW(None, False, False, name)
        if not handle:
            return None
        existed = (k32.GetLastError() == _ERROR_ALREADY_EXISTS)
        k32.CloseHandle(handle)
        return existed
    except Exception:
        return None


def watch(callback, log=None, name: str = EVENT_NAME) -> bool:
    """（主实例调）起一条等待线程：收到唤醒信号就执行 `callback`。

    ⚠️ `callback` 跑在**子线程**上，所以它只能做"投递"的事
        （`panel.wake_from_second_launch()` 会自己排进 UI 线程）。
    ⚠️ 线程是 daemon：主程序退出时不该被它拖住。
    ⚠️ **同一个名字只挂一条**（重复调返回 True 但不叠加线程）。
        判据是**名字**而不是"这个进程挂过没有" —— 测试会用临时名字各挂一条。
    """
    global _watch_started
    with _lock:
        if name in _watched:
            return True                 # 这个名字已经挂过了
        try:
            k32 = _kernel32()
            handle = k32.CreateEventW(None, False, False, name)
            if not handle:
                if log:
                    log("单实例唤醒事件建不出来（第二次启动将无法唤出窗口）")
                return False
            _event_handles.append(handle)   # 留住引用（见模块说明第 3 条）
            _watched.add(name)
            _watch_started = True
        except Exception as e:
            if log:
                log("单实例唤醒不可用: %s: %s" % (type(e).__name__, e))
            return False

    def loop():
        # ⚠️ 句柄要用**闭包里的这个**，不能读模块全局 ——
        #    挂了第二条监听之后全局那个会被改写，第一条就会去等别人的事件。
        while True:
            try:
                rc = k32.WaitForSingleObject(handle, 0xFFFFFFFF)   # INFINITE
                if rc != _WAIT_OBJECT_0:
                    return              # 句柄坏了/被关了，退出这条线程
                callback()
            except Exception as e:
                if log:
                    log("唤醒窗口失败: %s: %s" % (type(e).__name__, e))

    t = threading.Thread(target=loop, name="single-instance-wake-%d" % len(_watched),
                         daemon=True)
    t.start()
    return True
