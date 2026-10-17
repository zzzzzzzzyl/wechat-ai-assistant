# -*- coding: utf-8 -*-
"""弹窗按钮的语义：**「取消」不许等于「确定」**。

    python tests\\test_qt_dialogs.py

⛔ 2026-10-17 用户报的 bug：「点击取消按钮后会执行确定内容」。
    根因在 `layout.ShellDialog.add_button()` —— 它**无论点哪颗都调 `self.accept()`**，
    于是 `dlg.exec() == Accepted` 永远为真：

      · 确认框（移除资料 / 删除分组 / 恢复默认）→ 点「取消」照样执行删除；
      · 输入框（新建分组）→ 点「取消」拿回空名字，然后弹「组名不能为空」。

    为什么单测没抓到：**测试里 `show_dialogs = False`，弹窗整个被跳过**，
    `exec()` 那条路一次都没跑过（"单测全绿、真机才炸"的典型）。
    → 所以这里补两条：
        ① **行为**：`add_button(..., accept=False)` 点下去必须 `Rejected`；
        ② **静态**：`_ask_yes_no` / `_prompt` 的源码里必须写着 `accept=False`
           （防止以后有人"顺手"改回去，而动态测试仍然覆盖不到）。
"""
import inspect
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import dpi                                   # noqa: E402
dpi.enable()                     # ⚠️ 必须在 import Qt 之前（和 run_watch 同序）

from PySide6 import QtWidgets                                       # noqa: E402

from wechat_assistant.qt_ui import base                             # noqa: E402
from wechat_assistant.qt_ui.layout import ShellDialog               # noqa: E402
from wechat_assistant.settings import SettingsStore                 # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def _style(tmp):
    store = SettingsStore(os.path.join(tmp, "settings.json"), logger=lambda m: None)
    return base.Style(store.load(), 0, name="dark")


def test_buttons(app, style):
    print("\n1. ShellDialog 的按钮语义")
    dlg = ShellDialog(style, "测试")
    dlg.add_text("随便问一句？")
    cancel = dlg.add_button("取消", variant="ghost", accept=False)
    ok_btn = dlg.add_button("确定", variant="primary")
    # 不 exec()，直接点：accept()/reject() 会立刻把 result 设好
    cancel.click()
    check("1a 点「取消」→ Rejected（**不会再跑确定那一支**）",
          dlg.result() == QtWidgets.QDialog.Rejected, "-> %r" % dlg.result())
    dlg2 = ShellDialog(style, "测试2")
    cancel2 = dlg2.add_button("取消", variant="ghost", accept=False)
    ok2 = dlg2.add_button("确定", variant="primary")
    ok2.click()
    check("1b 点「确定」→ Accepted", dlg2.result() == QtWidgets.QDialog.Accepted,
          "-> %r" % dlg2.result())
    # 老行为（不传 accept）默认仍然是 accept() —— 只有"知道了/关闭"这种单按钮弹窗能这么用
    dlg3 = ShellDialog(style, "测试3")
    only = dlg3.add_button("知道了", variant="primary")
    only.click()
    check("1c 不传 accept 时仍默认 Accepted（单按钮弹窗靠这个）",
          dlg3.result() == QtWidgets.QDialog.Accepted)
    app.processEvents()


def test_sources():
    print("\n2. 静态守门：确认框/输入框的「取消」必须写 accept=False")
    from wechat_assistant.qt_ui import settings_ui

    for fn, label in ((settings_ui._ask_yes_no, "_ask_yes_no"),
                      (settings_ui.SettingsWindow._prompt, "_prompt")):
        try:
            src = inspect.getsource(fn)
        except Exception as e:
            check("%s 的源码读得到" % label, False, "%s: %s" % (type(e).__name__, e))
            continue
        check("%s 里「取消」那颗传了 accept=False" % label,
              "accept=False" in src, "")


def test_cancel_end_to_end(app, tmp, style):
    """真点一次「取消」—— 用假的 `exec()` 自动点那颗按钮，把 `exec` 这条路走通。

    ⚠️ 这两条是用户报的那句话的**正面判据**：
        · 确认框点取消 → 返回 False（不该执行动作）；
        · 输入框点取消 → 返回 None（不该拿空名字去建组）。
    """
    print("\n3. 端到端：点「取消」到底返回什么（把 exec 换成自动点按钮）")
    from wechat_assistant.qt_ui import settings_ui

    real_exec = ShellDialog.exec

    def _fake_exec(self, *a, **kw):
        """找到「取消」那颗，点它，然后返回 result()。"""
        for i in range(self.foot.count()):
            w = self.foot.itemAt(i).widget()
            if w is not None and w.text() == "取消":
                w.click()
                break
        return self.result()

    try:
        ShellDialog.exec = _fake_exec
        r = settings_ui._ask_yes_no(None, "删掉吗", "真的删掉吗？", style=style)
        check("3a 确认框点「取消」→ 返回 False（**不执行动作**）", r is False,
              "-> %r" % r)

        store = SettingsStore(os.path.join(tmp, "s2.json"), logger=lambda m: None)
        win = settings_ui.SettingsWindow(store.load(), store, style,
                                         logger=lambda m: None)
        win.show_dialogs = True
        v = win._prompt("新建分组", "起个名字", "")
        check("3b 输入框点「取消」→ 返回 None（**不拿空名字去建组**）", v is None,
              "-> %r" % v)
    finally:
        ShellDialog.exec = real_exec
    app.processEvents()


def main():
    print("=" * 72)
    print("弹窗按钮语义（取消 ≠ 确定）")
    print("=" * 72)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import tempfile
    tmp = tempfile.mkdtemp(prefix="dlg_test_")
    style = _style(tmp)
    test_buttons(app, style)
    test_sources()
    test_cancel_end_to_end(app, tmp, style)
    print("\n" + "=" * 72)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
