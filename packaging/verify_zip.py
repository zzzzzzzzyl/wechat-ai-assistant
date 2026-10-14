# -*- coding: utf-8 -*-
"""把打好的 zip **解到另一个目录真的跑一遍**，验收"发给别人的那个包"。

    python packaging/verify_zip.py [--seconds 40]

为什么值得单独写一个
--------------------
"我这台机器上能跑"和"别人拿到能跑"是两件事，中间隔着一堆只在**换目录/换机器**
时才暴露的东西：

  · `config.yaml` 到底在不在 exe 旁边（`paths.base_dir()` 对不对）
  · `state/` 会不会写到一个不存在的目录里
  · 资源是按包路径找的（图标）还是按开发现场的相对路径找的（打包后就没）
  · 中文文件名解压后会不会变乱码

这里的做法就是**模拟对方的动作**：解压 → 在一个全新的空目录里启动 → 等一会儿
→ 看 `logs/app.log` 里那几条关键日志在不在。**只看日志，不看窗口** ——
窗口长什么样是主观的，日志里那几个词是硬事实。

⚠️ 它**会真的启动一次界面**（会短暂出现悬浮窗和托盘图标），跑完自己关掉。
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(ROOT, "dist")
APP_NAME = "微信AI助手"

# 启动日志里**必须**出现的几条。少一条就说明那个功能在打包版里是坏的。
# ⚠️ 这些词是故意挑"只在真跑到那一步才打印"的，不是随便找的字符串。
MUST_HAVE = [
    ("界面已构建", "Qt 界面建起来了（PySide6 打进去了）"),
    ("悬浮窗已创建", "悬浮窗真的建了"),
    ("托盘图标已就绪", "系统托盘图标建了（QSystemTrayIcon 在打包版可用）"),
    ("发送者判定: 已启用", "cv2 + numpy + OCR 整条链在打包版里没坏"),
    ("OCR 引擎已加载", "rapidocr 的模型文件跟着进包了"),
    ("屏快照库", "sqlite 和 state 目录能写"),
]

# 出现这些就**直接判失败**：它们是"启动阶段就炸了"的典型。
MUST_NOT_HAVE = [
    "ImportError",
    "ModuleNotFoundError",
    "Traceback (most recent call last)",
    "发送者判定不可用",
    # ⚠️ 齿轮图标是**用户给的 SVG**，走 `paths.asset_path()`（`pkg_dir()`）；
    #    漏进包的话标题栏那颗会静默换成自绘齿轮 —— 不报错，只是"跟给你的图不一样"。
    "齿轮图标：用不了 SVG",
    "齿轮图标：渲染失败",
]

# 解压后**必须存在**的文件（相对解压根目录）。少一个 = 那个资源没进包。
MUST_EXIST = [
    (os.path.join(APP_NAME, "wechat_assistant", "assets", "gear.svg"),
     "设置按钮的齿轮图标（用户给的 SVG）"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=40, help="启动后观察多少秒")
    ap.add_argument("--zip", default=os.path.join(DIST, "%s.zip" % APP_NAME))
    ap.add_argument("--keep", action="store_true",
                    help="保留解压出来的那份（≈290MB，默认跑完就删）")
    args = ap.parse_args()

    if not os.path.isfile(args.zip):
        print("[verify] 找不到 %s，先跑 build_exe.bat + packaging/make_zip.py" % args.zip)
        return 1

    work = os.path.join(ROOT, ".tmp", "verify_zip")
    if os.path.isdir(work):
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)

    print("[verify] 解压到 %s" % work)
    with zipfile.ZipFile(args.zip) as z:
        z.extractall(work)

    exe = os.path.join(work, APP_NAME, "%s.exe" % APP_NAME)
    if not os.path.isfile(exe):
        print("[verify] **解压后找不到 exe**（中文文件名是不是乱了？）")
        for dirpath, _d, files in os.walk(work):
            for f in files[:5]:
                print("        ", os.path.relpath(os.path.join(dirpath, f), work))
            break
        return 1
    print("[verify] exe 在：%s" % os.path.relpath(exe, work))

    # 资源文件先查（比等日志快，而且能直接指认"哪个文件没进包"）
    _missing = [(rel, why) for rel, why in MUST_EXIST
                if not os.path.isfile(os.path.join(work, rel))]
    for rel, why in MUST_EXIST:
        print("[verify] %s %s（%s）"
              % ("有" if os.path.isfile(os.path.join(work, rel)) else "**缺**",
                 rel, why))
    if _missing:
        print("[verify] **有资源没进包**：%s" % ", ".join(r for r, _w in _missing))
        return 1

    # ⚠️⚠️ **先确认没有实例在跑**（2026-10-15 加，血泪）：
    #    单实例守卫是**按名字的互斥体**，而**源码版 `python run_watch.py` 和打包版
    #    用的是同一个名字** —— 于是"我还开着源码版"会让这次启动
    #    只做一件事：把那个窗口唤到前面，然后自己退出（退出码 0）。
    #    症状和"打包坏了、双击闪退"**一模一样**：下面每一条都是"缺"。
    #    实测连查三轮才发现是 `python -u run_watch.py` 还在跑。
    #    → 判据必须在**启动之前**问，事后从日志里看太绕。
    try:
        sys.path.insert(0, ROOT)
        from wechat_assistant import single_instance

        running = single_instance.is_running()
    except Exception as e:                                   # pragma: no cover
        running = None
        print("[verify] 单实例自查不可用（忽略）: %s: %s" % (type(e).__name__, e))
    if running:
        print("[verify] **已经有一个实例在跑**（源码版或打包版都算）——")
        print("[verify]    这次启动会去唤那个窗口、然后自己退出，"
              "下面每条都会判成「闪退」。")
        print("[verify]    先关掉它（源码版就是那个 `python run_watch.py`）再验收。")
        return 1
    if running is None:
        print("[verify] ⚠️ 判不出有没有实例在跑；若下面全是「缺」，先怀疑这个。")

    # ⚠️ 必须 cwd 设成它自己的目录 —— 这正是对方的做法（双击 exe）。
    #    从别处启动会掩盖"相对路径写错"这类问题。
    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
    print("[verify] 已启动 pid=%d，观察 %d 秒…" % (proc.pid, args.seconds))
    time.sleep(args.seconds)
    alive = proc.poll() is None
    if alive:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    print("[verify] 跑满观察期没自己退出：%s" % ("是" if alive else "**否（闪退了）**"))

    log = os.path.join(work, APP_NAME, "logs", "app.log")
    if not os.path.isfile(log):
        print("[verify] **没有 logs/app.log** —— 无控制台的打包版里，"
              "这一条就是「双击闪退且什么都看不到」的根因")
        return 1

    with open(log, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    # 兜底：万一那个实例是在**观察期内**才出现的（上面那问没拦住），
    # 日志里会有这一行。这时别打一堆"缺"，直接说清楚。
    if "已经有一个在跑了" in text:
        print("\n[verify] **这次启动没跑起来：它发现已经有一个实例在跑**")
        print("[verify]    单实例守卫会让新进程去唤那个窗口然后退出 —— "
              "这不是打包的问题，")
        print("[verify]    但要先关掉那个实例重新验收。")
        shutil.rmtree(work, ignore_errors=True)
        return 1

    print("\n[verify] 关键日志：")
    ok = True
    for needle, why in MUST_HAVE:
        hit = needle in text
        ok = ok and hit
        print("   [%s] %-18s %s" % ("有" if hit else "缺", needle, why))

    print("\n[verify] 不该出现的：")
    for needle in MUST_NOT_HAVE:
        hit = needle in text
        ok = ok and not hit
        print("   [%s] %s" % ("**出现了**" if hit else "干净", needle))

    print("\n[verify] 数据目录建在 exe 旁边：%s"
          % ("是" if os.path.isdir(os.path.join(work, APP_NAME, "state")) else "**否**"))

    print("\n[verify] %s" % ("全部通过 —— 这个包可以发出去" if ok else "**有问题，见上面**"))

    if args.keep:
        print("[verify] 日志全文：%s" % log)
        print("[verify] （--keep：解压出来的那份留在 %s）" % work)
    else:
        # 解压出来是 ≈290MB，跑完就删；要留证据就加 --keep。
        # 日志已经读进内存了，最后再打一遍尾部，删掉也不丢现场。
        if os.path.isfile(log):
            with open(log, "r", encoding="utf-8", errors="replace") as f:
                tail = [ln for ln in f.read().splitlines() if ln.strip()][-8:]
            print("[verify] 日志尾部：")
            for ln in tail:
                print("      " + ln)
        shutil.rmtree(work, ignore_errors=True)
        print("[verify] 已清理解压目录（想留着看就加 --keep）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
