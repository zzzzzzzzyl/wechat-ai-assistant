"""开机自启 —— 登录 Windows 时自动拉起本程序。

⚠️ 2026-11-09 用户要求「设置中可以设置开机自动启动」。原来项目里**完全没有**这一项
   （只有 `ui.attach`「跟随微信」= 窗口贴边跟随，跟启动无关）。

⚠️⚠️ **这是系统状态，不是配置**：**不落 `settings.json`**。
    存了就会变成假开关 —— 设置里显示"开着"，注册表里其实被清理软件删了、
    或者用户挪了目录还指着旧路径。正确做法是设置页每次打开都**读注册表真实值**。

⚠️ 只写 **HKCU**（当前用户），**不需要管理员**，也**不动**系统级 HKLM。

⚠️ 为什么"开机自启"≈"跟随微信启动"：程序启动时找不到微信窗口是**等着**不退出
   （`winlocate.pick_window` 返回 None → 等），每次生成前还会 `resolve_window()`
   重新找句柄 —— 所以开机把程序拉起来，微信一出现就能用；微信重启也能接上。
"""

import os
import sys

#: 启动项写在哪个键下（当前用户）。
#: ⚠️ 测试会把它换成临时子键（`Software\\wechat-ai-assistant-test\\Run`），
#:    绝不能在测试里碰真实的 Run 键 —— 那会真的改用户的开机启动。
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: 值的名字（在"任务管理器 → 启动"里显示的就是它）。
VALUE_NAME = "WeChatAIAssistant"


def command() -> str:
    """开机时该跑的命令行。

    · **打包版**（PyInstaller 冻结）：就是 exe 自己；
    · **源码版**：`pythonw.exe` + `run_watch.py` —— 必须用 `pythonw`，
      否则每次开机都弹一个黑框（用户会立刻来问"这个是病毒吗"）。
    """
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable

    exe = sys.executable or ""
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = exe          # 没有 pythonw 就退回 python（会弹黑框，但至少能启动）
    here = os.path.dirname(os.path.abspath(os.path.dirname(__file__)))
    script = os.path.join(here, "run_watch.py")
    if not os.path.exists(script):
        return '"%s"' % pyw
    return '"%s" "%s"' % (pyw, script)


def state():
    """注册表里的真实状态：返回 `(是否开启, 里面存的命令)`。

    ⚠️ 任何读取失败都当"没开"（**不抛**）—— 这个函数是给界面刷新用的，
       抛出去会让整个「外观」页建不出来。
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _typ = winreg.QueryValueEx(k, VALUE_NAME)
            return True, str(val or "")
    except FileNotFoundError:
        return False, ""
    except OSError:
        return False, ""
    except Exception:
        return False, ""


def is_enabled() -> bool:
    return state()[0]


def is_stale() -> bool:
    """开着，但存的命令**不是当前这份程序**（挪过目录 / 换过 exe / 源码转打包）。

    界面拿它提示"拨一下开关重新写入"，不然会出现"开机启动了但起的是旧版本"。
    """
    on, cmd = state()
    if not on:
        return False
    return cmd.strip() != command().strip()


def enable() -> str:
    """写启动项，返回写进去的命令。失败**抛异常**（由界面提示并回滚开关）。"""
    import winreg

    cmd = command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    return cmd


def disable() -> bool:
    """删启动项。`True` = 现在确实没有（包括"本来就没有"）。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True          # 键或值本来就不存在 = 已经是关的
    except OSError:
        return False
    except Exception:
        return False
