# -*- coding: utf-8 -*-
"""单实例：**第二次双击 exe 不该开第二个，而要把原来那个跳出来**。

    python tests/test_single_instance.py

用户要求（2026-10-13）：

    打包后让用户使用时，如果已经打开了程序，就不要重新开一个，
    再次打开 exe 就是把原来的跳出来。

⚠️ 这个文件**不依赖 Qt、不依赖微信**（纯 Win32 内核对象），随时能跑。
⚠️ 每个用例都用自己的**随机名字**：测试机上很可能正跑着真的程序，
   用固定名字会跟它撞上（那才是"假阴性"）。
"""
import ast
import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import single_instance as si  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def _uniq(tag):
    # ⚠️ 用 **`Local\`**（和产品里同名空间一致）：`Global\` 在受限令牌下可能
    #    建不出来（实测沙箱里出现过 NULL），那会让这一套测试**假绿**。
    return "Local\\wxai-test-%s-%s" % (tag, uuid.uuid4().hex[:10])


# ================================================================ 1. 互斥体

def test_mutex():
    print("\n1. 「有没有人在跑」用有名互斥体判（不数进程）")
    m = _uniq("mutex")
    check("第一个实例拿到互斥体 -> is_primary True", si.is_primary(name=m) is True)
    check("同一个名字再拿一次 -> False（说明有人在跑）",
          si.is_primary(name=m) is False)
    check("换个名字又是新的（不会互相干扰）",
          si.is_primary(name=_uniq("mutex")) is True)


# ================================================================ 2. 唤醒事件

def test_wake():
    print("\n2. 第二次启动 -> 置位事件 -> 等信号的那一头被叫醒")
    ev = _uniq("wake")
    got = []

    ok = si.watch(lambda: got.append("woke"), log=print, name=ev)
    check("watch() 挂上了等待线程", ok is True)
    check("没人置位时不会被误叫醒", got == [], repr(got))

    check("signal_primary() 置位成功", si.signal_primary(name=ev) is True)
    for _ in range(50):                       # 最多等 5 秒
        if got:
            break
        time.sleep(0.1)
    check("置位后回调被调到（这就是「把原来的跳出来」那一棒）",
          got == ["woke"], repr(got))

    # ⚠️ 事件是**自动复位**的：第二次置位还能收到（不然就只能唤出一次）
    got.clear()
    si.signal_primary(name=ev)
    for _ in range(50):
        if got:
            break
        time.sleep(0.1)
    check("还能再唤一次（事件会自动复位）", got == ["woke"], repr(got))

    check("重复 watch() 只起一条线程（不叠加）",
          si.watch(lambda: got.append("dup"), name=ev) is True)


def test_signal_without_listener():
    print("\n3. 没人在听的时候置位也不炸")
    check("没有监听者时 signal_primary() 不抛异常",
          si.signal_primary(name=_uniq("no-listener")) in (True, False))


def test_is_running():
    """`is_running()`：测试里判"有别的实例在跑吗"要用它
    （那两条"仓库文件被动过"的断言在有实例在跑时不能算失败）。
    ⚠️ 它必须**只"看一眼"**：不能持有任何东西，否则判完自己就变成"有实例在跑"。"""
    print("\n3b. is_running()：只看一眼，不持有")
    name = _uniq("running")
    check("还没人建过 -> False", si.is_running(name=name) is False)
    check("连问两次都还是 False（自己不留残留）",
          si.is_running(name=name) is False)
    check("watch() 之后 -> True（那个事件被建起来了）",
          si.watch(lambda: None, name=name) is True
          and si.is_running(name=name) is True)
    check("别的名字不受影响", si.is_running(name=_uniq("running")) is False)


# ================================================================ 4. fail-open

def test_fail_open():
    print("\n4. 内核对象拿不到时**当自己是主实例**（宁可多开，也不能打不开）")
    real = si._kernel32
    try:
        si._kernel32 = lambda: (_ for _ in ()).throw(OSError("假装没有 kernel32"))
        check("_kernel32 抛异常 -> is_primary 仍返回 True（不拦住启动）",
              si.is_primary(name=_uniq("boom")) is True)
        check("signal_primary 也返回 False 而不是抛",
              si.signal_primary(name=_uniq("boom")) is False)
    finally:
        si._kernel32 = real


# ================================================================ 5. 接线（AST）

def _func_src(path, name):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return src, node
    return src, None


def test_wiring():
    """⚠️ 判据都在"**接线对不对**"上 —— 机制对不对是上面几条，
    而"第二次启动到底有没有真拦住 / 唤醒那头挂上了没有"只能看接线。"""
    print("\n5. 接线：第二次启动真的会退出，而且唤醒挂在界面之后")
    rw = os.path.join(ROOT, "run_watch.py")
    src, main_fn = _func_src(rw, "main")
    check("run_watch.main() 找得到", main_fn is not None)
    if main_fn is None:
        return

    lines = src.splitlines()
    # ① guard 的调用点 与 ② 界面的构造点、③ watch 的挂载点，谁前谁后
    guard_ln = ui_ln = watch_ln = None
    for node in ast.walk(main_fn):
        if isinstance(node, ast.Call):
            f = node.func
            fname = getattr(f, "id", None) or getattr(f, "attr", None)
            if fname == "single_instance_guard" and guard_ln is None:
                guard_ln = node.lineno
            elif fname == "FloatingAssistant" and ui_ln is None:
                ui_ln = node.lineno
            elif fname == "watch" and watch_ln is None:
                watch_ln = node.lineno
    check("单实例检查在**界面之前**（不然会先占热键再报错）",
          guard_ln is not None and ui_ln is not None and guard_ln < ui_ln,
          "guard=%s ui=%s" % (guard_ln, ui_ln))
    check("唤醒的挂载在**界面之后**（信号来了得有人接）",
          watch_ln is not None and ui_ln is not None and watch_ln > ui_ln,
          "watch=%s ui=%s" % (watch_ln, ui_ln))

    # ② "已经在跑"那一支必须**真的退出**（老代码只打印一行警告，照样开第二个）
    seg = "\n".join(lines[(guard_ln or 1) - 1:(guard_ln or 1) + 6])
    check("⚠️ 已经在跑的那一支里有 return（不再走「只警告照样启动」的老路）",
          "return" in seg, repr(seg.replace("\n", " / ")[:120]))

    # ③ 机制必须走 single_instance 模块，而不是又手写一遍 ctypes
    check("用的是 wechat_assistant.single_instance（不重复实现）",
          "single_instance" in src and "CreateMutexW" not in src)


def test_panel_hook():
    print("\n6. 面板那一头：从子线程叫它，只能「投递」给 UI 线程")
    p = os.path.join(ROOT, "wechat_assistant", "qt_ui", "panel.py")
    src, fn = _func_src(p, "wake_from_second_launch")
    check("panel.wake_from_second_launch() 存在", fn is not None)
    if fn is None:
        return
    body = ast.dump(fn)
    check("它走 _invoke_on_ui_thread（子线程不能直接碰控件）",
          "_invoke_on_ui_thread" in body)
    check("它不直接调 show()/raise_()（那些必须在 UI 线程）",
          "'show'" not in body and "raise_" not in body)


def main():
    print("=" * 70)
    print("单实例测试（第二次双击 exe 应唤出原窗口，而不是开第二个）")
    print("=" * 70)
    test_mutex()
    test_wake()
    test_signal_without_listener()
    test_is_running()
    test_fail_open()
    test_wiring()
    test_panel_hook()
    print("\n" + "=" * 70)
    print("通过 %d，失败 %d" % (PASS, FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
