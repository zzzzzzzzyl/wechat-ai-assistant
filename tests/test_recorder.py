# -*- coding: utf-8 -*-
"""静默记录器 + 「升级而非重复」去重 测试（离线，不需要真实微信）。

锁住两条容易回归的行为：

  1. **白名单必须被尊重**。mode=off 的会话「完全不读不记」是隐私承诺，
     记录器不能因为「反正已经读了」就顺手存下来 —— 那等于绕过白名单。

  2. **同一条消息不能存成两份**。发送者判定依赖截图，窗口被遮挡时会判不出来；
     同一条消息先记成 is_self=NULL、后来判出来了，如果直接新增就会多一份。
     正确做法是**升级**那一条。

用法：python tests/test_recorder.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.recorder import (  # noqa: E402
    PROBE_CHURN_N, PROBE_CHURN_SLOW, RACE_GIVEUP_N, Recorder)
from wechat_assistant.store import MessageStore  # noqa: E402

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


# ============================================================ 假对象

class FakeMsg:
    def __init__(self, text, is_self=None, sender_name=None):
        self.text = text
        self.is_self = is_self
        self.sender_name = sender_name

    @property
    def kind(self):
        return "text"


class FakeSnap:
    def __init__(self, title, messages, at_bottom=True):
        self.title = title
        self.messages = messages
        self.is_group = False
        self.container_rect = (0, 0, 100, 100)
        self._at_bottom = at_bottom

    def at_bottom(self, tolerance=20):
        return self._at_bottom

    def bottom_gap(self):
        return 0.0 if self._at_bottom else -200.0


class FakeReader:
    def __init__(self, title="测试会话", messages=None, at_bottom=True):
        self.title = title
        self.messages = messages or []
        self.at_bottom = at_bottom
        self.reads = 0

    def find_window(self):
        return {"hwnd": 1, "pid": 2}

    def read_chat_title(self, hwnd):
        return self.title

    def read_chat(self, hwnd):
        self.reads += 1
        return FakeSnap(self.title, list(self.messages), self.at_bottom)


class BoomReader(FakeReader):
    """每次读都炸，用来验证 run() 扛得住异常。"""

    def find_window(self):
        raise RuntimeError("模拟 UIA 炸了")


class FakeRegistry:
    """记录器用到的注册表接口（登记 / 模式 / 确认与否）。

    ⚠️ `auto_confirm=True`（默认）表示**标题已经在册**。老的测试关心的是**库的
       逻辑**（去重 / 重叠点 / 升级），不是"两道人墙"，所以默认放行。
       要测登记闸门（`_register_session`）就设 `auto_confirm=False` —— 那时
       `discover()` 走真的语义：**同一个标题要在不同的拍里看到两次**才登记。
    """

    def __init__(self, mode="on", auto_confirm=True):
        self.mode = mode
        self.auto_confirm = auto_confirm
        self.sessions = {}
        self.saves = 0
        self._pending = {}

    def mode_of(self, title):
        return self.mode

    def key_for(self, title):
        return title

    def discover(self, title):
        if title in self.sessions:
            return False
        if self.auto_confirm:
            self.sessions[title] = {"mode": self.mode}
            return True
        n = self._pending.get(title, 0) + 1
        self._pending[title] = n
        if n < 2:
            return False
        self._pending.pop(title, None)
        self.sessions[title] = {"mode": self.mode}
        return True

    def is_registered(self, title):
        return title in self.sessions

    def save(self):
        self.saves += 1


class FakeSender:
    """模拟发送者判定。

    ⚠️ 只填「还没判定」的（is_self 为 None），**不覆盖**测试自己设定的值。
    早先的版本无条件把每条都设成 verdict，结果指纹全变了 ——
    因为指纹含 is_self，这会悄悄改变去重/重叠点判断的结果，
    让测试结果和预期对不上（踩过一次）。
    """

    def __init__(self, verdict=True):
        self.calls = 0
        self.verdict = verdict

    def resolve(self, hwnd, pid, snap):
        self.calls += 1
        for m in snap.messages:
            if m.is_self is None:
                m.is_self = self.verdict
                m.sender_name = None
        return len(snap.messages)


class FakeCfg:
    def __init__(self, **kw):
        self._d = kw

    def get(self, key, default=None):
        return self._d.get(key, default)


def make_recorder(store, reader, registry, sender, interval=0.05):
    cfg = FakeCfg(**{"record.enabled": True, "record.interval": interval,
                     "record.report_every": 0})
    return Recorder(cfg, reader, store, registry, sender=sender, logger=lambda m: None)


class BlindSender:
    """一条都判不出来的发送者判定（模拟窗口最小化 / PrintWindow 只拿到空白）。"""

    def __init__(self):
        self.calls = 0

    def resolve(self, hwnd, pid, snap):
        self.calls += 1
        return 0        # 返回 0 -> recorder 会置 _need_retry


def make_adaptive(store, reader, registry, sender, interval=1.0,
                  fast=0.2, hold=3.0):
    cfg = FakeCfg(**{"record.enabled": True, "record.interval": interval,
                     "record.report_every": 0,
                     "record.fast_interval": fast, "record.fast_hold": hold})
    return Recorder(cfg, reader, store, registry, sender=sender,
                    logger=lambda m: None)


# ============================================================ 自适应采样

def test_probe_churn_backoff(store_path):
    """⚠️⚠️ **动画表情 / GIF 刷屏要让真读间隔自动放慢**（2026-10-11 用户实测）。

    用户报的原话：「一直循环？悬浮窗也没处理」，并补充
    「刚才的一直出现是因为有 gif」。

    机制：GIF 每帧都在变像素 → 探针每拍都判「变了」→ **每拍真读一屏**
    （380ms ≈ 38% 一个核），而且日志里那两行每秒刷一次，
    **看起来像死循环**（其实不是）。

    判据（为什么可靠）：GIF 变的是**像素**、不是**文字**。
    `_screen_changed()` 比的是**屏幕文本签名** —— 动画不产生新文字，
    所以签名一模一样；而"来了新消息 / 往上翻一屏"一定会改签名。

    ⚠️ 恢复必须是**自动**的：真有新内容就立刻回到正常间隔。
    """
    print("\n关于探针的动画退避")
    store = MessageStore(store_path, max_per_session=100)
    logs = []
    cfg = FakeCfg(**{"record.enabled": True, "record.interval": 1.0,
                     "record.report_every": 0,
                     "ocr_read.probe": True,
                     "ocr_read.probe_interval": 1.0,
                     "ocr_read.probe_fast": 0.5})
    reader = FakeReader()
    reader.background_read_ok = False        # 让 _choose_interval 走探针那条路
    r = Recorder(cfg, reader, store, FakeRegistry("on"), sender=FakeSender(),
                 logger=logs.append)

    base = r.probe_interval
    check("起手是正常间隔", abs(r._choose_interval() - base) < 1e-6,
          "%.2f" % r._choose_interval())

    # ① 连续几次「探针说变了、读出来没变」= 动画
    for _ in range(PROBE_CHURN_N):
        r._note_probe_result(False)
    slow = r._choose_interval()
    check("认定动画后真读间隔放慢了 %d 倍" % PROBE_CHURN_SLOW,
          abs(slow - base * PROBE_CHURN_SLOW) < 1e-6, "%.2f 秒" % slow)
    check("放慢后确实比原来慢（不然等于没修）", slow > base,
          "%.2f > %.2f" % (slow, base))

    # ② ⚠️ 日志必须**只报一次**（用户就是被刷屏日志误导成"死循环"的）
    churn_logs = [l for l in logs if "GIF" in l or "动画" in l]
    check("「动画刷屏」只报一次日志", len(churn_logs) == 1,
          "%d 条" % len(churn_logs))
    before = len(logs)
    for _ in range(10):
        r._note_probe_result(False)
    check("继续刷屏也不再重复报日志", len(logs) == before,
          "又多了 %d 条" % (len(logs) - before))

    # ③ ⚠️ 真有新内容 -> **自动恢复**（别写成"退避之后就不管了"）
    r._note_probe_result(True)
    check("真有新内容就恢复原间隔",
          abs(r._choose_interval() - base) < 1e-6, "%.2f" % r._choose_interval())
    check("恢复也报一次日志（不然用户以为一直慢着）",
          any("恢复正常" in l for l in logs), "")

    # ④ 恢复之后如果又刷屏，要能**再次**退避
    for _ in range(PROBE_CHURN_N):
        r._note_probe_result(False)
    check("再次刷屏能再次退避（恢复是真恢复，不是一次性）",
          abs(r._choose_interval() - base * PROBE_CHURN_SLOW) < 1e-6,
          "%.2f" % r._choose_interval())


def test_adaptive_off(store_path):
    print("\n11. 自适应：fast_interval=0 -> 退化成固定间隔")
    store = MessageStore(store_path, max_per_session=100)
    r = make_adaptive(store, FakeReader(), FakeRegistry(), FakeSender(), fast=0)
    check("一直用 interval", r._choose_interval() == 1.0)
    r._content_changed = True
    check("内容变了也一样（等于关掉了自适应）", r._choose_interval() == 1.0)
    store.close()


def test_adaptive_switch(store_path):
    print("\n12. 自适应：内容变了进快速档，安静够了回落")
    store = MessageStore(store_path, max_per_session=100)
    r = make_adaptive(store, FakeReader(), FakeRegistry(), FakeSender(),
                      interval=1.0, fast=0.2, hold=3.0)

    check("一开始是空闲档", r._choose_interval() == 1.0)

    r._content_changed = True
    check("内容变了 -> 快速档", r._choose_interval() == 0.2)

    r._content_changed = False
    check("刚变过、还没安静够 -> 仍快速（有迟滞，不来回抖）",
          r._choose_interval() == 0.2)

    r._fast_until = time.perf_counter() - 0.01       # 假装安静窗口过了
    check("安静够了 -> 回落空闲", r._choose_interval() == 1.0)
    store.close()


def test_adaptive_clamp(store_path):
    print("\n13. 自适应：上限/下限钳位")
    store = MessageStore(store_path, max_per_session=100)
    r = make_adaptive(store, FakeReader(), FakeRegistry(), FakeSender(),
                      interval=1.0, fast=0.001)
    check("太快会被钳到 0.05（一拍本身就要 100ms 上下）", r.fast_interval == 0.05)

    r2 = make_adaptive(store, FakeReader(), FakeRegistry(), FakeSender(),
                       interval=1.0, fast=0.2, hold=-5)
    check("负的 hold 钳到 0", r2.fast_hold == 0.0)
    store.close()


def test_adaptive_retry_is_not_change(store_path):
    """⚠️ 核心回归：发送者判定失败时的「强制重试」不算内容变化。

    窗口最小化时 `resolve()` 永远返回 0 -> `_need_retry` 一直为真 ->
    `_screen_changed()` 每拍都返回 True。如果拿**返回值**去驱动加速，
    快速档就永远退不出来（一直在 0.2s 狂读一个最小化的窗口）。
    """
    print("\n14. 自适应：强制重试 ≠ 内容变化（否则最小化时退不出快速档）")
    store = MessageStore(store_path, max_per_session=100)
    msgs = [FakeMsg("一模一样的一句话")]
    reader = FakeReader("会话", msgs)
    r = make_adaptive(store, reader, FakeRegistry(), BlindSender())

    r._screen_changed(msgs)                      # 建立基准
    r._need_retry = True                         # 模拟"判不出来"
    ret = r._screen_changed(msgs)                # 文本没变，但被强制重试

    check("强制重试时 _screen_changed 返回真", ret is True)
    check("但 _content_changed 是假（内容其实没变）", r._content_changed is False)
    check("所以不加速，仍在空闲档", r._choose_interval() == 1.0)
    store.close()


def test_adaptive_driven_by_tick(store_path):
    print("\n15. 自适应：真实 tick 驱动 _content_changed")
    store = MessageStore(store_path, max_per_session=100)
    msgs = [FakeMsg("第一句"), FakeMsg("第二句")]
    reader = FakeReader("会话", msgs)
    r = make_adaptive(store, reader, FakeRegistry(), FakeSender())

    r.tick()
    check("第一次读到内容 -> 算变了", r._content_changed is True)
    check("于是进快速档", r._choose_interval() == 0.2)

    r._fast_until = 0.0
    r.tick()
    check("内容没变 -> 不算变", r._content_changed is False)
    check("没变就留在空闲档", r._choose_interval() == 1.0)

    reader.messages = [FakeMsg("第一句"), FakeMsg("第二句"), FakeMsg("新的")]
    r.tick()
    check("来了新消息 -> 又算变了", r._content_changed is True)
    store.close()


# ============================================================ 1. 白名单

def test_whitelist(store_path):
    print("\n1. 白名单：off 的会话绝对不读不记")
    store = MessageStore(store_path, max_per_session=100)
    msgs = [FakeMsg("你好"), FakeMsg("在吗")]

    reg_off = FakeRegistry("off")
    reader = FakeReader("别人的私聊", msgs)
    sender = FakeSender()
    r = make_recorder(store, reader, reg_off, sender)
    n = r.tick()

    check("off 会话不记录", n == 0 and store.count("别人的私聊") == 0,
          "新增 %d 条，库里 %d 条" % (n, store.count("别人的私聊")))
    check("off 会话连消息正文都不读", reader.reads == 0,
          "read_chat 被调用 %d 次" % reader.reads)
    check("off 会话不做发送者判定", sender.calls == 0)
    check("计入 skipped 统计", r.stats["skipped"] == 1)
    store.close()


# ============================================================ 2. 正常记录

def test_record_on_and_observe(store_path):
    print("\n2. on / observe 都记录；off 不记录")
    for mode, expect in (("on", True), ("observe", True), ("off", False)):
        store = MessageStore(store_path + "." + mode, max_per_session=100)
        reader = FakeReader("会话" + mode, [FakeMsg("你好"), FakeMsg("在吗")])
        sender = FakeSender()
        r = make_recorder(store, reader, FakeRegistry(mode), sender)
        n = r.tick()
        got = store.count("会话" + mode) == 2
        check("mode=%-8s -> 记录=%s" % (mode, "是" if expect else "否"),
              got == expect, "新增 %d 条，库里 %d 条" % (n, store.count("会话" + mode)))
        store.close()


# ============================================================ 3. 空闲优化

def test_idle_skip(store_path):
    print("\n3. 内容没变时跳过发送者判定（省掉 ~45ms）")
    store = MessageStore(store_path, max_per_session=100)
    reader = FakeReader("会话A", [FakeMsg("你好"), FakeMsg("在吗")])
    sender = FakeSender()
    r = make_recorder(store, reader, FakeRegistry("on"), sender)

    n1 = r.tick()
    calls_after_first = sender.calls
    n2 = r.tick()

    check("第一次记下 2 条", n1 == 2, "新增 %d" % n1)
    check("第一次做了判定", calls_after_first == 1, "调用 %d 次" % calls_after_first)
    check("第二次没新增", n2 == 0, "新增 %d" % n2)
    check("第二次跳过了判定", sender.calls == calls_after_first,
          "累计调用 %d 次" % sender.calls)
    check("计入 idle 统计", r.stats["idle"] == 1)

    # 来了一条新的 -> 应该再次判定并记下
    reader.messages.append(FakeMsg("新的消息"))
    n3 = r.tick()
    check("新消息被记下", n3 == 1 and store.count("会话A") == 3,
          "新增 %d，库里 %d" % (n3, store.count("会话A")))
    check("新消息触发了判定", sender.calls == calls_after_first + 1)
    store.close()


# ============================================================ 4. 异常与边界

def test_robust(store_path):
    print("\n4. 边界情况")
    store = MessageStore(store_path, max_per_session=100)

    # 找不到窗口
    class NoWindow(FakeReader):
        def find_window(self):
            return None

    r = make_recorder(store, NoWindow(), FakeRegistry("on"), FakeSender())
    check("找不到窗口时返回 0 且不崩", r.tick() == 0)

    # 读不到标题
    class NoTitle(FakeReader):
        def read_chat_title(self, hwnd):
            return ""

    r = make_recorder(store, NoTitle(), FakeRegistry("on"), FakeSender())
    check("读不到标题时返回 0", r.tick() == 0)

    # 没有消息
    r = make_recorder(store, FakeReader("空会话", []), FakeRegistry("on"), FakeSender())
    check("会话没有消息时返回 0", r.tick() == 0)

    # 关闭时完全不动作
    cfg = FakeCfg(**{"record.enabled": False, "record.interval": 0.05,
                     "record.report_every": 0})
    off = Recorder(cfg, FakeReader("x", [FakeMsg("hi")]), store,
                   FakeRegistry("on"), sender=FakeSender(), logger=lambda m: None)
    check("record.enabled=false 时 tick 直接返回", off.tick() == 0)
    check("record.enabled=false 时 stats 不动", off.stats["ticks"] == 0)

    # run() 必须扛得住异常，不能让线程悄悄死掉
    boom = make_recorder(store, BoomReader(), FakeRegistry("on"), FakeSender())
    t = threading.Thread(target=boom.run, daemon=True)
    t.start()
    time.sleep(0.35)
    boom.stop()
    t.join(timeout=3.0)
    check("run() 扛住异常并继续循环",
          boom.stats["errors"] > 1 and boom.stats["ticks"] > 1,
          "轮询 %d 次，错误 %d 次" % (boom.stats["ticks"], boom.stats["errors"]))
    store.close()


# ============================================================ 5. 升级而非重复

def test_upgrade_instead_of_duplicate(store_path):
    print("\n5. 同一条消息不能存成两份（? -> 对方 要升级，不要新增）")
    store = MessageStore(store_path, max_per_session=100)
    key = "张三"

    # 第一次：窗口被遮挡，发送者没判出来
    store.save_messages(key, [FakeMsg("现在中秋节"), FakeMsg("一个人在那边")])
    check("先记下 2 条（未判定）", store.count(key) == 2, "%d 条" % store.count(key))
    rows = store.get_recent(key, 10)
    check("此时 is_self 为空", all(r["is_self"] is None for r in rows),
          str([r["is_self"] for r in rows]))

    # 第二次：窗口可见了，判出来了 —— 应该升级，不是新增
    added = store.save_messages(key, [FakeMsg("现在中秋节", is_self=False),
                                      FakeMsg("一个人在那边", is_self=False)])
    rows = store.get_recent(key, 10)
    check("没有新增行", added == 0 and store.count(key) == 2,
          "新增 %d，库里 %d 条" % (added, store.count(key)))
    check("旧记录被补全成「对方」", all(r["is_self"] == 0 for r in rows),
          str([r["is_self"] for r in rows]))

    # 已经判定过的，重复读也不该再动
    added = store.save_messages(key, [FakeMsg("现在中秋节", is_self=False)])
    check("已判定的重复读不新增", added == 0 and store.count(key) == 2,
          "新增 %d，库里 %d 条" % (added, store.count(key)))

    # 自己的消息走正常新增路径
    added = store.save_messages(key, [FakeMsg("我不回去", is_self=True)])
    check("新的自己消息正常新增", added == 1 and store.count(key) == 3,
          "新增 %d，库里 %d 条" % (added, store.count(key)))
    store.close()


# ============================================================ 6. 库尾 ≠ 屏幕

def test_db_tail_diverges(store_path):
    print("\n6. 回归：库里尾部与屏幕对不上时，仍要正确判断「屏幕没变」")

    # 这是实测踩到的坑：最初拿「库里的最近 N 条」和「屏幕上的 N 条」比对，
    # 但库是累积的 —— 往上翻历史时 recorder 会把「对库是新的、对屏幕是旧的」
    # 消息也追加进去，库尾从此和屏幕尾对不上，判断永远返回「变了」，
    # 每次轮询都白做一遍发送者判定（日志里表现为「无变化 0」）。
    store = MessageStore(store_path, max_per_session=100)
    key = "会话S"
    reader = FakeReader(key, [FakeMsg("老一"), FakeMsg("老二")])
    sender = FakeSender()
    r = make_recorder(store, reader, FakeRegistry("on"), sender)

    r.tick()
    check("先记下 2 条", store.count(key) == 2, "%d 条" % store.count(key))

    # 模拟「用户往上翻，更早的消息被追加进库」-> 库尾换了，但屏幕没变
    store.save_messages(key, [FakeMsg("更早的甲", is_self=False),
                              FakeMsg("更早的乙", is_self=False)])
    calls_before = sender.calls
    n = r.tick()

    check("屏幕没变时仍然跳过", n == 0 and r.stats["idle"] == 1,
          "新增 %d，idle %d" % (n, r.stats["idle"]))
    check("没有白做发送者判定", sender.calls == calls_before,
          "判定调用 %d 次（之前 %d 次）" % (sender.calls, calls_before))

    # 屏幕真的变了才应该动作
    reader.messages.append(FakeMsg("新来的"))
    n = r.tick()
    check("屏幕变了才记录", n == 1 and sender.calls == calls_before + 1,
          "新增 %d，判定调用 %d 次" % (n, sender.calls))
    store.close()


# ============================================================ 7. 重试

def test_retry_when_unresolved(store_path):
    print("\n7. 一条都没判出发送者时，下次要强制重试（别让 ?  烂在库里）")

    class FailSender:
        """模拟窗口被完全遮挡：一条都判不出来。"""

        def __init__(self):
            self.calls = 0

        def resolve(self, hwnd, pid, snap):
            self.calls += 1
            return 0

    store = MessageStore(store_path, max_per_session=100)
    reader = FakeReader("会话R", [FakeMsg("一"), FakeMsg("二")])
    sender = FailSender()
    r = make_recorder(store, reader, FakeRegistry("on"), sender)

    r.tick()
    check("第一步：仍然把内容记下来了（不因为判不出作者就丢数据）",
          store.count("会话R") == 2, "%d 条" % store.count("会话R"))
    rows = store.get_recent("会话R", 10)
    check("此时作者未知", all(x["is_self"] is None for x in rows),
          str([x["is_self"] for x in rows]))

    # 屏幕内容没变，但上次没判出来 -> 必须再试一次
    r.tick()
    check("屏幕没变也强制重试了", sender.calls == 2, "判定调用 %d 次" % sender.calls)

    # 这次判出来了 -> 应该走升级，不是新增
    class OkSender:
        def resolve(self, hwnd, pid, snap):
            for m in snap.messages:
                m.is_self = False
            return len(snap.messages)

    r.sender = OkSender()
    r.tick()
    rows = store.get_recent("会话R", 10)
    check("补全后作者不再是未知", all(x["is_self"] == 0 for x in rows),
          str([x["is_self"] for x in rows]))
    check("补全没有产生重复行", store.count("会话R") == 2,
          "%d 条" % store.count("会话R"))
    store.close()


# ============================================================ 8. 指纹冲突

def test_no_fingerprint_conflict(store_path):
    print("\n8. 回归：'升级'不能撞 UNIQUE（实机报过 IntegrityError）")

    # 实机报错：IntegrityError: UNIQUE constraint failed:
    #           messages.session_key, messages.fingerprint
    # 复现序列（很真实：窗口可见 -> 被遮挡 -> 又可见）：
    #   ① 可见时记下 (X, 我)   -> 指纹 fp("1|X")
    #   ② 遮挡时又记成 (X, ?)  -> 指纹 fp("?|X")，两行并存
    #   ③ 再次可见时，旧的升级逻辑去 UPDATE 第②行、把它的指纹改成 fp("1|X")
    #      —— 而第①行已经是这个指纹了，直接撞 UNIQUE。
    store = MessageStore(store_path, max_per_session=100)
    key = "会话C"

    store.save_messages(key, [FakeMsg("这句话说得好", is_self=True)])
    check("① 先记下（作者已知）", store.count(key) == 1, "%d 条" % store.count(key))

    store.save_messages(key, [FakeMsg("这句话说得好")])          # 作者未知
    check("② 遮挡时又记成一条（作者未知）", store.count(key) == 2,
          "%d 条" % store.count(key))

    try:
        store.save_messages(key, [FakeMsg("这句话说得好", is_self=True)])   # ③ 再来
        check("③ 再次读到不抛异常", True)
    except Exception as e:
        check("③ 再次读到不抛异常", False, "%s: %s" % (type(e).__name__, e))

    # 正确版本已在库里 -> 那条「作者未知」的孪生行会被清掉（见第 9 节），只剩 1 条
    check("没有多出一行（孪生行被清掉）", store.count(key) == 1,
          "%d 条" % store.count(key))

    # 冲突之后连接还能继续用（之前会卡在半开事务里，每 2 秒刷一条异常）
    n = store.save_messages(key, [FakeMsg("换个内容", is_self=False)])
    check("冲突后连接仍然可用", n == 1 and store.count(key) == 2,
          "新增 %d，库里 %d 条" % (n, store.count(key)))
    store.close()


def test_cleanup_unknown_twin(store_path):
    print("\n9. 窗口最小化期间留下的「作者未知」孪生行，恢复后要清掉")

    # 实机序列：窗口可见时记下正确版本 -> 最小化时又记了一条作者未知的 ->
    # 恢复后虽然判出来了，但正确版本已在库里，于是那个 ? 一直留着，
    # 进 prompt 就是「?: xxx」。实测留下了 5 组（'有'/'还真'/'图片' 等）。
    store = MessageStore(store_path, max_per_session=100)
    key = "会话T"

    store.save_messages(key, [FakeMsg("有", is_self=False)])
    check("① 可见时记下（作者=对方）", store.count(key) == 1,
          "%d 条" % store.count(key))

    store.save_messages(key, [FakeMsg("有")])            # 最小化期间
    check("② 最小化时又记成一条（作者未知）", store.count(key) == 2,
          "%d 条" % store.count(key))

    store.save_messages(key, [FakeMsg("有", is_self=False)])   # 恢复了
    check("③ 恢复后被清掉，只剩一条", store.count(key) == 1,
          "%d 条" % store.count(key))
    rows = store.get_recent(key, 10)
    check("留下的是带作者的那条", rows and rows[0]["is_self"] == 0,
          str([(r["content"], r["is_self"]) for r in rows]))

    # 不同内容不能误删
    store.save_messages(key, [FakeMsg("有", is_self=True)])
    check("同内容但作者不同会各留一行", store.count(key) == 2,
          "%d 条" % store.count(key))
    store.close()


def test_no_old_messages_appended(store_path):
    print("\n10. 翻历史时旧消息不能接到库尾（靠内容重叠点判断，不看滚动位置）")

    # 用户报过：往上翻历史时那些旧消息被当成新消息接到**库尾**，
    # 而 get_recent(20) 按 id 取「最近 20 条」-> AI 拿到的成了旧消息。
    #
    # 最早用「列表在底部才记录」来防，但那个判据是在**间接代理**另一个问题，
    # 而且在虚拟化列表上有盲区（最后一条滚出渲染区后会换成上一条，
    # 「滚动距离 == 消息高度」时 gap 归零 -> 误判成在底部）。用户指出它别扭，
    # 确实如此。现在直接回答「哪几条还没记过」：拿指纹和库里比对，
    # 找最后一个已经在库里的可见消息，它之后的才是新的 —— 与滚动位置无关。
    store = MessageStore(store_path, max_per_session=100)
    key = "会话U"

    # 先攒一段已经在库里的历史
    store.save_messages(key, [FakeMsg("消息A", is_self=False),
                              FakeMsg("消息B", is_self=True),
                              FakeMsg("消息C", is_self=False),
                              FakeMsg("消息D", is_self=True)])
    check("先攒了 4 条历史", store.count(key) == 4, "%d 条" % store.count(key))

    # ① 在底部，来了两条新的 -> 应当写进去
    reader = FakeReader(key, [FakeMsg("消息C", is_self=False),
                              FakeMsg("消息D", is_self=True),
                              FakeMsg("消息E", is_self=False),
                              FakeMsg("消息F", is_self=True)])
    sender = FakeSender()
    r = make_recorder(store, reader, FakeRegistry("on"), sender)
    n = r.tick()
    check("在底部来了新消息 -> 写入", n == 2 and store.count(key) == 6,
          "新增 %d，库里 %d 条" % (n, store.count(key)))

    # ② 往上翻历史：可见的全是旧的 -> 一条都不该写
    reader.messages = [FakeMsg("消息A", is_self=False),
                       FakeMsg("消息B", is_self=True),
                       FakeMsg("消息C", is_self=False)]
    n = r.tick()
    check("翻历史 -> 不写", n == 0 and store.count(key) == 6,
          "新增 %d，库里 %d 条" % (n, store.count(key)))

    # ③ 关键：**不在底部，但确实有新消息** —— 旧判据会整轮跳过，新判据认得出来
    reader.messages = [FakeMsg("消息D", is_self=True),
                       FakeMsg("消息E", is_self=False),
                       FakeMsg("消息F", is_self=True),
                       FakeMsg("消息G", is_self=False)]
    n = r.tick()
    check("不在底部但确实有新消息 -> 只写新的那条",
          n == 1 and store.count(key) == 7,
          "新增 %d，库里 %d 条" % (n, store.count(key)))

    # ④ 库尾顺序没被打乱：最后一条应当是 G
    last = store.last_row(key)
    check("库尾仍是真正最新的那条", last and last["content"] == "消息G",
          repr(last["content"] if last else None))

    # ⑤ 整屏都不在库里（翻到了程序开始记录之前的消息）-> 不写
    #    ⚠️ 2026-10-14 改：**不再看 at_bottom** —— 那个信号在快速上翻历史时不可靠
    #       （虚拟列表盲区：最后一条滚出渲染区后 gap 归零），实测把同一屏反复
    #       当新消息追加（最近 10 分钟 38 行里 24 行是库里已有的内容）。
    reader.messages = [FakeMsg("史前消息1", is_self=False),
                       FakeMsg("史前消息2", is_self=True)]
    reader.at_bottom = False
    n = r.tick()
    check("整屏都不在库里 + 不在底部 -> 不写（位置无法确定）",
          n == 0 and store.count(key) == 7, "新增 %d，库里 %d 条" % (n, store.count(key)))
    reader.at_bottom = True
    n = r.tick()
    check("⚠️ 整屏都不在库里 + **在底部**也 -> 不写（2026-10-14 改）",
          n == 0 and store.count(key) == 7, "新增 %d，库里 %d 条" % (n, store.count(key)))
    reader.at_bottom = False

    # ⑥ 空库：首次记录，全部写入
    store2 = MessageStore(store_path + ".fresh", max_per_session=100)
    reader2 = FakeReader("新会话", [FakeMsg("第一条", is_self=False),
                                    FakeMsg("第二条", is_self=True)])
    r2 = make_recorder(store2, reader2, FakeRegistry("on"), FakeSender())
    n = r2.tick()
    check("空库时全部当新消息写入", n == 2 and store2.count("新会话") == 2,
          "新增 %d，库里 %d 条" % (n, store2.count("新会话")))
    # ⑦ 边界：**新消息多于一屏**（离开一会儿来了 30 条，回来只看到最后一屏）
    #    ⚠️ 2026-10-14 语义变了：这里**不再兜底写入**。理由是"上翻历史"和
    #       "离开后回来"这两种情形**在数据上分不开**（都是"这屏一条都没记过"），
    #       而选错一边的代价不对称：
    #         · 误写  -> 把翻上来的旧消息接到库尾（顺序错、重复，污染口吻样本）；
    #         · 不写  -> 这几条不进消息表 —— 但**同一屏已经无条件进了屏快照**，
    #                    喂给 AI 的上下文来自 screens.stitch()，不靠这张表。
    store3 = MessageStore(store_path + ".multi", max_per_session=100)
    store3.save_messages("会话M", [FakeMsg("离开前的最后一条", is_self=True)])
    check("离开前库里只有 1 条", store3.count("会话M") == 1,
          "%d 条" % store3.count("会话M"))

    many = [FakeMsg("新消息%d" % i, is_self=(i % 2 == 0)) for i in range(1, 8)]
    reader3 = FakeReader("会话M", many, at_bottom=True)
    r3 = make_recorder(store3, reader3, FakeRegistry("on"), FakeSender())
    n = r3.tick()
    check("⚠️ 新消息多于一屏 + 在底部 -> 也不写（宁可不写，也不接错位置）",
          n == 0 and store3.count("会话M") == 1,
          "新增 %d，库里 %d 条" % (n, store3.count("会话M")))

    # ⚠️ 但"首次见到这个会话"（库里一条都没有）仍然整屏写 —— 那条路没变
    store5 = MessageStore(store_path + ".first", max_per_session=100)
    reader5 = FakeReader("会话Z", [FakeMsg("甲", is_self=False),
                                   FakeMsg("乙", is_self=True)], at_bottom=False)
    r5 = make_recorder(store5, reader5, FakeRegistry("on"), FakeSender())
    n = r5.tick()
    check("库里没这个会话 -> 整屏写（首次记录不受影响）",
          n == 2 and store5.count("会话Z") == 2,
          "新增 %d，库里 %d 条" % (n, store5.count("会话Z")))
    store5.close()

    # 同一个场景，但不在底部 -> 不是新消息，是翻到了旧消息 -> 不写
    store4 = MessageStore(store_path + ".multi2", max_per_session=100)
    store4.save_messages("会话N", [FakeMsg("最近的", is_self=True)])
    reader4 = FakeReader("会话N",
                         [FakeMsg("远古消息%d" % i, is_self=False) for i in range(1, 5)],
                         at_bottom=False)
    r4 = make_recorder(store4, reader4, FakeRegistry("on"), FakeSender())
    n = r4.tick()
    check("同样找不到重叠点，但不在底部 -> 不写（那是旧消息）",
          n == 0 and store4.count("会话N") == 1,
          "新增 %d，库里 %d 条" % (n, store4.count("会话N")))
    store4.close()
    store3.close()
    store.close()


def test_junk_title_not_recorded(store_path):
    print("\n11. ⚠️ 垃圾标题**不进库**（连正文都不读）")

    # 用户报的「翻记录时冒出一堆不知道从哪来的记录」：库里躺着 15 个会话名，
    # 而 state.json 里只登记了 2 个 —— 因为 `is_junk_title` / 两次确认
    # 原来只挡 state.json，记录器是直接 key_for(title) 就写库的。
    store = MessageStore(store_path, max_per_session=100)
    for junk in ("X", "+", "①", "0", "G"):
        reader = FakeReader(junk, [FakeMsg("随便什么", is_self=False)])
        r = make_recorder(store, reader, FakeRegistry("on"), FakeSender())
        n = r.tick()
        check("垃圾标题 %r 一条都不写" % junk, n == 0 and store.count(junk) == 0,
              "写入 %d，库里 %d 条" % (n, store.count(junk)))
        check("  ↑ 而且**正文都没读**（省掉那一次读屏）", reader.reads == 0,
              "读了 %d 次" % reader.reads)
        check("  ↑ 记进 skip_junk（不是混进 skip_disabled）",
              r.stats.get("skip_junk") == 1, str(r.stats.get("skip_junk")))

    # 正常名字照旧（别一刀切按长度砍 —— '白' / '周' 是正常昵称）
    reader = FakeReader("白", [FakeMsg("在吗", is_self=False)])
    r = make_recorder(store, reader, FakeRegistry("on"), FakeSender())
    check("单个**汉字**是正常昵称，照记", r.tick() == 1 and store.count("白") == 1,
          "库里 %d 条" % store.count("白"))
    store.close()


def test_unconfirmed_session_not_recorded(store_path):
    print("\n12. ⚠️ 只给**确认过的会话**写库（两次确认挪到每拍数）")

    store = MessageStore(store_path, max_per_session=100)
    reg = FakeRegistry("on", auto_confirm=False)      # 走真的"两次确认"语义
    reader = FakeReader("新会话", [FakeMsg("第一条", is_self=False),
                                   FakeMsg("第二条", is_self=True)])
    r = make_recorder(store, reader, reg, FakeSender())

    n = r.tick()
    check("第一拍：还没确认 -> 不写库", n == 0 and store.count("新会话") == 0,
          "写入 %d，库里 %d 条" % (n, store.count("新会话")))
    check("  ↑ 而且**正文都没读**（闸门在读屏之前）", reader.reads == 0,
          "读了 %d 次" % reader.reads)
    check("  ↑ 记进 skip_unconfirmed",
          r.stats.get("skip_unconfirmed") == 1, str(r.stats.get("skip_unconfirmed")))

    n = r.tick()
    check("第二拍：确认了 -> 照记（不会因为少记而丢内容）",
          n == 2 and store.count("新会话") == 2,
          "写入 %d，库里 %d 条" % (n, store.count("新会话")))
    check("首次登记要落盘（强杀 / 崩溃不能把名单丢了）", reg.saves >= 1,
          "save 了 %d 次" % reg.saves)
    check("确认之后不再算跳过", r.stats.get("skip_unconfirmed") == 1,
          str(r.stats.get("skip_unconfirmed")))
    store.close()


class RaceReader(FakeReader):
    """标题读取按调用次序给值 —— 用来复现「读完标题就切了会话」。

    ⚠️ 一拍里标题会被读**两次**（开头一次、读完正文确认一次），所以序列
       要按"读标题的次数"排，不是按拍数。
    """

    def __init__(self, seq, messages=None, cycle=False):
        super().__init__(seq[0], messages)
        self.seq = list(seq)
        self.cycle = cycle
        self.n = 0

    def read_chat_title(self, hwnd):
        i = (self.n % len(self.seq)) if self.cycle else min(self.n, len(self.seq) - 1)
        self.n += 1
        return self.seq[i]


def test_session_race_skip_and_recover(store_path):
    print("\n13. ⚠️ 切会话竞态：标题前后不一致 -> 这一拍不写，**下一拍补上**")

    # 取证（CLAUDELOG 第五十八轮）：tick 是"先读标题、再读正文"，切会话正好
    # 落在两步之间时这一拍拿到 (旧标题, 新正文) -> 同一段正文记到错误的会话名下。
    # 现在：正文读完后再读一次标题，两次不一致就不写这一拍。
    store = MessageStore(store_path, max_per_session=100)
    msgs = [FakeMsg("碱水包", is_self=False), FakeMsg("皮韧咸香", is_self=False)]
    reader = RaceReader(["示例代购群", "示例群", "示例群", "示例群"], msgs)
    r = make_recorder(store, reader, FakeRegistry("on"), FakeSender())

    n = r.tick()
    check("这一拍标题不一致 -> 不写", n == 0 and store.count("示例代购群") == 0,
          "写入 %d，示例代购群库里 %d 条" % (n, store.count("示例代购群")))
    check("  ↑ 记进 skip_race", r.stats.get("skip_race") == 1,
          str(r.stats.get("skip_race")))
    check("  ↑ 内容**没记到错的那个会话名下**", store.count("示例群") == 0,
          "示例群库里 %d 条" % store.count("示例群"))

    n = r.tick()
    check("下一拍标题稳定了 -> 照记（**不是丢数据**，只是晚一拍）",
          n == 2 and store.count("示例群") == 2,
          "写入 %d，示例群库里 %d 条" % (n, store.count("示例群")))
    check("  ↑ 也没有多写一屏重复的", store.count("示例代购群") == 0,
          "示例代购群库里 %d 条" % store.count("示例代购群"))
    store.close()


def test_session_race_gives_up(store_path):
    print("\n14. ⚠️ 标题**一直对不上**（OCR 读不稳）-> 连续几次之后放行，别永久不写")

    # ⚠️ 这条兜底是必须的：OCR 对同一屏两次抓图可能给出不同的字。如果一直拦，
    #    就成了"这个会话永远不记录"——那是更糟的**静默丢数据**。
    store = MessageStore(store_path, max_per_session=100)
    msgs = [FakeMsg("在吗", is_self=False)]
    reader = RaceReader(["甲会话", "乙会话"], msgs, cycle=True)     # 永远对不上
    r = make_recorder(store, reader, FakeRegistry("on"), FakeSender())

    check("第 1 拍拦下", r.tick() == 0 and store.count("甲会话") == 0,
          "库里 %d 条" % store.count("甲会话"))
    check("第 2 拍拦下", r.tick() == 0 and store.count("甲会话") == 0,
          "库里 %d 条" % store.count("甲会话"))
    n = r.tick()
    check("第 %d 拍放行（判为标题读不稳，不是切会话）" % RACE_GIVEUP_N,
          n == 1 and store.count("甲会话") == 1,
          "写入 %d，库里 %d 条" % (n, store.count("甲会话")))
    check("  ↑ 只拦了 %d 拍" % (RACE_GIVEUP_N - 1),
          r.stats.get("skip_race") == RACE_GIVEUP_N - 1,
          str(r.stats.get("skip_race")))
    store.close()


def test_bottom_unknown_no_write(store_path):
    print("\n15. ⚠️ 找不到重叠点 -> **一律不写**（2026-10-14 起不再看 at_bottom）")

    # 实测（2026-10-14，用户快速上翻历史那一段）：
    #   · screens.db 40 屏里 **15 屏** at_bottom=True，而用户明明在往上翻；
    #     UIA 的 gap 判据有已知盲区（最后一条滚出渲染区 -> gap 归零）；
    #   · 于是同一屏被反复当"新消息"追加：最近 10 分钟 38 行里 **24 行**是
    #     库里已有的内容，库尾同一句话存了 3 份（sender 还各不相同）。
    # 现在 `bottom_known` / `at_bottom` **都不再参与决定**：cut < 0 一律不写。
    store = MessageStore(store_path, max_per_session=100)
    store.save_messages("会话P", [FakeMsg("离开前的最后一条", is_self=True)])

    msgs = [FakeMsg("远古消息%d" % i, is_self=False) for i in range(1, 5)]
    n = store.save_new_only("会话P", msgs, at_bottom=True, bottom_known=False)
    check("at_bottom=True 但**不知道**是不是底部 -> 不写",
          n == 0 and store.count("会话P") == 1, "写入 %d 条" % n)

    n = store.save_new_only("会话P", msgs, at_bottom=True, bottom_known=True)
    check("⚠️ 同一屏，UIA 那条路（bottom_known=True）**也不写**（行为已统一）",
          n == 0 and store.count("会话P") == 1, "写入 %d 条" % n)

    n = store.save_new_only("会话P", msgs, at_bottom=False, bottom_known=True)
    check("不在底部时更不写", n == 0 and store.count("会话P") == 1, "写入 %d 条" % n)

    # 重叠点找得到时照旧"只写新的"（这才是正常路径）
    store2 = MessageStore(store_path + ".b", max_per_session=100)
    store2.save_messages("会话Q", [FakeMsg("消息A", is_self=False),
                                   FakeMsg("消息B", is_self=True)])
    n = store2.save_new_only("会话Q",
                             [FakeMsg("消息A", is_self=False),
                              FakeMsg("消息B", is_self=True),
                              FakeMsg("消息C", is_self=False)],
                             at_bottom=True, bottom_known=False)
    check("有重叠点时照常只写新的",
          n == 1 and store2.count("会话Q") == 3, "写入 %d 条" % n)
    store2.close()
    store.close()


class FakeProbeReader(FakeReader):
    """模拟 **OCR 后端**：屏幕像素不变 -> 探针判"没变"。

    ⚠️ 它的关键性质是：标题**只在探针命中（屏幕变了）的那拍才读**
        （那是设计，见 `tick` 里那段"探针提前"的说明）。
    """

    background_read_ok = False

    def __init__(self, title="新会话", messages=None):
        super().__init__(title, messages)
        self.probes = 0
        self.titles = 0
        self.h = b"\x07" * 64

    def read_chat_title(self, hwnd):
        self.titles += 1
        return self.title

    def probe(self, hwnd):
        self.probes += 1
        return self.h           # 永远一样 -> 探针永远判"没变化"


def test_probe_mode_confirms_new_session(store_path):
    print("\n16. ⚠️ OCR 探针模式：新会话也要能凑满两次确认（否则头一屏白丢）")

    # ⚠️ 这一条是"两次确认挪到每拍数"的**必要条件**：探针模式下标题只在
    #    "屏幕变了"的拍才读，而那拍往往只有一次 —— 不特殊处理的话
    #    新会话永远确认不了，它头一屏会被整屏丢掉（比不登记更糟）。
    store = MessageStore(store_path, max_per_session=100)
    reg = FakeRegistry("on", auto_confirm=False)
    reader = FakeProbeReader("新会话", [FakeMsg("第一句", is_self=False),
                                        FakeMsg("第二句", is_self=True)])
    cfg = FakeCfg(**{"record.enabled": True, "record.interval": 1.0,
                     "record.report_every": 0,
                     "ocr_read.probe": True, "ocr_read.probe_interval": 1.0})
    r = Recorder(cfg, reader, store, reg, sender=FakeSender(),
                 logger=lambda m: None)

    check("第 1 拍：读标题数到第 1 次 -> 不写", r.tick() == 0 and store.count("新会话") == 0,
          "库里 %d 条" % store.count("新会话"))
    check("  ↑ 也没读整屏", reader.reads == 0, "读了 %d 次" % reader.reads)

    n = r.tick()
    check("第 2 拍：屏幕**没变**，但为了凑确认仍然读了标题，然后照记",
          n == 2 and store.count("新会话") == 2,
          "写入 %d，库里 %d 条，读标题 %d 次" % (n, store.count("新会话"), reader.titles))
    check("  ↑ 标题一共读了 3 次（每拍开头 + 写完确认）",
          reader.titles == 3, "读了 %d 次" % reader.titles)
    check("  ↑ 只有确认之后才真读整屏", reader.reads == 1, "读了 %d 次" % reader.reads)
    store.close()


def main():
    print("=" * 76)
    print("静默记录器 & 去重升级 测试")
    print("=" * 76)
    tmp = tempfile.mkdtemp(prefix="wca_rec_")
    try:
        test_whitelist(os.path.join(tmp, "w.db"))
        test_record_on_and_observe(os.path.join(tmp, "m.db"))
        test_idle_skip(os.path.join(tmp, "i.db"))
        test_robust(os.path.join(tmp, "r.db"))
        test_upgrade_instead_of_duplicate(os.path.join(tmp, "u.db"))
        test_db_tail_diverges(os.path.join(tmp, "d.db"))
        test_retry_when_unresolved(os.path.join(tmp, "t.db"))
        test_no_fingerprint_conflict(os.path.join(tmp, "c.db"))
        test_cleanup_unknown_twin(os.path.join(tmp, "k.db"))
        test_no_old_messages_appended(os.path.join(tmp, "n2.db"))
        test_junk_title_not_recorded(os.path.join(tmp, "junk.db"))
        test_unconfirmed_session_not_recorded(os.path.join(tmp, "unconf.db"))
        test_session_race_skip_and_recover(os.path.join(tmp, "race.db"))
        test_session_race_gives_up(os.path.join(tmp, "race2.db"))
        test_bottom_unknown_no_write(os.path.join(tmp, "bot.db"))
        test_probe_mode_confirms_new_session(os.path.join(tmp, "probe2.db"))
        test_probe_churn_backoff(os.path.join(tmp, "churn.db"))
        test_adaptive_off(os.path.join(tmp, "a1.db"))
        test_adaptive_switch(os.path.join(tmp, "a2.db"))
        test_adaptive_clamp(os.path.join(tmp, "a3.db"))
        test_adaptive_retry_is_not_change(os.path.join(tmp, "a4.db"))
        test_adaptive_driven_by_tick(os.path.join(tmp, "a5.db"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)      # 测试数据即用即删

    print("\n" + "=" * 76)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
