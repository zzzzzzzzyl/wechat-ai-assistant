"""开机自启模块的测试（2026-11-09）。

⚠️⚠️ **绝不碰真实的 `HKCU\\...\\Run`**：那是用户真正的开机启动项，
    写进去会让这台机器每次开机都跑测试代码。这里把 `autostart.RUN_KEY`
    替换成 `Software\\wechat-ai-assistant-test\\Run`，跑完就删。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s" % name)
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def cleanup(key):
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0,
                            winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, "WeChatAIAssistant")
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
    except OSError:
        pass


def test_roundtrip():
    import winreg

    from wechat_assistant import autostart

    print("\n1. 开关往返（临时子键，不碰真实 Run 键）")
    old_key = autostart.RUN_KEY
    autostart.RUN_KEY = r"Software\wechat-ai-assistant-test\Run"
    try:
        cleanup(autostart.RUN_KEY)
        check("一开始是关的", autostart.is_enabled() is False, str(autostart.state()))

        cmd = autostart.enable()
        check("打开之后读到的是开的", autostart.is_enabled() is True, str(autostart.state()))
        check("写进去的就是 command()", cmd == autostart.command(), repr(cmd))
        check("注册表里确实有这个值",
              autostart.state()[1].strip() == cmd.strip(), str(autostart.state()))

        # 再开一次是幂等的（不报错、值不变）
        cmd2 = autostart.enable()
        check("重复打开是幂等的", cmd2 == cmd, repr(cmd2))

        check("刚写的不算「过期」", autostart.is_stale() is False)

        # 模拟"指向旧位置"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, autostart.RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, autostart.VALUE_NAME, 0, winreg.REG_SZ,
                              r"C:\old\path\wechat-ai.exe")
        check("指向旧路径时会报「过期」", autostart.is_stale() is True,
              str(autostart.state()))

        check("关掉返回 True", autostart.disable() is True)
        check("关掉之后读不到", autostart.is_enabled() is False, str(autostart.state()))
        check("再关一次也不报错（幂等）", autostart.disable() is True)
        check("关掉之后不报「过期」", autostart.is_stale() is False)
    finally:
        cleanup(autostart.RUN_KEY)
        autostart.RUN_KEY = old_key
        # 清掉测试用的父键（删空键要两级）
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                             r"Software\wechat-ai-assistant-test\Run")
        except OSError:
            pass
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                             r"Software\wechat-ai-assistant-test")
        except OSError:
            pass


def test_command():
    from wechat_assistant import autostart

    print("\n2. 命令行怎么拼")
    cmd = autostart.command()
    check("带引号（路径可能有空格）", cmd.startswith('"'), cmd)
    if getattr(sys, "frozen", False):
        check("打包版 = exe 自己", sys.executable in cmd, cmd)
    else:
        check("源码版用 pythonw（否则开机弹黑框）",
              "pythonw" in cmd.lower() or "run_watch.py" in cmd, cmd)
        check("源码版带上了 run_watch.py",
              cmd.rstrip('"').endswith("run_watch.py") or "run_watch.py" in cmd, cmd)


def main():
    print("=" * 70)
    print("开机自启测试")
    print("=" * 70)
    test_roundtrip()
    test_command()
    print("\n" + "=" * 70)
    print("通过 %d，失败 %d" % (PASS, FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
