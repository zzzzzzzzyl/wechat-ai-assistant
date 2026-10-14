# -*- coding: utf-8 -*-
"""静默记录：定时把「当前打开的那个会话」读下来存进历史库。

它解决的问题
------------
在这之前，历史库是**用户按热键那一刻的快照集合** —— `save_messages()`
只在 `do_generate()` 里调用，不按热键就一条都不存。于是：

  顺序问题：两次触发之间的对话永远丢了；跨快照只能按「你按热键的顺序」排
  重复问题：两次触发间隔很久，同一条消息被反复重读，一旦发送者判定状态
            变了（? -> 对方），指纹就变了，去重失效 -> 存成两份

**这是同一个根因的两个症状：把「离散快照」当成了「连续记录」。**

本模块让记录连续化：每隔几秒读一次当前会话并写库。
快照之间的洞被补上，重读也不再跨越很长时间。

和 Watcher 的区别（别搞混）
--------------------------
    Watcher  = 盯**所有**会话「有没有新消息」-> 用来提醒（当前未启用）
    Recorder = 盯**当前**会话「把内容记全」-> 只写库

它不做的事（都是有意的）
------------------------
    ✗ 不调 AI      -> token 消耗为 0
    ✗ 不弹窗提醒   -> 静默，不打扰
    ✗ 不改任何微信状态（不置顶、不激活、不点击）

红线：只读。和整个项目一致。
"""

import threading
import time
from typing import Callable, List, Optional

from .sessions import MODE_OBSERVE, MODE_ON, is_junk_title
from .uia_worker import UiaError, UiaTimeout
from .window import items_from_snapshot
# 需要被记录的会话模式：on = 提醒+记录+生成；observe = 只记录不生成。
# off 的会话**绝不读内容也不记录** —— 白名单的隐私承诺不能破。
RECORD_MODES = (MODE_ON, MODE_OBSERVE)

#: ⚠️ 切会话竞态的保护**连续失败多少次就放弃**（见 `Recorder._session_stable`）。
#:
#: 判据是"读正文前后各读一次标题，两次指向的会话不一样"—— 那多半是**切会话**
#: 正好落在两次读取之间。但也可能是**标题本身读不稳**（OCR 对同一屏两次抓图
#: 给出不同的字），那种情况下如果一直拦，就变成"这个会话永远不记录"（静默丢数据）。
#: 所以连续 `RACE_GIVEUP_N` 次都对不上就判为"标题读不稳"，放行并打一行警告。
RACE_GIVEUP_N = 3

#: ⚠️ **动画刷屏的退避**（2026-10-11 用户实测：群里一个 GIF 让探针每拍都判
#: 「变了」→ 每拍真读一屏 380ms ≈ 38% 一个核，日志还每秒刷两行像死循环）。
#:
#: 连续多少次「探针说变了、读出来内容没变」就认定是动画；认定之后把**真读**的
#: 间隔放慢几倍。判据和恢复方式见 `Recorder._note_probe_result`。
#: ⚠️ 探针本身（34ms）不变 —— 它便宜，而且要靠它发现真正的新内容。
PROBE_CHURN_N = 3
PROBE_CHURN_SLOW = 4


