# -*- coding: utf-8 -*-
"""轮询循环与新消息识别。

分两层（见《需求与设计文档.md》5.1）：
  常态：轻量读会话列表（约 15ms）+ 轻量读当前聊天标题
  变化时：才做全量读取（约 50ms）取消息详情

新消息识别用「尾部长匹配法」而不是简单哈希去重 —— 因为同一句话
可能被连发两次，哈希会把第二条吃掉。
"""

import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .reader import ChatSnapshot, MessageItem, SessionItem
from .sessions import MODE_OFF, MODE_OBSERVE, MODE_ON, SessionRegistry
from .uia_worker import UiaError, UiaTimeout

TAIL_LEN = 20      # 每个会话保留最近多少条消息用于比对
MAX_ANCHOR = 6     # 匹配时最多用尾部多少条做锚


# ------------------------------------------------------------------ 增量识别

def to_keys(messages: List[MessageItem]) -> List[tuple]:
    """把消息列表转成用于比对的轻量标识序列。"""
    return [(m.is_self, m.sender_name, m.text) for m in messages]


def find_new_messages(old_tail: List[tuple], new_list: List[tuple]) -> List[tuple]:
    """尾部长匹配法：找出 new_list 里属于「新」的部分。

    做法：拿 old_tail 的**后缀**当锚，在 new_list 里找**第一次**出现的位置，
    该位置之后的就是新消息。

    两个关键取舍（都被单元测试逼出来的）：

    1. **从前往后找第一个匹配，不能从后往前。**
       同一句话可能被连发两次（旧 [A] / 新 [A, A]）。若从后往前找，
       会匹配到最后一个 A，于是返回空 —— 真正的新消息被吃掉了。

    2. **用后缀当锚，不用整段。**
       用户向上滚动后可见范围会变，用整段 tail 会永远匹配不上，
       进而退化成「只认最后一条」，漏掉中间的新消息。

    3. 锚从长到短依次尝试：长锚更可靠（不容易撞车），
       实在匹配不上再缩短，最后才退化为保守策略。
    """
    if not old_tail:
        return list(new_list)
    if not new_list:
        return []

    max_k = min(len(old_tail), MAX_ANCHOR)
    for k in range(max_k, 0, -1):
        anchor = old_tail[-k:]
        for start in range(len(new_list) - k + 1):
            if new_list[start:start + k] == anchor:
                return new_list[start + k:]

    # 完全匹配不上（界面滚动导致可见范围变化）→ 保守地只认最后一条，
    # 避免把整屏历史消息都当成新的。
    return [new_list[-1]]


# ------------------------------------------------------------------ 事件

@dataclass
class Event:
    kind: str                       # discovered / changed / messages
    session: str = ""
    preview: str = ""
    time_text: str = ""
    snapshot: Optional[ChatSnapshot] = None
    messages: Optional[List[MessageItem]] = None


# ------------------------------------------------------------------ 轮询器

