# -*- coding: utf-8 -*-
"""点【生成建议】之后，把**所有线程**的调用栈定时转储出来。

    python -X utf8 probes/qt/dump_hang.py

用 `faulthandler.dump_traceback_later` 而不是自己写心跳 ——
它由 C 层定时器驱动，**Qt 主循环卡死也照样会输出**，
不像 Python 线程那样会被 GIL / 事件循环的问题连带影响。
"""
import faulthandler
import os
import sys
import threading
import time

os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.getcwd())
sys.argv = ["run_watch.py"]

CLICK_AFTER = float(os.environ.get("CLICK_AFTER", "8"))


def main():
    import run_watch

    # ★ 每 5 秒无条件把**全部线程**的栈打到 stderr。
    #   不设 repeat=False，就一直打 —— 卡不卡、卡在哪，一看便知。
    faulthandler.enable()
    faulthandler.dump_traceback_later(5.0, repeat=True, file=sys.stderr)

    def clicker():
        time.sleep(CLICK_AFTER)
        from PySide6 import QtCore, QtWidgets

        app = QtWidgets.QApplication.instance()
        target = None
        for w in app.topLevelWidgets():
            if w.__class__.__name__ == "FloatingAssistant":
                target = w
                break
        if target is None:
            print("!! 没找到悬浮窗", flush=True)
            return

        class Fire(QtCore.QObject):
            go = QtCore.Signal()

        f = Fire()
        f.go.connect(target.btn_gen.click, QtCore.Qt.QueuedConnection)
        print("\n>>> 投递点击事件（跨线程走 signal，不是 singleShot）", flush=True)
        f.go.emit()
        # 引用要留住，别被 GC —— 否则信号连接会被断开
        globals()["_keep"] = (f, target)

    threading.Thread(target=clicker, daemon=True).start()
    rc = run_watch.main()
    faulthandler.cancel_dump_traceback_later()
    print("main 返回 %s" % rc, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