class Recorder:
    """定时读取当前会话并存入历史库。线程安全，配合独立线程跑 run()。"""

    def __init__(self, cfg, reader, store, registry, sender=None,
                 logger: Optional[Callable[[str], None]] = None,
                 on_session: Optional[Callable] = None, screens=None):
        self.cfg = cfg
        self.reader = reader
        self.store = store
        self.screens = screens          # 屏快照库（可空）；见 window.py
        self.registry = registry
        self.sender = sender
        self._log = logger or (lambda m: None)
        # `on_session(title, is_group)`：当前打开的会话**变了**就回调一次。
        # 界面用它把标题栏的会话名跟着走，不用等用户按热键。
        # ⚠️ 它搭在**本来就有的**这次读取上，不额外查一次 UIA（零开销）。
        # is_group 可能是 None —— 那一步还没读快照，界面应保留上一次的值。
        self.on_session = on_session
        self._last_title = None
        #: ⚠️ **已经报过一次的垃圾标题**（`is_junk_title` 命中）——
        #: 只记一次日志，否则标题一直读错就每拍刷一行。
        self._junk_logged: set = set()
        #: ⚠️ **还没通过"两次确认"的标题**（见 `_register_session`）。
        #: 它的用处不只是打日志：OCR 探针模式下标题**只在"屏幕变了"的那拍才读**，
        #: 所以必须靠它让**下一拍再来读一次标题**，否则新会话永远攒不满两次确认
        #: （而它头一屏会被整屏丢掉）。
        self._pending_title: Optional[str] = None
        #: 切会话竞态：上一次"两次标题对不上"的那一对 key，以及连续次数
        self._race_pair = None
        self._race_n = 0
        self._race_warned = False

        self.enabled = bool(cfg.get("record.enabled", True))
        self.interval = float(cfg.get("record.interval", 2.0))
        # 每多少次 tick 打一行统计（避免刷屏）
        self.report_every = int(cfg.get("record.report_every", 30))

        # ---- 自适应采样 ----
        # 内容在变（用户在翻记录 / 来了新消息）时自动加快，安静下来再回落。
        # 为什么需要：固定 1 秒一拍，而人快速滑动时一秒能滑过好几屏 ——
        # 中间那些屏根本没被采到（加上虚拟列表，滚太快时它们也没渲染出来）。
        # ⚠️ fast_interval = 0 表示**关掉**自适应，永远用 interval。
        #    下限钳到 0.05：一拍本身要 100ms 上下（读 60 + 像素判定 45），
        #    再快只会堆积，不会更灵。
        fast = float(cfg.get("record.fast_interval", 0.2) or 0.0)
        self.fast_interval = max(0.05, fast) if fast > 0 else 0.0
        self.fast_hold = max(0.0, float(cfg.get("record.fast_hold", 3.0) or 0.0))
        self._fast_until = 0.0      # perf_counter 时间戳；现在 < 它 = 快速档

        self._stop = False
        self._thread = None
        self._last_sig = None       # 上一次看到的屏幕文本签名
        self._need_retry = False    # 上次没判出发送者 -> 强制重试
        #: 「只跟会话名、不连续记录」这件事只提示一次
        self._told_no_full_read = False
        #: 「走探针」这件事只提示一次
        self._told_probe = False
        #: ⚠️ **动画刷屏的退避**（2026-10-11 用户实测：屏幕上有个 GIF，
        #:    探针每拍都判「变了」→ 每拍真读一屏 380ms ≈ 38% 一个核，
        #:    而且日志里那两行每秒刷一次，看起来像死循环）。
        #:
        #:    判据：`探针说变了` **但** `读出来内容没变` —— 连发 `PROBE_CHURN_N`
        #:    次就认定是动画（GIF / 动画表情）在刷新，把真读的间隔放慢
        #:    `PROBE_CHURN_SLOW` 倍；等真有新内容（签名变了）立刻恢复。
        #:    ⚠️ 探针本身照旧（34ms，3.4%），放慢的只是那次 380ms 的真读。
        self._probe_churn = 0
        self._churn_logged = False

        # ---- OCR 后端的「便宜探针」（见 tick 里那段说明）----
        #
        # 探针 = 抓图 + 缩略图哈希 ≈32ms（实测，probes/bench_capture_hash.py），
        # 比真读一屏（~250ms）便宜约 8 倍。于是拍可以打得很密。
        # ⚠️ `probe_interval` 比 `title_interval` **更短**是有意的：
        #    探针便宜，密一点才抓得住"快速滑动"；而只跟会话名那条路
        #    一次要读标题栏（OCR），反而更贵，所以间隔更长。
        self.probe_on = bool(cfg.get("ocr_read.probe", True))
        self.probe_interval = max(0.2, float(
            cfg.get("ocr_read.probe_interval", 1.0) or 1.0))
        #: 只用于**打印估算**的初值（真实耗时每拍都量，见 `_probe_hash`，
        #: 用指数滑动平均）。实测 `probes/bench_tick_cost.py`：探针 **34ms**。
        #: ⚠️ 这个数曾经因为探针里混进像素分析涨到 132ms（比 UIA 还贵），
        #:    所以它是需要**实测**盯着的。
        self.probe_ms = float(cfg.get("ocr_read.probe_ms_est", 34.0))
        #: 探针模式下的"滑动档"间隔。
        #: ⚠️⚠️ 下限 0.5s，**不是 0.25s**：探针命中之后紧接着要真读一屏
        #:    （实测 **381ms**），所以"命中那一拍"本身要 381+34 ≈ 415ms。
        #:    间隔设到 0.3s 就是**拍比拍还长**，`sleep(max(0.02, iv-dt))`
        #:    会退化成一刻不停、CPU 拉满却仍然追不上屏幕（丢屏）。
        #:    第一版这里写的是 0.3 —— 我只按"探针 34ms"算，**漏了那次真读**。
        self.probe_fast = max(0.5, float(cfg.get("ocr_read.probe_fast", 0.5) or 0.5))
        #: 探针"变了"的判据（**带容差**，别退化成逐字节相等）：
        #: `probe_tol` = 单像素差多少才算变了；`probe_changed` = 至少多少比例的
        #: 像素变了才算整屏变了。默认值来历见 `ocr_reader.probe_changed`。
        self.probe_tol = int(cfg.get("ocr_read.probe_tol", 8))
        self.probe_changed = float(cfg.get("ocr_read.probe_changed", 0.01) or 0.01)
        self._last_probe = None

        #: 只读标题时的拍间隔。⚠️ 不能沿用 `record.interval`（默认 1s）——
        #:    那是按 UIA 的 ~100ms/拍 定的；OCR 读标题那条也要 60~100ms，
        #:    每秒来一次同样是白烧 CPU，而"会话名变化"根本不需要秒级响应。
        self.title_interval = max(
            self.interval, float(cfg.get("ocr_read.title_interval", 3.0) or 3.0))
        # ⚠️ 它和 _screen_changed 的返回值**不是一回事**：
        #    强制重试时返回值也是 True，但内容其实没变 ——
        #    用返回值去驱动"加速"的话，窗口一被最小化就永远退不出快速档。
        self._content_changed = False
        self.stats = {"ticks": 0, "saved": 0, "skipped": 0, "errors": 0,
                      "idle": 0, "fast": 0, "probe": 0,
                      # ⚠️ **三个"跳过"的原因必须分开记**。原来它们都算进
                      #    `skipped`、而报表只写「跳过(未启用)」，于是
                      #    「微信没开 / 最小化读不到会话名」和「这个会话被设成 off」
                      #    在日志里长得一模一样 —— 排查时会往完全错的方向走。
                      #    实测：空库启动、微信最小化时前 30 拍全是「跳过(未启用) 30」，
                      #    看起来像"所有会话都被禁用了"。
                      "skip_nowin": 0, "skip_notitle": 0, "skip_disabled": 0,
                      # ⚠️ 2026-10-13 新增的两类跳过，也必须**单独记**：
                      #    · skip_junk        = 标题不像会话名（OCR 碎片 / 微信界面文案）
                      #    · skip_unconfirmed = 这个标题还没通过"两次确认"
                      #    · skip_race        = 切会话竞态（标题前后不一致）
                      #    它们原来**根本不存在**：垃圾标题照样读正文、照样写库，
                      #    于是库里攒出 15 个会话名而设置里只登记了 2 个。
                      "skip_junk": 0, "skip_unconfirmed": 0, "skip_race": 0}

    # ------------------------------------------------------------ 单次记录

    def _probe_hash(self, hwnd):
        """调后端的便宜探针，并**把真实耗时记下来**（用于日志里的占用估算）。

        ⚠️ 后端的 `probe()` 出错/不支持时返回 None —— 那时这一拍就当"没变化"
           处理（`tick` 里 `h is None` 就 return）。**不能**把 None 当成"变了"：
           抓图失败的窗口（最小化 / 停在扫码登录窗）会永远失败，
           那会变成每一拍都去跑一次 250ms 的完整读屏。

        ⚠️ `probe_ms` 用**指数滑动平均**，不要用最后一次 ——
           实测抓图会偶尔窜到 179ms（别的进程抢核），
           拿单次值估算占用会得出很荒唐的数（见 bench_capture_hash.py 里那条注）。
        """
        try:
            t0 = time.perf_counter()
            h = self.reader.probe(hwnd)
            ms = (time.perf_counter() - t0) * 1000
            self.probe_ms = (self.probe_ms * 0.8 + ms * 0.2) if self.probe_ms else ms
            return h
        except Exception as e:
            self.stats["errors"] += 1
            self._log("探针失败（这一拍跳过）: %s: %s" % (type(e).__name__, e))
            return None

    def _screen_changed(self, messages) -> bool:
        """屏幕内容和上一次相比变了吗？（变了才值得做发送者判定）

        ⚠️ 一开始这里比的是「库里的最近 N 条 vs 屏幕上的 N 条」，**是错的**：
        库是累积的，往上翻历史或切换会话时，recorder 会把「对库是新的、
        对屏幕是旧的」消息也追加进去，导致库尾从此和屏幕尾对不上 ——
        于是这个判断永远返回「变了」，每次轮询都白做一遍发送者判定
        （实测日志里 `无变化 0`，就是这个原因，见 probes/diag_recorder.py）。

        正确做法是只关心**屏幕自己变没变**：记住上一次的屏幕文本签名即可。
        """
        sig = tuple((getattr(m, "text", "") or "").strip()
                    for m in (messages or []) if (getattr(m, "text", "") or "").strip())
        if not sig:
            self._content_changed = False
            return False
        # 内容到底变没变 —— 和返回值分开记（见 __init__ 里那段说明）
        self._content_changed = (sig != self._last_sig)
        self._last_sig = sig
        # 上一次没能判出发送者（比如窗口被最小化、PrintWindow 只拿到空白），
        # 就强制重试一次 —— 否则这条会永远停在「?」上没人管。
        if self._need_retry:
            return True
        return self._content_changed

    def _note_probe_result(self, changed: bool) -> None:
        """记一次「探针命中」的结果 —— 用来识别**动画刷屏**。

        ⚠️ 用户实测（2026-10-11）：群里有人发了个 **GIF**，屏幕像素每帧都在变，
            探针于是每拍都判"变了" → 每拍真读一屏（380ms ≈ **38% 一个核**），
            而且日志里那两行每秒刷一次，用户以为程序死循环了。

        ⚠️ 为什么"内容没变"是动画的可靠判据：GIF 变的是**像素**，不是**文字**。
            `_screen_changed()` 比的是**屏幕文本签名** —— 动画不产生新文字，
            所以签名一模一样。而真正的"来了新消息 / 往上翻了一屏"一定会改签名。

        ⚠️ 恢复是**自动**的：一旦签名真的变了就清零，读屏间隔立刻回到
            `probe_interval`。别写成"退避之后就不管了"。
        """
        if changed:
            if self._churn_logged:
                self._log("屏幕有真正的新内容了，读屏间隔恢复正常（%.1f 秒）"
                          % self.probe_interval)
            self._probe_churn = 0
            self._churn_logged = False
            return
        self._probe_churn += 1
        if self._probe_churn >= PROBE_CHURN_N and not self._churn_logged:
            self._churn_logged = True
            slow = self.probe_interval * PROBE_CHURN_SLOW
            self._log("⚠ 屏幕一直在变、但读出来的内容没变（连续 %d 次）—— "
                      "多半是**动画表情 / GIF 在刷屏**。已把真读的间隔放慢到 %.1f 秒"
                      "（一个核约占 %.1f%%），真有新内容会自动恢复。"
                      % (self._probe_churn, slow,
                         self.probe_ms / (slow * 1000) * 100))
            # ⚠️ 2026-10-14：`ocr_read.probe` 已经从设置界面撤掉了（默认 true），
            #    所以这里**不能再教用户去关它** —— 想彻底省只能改配置文件。
            self._log("   想更省：把那个会话设成「关」（或写进设置里的黑名单）。")

    def _notify_session(self, title: str, is_group):
        """会话变了就回调一次。is_group 为 None 表示"还不知道"（还没读快照）。"""
        if title == self._last_title:
            return
        self._last_title = title
        if not self.on_session:
            return
        try:
            self.on_session(title, is_group)
        except Exception as e:
            self._log("会话回调失败: %s: %s" % (type(e).__name__, e))

    def _register_session(self, title: str) -> bool:
        """把「两道人墙」挪到**每一拍**来数，返回**这个标题现在能不能写库**。

        ⚠️⚠️ 为什么必须挪（2026-10-13 查库查出来的）：

            原来 `registry.discover()`（登记新会话）**只在 `_on_session` 里**调，
            而那个回调由 `_notify_session` 发出 —— 它**只在标题变了的那一拍**才发。
            也就是说：一共只数过**一拍**，而"两次确认"要求"在**不同的拍**里看到
            两次"（`_CONFIRM_WINDOW` = 10 秒内连续）——
            **除非你在 10 秒内切走再切回来，任何会话都登记不上。**

            实测后果：`state.json` 里只有 **2** 个会话，而两个库里有 **15** 个。
            （用户早就报过"设置里的会话列表没有/长不出来"，一直没找到这一层。）

        ⚠️ 返回值是「**可以写库**」而不是「这次是新发现」：没登记的标题一律不写，
            于是一次性的乱码标题（OCR 读错的那一拍）不会在库里留下记录。
        """
        reg = self.registry
        if reg is None:
            return True                     # 没有注册表就退回老行为（不拦）
        try:
            if reg.discover(title):         # True = 首次登记 -> 要落盘
                self._log("发现新会话: %s" % title)
                # ⚠️ 发现就立刻落盘：只在退出时 `save()` 的话，强杀 / 崩溃
                #    会让这一轮发现的所有会话全丢（实测丢过十来个）。
                reg.save()
        except Exception as e:
            self._log("登记会话失败: %s: %s" % (type(e).__name__, e))
            return True                     # 登记这条路坏了不该顺手把记录也停掉
        try:
            ok = bool(reg.is_registered(title))
        except Exception:
            return True
        # ⚠️ 未确认时留个记号：OCR 探针模式下标题只在"屏幕变了"的拍才读，
        #    要靠它让下一拍再来读一次标题（否则永远凑不满两次确认）。
        self._pending_title = None if ok else title
        return ok

    def _session_stable(self, hwnd, key: str) -> bool:
        """⚠️ 切会话那一拍的竞态：确认这一拍的 **(标题, 正文) 是同一时刻的**。

        问题（实测取证见 CLAUDELOG 第五十八轮）：`tick` 是**两步读** ——
        先 `read_chat_title()`、再 `read_chat()`。用户切会话正好落在两步**之间**时，
        这一拍拿到的是 **(旧标题, 新正文)**，于是同一段正文被记到**错误的会话**
        名下；下一拍再正确地记一遍 -> 同一条内容挂到两个会话名下
        （实测库里 29 组指纹挂在多个会话名下，最大一组横跨 7 个会话名）。

        做法（"读两次标题夹住正文"）：正文读完后再读一次标题，
        两次指向**同一个会话**才放行；不一样就不写这一拍，
        并且**让下一拍重读这一屏**（`_last_sig = None`），
        下一拍两次读取就稳定了 —— 所以那次记录**不会丢**，只是晚一拍（约 1 秒）。

        ⚠️ 代价：每写一拍多一次标题读取（UIA ~50ms / OCR ~65ms）。**只在正文真的
           变了的那一拍**才做（空闲拍在读标题之前就 return 了），所以不心疼。
        ⚠️ 兜底：连续 `RACE_GIVEUP_N` 次都对不上 -> 判为**标题本身读不稳**
           （OCR 对同一屏两次抓图给出不同的字），这时**放行**并打一行警告 ——
           否则就成了"这个会话永远不记录"，那是更糟的静默丢数据。
        """
        try:
            t2 = self.reader.read_chat_title(hwnd)
        except Exception as e:
            self._log("确认会话（第二次读标题）失败: %s: %s" % (type(e).__name__, e))
            t2 = ""
        k2 = self.registry.key_for(t2) if t2 else ""
        if not k2 or k2 == key:
            self._race_pair = None
            self._race_n = 0
            self._race_warned = False
            return True

        pair = (key, k2)
        self._race_n = self._race_n + 1 if self._race_pair == pair else 1
        self._race_pair = pair
        if self._race_n >= RACE_GIVEUP_N:
            if not self._race_warned:
                self._race_warned = True
                self._log("⚠ 会话标题连续 %d 次对不上（%r vs %r）—— 判为**标题读不稳**"
                          "（不是切会话），这一拍按第一次读取的结果记录；"
                          "对得上之后自动恢复保护。" % (self._race_n, key, k2))
            self._race_pair = None
            self._race_n = 0
            return True

        self.stats["skipped"] += 1
        self.stats["skip_race"] = self.stats.get("skip_race", 0) + 1
        if self._race_n == 1:
            self._log("跳过一拍：读正文前后会话名不一致（%r -> %r）—— 多半是**刚好在切会话**，"
                      "这一拍不写库（下一拍会重读这一屏）" % (key, k2))
        # ⚠️⚠️ 必须让**下一拍重读这一屏**：`_screen_changed()` 已经把这屏的签名
        #    记进 `_last_sig` 了，不清掉的话下一拍会判成"没变化"而**把正确的那次
        #    记录也丢掉**（那就成了丢数据，比记错更糟）。
        self._last_sig = None
        self._content_changed = True        # 顺便进快速档，0.2s 后就补上
        return False

    def tick(self) -> int:
        """读一次当前会话并存库。返回新增条数。**异常不外抛**（记录失败不该拖垮程序）。"""
        if not self.enabled or (self.store is None and self.screens is None):
            return 0
        self.stats["ticks"] += 1
        # 先清掉，免得中途早退时留着上一拍的旧值（会让快速档退不出来）
        self._content_changed = False

        win = self.reader.find_window()
        if not win:
            self.stats["skipped"] += 1
            self.stats["skip_nowin"] += 1
            return 0
        hwnd = win["hwnd"]

        # ---- ⚠️ 探针模式下：**先探，再决定要不要读标题** ----------------
        #
        # 2026-10-11 实测（`probes/bench_tick_cost.py`）发现的一个真问题：
        #
        #     ① read_chat_title  65 ms   ← 原来**每拍都做**
        #     ② probe（探针）     34 ms
        #     ③ read_chat        381 ms   ← 命中了才做
        #
        # 于是"空闲"其实是 65+34 = **99ms（9.9% 一个核）**，
        # 而日志里那句「约 34 ms，只占 3.4%」**只量了第 ②步** ——
        # 我据此说过"探针比 UIA 还省"，**那是错的**（UIA 一拍 ~100ms = 10%）。
        #
        # ⚠️ 而第 ①步在探针模式下**是冗余的**：标题就在探针的覆盖范围里
        #    （`_probe_box` 从 y=0 开始），所以
        #      · 切会话 -> 标题像素变了 -> 探针命中 -> 那次 `read_chat`
        #        自己会填 `snap.title`（见 `OcrReader.read_chat`），标题不会漏
        #      · 没变   -> 标题当然也没变，根本不需要读
        #    去掉之后空闲 = 34ms = **3.4%**。
        #
        # ⚠️ 只留**第一拍**读一次标题：界面左上角要有个会话名，
        #    否则启动后一直显示「—」直到你切一次会话。
        probe_mode = (not getattr(self.reader, "background_read_ok", True)
                      and self.probe_on)
        seed_title_only = False
        if probe_mode:
            h = self._probe_hash(hwnd)
            if h is None:
                self.stats["idle"] += 1
                return 0
            if self._last_probe is None:
                self._last_probe = h
                seed_title_only = True          # 第一拍：只把会话名种上
            else:
                # ⚠️ 比较走 `probe_changed()`（**带容差**），不是 `h != last`。
                #    逐字节相等会被抓图的微小抖动骗到（实测整体加噪声 ±2 就判成
                #    "变了"），于是每拍都真读 400ms，**探针比不做还亏**。
                #    判据和默认值的来历见 `ocr_reader.probe_changed`。
                from .ocr_reader import probe_changed
                if not probe_changed(self._last_probe, h, self.probe_tol,
                                     self.probe_changed):
                    # ⚠️⚠️ 但有**一个还没确认的标题**待处理时，这次标题不能省：
                    #    标题只在这条路上读，而"同一个标题要在不同的拍里看到两次"
                    #    才登记 —— 省掉的话新会话永远确认不了，它的头一屏会被丢。
                    #    （见 `_register_session` / `self._pending_title`）
                    if self._pending_title:
                        self._last_probe = h
                    else:
                        self._last_probe = h
                        self.stats["idle"] += 1
                        return 0
                self._last_probe = h
                self.stats["probe"] = self.stats.get("probe", 0) + 1
                if not self._told_probe:
                    self._told_probe = True
                    self._log("静默记录: 走「探针」—— 每 %.1fs 抓一次图比哈希"
                              "（约 %.0f ms，空闲时只占 %.1f%% 一个核），"
                              "**变了才真读一屏**。"
                              % (self.probe_interval, self.probe_ms,
                                 self.probe_ms / (self.probe_interval * 1000) * 100))
                    self._log("   所以「你翻过的屏 / 来的新消息」都会留下来，"
                              "而空闲时几乎不花钱。")

        # 先看当前是哪个会话 —— 白名单外的会话连读都不读
        title = self.reader.read_chat_title(hwnd)
        if not title:
            self.stats["skipped"] += 1
            self.stats["skip_notitle"] += 1
            return 0

        # ---- ⚠️ 垃圾标题：**连正文都不读，更不写库**（2026-10-13）-----------
        #
        # 为什么加在这里：`is_junk_title` / "两次确认"原来**只挡 `state.json`**
        # （设置里那份会话列表），而记录器是直接 `key_for(title)` 就写库的 ——
        # 于是库里攒出 15 个会话名、其中 `X` / `+` / `①` 是 OCR 认错的碎片，
        # 用户看到的就是"翻记录时冒出一堆不知道从哪来的记录"。
        # ⚠️ 放在 `_notify_session` **之前**：界面上也别显示这种名字。
        if is_junk_title(title):
            self.stats["skipped"] += 1
            self.stats["skip_junk"] = self.stats.get("skip_junk", 0) + 1
            if title not in self._junk_logged:
                self._junk_logged.add(title)
                self._log("忽略一个不像会话名的标题 %r（归一化后为空/单字/纯数字，"
                          "或微信自己的界面文案）—— 不读正文、不写库" % title)
            return 0

        # 会话名一变就报给界面 —— 标题栏跟着当前打开的会话走，
        # 不用等用户按热键（实测反馈："左上角昵称能不能跟着当前会话加载"）。
        # ⚠️ 放在 mode 判断**之前**：`off` 的会话不该被记录，
        #    但标题栏仍然应该显示它的名字。
        self._notify_session(title, None)

        # ---- ⚠️ 会话登记：**每拍都数**（2026-10-13 修，见 `_register_session`）----
        # 只有**确认过的会话**才写库：一次性乱码标题（OCR 读错的那一拍、
        # 或者恰好抓到别的窗口画面的那一拍）不会在库里留下记录。
        if not self._register_session(title):
            self.stats["skipped"] += 1
            self.stats["skip_unconfirmed"] = self.stats.get("skip_unconfirmed", 0) + 1
            return 0

        mode = self.registry.mode_of(title)
        if mode not in RECORD_MODES:
            self.stats["skipped"] += 1
            self.stats["skip_disabled"] += 1
            return 0

        # ---- 这一拍该不该真的读整屏 ----
        #
        # ⚠️ 探针那一段**已经挪到函数开头了**（在读标题之前）—— 见那里那段说明：
        #    标题 OCR 有 65ms，而"变了没"只要 34ms 的探针；把探针提前，
        #    没变的那些拍就**完全不用读标题**，空闲成本从 9.9% 降到 3.4%。
        #    这里只处理剩下的两种情况。
        #
        # ⚠️ 这个开关**每拍都重新读**（`background_read_ok` 是 reader 的属性）：
        #    后端可能在运行期从 UIA 切到 OCR，启动时看一次就不够了。
        if not getattr(self.reader, "background_read_ok", True):
            if not self.probe_on:
                # ② **只跟会话名**：连抓图都不做，每 `title_interval` 秒
                #    只读一次标题栏。最省，但翻过的屏不留。
                self.stats["idle"] += 1
                if not self._told_no_full_read:
                    self._told_no_full_read = True
                    self._log("静默记录: 当前后端（截屏 + OCR）**只跟会话名，"
                              "不连续记录内容** —— 一次读屏约 380ms。")
                    self._log("   想把翻过的屏也攒下来：得改配置文件 —— "
                              "`ocr_read.probe: true`（探针 + 变了才读，"
                              "空闲只占约 3.4% 一个核）或 "
                              "`ocr_read.record: true`（每拍都真读，贵 5 倍）。"
                              "⚠️ 这两项 2026-10-14 已从设置界面撤掉。")
                return 0
            self._told_no_full_read = False
            if seed_title_only:
                # 第一拍：探针刚建立基准，标题也种给界面了 —— 但**不读整屏**
                # （这一屏要么用户马上翻走，要么他按热键时 do_generate 会写它）
                self.stats["idle"] += 1
                return 0
        else:
            self._told_no_full_read = False

        snap = self.reader.read_chat(hwnd)
        if not snap or not snap.messages:
            return 0
        # 这时候知道是不是群聊了，补报一次（界面会更新群标记）
        self._notify_session(title, snap.is_group)

        key = self.registry.key_for(title)
        if not self._screen_changed(snap.messages):
            self.stats["idle"] += 1
            # ⚠️ 探针说"变了"、读出来却**没有新内容** —— 记一笔，用来识别动画刷屏。
            #    见 `_note_probe_result` 和 `__init__` 里 `_probe_churn` 的说明。
            if probe_mode:
                self._note_probe_result(False)
            return 0
        if probe_mode:
            self._note_probe_result(True)

        # ---- ⚠️ 切会话竞态：读正文**前后**两次标题必须指向同一个会话 ----
        # 顺序放在发送者判定之前（那一步要 45ms，不值得先花在错的一拍上）。
        if not self._session_stable(hwnd, key):
            return 0

        if self.sender is not None:
            resolved = self.sender.resolve(hwnd, win["pid"], snap)
            # 一条都没判出来（窗口最小化 / 被完全遮挡）-> 下次强制重试，
            # 让「?」有机会被补全，而不是一直烂在库里
            self._need_retry = (resolved == 0 and bool(snap.messages))
        else:
            self._need_retry = False

        # ---- 屏快照：**无条件**写一屏（不去重、不定位、不判断）
        #
        # 顺序留到读取时由 window.stitch() 缝出来。所以「用户往上翻历史」这种
        # 旧逻辑会整屏丢掉的场景，在这里天然被记下来 —— 不需要新消息检测、
        # 不需要重叠点、不需要「在不在底部」兜底。
        if self.screens is not None:
            try:
                self.screens.append_screen(key, items_from_snapshot(snap),
                                           snap.at_bottom())
            except Exception as e:
                self.stats["errors"] += 1
                self._log("[记录] 屏快照写入失败: %s: %s" % (type(e).__name__, e))

        if self.store is None:
            return 0

        # ⚠️ 用 save_new_only 而不是 save_messages：由它靠「和库里已有的重叠点」
        # 决定哪几条是新的，**与滚动位置无关**。
        #
        # 最早这里用的是「列表在底部才记录」，但那个判据是在**间接代理**一个
        # 别的问题（用户指出它别扭，确实如此）：虚拟化列表上最后一条滚出渲染区
        # 后会换成上一条，导致「滚动距离 == 消息高度」时 gap 归零 -> 误判；
        # 而且翻历史时即便真有新消息也会整轮跳过。
        # 直接答「哪几条还没记过」反而更简单也更可靠。
        # ⚠️ `bottom_known`：截屏 + OCR 后端**判不出在不在底部**（`at_bottom`
        #    在那种后端上恒为真，实测 97/97 屏）。传下去让 `save_new_only` 在
        #    "这屏一条都没记过"时**宁可不写**，而不是把旧消息接到库尾。
        added = self.store.save_new_only(key, snap.messages,
                                         at_bottom=snap.at_bottom(),
                                         bottom_known=getattr(snap, "bottom_known", True))
        if added:
            self.stats["saved"] += added
            self._log("[记录] %s +%d 条（该会话共 %d 条）"
                      % (title, added, self.store.count(key)))
        return added

    # ------------------------------------------------------------ 循环

    def _choose_interval(self) -> float:
        """这一拍之后该睡多久 —— 自适应采样。

        内容刚变过（`_content_changed`）就把快速档的时间窗续到 `fast_hold` 秒后；
        窗口没到就用快速间隔，过了就回落成空闲间隔。

        ⚠️ 后端不连续记录内容时（OCR），分两种情况：

            · **探针模式（默认）**：自适应**照旧开着** —— 因为探针很便宜
              （~32ms），可以密一点。滑动时按 `probe_fast`（默认 0.3s）探，
              抓住"滑过去的那几屏"；安静下来回落 `probe_interval`（1s）。
              成本：安静 3%，滑动时最多 ~11%（而且只在 `fast_hold` 那几秒内）。

            · **只跟会话名**（`ocr_read.probe: false`）：自适应整条关掉，
              只按 `title_interval` 慢慢走。那条路上**本来就不记录内容**，
              加速只是白烧 CPU；而且一次读标题栏（OCR）比探针还贵。

        ⚠️ 更要紧的一条（对上面第二种情况）：快速间隔（0.2s）比一次读屏还短时，
            loop 里 `sleep(max(0.02, iv - dt))` 会退化成"一刻不停"，CPU 拉满
            还追不上屏幕。所以 `probe_fast` 的下限钳在 0.25s。**

        抽成独立方法是为了**可测**：循环里的 sleep 没法直接断言。
        """
        if not getattr(self.reader, "background_read_ok", True):
            if not self.probe_on:
                return max(self.interval, self.title_interval)
            now = time.perf_counter()
            if self._content_changed:
                self._fast_until = now + self.fast_hold
            if now < self._fast_until:
                return self.probe_fast
            # ⚠️ **动画刷屏就放慢**（见 `_note_probe_result`）：探针说变了、
            #    读出来却没新内容，连续几次就认定是 GIF，把 380ms 那次真读
            #    从每 1 秒一次降到每 4 秒一次（38% -> 9.5% 一个核）。
            #    ⚠️ 探针本身（34ms）不变 —— 它便宜，而且要靠它发现真正的新内容。
            if self._probe_churn >= PROBE_CHURN_N:
                return self.probe_interval * PROBE_CHURN_SLOW
            return self.probe_interval
        if self.fast_interval <= 0:          # 关掉自适应
            return self.interval
        now = time.perf_counter()
        if self._content_changed:
            self._fast_until = now + self.fast_hold
        return self.fast_interval if now < self._fast_until else self.interval

    def run(self) -> None:
        """记录循环。放在独立线程里跑，stop() 可退出。

        **两档自适应**：内容在变 -> 快速档；安静超过 `fast_hold` 秒 -> 回落。
        `fast_interval = 0` 时退化成固定间隔（旧行为）。
        """
        if not self.enabled:
            self._log("静默记录: 已关闭（record.enabled=false）")
            return
        if self.store is None and self.screens is None:
            self._log("静默记录: 没有可写的库，跳过")
            return

        # ⚠️ 启动日志必须**说这一拍实际会干什么**。原来不管什么后端都打
        #    「空闲每 1.0s，内容在变时每 0.2s」—— 在 OCR 后端那两句都是假的
        #    （既不是 1.0s 也不是 0.2s，而且探针模式下"内容在变"用的是 0.3s），
        #    排查时会被这行日志带偏。
        if not getattr(self.reader, "background_read_ok", True):
            if self.probe_on:
                self._log("静默记录: 已启用「探针」—— 每 %.1fs 抓一次图比哈希"
                          "（内容在变时每 %.1fs），**变了才真读一屏**；"
                          "只写库、不调 AI" % (self.probe_interval, self.probe_fast))
            else:
                self._log("静默记录: 已启用（每 %.1fs 只读一次会话名，"
                          "**不记录内容**；只写库、不调 AI）" % self.title_interval)
        elif self.fast_interval > 0:
            self._log("静默记录: 已启用（空闲每 %.1fs，内容在变时每 %.1fs、"
                      "安静 %.0fs 后回落；只写库、不调 AI）"
                      % (self.interval, self.fast_interval, self.fast_hold))
        else:
            self._log("静默记录: 已启用（每 %.1fs 读一次当前会话，只写库、不调 AI）"
                      % self.interval)

        n = 0
        in_fast = False
        while not self._stop:
            t0 = time.perf_counter()
            try:
                self.tick()
            except (UiaTimeout, UiaError) as e:
                self.stats["errors"] += 1
                self._log("[记录] UIA 异常: %s" % e)
            except Exception as e:
                self.stats["errors"] += 1
                self._log("[记录] 异常: %s: %s" % (type(e).__name__, e))

            # ⚠️ 用 `_content_changed` 而**不是** tick 的返回值：
            #    发送者判定失败时的「强制重试」也会让返回值是真，
            #    而那种"变化"是假的 —— 窗口最小化时它会一直为真，
            #    快速档就永远退不出来（实测过这个坑）。
            iv = self._choose_interval()
            fast = self.fast_interval > 0 and iv == self.fast_interval
            if fast:
                self.stats["fast"] += 1
            if fast != in_fast:
                in_fast = fast
                self._log("[记录] %s采样（%.1fs/拍）"
                          % ("进入快速" if fast else "回到空闲", iv))

            n += 1
            if self.report_every > 0 and n % self.report_every == 0:
                s = self.stats
                # ⚠️ 「探针命中」这一栏是调频率用的关键指标：
                #    它 = 有多少拍真的付了那 250ms。
                #    · 一直 0 -> 你没在翻，探针白跑（可以把 probe_interval 调长）
                #    · 几乎每拍都命中 -> 屏幕上有什么东西一直在动
                #      （动画表情？），那 probe_interval 调长也不会省，得换思路
                self._log("[记录] 已轮 %d 次 | 新增 %d 条 | 无变化 %d | "
                          "跳过 %d（没窗口 %d / 没会话名 %d / 会话关 %d / "
                          "垃圾标题 %d / 未确认 %d / 切会话 %d）| "
                          "探针命中 %d | 快速档 %d | 错误 %d"
                          % (s["ticks"], s["saved"], s["idle"], s["skipped"],
                             s.get("skip_nowin", 0), s.get("skip_notitle", 0),
                             s.get("skip_disabled", 0),
                             s.get("skip_junk", 0), s.get("skip_unconfirmed", 0),
                             s.get("skip_race", 0),
                             s.get("probe", 0), s["fast"], s["errors"]))

            dt = time.perf_counter() - t0
            time.sleep(max(0.02, iv - dt))

    def start(self) -> None:
        """后台线程启动（daemon，不阻塞退出）。"""
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self.run, name="recorder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
