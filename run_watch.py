# -*- coding: utf-8 -*-
"""入口：微信 AI 聊天辅助（只读）。

════════════════════════════════════════════════════════════════════
红线（本文件及整个项目都不得突破）
    - 不模拟键鼠、不点击微信、不自动复制粘贴、**不自动发送任何消息**
    - 不读写微信数据库、不注入进程、不 hook 网络
    - 只做「读窗口内容 + 展示建议」；复制、粘贴、发送**全部由用户手动完成**
════════════════════════════════════════════════════════════════════

两种运行模式（config.yaml 的 runtime.mode）：
    trigger = 不轮询，等你触发（热键 / 切会话 / 切到微信窗口）—— 默认
    watch   = 主动轮询，发现新消息就提醒

用法：
    python run_watch.py                 # 按配置启动
    python run_watch.py --once          # 只跑一轮轮询，调试用
    python run_watch.py --reset         # 清空状态重新开始
    python run_watch.py --topmost       # 窗口被遮挡时临时置顶截图（做发送者判定）
    python run_watch.py --no-ui         # 不开悬浮窗，只在命令行输出
    python run_watch.py --generate      # 触发后直接调 AI 生成（覆盖配置）
"""

import argparse
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _attach_frozen_log():
    """打包成**无控制台**的 exe 之后，把 print 接到 `logs/app.log`。

    ⚠️ 这不是锦上添花，是**打包版的启动前提**：
        无控制台的 exe（`console=False`）里 `sys.stdout` / `sys.stderr` 是 `None`，
        而本程序的日志**全靠 `print()`** —— 于是任何一句日志都会抛
        `AttributeError: 'NoneType' object has no attribute 'write'`，
        表现就是**双击 exe 闪一下就没了，连报错都看不到**。
        （顺带：`print()` 对 `None` 到底抛不抛异常，各版本不一致，别赌。）

    接到文件还有第二个理由：用户的任务栏旁边**没有控制台**，
    `logs/app.log` 是出问题时唯一能看的东西。

    源码运行时**什么都不做**（终端里的日志本来就看得见）。
    """
    if not getattr(sys, "frozen", False):
        return
    if sys.stdout is not None and sys.stderr is not None:
        return           # 打包成带控制台的版本时不用管

    stream = None
    try:
        from wechat_assistant import paths

        p = paths.log_path()
        paths.ensure_parent(p)
        # ⚠️ 追加而不是覆盖：否则每次重启都把上一次的现场抹掉，
        #    而"崩在启动阶段"恰恰是最需要看上一次日志的时候。
        stream = open(p, "a", encoding="utf-8", errors="replace", buffering=1)
        stream.write("\n%s 启动（pid=%d，%s）\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(),
                        sys.executable))
        stream.flush()
    except Exception:
        try:
            stream = open(os.devnull, "w")
        except Exception:
            class _Sink:
                def write(self, *_a):
                    return 0

                def flush(self):
                    pass

                def reconfigure(self, **_k):
                    pass
            stream = _Sink()

    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


_attach_frozen_log()


# ⚠️ 必须尽早把标准输出切成 UTF-8。
# 中文 Windows 上 stdout 默认是 GBK；会话名里只要出现 GBK 表示不了的字符
# （上标 ⁴、emoji、其他语种文字），print() 就会抛 UnicodeEncodeError 把程序打挂。
# 实测踩过：某群名 "yume玉(米)⁴¿"，启动阶段打印白名单时直接崩。
# errors="replace" 再兜一层，保证任何情况下都不会因编码问题中断。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ⚠️ DPI 感知必须在**创建任何窗口之前**设好，而且必须是**显式**的。
# 以前它是被意外获得的：uiautomation 库在被 import 时会自己设这个属性，
# 而项目里的它是在 uia_worker 的**函数内部**懒加载的 —— 于是悬浮窗先按 96 DPI
# 建好，之后 worker 一读 UIA 就把进程变成 DPI 感知，那个窗口随即被 Windows
# 按 1.5 倍位图拉伸，**文字明显发虚**（实测反馈过）。
# 现在的时序是确定的：进程一起来就是 DPI 感知。详见 wechat_assistant/dpi.py。
from wechat_assistant import dpi as _dpi

_dpi.enable()

from wechat_assistant import paths
from wechat_assistant.config import ConfigError, load_config, resolve_state_path
from wechat_assistant.context import (block_to_dicts, block_to_text, collect_default_block,
                                      collect_target_block, collect_visible_block,
                                      find_anchor, history_to_dicts,
                                      merge_with_store)
from wechat_assistant.reader import WeChatReader
from wechat_assistant.sessions import SessionRegistry, merge_known_sessions
from wechat_assistant.uia_worker import UiaError, UiaWorker
from wechat_assistant.watcher import Watcher
from wechat_assistant.window import (is_time_item, items_from_snapshot,
                                     to_prompt_dicts)


def make_logger(quiet=False):
    def log(msg):
        if not quiet:
            print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)
    return log


def start_follower_later(ui, follower, delay_ms: int = 200):
    """让贴边跟随在 UI 事件循环**跑起来之后**再启动。

    ⚠️ 两版界面用的定时器不一样（Tk 的 `after` / Qt 的 `QTimer`），
        但都是"事件循环不跑就不触发"的同一个道理。
        `follow.WindowFollower` 必须跑在 UI 线程上，因为它要调
        `ui.set_docked()` —— 跨线程碰控件是踩过的坑。

    ⚠️ Qt 版**不要**在这里用 `QTimer.singleShot(0, ...)`：那条 0ms 定时器
        会在 `ui.run()` 之前就绪、立刻执行，而这时窗口还没进事件循环。
    """
    if hasattr(ui, "root") and hasattr(ui.root, "after"):
        ui.root.after(delay_ms, follower.start)          # Tk 版
        return
    try:
        from PySide6 import QtCore

        QtCore.QTimer.singleShot(delay_ms, follower.start)   # Qt 版
    except Exception:
        follower.start()                                  # 兜底：直接起


def single_instance_guard(log) -> str:
    """确保同一次只有一个实例在跑。返回 **"first"** 或 **"existing"**。

    为什么要它（而不是靠 `start.bat` 去数进程）：
    残留实例**占着全局热键**，新实例只会打印"热键注册失败"然后静默地
    没有任何触发源 —— 非常隐蔽（实测踩过）。

    ⚠️ 不能用"有几个 python.exe"来判：本机上还跑着别的 Python 脚本，
        一看"有 python.exe"就拦是**误报**。
    ⚠️ 也不能用 `wmic process get commandline`：**本机没有 wmic**
        （Win11 已移除，实测 `process - Alias not found`），写了也永远不触发。
    正解是**有名互斥体**：由内核保证唯一，跨机器都可靠，也不依赖进程名。
    进程退出时内核自动释放，所以**不会**留下"上一次没收干净"的假阳性。

    ⚠️⚠️ **2026-10-13 起不再是"只警告、照样启动"**。用户要求：
        「打包后……如果已经打开了程序，就不要重新开一个，
        再次打开 exe 就是把原来的跳出来」。所以：
          · 不是第一个 -> `signal_primary()` 置位唤醒事件，然后**直接退出**
            （不建窗口、不占热键、不写库）；
          · 是第一个   -> 返回 "first"，由调用方在**界面建好之后**
            调 `single_instance.watch(...)` 等信号。
        机制和实测踩到的坑（为什么不能用 `OpenEventW` 等）见
        `wechat_assistant/single_instance.py` 的模块说明。
    """
    try:
        from wechat_assistant import single_instance

        if single_instance.is_primary(log):
            return "first"
        single_instance.signal_primary()
        return "existing"
    except Exception as e:
        # fail-open：宁可多开一个，也不要因为一个 API 失败就打不开程序。
        log("单实例检查失败（忽略，继续启动）: %s: %s" % (type(e).__name__, e))
        return "first"