class Watcher:
    """主轮询循环。"""

    def __init__(self, cfg, reader, registry: SessionRegistry,
                 sender=None, logger: Callable[[str], None] = None):
        self.cfg = cfg
        self.reader = reader
        self.registry = registry
        self.sender = sender          # SenderResolver，可为 None（则不判定发送者）
        self._log = logger or print

        self.interval = float(cfg.get("runtime.poll_interval", 1.0))
        self.show_ignored = bool(cfg.get("logging.show_ignored_sessions", True))

        self._stop = False
        self._session_fp: dict = {}      # 会话名 -> 上次看到的 (预览|时间)
        self._current_title: Optional[str] = None
        self._force_chat_read = False    # 切换会话后强制读一次详情
        self.stats = {"ticks": 0, "light_ms": 0.0, "full_reads": 0,
                      "errors": 0, "sender_resolved": 0}

    def stop(self) -> None:
        self._stop = True

    # -------------------------------------------------------------- 单次轮询

    def tick(self) -> List[Event]:
        events: List[Event] = []
        t0 = time.perf_counter()

        win = self.reader.find_window()
        if not win:
            self._log("[警告] 没找到微信窗口（需要微信已启动且主窗口存在）")
            return events
        hwnd = win["hwnd"]

        # --- 第 1 层：轻量读会话列表 + 当前聊天标题
        sessions = self.reader.read_sessions(hwnd)
        title_now = self.reader.read_chat_title(hwnd)
        t_light = (time.perf_counter() - t0) * 1000
        self.stats["light_ms"] += t_light

        if title_now != self._current_title:
            if title_now:
                self._log("[切换会话] -> %s" % title_now)
            self._current_title = title_now
            self._force_chat_read = True

        # --- 逐个会话：发现登记 / 会话开关过滤 / 变化检测
        changed_titles = []
        for s in sessions:
            if self.registry.discover(s.raw_title):
                events.append(Event("discovered", session=s.raw_title))
                # ⚠️ 这句话以前是写死的"默认关闭...加进 bootstrap_whitelist 即可启用"，
                #    那是**白名单时代**的说法。现在默认全开（黑名单排除），
                #    新会话本来就是启用的，照旧打印会把人误导成"它没生效"。
                if self.registry.is_enabled(s.raw_title):
                    self._log("[发现新会话] %s（已启用）" % s.raw_title)
                else:
                    self._log("[发现新会话] %s（未启用；当前是白名单模式，"
                              "或者它被写进了 sessions.disabled）" % s.raw_title)
                continue

            mode = self.registry.mode_of(s.raw_title)
            fp = s.fingerprint
            old_fp = self._session_fp.get(s.raw_title)
            self._session_fp[s.raw_title] = fp

            if mode == MODE_OFF:
                if old_fp is not None and old_fp != fp and self.show_ignored:
                    self._log("[忽略] %s 有新消息，但该会话未启用" % s.raw_title)
                continue

            if old_fp is None:
                # 首次看到，只记录基准，不报警（避免把历史消息当成新消息）
                self._log("[基线] %s 当前状态: %s (%s)"
                          % (s.raw_title, s.preview, s.time_text))
                continue

            if fp != old_fp:
                events.append(Event("changed", session=s.raw_title,
                                    preview=s.preview, time_text=s.time_text))
                self._log("[新消息] %s: %s  (%s)"
                          % (s.raw_title, s.preview or "<无预览>", s.time_text))
                changed_titles.append(s.raw_title)

        # --- 第 2 层：只在必要时才做全量读取
        need_full = self._force_chat_read or bool(changed_titles)
        if need_full and title_now:
            self._force_chat_read = False
            if not self.registry.is_enabled(title_now) and \
                    self.registry.mode_of(title_now) == MODE_OFF:
                # 当前会话没启用 -> 不读内容，保护隐私
                pass
            else:
                snap = self.reader.read_chat(hwnd)
                self.stats["full_reads"] += 1
                new_msgs = self._diff(title_now, snap)
                if new_msgs:
                    # 发送者判定只对新消息做 —— 截图 + OCR 有成本，不能每条历史消息都跑
                    if self.sender is not None:
                        try:
                            n = self.sender.resolve(hwnd, win["pid"], snap, targets=new_msgs)
                            self.stats["sender_resolved"] += n
                        except Exception as e:
                            self._log("[发送者判定失败] %s: %s" % (type(e).__name__, e))
                    events.append(Event("messages", session=title_now,
                                        snapshot=snap, messages=new_msgs))

        self.stats["ticks"] += 1
        return events

    def _diff(self, title: str, snap: ChatSnapshot) -> List[MessageItem]:
        """把本次读到的消息与上次比对，返回新增的消息对象。"""
        key = self.registry.key_for(title)
        cur = to_keys(snap.messages)
        old = self.registry.get_tail(key)

        if not old:
            # 首次见到这个会话：只记录基准，不产生「新消息」
            self.registry.set_tail(key, cur[-TAIL_LEN:])
            self._log("[基线] %s 记录了 %d 条消息用于后续比对" % (title, len(cur)))
            return []

        new_keys = find_new_messages(old, cur)
        self.registry.set_tail(key, cur[-TAIL_LEN:])
        if not new_keys:
            return []

        # new_keys 一定是 cur 的尾部切片，所以对应的也是 messages 的尾部切片
        k = len(new_keys)
        new_items = snap.messages[len(snap.messages) - k:] if k <= len(snap.messages) \
            else list(snap.messages)
        self._log("[增量] %s 发现 %d 条新消息" % (title, len(new_items)))
        return new_items

    # -------------------------------------------------------------- 主循环

    def run(self) -> None:
        self._log("=" * 66)
        self._log("开始轮询   间隔 %.2fs   Ctrl+C 退出" % self.interval)
        enabled = self.registry.enabled_sessions()
        self._log("已启用会话 (%d): %s" % (len(enabled), enabled or "（无，全部会被忽略）"))
        self._log("=" * 66)

        n = 0
        while not self._stop:
            t0 = time.perf_counter()
            self.stats["ticks"] += 0
            try:
                self.tick()
            except UiaTimeout as e:
                self.stats["errors"] += 1
                self._log("[警告] %s" % e)
            except UiaError as e:
                self.stats["errors"] += 1
                self._log("[错误] %s" % e)
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.stats["errors"] += 1
                self._log("[异常] %s: %s" % (type(e).__name__, e))

            n += 1
            if n % 30 == 0:
                self.registry.save()
                avg = self.stats["light_ms"] / max(1, self.stats["ticks"])
                self._log("[统计] 已轮询 %d 次 | 轻量读取均值 %.1fms | 全量 %d 次 | 错误 %d"
                          % (self.stats["ticks"], avg,
                             self.stats["full_reads"], self.stats["errors"]))

            dt = time.perf_counter() - t0
            time.sleep(max(0.02, self.interval - dt))

        self.registry.save()
        self._log("已停止，状态已保存。")
