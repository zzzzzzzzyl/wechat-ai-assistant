# -*- coding: utf-8 -*-
"""验证「Ctrl+C 能不能真的退出」。

    python probes/check_sigint_exit.py

背景（2026-10-10 用户实测贴出来的 traceback）
--------------------------------------------
在控制台按 Ctrl+C，屏幕上会出现：

    File "wechat_assistant/follow.py", line 449, in _tick
        def _tick(self):
    KeyboardInterrupt

然后**程序不退出**。原因是 Python 在下一个字节码边界抛 KeyboardInterrupt，
而这个边界落在**某个 Qt 槽函数内部**；**PySide 不让槽里的异常穿出 C++ 层**，
只交给 `sys.excepthook` 打印一下，然后继续跑事件循环 ——
外层那句 `except KeyboardInterrupt` 永远等不到，`finally` 里的清理一条都不跑。

所以 `start.bat` 里写的「Ctrl+C quit」以前是假的。

这个脚本怎么验
--------------
自己开一个子进程跑真程序（`CREATE_NEW_PROCESS_GROUP`），等它起来，
然后 `os.kill(pid, CTRL_BREAK_EVENT)`。

⚠️ 用 CTRL_BREAK 而不是 CTRL_C：CTRL_C 只能发给**整个控制台的所有进程**，
   会把我自己也一起打断；CTRL_BREAK 能精确发给指定进程组，而它在 Windows 上
   映射成 **SIGBREAK** —— 产品和它注册的是同一段处理器（SIGINT/SIGBREAK 一起装），
   所以验的是同一段代码。

判据（两条都要）：
    ① 进程**真的退出了**（以前不退）
    ② 退出的过程走了 `finally` 的清理 —— 看日志里有没有「记录统计」
       （那是清理阶段才打印的；不打印就说明又是在槽里被打断、清理被跳过）
"""

import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED = 0x00000008


def main():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    print("[1] 启动真程序（独立进程组）…")
    p = subprocess.Popen(
        [sys.executable, "-u", "run_watch.py", "--allow-multi"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=CREATE_NEW_PROCESS_GROUP,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    # 等它把界面建起来（OCR 引擎加载要 1~2 秒）
    deadline = time.time() + 40
    started = False
    lines = []
    while time.time() < deadline:
        line = p.stdout.readline()
        if not line:
            break
        lines.append(line.rstrip())
        if "悬浮窗已创建" in line or "触发源已就绪" in line:
            started = True
            break
    print("    起来了: %s（读了 %d 行日志）" % (started, len(lines)))
    if not started:
        print("    **没起来**，日志尾部：")
        for x in lines[-10:]:
            print("      " + x)
        p.kill()
        return 1

    time.sleep(2.0)          # 让跟随器的心跳跑起来（被打断的就是它）

    print("[2] 发 CTRL_BREAK（-> SIGBREAK）…")
    try:
        os.kill(p.pid, signal.CTRL_BREAK_EVENT)
    except Exception as e:
        print("    发信号失败: %s: %s" % (type(e).__name__, e))
        p.kill()
        return 1

    try:
        rest, _ = p.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        rest = ""
        p.kill()
        print("    ⚠️ 25 秒还没退出 -> **Ctrl+C 仍然无效**")
    lines += [x.rstrip() for x in (rest or "").splitlines()]

    print("\n[3] 结果")
    exited = p.returncode is not None and p.returncode != -1
    print("    退出码 = %s  ->  退出了: %s" % (p.returncode, "是" if exited else "否"))
    saw_msg = any("收到 Ctrl+C" in x for x in lines)
    saw_cleanup = any("记录统计" in x for x in lines)
    saw_tb = any("KeyboardInterrupt" in x for x in lines) or \
        any("Traceback" in x for x in lines)
    print("    打出了「收到 Ctrl+C」      : %s" % ("是" if saw_msg else "否"))
    print("    走了 finally 清理（记录统计）: %s" % ("是" if saw_cleanup else "否"))
    print("    还吐 traceback              : %s" % ("是（不该有）" if saw_tb else "否"))

    if not saw_msg and not saw_tb:
        print("\n    最后 12 行日志（看不清发生了什么时看它）：")
        for x in lines[-12:]:
            print("      " + x)

    ok = exited and saw_cleanup and not saw_tb
    print("\n结论: %s" % ("Ctrl+C 能干净退出了" if ok
                        else "**还没修好**（见上面三行判据）"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