def pump_ui(ui, ms: int = 1200):
    """在一段**有界**的事件循环里把界面队列排空，然后返回。

    ⚠️ 为什么需要它：`--demo` 是"触发一次就退出"，**不进 `run()` 的主循环**，
        而生成跑在子线程、结果通过 `post_replies()` 进队列 ——
        主线程这时还没排空过队列，消息一条都没被取走。
        不主动排空的话 `--demo` 只在命令行打印，窗口里空着
        （「验证链路用」的那条路自己看不见结果，等于没验证）。
    """
    if ui is None:
        return
    try:
        if hasattr(ui, "pump_now"):
            ui.pump_now(ms)                     # Qt 版（有界嵌套事件循环）
            return
        # Tk 版：after 排空循环就是靠 mainloop 驱动的，这里手工转够时间
        end = time.time() + ms / 1000.0
        while time.time() < end:
            ui.root.update()
            time.sleep(0.01)
    except Exception as e:
        print("[pump] 排空界面队列失败: %s: %s" % (type(e).__name__, e), flush=True)


def get_persona(cfg, session_key: str) -> str:
    """会话级人设优先，其次全局默认。"""
    per = cfg.get("persona.per_session", {}) or {}
    if isinstance(per, dict) and per.get(session_key):
        return per[session_key]
    return cfg.get("persona.default", "") or ""


def get_relationship(cfg, session_key: str) -> str:
    """你们的关系：会话级覆盖优先，其次全局默认。影响称呼、语气和分寸。"""
    per = cfg.get("persona.relationships", {}) or {}
    if isinstance(per, dict) and per.get(session_key):
        return per[session_key]
    return cfg.get("persona.default_relationship", "") or ""


def install_sigint_handler(ui, log):
    """让 Ctrl+C **真的能退出**。

    ⚠️ 为什么原来不行（2026-10-10 用户实测贴出来的 traceback）：

        在控制台按 Ctrl+C 时，Python 会在**下一个字节码边界**抛
        `KeyboardInterrupt`。而这个程序的主线程正卡在 Qt 的 C++ 事件循环里 ——
        那个边界落在**某个 Qt 槽函数内部**（用户那次是 `follow._tick`，
        跟随器 16ms 一拍，最容易中）：

            File "wechat_assistant/follow.py", line 449, in _tick
                def _tick(self):
            KeyboardInterrupt

        ⚠️ **PySide 不会让槽里抛出的异常穿出 C++ 层** —— 它交给
        `sys.excepthook` 打印一下，然后**继续跑事件循环**。后果三条：
            · 屏幕上是一段吓人的 traceback
            · 外层那句 `except KeyboardInterrupt` **永远等不到**
            · 程序**不退出**，`finally` 里的清理（停跟随/记录/触发、
              存注册表、拆托盘）**一条都不跑** —— 会留下僵尸托盘图标
              和占着全局热键的残留进程
        也就是说 `start.bat` 里写的「Ctrl+C quit」**是假的**。

    修法：装一个 SIGINT 处理器，**它自己不抛异常**，只做退出动作。
        Python 仍然只在字节码边界跑处理器，但槽函数每秒被调很多次
        （心跳 `_pump` + 跟随 16ms），所以最多迟一拍就生效。

    ⚠️ `--no-ui` 那条路主循环是**纯 Python** 的 `while True: sleep(1)`，
        没有 C++ 边界，所以那里**照旧抛 `KeyboardInterrupt`** 让外层接住 ——
        两种情况的处理**必须分开**，别统一成抛异常（那就又踩回原坑）。
    """
    import signal

    def _on_sigint(signum, frame):
        if ui is None:
            # 纯 Python 主循环：抛出去，让 main() 里那句 except 接住
            raise KeyboardInterrupt
        log("收到 Ctrl+C，正在退出…")
        try:
            ui.stop()          # 让 Qt 主循环退出；真正的清理在 main() 的 finally 里
        except Exception as e:
            log("退出时出错（不影响退出）: %s: %s" % (type(e).__name__, e))

    for name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_sigint)
        except Exception as e:
            log("注册 %s 处理器失败（Ctrl+C 可能不生效）: %s: %s"
                % (name, type(e).__name__, e))


def main():
    ap = argparse.ArgumentParser(description="微信 AI 聊天辅助（只读，不自动发送）")
    ap.add_argument("--config", default=None, help="配置文件路径")
    ap.add_argument("--once", action="store_true", help="只跑一轮轮询就退出")
    ap.add_argument("--reset", action="store_true", help="清空状态文件后重新开始")
    ap.add_argument("--topmost", action="store_true",
                    help="窗口被遮挡时临时置顶截图以完成发送者判定（会短暂打扰）")
    ap.add_argument("--no-ui", action="store_true", help="不开悬浮窗，只在命令行输出")
    ap.add_argument("--quiet", action="store_true", help="静音命令行日志")
    ap.add_argument("--generate", action="store_true", help="触发后直接调 AI 生成")
    ap.add_argument("--demo", action="store_true",
                    help="启动后立即触发一次并退出前的处理（用于验证链路）")
    ap.add_argument("--force", action="store_true",
                    help="（已默认生效，保留仅为兼容旧命令）")
    ap.add_argument("--allow-multi", action="store_true",
                    help="允许多开（默认第二个实例会把已开的那个唤到前面并退出）")
    args = ap.parse_args()

    log = make_logger(quiet=args.quiet)

    # ⚠️ 单实例检查放在**最前面**：残留实例占着全局热键，晚一步提示的话
    #    用户会先看到"热键注册失败"再看到原因，容易以为是热键坏了。
    #
    # ⚠️⚠️ 2026-10-13：**已经在跑就不再开第二个**（用户要求：再次双击 exe
    #    应该"把原来的跳出来"）。这里只负责"通知 + 退出"，
    #    等信号的那一头在下面界面建好之后（`single_instance.watch`）。
    if not args.allow_multi and single_instance_guard(log) == "existing":
        log("已经有一个在跑了 —— 已把它唤到前面，这次不重复启动。"
            "（确实想多开：加 --allow-multi）")
        return 0

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print("配置错误: %s" % e)
        return 2

    state_path = resolve_state_path(cfg)
    if args.reset and os.path.exists(state_path):
        os.remove(state_path)
        log("已清空状态文件: %s" % state_path)

    auto_generate = bool(args.generate or cfg.get("trigger.auto_generate", False))

    # ---------- 会话表
    registry = SessionRegistry(
        state_path=state_path,
        bootstrap_whitelist=cfg.get("sessions.bootstrap_whitelist", []) or [],
        default_mode=cfg.get("sessions.default_mode", "off"),
        disabled_sessions=cfg.get("sessions.disabled", []) or [],
        # 系统会话（公众号 / 微信支付 / 服务通知 …）—— 关键词**子串**匹配。
        # 这类东西不是聊天，但微信里长得一模一样（有标题栏、有内容区），
        # 所以像素这条路照样会把它们读成会话、把内容读成消息 —— 只能按名字挡。
        disabled_contains=cfg.get("sessions.disabled_contains", []) or [],
        # 只差 emoji/符号的会话名当成同一个会话 —— 不然换一次读取后端
        # （UIA 读控件 Name 是原样的、OCR 认字形认不出 emoji）就会把
        # 同一个会话的历史劈成两半（见 sessions.norm_title 的说明）。
        merge_variants=bool(cfg.get("sessions.merge_variants", True)),
        logger=log,
    )

    # ---------- 读取后端（UIA / OCR / 自动降级）
    #
    # ⚠️ **不要在这里直接 `WeChatReader(...)`** —— UIA 读不到的环境真实存在
    #    （实测是「机器 × 账号」的交互），降级逻辑在 `make_reader` 里。
    #    走 UIA 才需要 worker；ocr 后端下连 UIA 线程都不该建
    #    （建了就会去 ComInitialize + 加载 uiautomation，而那条路根本不用）。
    _backend = str(cfg.get("reader.backend", "auto") or "auto").strip().lower()
    worker = None
    if _backend != "ocr":
        try:
            worker = UiaWorker(timeout=float(cfg.get("runtime.uia_timeout", 20.0)),
                               logger=log)
        except UiaError as e:
            if _backend == "uia":
                log("UIA 初始化失败: %s" % e)
                return 3
            log("UIA 初始化失败: %s —— 改用「截屏 + OCR」后端" % e)
    from wechat_assistant.ocr_reader import make_reader

    reader = make_reader(cfg, logger=log, worker=worker)

    def close_worker():
        """收尾时关掉 UIA 工作线程 —— **OCR 后端下它根本不存在**。

        ⚠️ 2026-09-27 第四十七轮实测抓到的崩溃：
            `backend: ocr` 时 `worker` 是 None（刻意的：那条路不该建 UIA 线程），
            而 `main()` 里有**四处** `worker.close()`，包括**正常退出**那一处 ——
            于是退出程序会抛
                AttributeError: 'NoneType' object has no attribute 'close'
            `--demo` / `--once` 一样。⚠️ 而且它是在**收尾阶段**抛的，
            会把它后面的清理（`trigger.stop()` 等）**整段跳过**。

        所以关 worker 一律走这里，**别在别处直接写 `worker.close()`**。
        ⚠️ 也别用编辑器"全部替换"把 `worker.close()` 换成 `close_worker()` ——
           那会把**本函数体内那一行**也换掉，变成自己调自己（无限递归）。
           我就这么干过一次。
        """
        if worker is None:
            return
        try:
            worker.close()
        except Exception as e:
            log("关闭 UIA 工作线程时出错（不影响退出）: %s: %s" % (type(e).__name__, e))
    _bname = type(reader).__name__
    log("读取后端: %s（配置 reader.backend=%s）" % (_bname, _backend))
    if _backend == "ocr":
        log("   纯 OCR：不读控件树，只从窗口像素认内容；上下文只有当前一屏。")

    # ---------- 发送者判定
    sender = None
    if cfg.get("sender.enabled", True):
        try:
            # ⚠️ **走 `get_engine()` 而不是自己 `OcrEngine(...)`。**
            #
            # `ocr.py` 里那个 `get_engine()` 是**进程内单例**，设计意图就是
            # "引擎绝不能每次 OCR 都重建（重建要 1~2 秒）"。
            # 而 `ocr_reader.OcrReader` 用的正是 `get_engine()` ——
            # 这里要是自己 new 一个，OCR 模式下就会存在**两份引擎**：
            # 实测日志里连续两行「OCR 引擎已加载（690 ms）」和「（634 ms）」，
            # 也就是**多花 1.3 秒启动 + 双份 15MB 模型的常驻内存**。
            # （UIA 模式下看不出来：那条路根本不建第二个。）
            from wechat_assistant.ocr import get_engine
            from wechat_assistant.sender import SenderResolver

            ocr = get_engine({
                "intra_op_num_threads": cfg.get("ocr.intra_op_num_threads", 4),
                "inter_op_num_threads": cfg.get("ocr.inter_op_num_threads", 1),
                "limit_side_len": cfg.get("ocr.limit_side_len", 736),
                "limit_type": cfg.get("ocr.limit_type", "max"),
                "use_cls": cfg.get("ocr.use_cls", False),
            }, logger=log)
            ocr.load()
            if cfg.get("ocr.warmup", True):
                ocr.warmup()
            sender = SenderResolver(cfg, ocr, logger=log)
            if args.topmost:
                sender.auto_topmost = True
            _mode = str(cfg.get("sender.capture_mode", "printwindow")).strip().lower()
            if _mode == "printwindow":
                log("发送者判定: 已启用（PrintWindow 取图，不受窗口遮挡影响；"
                    "群聊另按行高筛 + OCR 读昵称）")
            else:
                log("发送者判定: 已启用（screen 模式屏幕截图，需微信窗口可见且不被遮挡）")
        except Exception as e:
            log("发送者判定不可用，将跳过: %s: %s" % (type(e).__name__, e))

    # ---------- AI
    ai = None
    try:
        from wechat_assistant.ai import AIClient

        ai = AIClient(cfg, logger=log)
        if ai.available:
            log("AI 接口: %s  模型=%s" % (ai.base_url, ai.model))
        else:
            # ⚠️ 2026-10-13：接口地址/模型也可能是空的（出厂不再预填），
            #    所以要点名**缺哪几项**，不然用户只补 Key 还是调不通。
            log("AI 接口: 还没配置 %s，将只读取不生成（在设置 → AI 接口里填）"
                % "、".join(ai.missing))
    except Exception as e:
        log("AI 模块不可用: %s: %s" % (type(e).__name__, e))

    # ---------- 历史库（SQLite）：累积上下文用，纯本地，不花 token
    store = None
    if cfg.get("history.enabled", True):
        try:
            from wechat_assistant.store import MessageStore

            db = paths.resolve(cfg.get("history.path", "state/history.db"))
            store = MessageStore(db,
                                 max_per_session=int(cfg.get("history.max_per_session", 500)),
                                 logger=log)
            total = sum(s["n"] for s in store.stats())
            log("历史库: %s（已存 %d 条，覆盖 %d 个会话）" % (db, total, len(store.stats())))
        except Exception as e:
            log("历史库不可用，本次不累积: %s: %s" % (type(e).__name__, e))
            store = None

    # ---------- 屏快照库（新）：接管「上下文的顺序」
    #
    # 和上面的 messages 表分工：
    #   screens  = 一屏一条的观测，读取时才缝合出顺序（用户翻历史也能被记下来）
    #   messages = 继续服务口吻样本（own_style_texts）/ AI 自我污染标记 / 清空
    #
    # 两套并行写，代价极小（一屏一条 insert），但出问题随时能退回旧路径：
    #   把 record.backend 改成 "messages" 即可。
    screens = None
    backend = (cfg.get("record.backend", "screens") or "screens").strip().lower()
    if backend == "screens" and cfg.get("history.enabled", True):
        try:
            from wechat_assistant.window import ScreenStore

            sdb = paths.resolve(cfg.get("history.screens_path", "state/screens.db"))
            screens = ScreenStore(sdb,
                                  keep=int(cfg.get("record.keep_screens", 40)),
                                  logger=log)
            n = len(screens.stats())
            log("屏快照库: %s（覆盖 %d 个会话）" % (sdb, n))
        except Exception as e:
            log("屏快照库不可用，退回 messages 顺序: %s: %s" % (type(e).__name__, e))
            screens = None

    # ---------- 静默记录：把对话记全（**只写库，不调 AI、不提醒**）
    #
    # 为什么要有它：在此之前 save_messages() 只在 do_generate() 里调用，
    # 也就是「只有你按热键才存一次快照」—— 两次触发之间的对话会丢，
    # 顺序也变成「按你按热键的顺序」。连续记录把快照之间的洞补上。
    recorder = None
    if store is not None or screens is not None:
        try:
            from wechat_assistant.recorder import Recorder

            recorder = Recorder(cfg, reader, store, registry, sender=sender,
                                logger=log, screens=screens)
        except Exception as e:
            log("静默记录不可用: %s: %s" % (type(e).__name__, e))
            recorder = None

    # ---------- 找窗口（自检）
    win = reader.find_window()
    if not win:
        log("找不到微信主窗口。请确认微信已启动并登录。")
        close_worker()
        return 4
    hwnd = win["hwnd"]
    log("找到微信窗口: hwnd=%s pid=%d" % (hex(hwnd), win["pid"]))

    # ---------- 悬浮窗
    ui = None
    follower = None

    def on_mode_change(key, mode):
        """会话面板里改了模式 —— 写回注册表并落盘。

        `set_mode` 还会标记 `enabled_by="user"`，之后改 `default_mode`
        不会再把这个会话覆盖掉（见 sessions.py）。
        """
        registry.set_mode(key, str(mode))
        registry.save()
        log("会话「%s」的模式改为 %s（已生效，不用重启）" % (key, mode))

    def on_config_changed(new_cfg):
        """设置窗口保存后，把新配置接过来并**重读那些启动时缓存过的值**。

        ⚠️ 之前这里没有：`AIClient` 在构造时把 `ai.model` / `runtime.max_chars`
        这些读进实例属性了，所以设置窗口里改完「已保存」，生成时却还用旧的，
        要重启才认 —— 界面上完全看不出来。现在重建它，改完立刻生效。

        还有些东西**构造时就定死了**（记录节奏、历史库容量、注册表名单），
        那些在 SPEC 里标了「需重启」，只能重启。
        """
        nonlocal cfg, ai, follower
        cfg = new_cfg
        # ⚠️ **跟随器也要换配置对象**。它每拍读的是构造时传进去那个对象，
        #    而这里 `cfg = new_cfg` 是**换了个新对象**（不是原地改）。
        #    不换的话"设置里改成固定悬浮/贴边"完全没反应，而且不报错。
        if follower is not None:
            try:
                follower.use_config(cfg)
            except Exception as e:
                log("跟随器换配置失败: %s: %s" % (type(e).__name__, e))
        try:
            ai = AIClient(cfg, logger=log)
            log("配置已重新加载（AI 参数、上下文长度、会话级人设/关系立即生效）")
        except Exception as e:
            log("重建 AI 客户端失败（沿用旧的）: %s: %s" % (type(e).__name__, e))

    if not args.no_ui:
        # 界面只有一套：`wechat_assistant/qt_ui/`（PySide6，无边框 + 真圆角抗锯齿）。
        # ⚠️ 2026-09-26 起 **Tk 版已经删掉了**。之前是两套并存、靠 `ui.backend`
        #    切换（qt 默认 / tk 兜底）。删掉的理由：两套的观感与尺寸各自在漂，
        #    留着只会让人分不清"现在跑的到底是哪套"（用户就问过这个问题）。
        #    要回退请回退到本提交之前的版本，**不要再在代码里留第二套 UI**。

        # ---------- 「会话」列表的**名单**：注册表 ∪ 屏快照库
        #
        # ⚠️⚠️ 为什么要并上库（2026-10-11 用户报「重新启动程序后设置中的会话历史会不见」）：
        #
        #     注册表（`state.json`）**只在正常退出时**才落盘。实测证据：那次
        #     `state.json` 最后写入 21:35，而 `screens.db` 里的记录一直到 23:18
        #     —— 说明那一轮**没走到保存就被结束了**，于是它发现的十来个会话全丢，
        #     重启后设置里只剩上次干净退出时的 1 个（库里其实有 16 个）。
        #
        #     `screens.db` 是**连续写**的，它一直知道有哪些会话 ——
        #     并进来之后，列表就**自愈**了：注册表整个丢了也不怕。
        #
        # ⚠️ 补进来的是**临时条目**（`from_db`），不会写回注册表。
        #    只有当用户在面板上真的改了模式，`on_mode_change` -> `registry.set_mode()`
        #    才会把它建成正式条目（那边是 `setdefault`，不会覆盖已有的）。
        # ⚠️ 这里只用 `last_seen_map()` 的**键**（有没有这个会话）；时间那份由
        #    `session_seen` 负责，两边各算一次是有意的（那边还要合注册表的时间）。
        #
        # ⚠️⚠️ **这个函数必须定义在 `FloatingAssistant(...)` 之前**（2026-10-11 血案）：
        #    我第一版把它写在了**下面**（和 `session_seen` 挨着），于是
        #    `session_source=_session_source` 求值时 `NameError` ——
        #    而那一整段被 `except Exception` 包着，异常被吞成
        #    「悬浮窗不可用（改用命令行输出）」→ **悬浮窗根本不出现**，
        #    看起来像"界面坏了"，其实是**代码顺序错了**。
        #    ⚠️ 这个项目在 `except` 吞异常这条上已经栽过好几次了。
        def _session_source():
            """注册表里的会话 ∪ 屏快照库里的会话（合并规则见那个纯函数）。"""
            db_names = {}
            try:
                if screens is not None:
                    db_names = screens.last_seen_map() or {}
            except Exception as e:
                log("读屏快照库的会话名单失败: %s: %s" % (type(e).__name__, e))
            return merge_known_sessions(registry.sessions, db_names,
                                       registry.default_mode)

        ui = None
        try:
            from wechat_assistant.qt_ui.panel import FloatingAssistant

            ui = FloatingAssistant(
                cfg, logger=log,
                # ⚠️⚠️ **注册表 ∪ 屏快照库**，不是只有注册表（2026-10-11 修）。
                #    见上面那段说明。
                session_source=_session_source,
                on_mode_change=on_mode_change,
                on_config_changed=on_config_changed,
            )
        except Exception as e:
            log("悬浮窗不可用（改用命令行输出）: %s: %s" % (type(e).__name__, e))
            # ⚠️⚠️ **这一类异常很容易是代码 bug，不是环境问题**（2026-10-11 血案）：
            #    我把 `_session_source` 的定义写在了这一行**之后**，于是
            #    `NameError` 被这里吞掉 —— 用户看到的症状是「**悬浮窗没出现**」，
            #    日志里只有一行「悬浮窗不可用」，看起来像界面/环境问题，
            #    实际是**代码顺序错了**。
            #    → 所以对"像代码 bug"的异常**把调用栈打出来**，
            #      别让下一个人再花时间往环境方向查。
            if isinstance(e, (NameError, AttributeError, TypeError,
                              ImportError, KeyError)):
                log("⚠️ 这更像**代码问题**而不是环境问题，调用栈：\n%s"
                    % traceback.format_exc())
            ui = None

    # ---------- 单实例：用户又双击了一次 exe -> 把**这个**窗口跳出来
    #
    # 用户要求（2026-10-13）：「打包后让用户使用时，如果已经打开了程序，
    # 就不要重新开一个，再次打开 exe 就是把原来的跳出来」。
    # ⚠️ 必须挂在**界面建好之后**：信号来了得有人接，太早注册的话
    #    用户双击的那一刻窗口还没建出来，这次唤醒就白费了
    #    （那第二次双击看起来"没反应"）。
    # ⚠️ `wake_from_second_launch()` 自己会把动作排进 UI 线程 ——
    #    等待线程是子线程，**不能直接碰控件**。
    if ui is not None and not args.allow_multi:
        try:
            from wechat_assistant import single_instance

            single_instance.watch(ui.wake_from_second_launch, log)
        except Exception as e:
            log("单实例唤醒挂不上（第二次双击 exe 不会跳窗口）: %s: %s"
                % (type(e).__name__, e))

    # ---------- 让标题栏跟着「当前打开的会话」走
    #
    # ⚠️ 这**不额外花任何开销**：记录器每 2 秒本来就在读会话名，
    #    这里只是让它顺手把"会话变了"报给界面。
    # 以前标题栏只在**按热键之后**才更新，你切了会话它还是旧的（实测反馈）。
    if recorder is not None and ui is not None:
        recorder.on_session = lambda title, is_group: ui.post_session(title, is_group)

    # ⚠️ `_session_source`（名单）**定义在上面 `FloatingAssistant(...)` 之前** ——
    #    定义在使用之后会 `NameError`，而且被 `except` 吞掉 → 悬浮窗不出现。
    #    见那边的注释。

    # ---------- 「会话」列表按时间排的时间表
    #
    # ⚠️ 为什么不用微信左栏的顺序：那个列表是**虚拟列表**，UIA 只给**渲染出来的**
    #    那几项（实测 19 个会话只读得到 8 个），而且它按的是"会话活动时间"
    #    （点开也会往上顶），跟列表里显示的那个时间**不是一回事**。
    #    所以用**本地记录的时间**：
    #      ① 屏快照库每个会话的 `MAX(seen_at)`  —— 覆盖到的大部分会话（实测 13/19）
    #      ② 注册表里"刚刚打开过"的那个        —— 补上还没写进库的那些
    #    两个来源的时间都是 `"YYYY-MM-DD HH:MM:SS"` 字符串，取**较大的那个**（更新的）。
    def session_seen():
        out = {}
        try:
            if screens is not None:
                out.update(screens.last_seen_map())
        except Exception as e:
            log("读屏快照时间失败: %s: %s" % (type(e).__name__, e))
        try:
            for k, v in registry.sessions.items():
                t = (v or {}).get("last_seen_at")
                if t and t > out.get(k, ""):
                    out[k] = t
        except Exception as e:
            log("读会话时间失败: %s: %s" % (type(e).__name__, e))
        return out

    if ui is not None:
        ui._session_seen_source = session_seen
        # 「刚刚打开过」也要记一笔：记录器每切一次会话会回调一次
        # （在 mode 判断**之前**，所以 off 的会话也记得到）。
        if recorder is not None:
            _prev_on_session = recorder.on_session

            def _on_session(title, is_group):
                try:
                    # ⚠️⚠️ **登记（`discover`）不在这里做了 —— 挪进 `Recorder` 每拍数**
                    #    （2026-10-13 修的真 bug）。
                    #
                    #    `discover()` 是「登记一个新会话」，而"两次确认"要求
                    #    **同一个标题在不同的拍里看到两次**。可这个回调由
                    #    `_notify_session` 发出，它**只在标题变了的那一拍**才发 ——
                    #    也就是说一共只数过一拍：**除非你在 10 秒内切走再切回来，
                    #    任何会话都登记不上**。
                    #
                    #    实测后果（查库）：`state.json` 里只有 2 个会话，
                    #    而 `screens.db` / `history.db` 里有 15 个。
                    #    用户早就报过「设置里的会话列表长不出来」，根因就是这一层。
                    #
                    #    现在登记和"只给确认过的会话写库"都在
                    #    `Recorder._register_session()` 里，每拍都会数一次。
                    #    这里只负责「**刚刚打开过它**」这件事 —— 它天生就是
                    #    "在切换会话的那一刻"记一笔（设置面板按时间排要用）。
                    registry.touch(title)
                except Exception as e:
                    log("记会话时间失败: %s: %s" % (type(e).__name__, e))
                if _prev_on_session:
                    _prev_on_session(title, is_group)

            recorder.on_session = _on_session

    # ---------- 贴边跟随 / 固定悬浮（面板跟微信窗口的关系）
    #
    # ⚠️ 它**只读**微信窗口的位置（GetWindowRect / IsIconic），
    #    不动微信任何状态。理由见 follow.py 的模块说明。
    # ⚠️ **`off` 也要建**（2026-09-27 改）。原来只在启动时 `attach == "dock"`
    #    才建，于是「启动时是浮动、后来在设置里改成贴边」会**完全没有跟随器** ——
    #    设置改了、界面也显示保存了，但什么都不会发生，且不报错。
    #    现在跟随器自己按 `ui.attach` 分派（dock / float / off），
    #    `off` 只是每拍立刻返回，开销可以忽略。
    if ui is not None:
        try:
            from wechat_assistant.follow import WindowFollower

            follower = WindowFollower(ui, cfg, logger=log)
        except Exception as e:
            log("跟随器不可用（面板位置将完全由你自己控制）: %s: %s"
                % (type(e).__name__, e))
            follower = None

    # ---------- 触发处理
    gen_lock = threading.Lock()
    state = {"busy": False, "last_copy_ts": 0.0}

    def emit(msg):
        if ui is not None:
            ui.post_status(msg)
        else:
            log(msg)

    def show_replies(session, replies, error="", intent=""):
        if ui is not None:
            ui.post_replies(replies, error, intent)
        else:
            if error:
                log("生成失败: %s" % error)
            if intent:
                log("  判断: %s" % intent)
            for i, r in enumerate(replies, 1):
                log("  %d. %s" % (i, r))

    def do_generate(reason, want_generate=True, user_intent=""):
        """读取当前会话并（可选）生成建议。跑在独立线程，避免阻塞触发用的消息循环。

        user_intent：「我」自己想表达的意思（悬浮窗「我想说」里填的）。
        非空时 AI 严格按它写，不再自己猜立场 —— 见 ai.py 的
        【最高优先级 ·「我」想表达的意思】。
        """
        with gen_lock:
            if state["busy"]:
                emit("上一条还没处理完")
                return
            state["busy"] = True
        if ui is not None:
            # 用户明确要求生成（热键 / 按钮）—— 顺手把窗口唤醒到最前。
            # ⚠️ 无边框窗口**没有任务栏条目、alt-tab 也切不到**，一旦沉到别的
            #    窗口后面就找不回来了（实测踩过：窗口其实还在，但用户以为它没了）。
            #    所以凡是「明确意图」的入口都要带一次 wake()。
            ui.wake()
            ui.set_busy(True)
        try:
            emit("正在读取会话…")
            snap = reader.read_chat(hwnd)
            if not snap:
                # ⚠️ 区分「没打开会话」和「读出来了但什么都认不到」——
                #    两者在界面上长得一模一样，但该做的事完全不同。
                if getattr(reader, "geom_bad", False):
                    # ⚠️ 2026-10-13：原来这句直接把**探针命令**写给用户看
                    #    （"跑 probes\probe_ocr_read.py --calibrate"）。
                    #    那是给维护者的排查手段；用户能做的只有两件事 ——
                    #    把窗口拉宽一点，或者换个「读取方式」。状态行也别太长。
                    emit("读不到内容，拉宽微信窗口或换「读取方式」")
                else:
                    emit("当前没有打开的会话")
                return
            if not snap.title:
                emit("认不出当前会话名")
                return

            sess = snap.title
            if sender is not None and snap.messages:
                sender.resolve(hwnd, win["pid"], snap)

            enabled = registry.is_enabled(sess)
            if ui is not None:
                ui.post_session(sess, snap.is_group,
                                "" if enabled else "⚠ 不在白名单，仅预览不生成")

            # ---- 把这次读到的消息存进历史库（累积上下文；存储本身不花 token）
            #
            # ① 屏快照（新）：无条件写一屏，顺序留到读取时缝。
            # ② messages（旧）：口吻样本 / AI 标记 / 清空还靠它。
            if screens is not None and snap.messages:
                try:
                    screens.append_screen(registry.key_for(sess),
                                          items_from_snapshot(snap),
                                          snap.at_bottom())
                except Exception as e:
                    log("屏快照写入失败: %s: %s" % (type(e).__name__, e))
            #
            # 用 save_new_only：由它靠「和库里已有的重叠点」判断哪几条是新的，
            # **与滚动位置无关**。所以往上翻历史时按热键也不会把旧消息接到库尾 ——
            # 那些消息已经在库里，重叠点就是可见的最后一条，自然什么都不写。
            if store is not None and snap.messages:
                added = store.save_new_only(registry.key_for(sess), snap.messages,
                                            at_bottom=snap.at_bottom())
                if added:
                    log("历史库 +%d 条（该会话共 %d 条）"
                        % (added, store.count(registry.key_for(sess))))

            # ---- 决定「这次回复什么」
            #
            # 默认（`reply.use_cursor_anchor: false`）：把**当前屏幕上的全部对话**
            # 当成回复目标，**不用鼠标锚点**。理由见 context.collect_visible_block：
            # 鼠标位置不可靠（实测抓到过我们自己的「看提示词」窗口里的文字，
            # 把提示词全文当成了对方的消息，一次调用贵 3 倍），
            # 而且"先移鼠标再按热键"平白多一个动作。
            # 想看旧行为就把 `reply.use_cursor_anchor` 打开。
            anchor_idx = None
            if cfg.get("reply.use_cursor_anchor", False):
                try:
                    import win32api

                    anchor_idx = find_anchor(snap, win32api.GetCursorPos())
                except Exception:
                    anchor_idx = None

            if anchor_idx is not None:
                block, visible_hist, _ = collect_target_block(
                    snap, anchor_idx, int(cfg.get("reply.target_max_block", 8)))
                log("鼠标锚点：第 %d 条「%s」，目标块 %d 条"
                    % (anchor_idx, (snap.messages[anchor_idx].text or "")[:20], len(block)))
                if block and block[0].is_self is True:
                    emit("这条是你自己发的")
                    show_replies(sess, [])
                    return
            else:
                block, visible_hist, why = collect_visible_block(snap)
                log("回复目标：%s（%d 条）" % (why, len(block) if block else 0))
                # ⚠️ 这里原来会**拦截**：「你正在往上翻历史消息，已跳过生成」。
                #    那是**鼠标锚点时代**的遗留 —— 当时目标是"可见的最后一条"，
                #    所以必须确认你真在底部，否则那一条不是最新的。
                #    现在改成"用当前屏幕上的全部对话"当目标，前提就不成立了：
                #    你翻到哪，它就看你翻到的那段。
                #
                #    而且它原来那句提示是"把鼠标停到要回复的那条上再按一次" ——
                #    鼠标锚点已经删掉，**那句话现在指向一个不存在的操作**，
                #    会让人以为功能坏了。
                #
                #    改成**只提示、不拦截**：翻历史时确实可能没看到下面更新的消息，
                #    这是个值得知道的事实，但不是该被拦下的理由。
                if not snap.at_bottom() and block:
                    log("注意：你在往上翻历史（超出底部 %d px），"
                        "将按当前屏幕这段内容生成"
                        % max(0, -snap.bottom_gap()))
                if block is None:
                    # ⚠️ 2026-10-13 起这里**基本不会走到**：`collect_visible_block`
                    #    不再因为“最后一条是自己发的”返回 None
                    #    （用户要求：「不要拦截，毕站也可以跟着自己的话说」）。
                    #    留着是**兜底**（万一有别的取法返回 None）。
                    if args.force and visible_hist:
                        block = [visible_hist[-1]]
                        visible_hist = visible_hist[:-1]
                    else:
                        emit("最后一条是你自己发的")
                        show_replies(sess, [])
                        return

            if not block:
                # ⚠️ 2026-10-13 精简（用户要求"悬浮窗下面的提示精简一点"）。
                #    原来那句还带着"（公众号/服务号这类页面本身就没有聊天消息…）"
                #    —— 那是对**为什么**的解释，塞在状态行里太长（状态行一长
                #    就会把面板顶宽，见 panel.lbl_status）。用户只需要知道结论。
                emit("「%s」这里没有可读的消息" % sess)
                show_replies(sess, [])
                return

            # ---- 组装上下文
            #
            # 两条路：
            #   screens（新）: 由 stitch() 把最近若干屏缝成一条按对话顺序的序列。
            #                  用户往上翻历史看到的内容也在里面（旧路径会整屏丢掉），
            #                  因为写入时不做任何判断。
            #   messages（旧）: 可见历史 + 库里最近 N 条，按重叠点合并。
            max_ctx = ai.max_messages if ai is not None else 20
            history = None
            if screens is not None:
                key = registry.key_for(sess)
                items, gap = screens.stitch(
                    key,
                    max_items=int(cfg.get("reply.history_messages", 40)),
                    max_screens=int(cfg.get("record.keep_screens", 40)),
                    cut_at_date=bool(cfg.get("reply.cut_at_date", True)))
                if items:
                    history = to_prompt_dicts(items)
                    # 把「回复目标」那几条从历史里去掉：它们在 prompt 里另有位置，
                    # 留着会重复一遍（block 就是当前屏幕上的对话）
                    try:
                        first = block_to_dicts(block)[0]["text"]
                        for i in range(len(history) - 1, -1, -1):
                            if history[i]["text"] == first:
                                history = history[:i]
                                break
                    except Exception:
                        pass
                    if gap:
                        log("屏快照：缝合时有屏接不上（中间可能有没看到的内容）")
                    # 切段是**有意为之**，别让它静默发生 —— 用户得知道
                    # "为什么这次的历史比上次短"
                    if items and is_time_item(items[0]) and items[0].get("date"):
                        log("屏快照：按跨天分隔「%s」切段，更早的内容没进上下文"
                            % items[0].get("text"))
            if history is None:
                visible_history = history_to_dicts(visible_hist)
                stored = []
                if store is not None:
                    stored = [{"who": ("我" if r.get("is_self") == 1 else
                                       (r.get("sender_name") or ("对方" if r.get("is_self") == 0 else "?"))),
                               "text": r.get("content") or ""}
                              for r in store.get_recent(registry.key_for(sess),
                                                        int(cfg.get("reply.history_messages", 40)))]
                history = merge_with_store(visible_history, stored, max_ctx)
            else:
                history = history[-max_ctx:]

            current_text = "\n".join(
                "%s: %s" % (m.get("who"), m.get("text")) for m in block_to_dicts(block))
            target_preview = block_to_text(block)

            # 把「程序理解的目标」交给悬浮窗显示 —— 取错了一眼能发现
            if ui is not None:
                ui.post_target(target_preview)
            emit("待回复 %d 条：%s" % (len(block), (block[-1].text or "")[:30]))

            if not enabled:
                # ⚠️ 原来这里写着"（在 config.yaml 的 bootstrap_whitelist 里可加上）"
                #    —— 用户不该看到配置键名，而且那个键现在也不是主要入口
                #    （开关在「设置 → 会话」里）。2026-10-13 改。
                emit("这个会话没启用（在设置里打开）")
                show_replies(sess, [])
                return
            if ai is None or not ai.available:
                # ⚠️ 说清缺哪几项（状态行也要够短：三样都缺时 20 字上下）
                miss = "、".join(ai.missing) if ai is not None else "AI 模块"
                emit("还没配置 %s，只读取不生成" % miss)
                show_replies(sess, [])
                return
            if not (auto_generate and want_generate):
                preview = ["%s: %s" % (m["who"], m["text"]) for m in history[-5:]]
                # ⚠️⚠️ 这里是**旧「鼠标锚点」时代的遗留**（2026-09-27 修）。
                #    原来 `find_anchor()` 会返回**一条** `current` 消息，
                #    这一行就印它。改成"回复目标 = 当前屏幕的全部对话"
                #    之后（2026-09-24 的 `collect_visible_block`），
                #    `current` 这个变量**根本不存在了**，但这一行没跟着改 ——
                #    于是点【只看对话】必然
                #        NameError: name 'current' is not defined
                #    用户报的就是这个。
                #    → 按**跟上面 `current_text` 同一种格式**把整块列出来，
                #      "→" 表示"这几条是我准备回复的"。
                for m in block_to_dicts(block):
                    preview.append("→ %s: %s" % (m.get("who"), m.get("text")))
                # ⚠️ 2026-09-27：**不再塞进主面板**，改成**单独开一个窗口**显示
                #    （用户提的：「我感觉只看对话也用单独窗口来吧」）。
                #    原来走 `show_replies(sess, preview)`，把整段对话当成"建议"
                #    铺在主面板的滚动区里 —— 两个后果：
                #      ① 刚生成的三条建议**被顶掉**，看一眼对话就得重新生成（白花 token）；
                #      ② 长对话把主面板撑成一坨可滚动的东西，状态栏那行小字跟着变长，
                #         而**状态栏文字宽度就是面板的最小宽度** → 面板被顶着变宽
                #         （用户报的「鼠标放到【生成建议】上，右边框往右变宽」就是这个）。
                if ui is not None:
                    ui.post_read("\n".join(preview))
                else:
                    for line in preview:
                        print("  " + line)
                emit("只看对话 · 未调用 AI")
                return

            emit("正在生成建议…")
            persona = get_persona(cfg, registry.key_for(sess))
            relationship = get_relationship(cfg, registry.key_for(sess))
            # 口吻样本：拿「我」刚发过的短消息当风格参照。
            # 优先用历史库里更多的样本，库不够时退回窗口里可见的。
            #
            # ⚠️ 两条路径都要**排除 AI 建议过的消息**（用户发现的自我污染）：
            # AI 生成建议 -> 用户复制发出去 -> 进库 -> 下一轮被当成「我平时说话
            # 的样子」-> AI 开始模仿自己的输出，几轮下来 AI 腔越来越重。
            samples = []
            if cfg.get("style.enabled", True):
                from wechat_assistant.ai import collect_style_samples

                limit = int(cfg.get("style.max_samples", 12))
                max_len = int(cfg.get("style.max_length", 60))
                if store is not None:
                    # 库里那条路径：own_style_texts 内部已经过滤 from_ai=1
                    samples = store.own_style_texts(registry.key_for(sess),
                                                    limit=limit, max_len=max_len)
                if not samples:
                    samples = collect_style_samples(
                        snap.messages, limit=limit, max_len=max_len,
                        exclude=(store.ai_suggestion_texts() if store else None))
            if samples:
                log("口吻样本 %d 条: %s" % (len(samples), " / ".join(samples[:4])))
            if relationship:
                log("关系设定: %s" % relationship)
            if user_intent:
                log("你说要表达的意思: %s" % user_intent)
            try:
                # ⚠️ 图片只有 `ocr_read.attach_image` 打开时才会有
                #    （见 `OcrReader._crop_images`）；关着时 `snap.images` 恒为空。
                replies = ai.generate_replies(persona, history, current_text,
                                              style_samples=samples,
                                              relationship=relationship,
                                              user_intent=user_intent,
                                              images=(snap.images or None))
                intent = getattr(ai, "last_intent", "") or ""
                if intent:
                    log("判断: %s" % intent)
                # 记下这批建议的原文：它们之后若被用户复制发出去并记进库，
                # 会被打上 from_ai=1 标记，取样口吻时排除掉 ——
                # 避免 AI 模仿自己的输出（自我污染）。
                if store is not None and replies:
                    store.note_ai_suggestions(replies)
                # 把「实际提交的提示词」交给悬浮窗（设置里的【看提示词】展示它），
                # 同时在命令行日志里留一行尺寸，便于不开 UI 时也能判断
                _sys = getattr(ai, "last_system", "") or ""
                _usr = getattr(ai, "last_user", "") or ""
                _rsn = getattr(ai, "last_reasoning", "") or ""
                if ui is not None:
                    ui.post_prompt(_sys, _usr, _rsn)
                if _sys or _usr:
                    log("提示词: system %d 字 + user %d 字、思考 %d 字"
                        "（设置里的【看提示词】可看全文）"
                        % (len(_sys), len(_usr), len(_rsn)))
                # 本次真实用量（模型侧统计的准确值），并按配置单价估个花费。
                # 为什么要打：输出单价是输入的 4 倍，**输出才是成本大头**；
                # 而输出里又有八成是模型的「思考」（reasoning_tokens），
                # 光看提示词多长根本判断不出贵不贵。
                _u = getattr(ai, "last_usage", None) or {}
                if _u.get("total_tokens"):
                    p_in = float(cfg.get("ai.price_in", 1.0) or 0)
                    p_out = float(cfg.get("ai.price_out", 4.0) or 0)
                    _cost = (_u["prompt_tokens"] * p_in
                             + _u["completion_tokens"] * p_out) / 1e6
                    _money = ("（约 ¥%.4f）" % _cost) if (p_in or p_out) else ""
                    _rt = _u.get("reasoning_tokens", 0)
                    _extra = ("，其中思考 %d" % _rt) if _rt else ""
                    log("本次用量: 入 %d + 出 %d = %d token%s%s"
                        % (_u["prompt_tokens"], _u["completion_tokens"],
                           _u["total_tokens"], _extra, _money))
                show_replies(sess, replies, intent=intent)
                log("生成 %d 条建议" % len(replies))
            except Exception as e:
                show_replies(sess, [], "%s: %s" % (type(e).__name__, e))
        except Exception as e:
            emit("处理失败: %s: %s" % (type(e).__name__, e))
        finally:
            state["busy"] = False
            if ui is not None:
                ui.set_busy(False)

    def on_trigger(reason, want_generate=True, user_intent=None):
        # 冷静期：刚点过【复制】的话，用户正在去微信粘贴的路上，
        # 此时忽略「无意图」的自动触发源，免得悬浮窗内容被顶掉（用户反馈过这个体验问题）。
        # 热键和按钮是明确意图，不受冷静期限制。
        if reason in ("window_focus", "session_switch"):
            cool = float(cfg.get("trigger.cooldown_after_copy", 20.0))
            if time.time() - state.get("last_copy_ts", 0.0) < cool:
                log("[触发] %s 处于复制后的冷静期内，忽略" % reason)
                return
        # 用户意图：点按钮那条路径会显式传进来（已在 UI 线程清空输入框）；
        # 热键等路径传 None，这里补读输入框的镜像 —— 子线程不能直接碰 tk 控件，
        # 读镜像才安全。用掉之后同样要请 UI 线程清掉，
        # 否则这句意图会被下一个会话误用。
        if user_intent is None:
            if ui is not None:
                user_intent = ui.peek_intent()
                if user_intent:
                    ui.clear_intent()
            else:
                user_intent = ""
        threading.Thread(target=do_generate,
                         args=(reason, want_generate, user_intent), daemon=True).start()

    def do_clear():
        """清空**当前会话**的历史库 —— 让 AI 下次生成时没有旧上下文。

        ⚠️ 这个回调以前**根本没接**（`FloatingAssistant` 没传 `on_clear`），
        于是「清空上下文」是个**说谎的按钮**：点了只把状态栏改成"已清空"，
        实际什么都没删。顺带把 `ui._clear()` 也改成没接上就如实说。

        这是破坏性操作，但历史库只是"攒起来的上下文"，删了不影响微信本身；
        下次生成时会重新从当前窗口读，所以最多是"忘了之前聊过什么"。
        """
        if ui is None:
            return
        try:
            snap = reader.read_chat(hwnd)
            if not snap or not snap.title:
                ui.post_status("当前没有打开的会话，没法清空")
                return
            key = registry.key_for(snap.title)
            n = store.clear(key) if store is not None else 0
            log("已清空「%s」的上下文（%d 条）" % (snap.title, n))
            ui.post_status("已清空「%s」的上下文（%d 条）" % (snap.title, n))
        except Exception as e:
            log("清空上下文失败: %s: %s" % (type(e).__name__, e))
            ui.post_status("清空失败: %s" % e)

    if ui is not None:
        ui._on_regenerate = lambda intent: on_trigger("manual_generate", True, intent)
        ui._on_read = lambda: on_trigger("manual_read", False)
        ui._on_copy = lambda: state.__setitem__("last_copy_ts", time.time())
        # ⚠️ 清空要读窗口 + 写数据库（UIA 一次 50ms 起），
        #    直接在 UI 线程里做会把界面卡住，所以丢到子线程。
        ui._on_clear = lambda: threading.Thread(target=do_clear,
                                                daemon=True).start()

    # ---------- 触发源
    trigger = None
    if cfg.get("trigger.enabled", True):
        from wechat_assistant.trigger import TriggerManager

        trigger = TriggerManager(cfg, reader, logger=log, on_trigger=on_trigger)
        trigger.start(hwnd)
    else:
        log("触发源已关闭（trigger.enabled=false）")

    # ---------- 立即触发一次（验证链路用，注意要放在 trigger 初始化之后）
    if args.demo:
        log("--- 立即触发一次（--demo）---")
        do_generate("demo")
        # ⚠️ `do_generate` 是**同步**跑完的，而生成是在子线程里、结果通过
        #    `ui.post_replies()` 进队列 —— 这时主线程还没进事件循环，
        #    **队列里的消息一条都没被取走**。不主动排空的话 `--demo` 只在
        #    命令行打印，窗口里空着 ——「验证链路用」的那条路自己看不见结果。
        pump_ui(ui, 1200)
        trigger.stop()
        close_worker()
        return 0

    # ---------- 轮询器
    watcher = Watcher(cfg, reader, registry, sender=sender, logger=log)
    mode = cfg.get("runtime.mode", "watch")

    # ---------- 单次模式（调试）
    if args.once:
        log("--- 单次轮询 ---")
        events = watcher.tick()
        log("本轮事件数: %d" % len(events))
        for ev in events:
            if ev.kind == "messages" and ev.messages:
                log("  会话 %s 的新消息:" % ev.session)
                for m in ev.messages:
                    log("    %s: %s" % (m.who, m.text))
        if recorder is not None:
            log("--- 单次静默记录 ---")
            added = recorder.tick()
            log("本次新增 %d 条" % added)
        registry.save()
        if trigger is not None:
            trigger.stop()
        close_worker()
        return 0

    # ---------- 主循环
    hotkey = cfg.get("trigger.on_hotkey", "") or "（未设热键）"
    log("手动触发方式：")
    log("  · 悬浮窗【生成建议】按钮，或按 %s   ->  读取 + 调 AI 生成" % hotkey)
    log("  · 设置 → AI 接口 →「只看对话」        ->  只读取，不花 token")
    if cfg.get("trigger.on_window_focus", False) or cfg.get("trigger.on_session_switch", False):
        log("  （已开启自动触发源；复制后 %.0fs 内会进入冷静期不打扰）"
            % float(cfg.get("trigger.cooldown_after_copy", 20.0)))
    log("  · 后台静默记录中（只写库、不调 AI）—— 想问 AI 时再按热键")

    # 静默记录循环：只写历史库，不调 AI、不提醒
    if recorder is not None:
        recorder.start()

    # 跟随器必须在 UI 主循环**开始之后**才启动 —— 两版界面都是靠事件循环定时
    # （Tk 的 `after` / Qt 的 `QTimer`），主循环没跑起来时定时器不会触发。
    if follower is not None:
        start_follower_later(ui, follower, 200)
        log("贴边跟随已启用（面板会站到微信窗口旁边，高度与微信对齐）")

    # ⚠️ 在进主循环**之前**装好 Ctrl+C 处理器，见函数说明。
    #    放这么晚是因为它要拿到 `ui`（`--no-ui` 时是 None，两条路处理不一样）。
    install_sigint_handler(ui, log)

    try:
        if ui is not None:
            ui.run()            # 阻塞在主线程
        else:
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出中…")
    finally:
        if follower is not None:
            follower.stop()
        if recorder is not None:
            recorder.stop()
            s = recorder.stats
            # ⚠️ 「跳过」要拆开说：没窗口 / 没会话名 / 会话被关，三件事的
            #    排查方向完全不同。合在一起时它们都显示成「未启用」，
            #    会让人去查会话设置，而其实只是微信最小化了。
            log("记录统计: 轮询 %d 次 | 新增 %d 条 | 无变化 %d | 翻历史中跳过 %d | "
                "跳过 %d（没窗口 %d / 没会话名 %d / 会话关 %d）| 探针命中 %d | 错误 %d"
                % (s["ticks"], s["saved"], s["idle"], s.get("scrolled", 0),
                   s["skipped"], s.get("skip_nowin", 0), s.get("skip_notitle", 0),
                   s.get("skip_disabled", 0), s.get("probe", 0), s["errors"]))
        if trigger is not None:
            trigger.stop()
            if trigger.counts:
                log("触发统计: %s" % trigger.counts)
        watcher.stop()
        registry.save()
        if ui is not None:
            ui.stop()
        close_worker()
    return 0


if __name__ == "__main__":
    sys.exit(main())
