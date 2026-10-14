# -*- coding: utf-8 -*-
"""屏快照存储 + 缝合 —— 替代「边写边定位」的消息表。

════════════════════════════════════════════════════════════════════
为什么换掉旧做法

旧做法（`store.py` 的 `save_new_only`）是：每读到一屏，当场判断
「哪几条是新的、该接到库尾哪一条之后」，再写库。于是需要：

    重叠点 cut、cut=-1 时的 at_bottom 兜底、指纹去重、
    「升级而非新增」、清理「作者未知」的孪生行、occurrence…

这些判断一旦做错就**丢数据且不可逆**，而且它们恰好挡掉了「用户往上翻
聊天记录」——翻上来的内容在库里找不到重叠点，于是整屏不写。

新做法把「判断」挪到读取时：

    写入：一屏一条，不做新旧判断、不定位（只避免把同一屏存两遍）
    读取：从最新一屏往前缝，内容重叠就接上，接不上就停

于是：
  · 用户滑动翻历史 = 天然被记录（相邻屏本来就有重合），不需要任何特殊逻辑
  · 翻到不认识的地方  = 停下（宁可不接，也不要接错顺序）
  · 判定改主意（? -> 对方）= 新屏的判定自然覆盖旧的，不需要「升级」补丁
  · 同名同内容多条     = 是序列里的不同位置，天然区分

时效性优先：缝合只保留「连得上的那一段」，链一断就停 ——
因为进 prompt 的本来就只有最近一二十条。

代价（已知并接受）：
  · 相邻屏大量重叠，同一个内容会被存多次 —— 存储不花 token，且只留最近 K 屏
  · 拖滚动条 / PageUp 这种大跳跃会让相邻屏不重合 -> 断链 -> 只保留最新那段
  · 消息级时间戳依然拿不到（UIA 不给）
════════════════════════════════════════════════════════════════════
"""

import difflib
import json
import os
import re
import sqlite3
import threading
import time
from typing import Callable, List, Optional, Tuple

# 每个会话保留最近多少屏（一屏约 5~8 条，40 屏 ≈ 300 条）
DEFAULT_KEEP = 40

# 去重时**回头看多少屏**，而不是只跟上一屏比。
#
# ⚠️ 这个是踩出来的：原来只查最后一屏（`ORDER BY id DESC LIMIT 1`），
#    而用户来回翻的时候屏幕顺序是 A、B、A、B…… —— 每一屏都跟紧邻的上一屏
#    不同，于是**全都被存下来**。实测 20 屏里只有 16 种不同的内容，
#    其中 2 屏连发送者判定都一模一样（纯粹的重复）。
#    存进去的重复屏会让缝合在重复内容上接错位置，整段被接两遍。
DEDUP_LOOKBACK = 8

# 缝合后折叠「重复出现的连续片段」的最短长度。
#
# ⚠️ 为什么需要（2026-09-27 实测）：用户来回滚时屏幕对顺序的说法会互相矛盾，
#    贪心缝合可能把**同一段既前插到开头、又追加到结尾**。张三实测 32 条里
#    出现 10 种重复条目，开头和结尾各有一份 `图片|没吃|哇|挖槽`。
#
# 判据：**正确的缝合结果里，3 条以上逐字相同的连续片段不该出现两次**。
#   单条重复很常见（「图片」「哈哈哈哈」满屏都是），但连续三条逐字相同、
#   且位置不重叠 —— 基本只可能是拼接产物（除非真有人把同一段贴两遍）。
COLLAPSE_MIN_RUN = 3

# 少于这么多条的连续重合不认作「接得上」。
#
# 为什么是 1（而且**不能**调成 2）：相邻两屏的重合不一定大 ——
# 大幅滑动（一次翻过好几条）时可能只重合 1 条。设成 2 会把「快速滑动补录」
# 整批拒掉，而那正是这个模块存在的理由（对应用户的实际场景：打开微信、
# 一堆新消息、快速滑一遍）。
#
# 单条重合确实有误配风险（「图片」「动画表情」这类内容满屏都是），但这里的
# 候选池只有**同一会话的最近 20 屏**，不是全量历史；而且真误配时多半是个
# no-op（缝合结果不变）。取舍：宁可偶尔多接一条，也不要整批丢。
MIN_OVERLAP = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS screens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT    NOT NULL,
    seen_at     TEXT    NOT NULL,   -- 我们看到这一屏的时刻（这个时间是真的）
    at_bottom   INTEGER,            -- 当时列表是否在底部
    sig         TEXT    NOT NULL,   -- 内容签名（含发送者判定），用来跳过重复的一屏
    items_json  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screens_sess ON screens(session_key, id);
"""


# ------------------------------------------------------------------ 数据转换

def items_from_snapshot(snap) -> List[dict]:
    """reader.ChatSnapshot -> 屏内条目，按竖直顺序（= 旧到新）。

    时间分隔符（`mmui::ChatItemView`，形如 `16:19` / `昨天 23:28`）也按它自己的
    竖直位置插进去：kind='time'，`date` 标出是不是跨天的分隔。
    它们**不是消息**（不会进 prompt），但：
      · 缝合时是有辨识度的锚点，能让对齐更稳
      · 跨天的分隔是**天然的分段线**，用来把很久以前的内容切掉
    """
    rows = []
    for m in (snap.messages or []):
        text = (m.text or "").strip()
        if not text:
            continue
        r = getattr(m, "rect", None)
        rows.append(((r[1] if r else 0), 0,
                     {"text": text,
                      "is_self": m.is_self,
                      "sender": m.sender_name,
                      "kind": getattr(m, "kind", None)}))
    for mk in (getattr(snap, "markers", None) or []):
        text = (mk.text or "").strip()
        if not text:
            continue
        r = getattr(mk, "rect", None)
        rows.append(((r[1] if r else 0), 1,
                     {"text": text, "is_self": None, "sender": None,
                      "kind": "time",
                      "date": bool(getattr(mk, "is_date_level", False))}))
    rows.sort(key=lambda x: (x[0], x[1]))       # 同一 y 上消息排在分隔符前
    return [x[2] for x in rows]


def is_time_item(it: dict) -> bool:
    """这条是不是时间分隔（不是消息）。"""
    return (it.get("kind") or "") == "time"


def who_of(it: dict) -> str:
    """展示用的发送者名字，语义与 reader.MessageItem.who 一致。"""
    if is_time_item(it):
        return ""               # 时间分隔不是谁发的
    v = it.get("is_self")
    if v is True:
        return "我"
    if v is False:
        return it.get("sender") or "对方"
    return "?"          # 没判出来（窗口最小化 / 被遮挡）


def to_prompt_dicts(items) -> List[dict]:
    """屏内条目 -> [{'who':..,'text':..}]，直接喂 ai.render_history。

    时间分隔符**不进 prompt** —— 它只用于缝合时对齐和切段。
    """
    out = []
    for it in (items or []):
        if is_time_item(it):
            continue
        text = (it.get("text") or "").strip()
        if text:
            out.append({"who": who_of(it), "text": text})
    return out


def signature(items) -> str:
    """一屏的签名。**含发送者判定**：判定变了要当成新的一屏存下来，
    这样「同一条先记成 ?、后来判出来」的升级才会生效（缝合时取最新那屏的值）。"""
    return "\x1f".join("%s\x1e%s" % (it.get("text") or "", it.get("is_self"))
                       for it in (items or []))


def text_signature(items) -> str:
    """只含**文本**的签名（不含发送者判定）。

    用来区分两种情况：
      · 文本和判定都一样 -> 同一屏，别重复存
      · 文本一样、只有判定变了 -> **原地更新**那一屏（升级生效，但不占窗口位）
    """
    return "\x1f".join((it.get("text") or "") for it in (items or []))


# ------------------------------------------------------------------ 缝合算法

#: 缝合比对前的**归一化**：去掉空白 / 常见标点 / 大小写差异，只留"字"。
#:
#: ⚠️⚠️ 为什么必须归一化（2026-10-14 实测）：
#:     同一段滚动，UIA 下缝合 0 错位，**OCR 下 67% 的屏被拼错**。原因是
#:     OCR 同一句话两拍读出来不一样：
#:         `11:46`  vs  `11-46`      （冒号被认成横杠）
#:         链接卡片末尾被截：`…3.7万` vs `…`
#:     精确相等时重合段从 3~5 条退化到 1~4 条，剩下的那一条往往是**偶然撞上的
#:     短词**（`图片` / `没绷住`）—— 锚点错一次，贪心缝合就把错误传染给后面每一屏。
#: 保留 `【】` 之类成对符号里的内容：它们可能就是消息本身。
_PUNCT_RE = re.compile(
    r"[\s\u3000,.，。！？!?、；;:：'\"“”‘’()（）\[\]<>《》~～\-—_/\\|*#`+]+")


def _norm_key(item) -> str:
    """缝合比对用的键：归一化后的正文（时间分隔符也走同一条路）。"""
    return _PUNCT_RE.sub("", (item.get("text") or "").casefold())


def _is_window(seq: List[dict], sub: List[dict]) -> bool:
    """sub 是不是 seq 里一段**连续**子序列（**逐字**比，只去首尾空白）。

    ⚠️⚠️ 这里**故意不归一化**（2026-10-14 实测）：
        归一化只用来**提出候选**（`_overlaps`），验收必须逐字 ——
        用归一化验收时，"已经接上的屏"里 `11:46` 和 `11-46` 也算相等，
        于是**明显拼错的位置也能通过验收**：同一批数据上错位 35/87，
        改回逐字验收后降到 3/87（v4 原型量过的那套）。
        宽松的判据用在"提候选"上没问题（错了会被验收拦下），
        用在"验收"上就等于没有验收。
    """
    if not sub:
        return True
    a = [(x.get("text") or "").strip() for x in seq]
    b = [(x.get("text") or "").strip() for x in sub]
    if len(b) > len(a):
        return False
    for i in range(len(a) - len(b) + 1):
        if a[i:i + len(b)] == b:
            return True
    return False


def _overlaps(cur: List[dict], res: List[dict], limit: int = 8) -> List[Tuple[int, int, int]]:
    """`cur` 与 `res` 的**所有候选**重合段，按可信度排序。

    可信度（2026-10-14 定）：
      ① **长的优先**；
      ② 同等长度优先"**贴着老屏尾部**"的那个 —— 相邻两屏本该这么接
         （老屏的尾巴 = 新屏的里头），而不是原来那句"取最靠后"；
      ③ `size == 1` 的重合**只在两边都唯一时**才要 —— 满屏「图片」时那条
         单条重合是噪声，原来 `MIN_OVERLAP = 1` 会拿它当锚点。
    """
    a = [_norm_key(x) for x in cur]
    b = [_norm_key(x) for x in res]
    if not a or not b:
        return []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out = []
    for bl in sm.get_matching_blocks():
        if bl.size <= 0:
            continue
        if bl.size == 1:
            k = a[bl.a]
            if not k or a.count(k) > 1 or b.count(k) > 1:
                continue
        out.append((bl.a, bl.b, bl.size))
    out.sort(key=lambda m: (m[2], -abs((m[0] + m[2]) - len(cur)), -m[1]), reverse=True)
    return out[:limit]


def find_overlap(cur: List[dict], res: List[dict]) -> Optional[Tuple[int, int, int]]:
    """找 cur 与 res 最可信的重合段，返回 (cur 起点, res 起点, 长度)。

    只返回**最可信的那一个**；缝合时如果它拼不通，还会去试下一个
    （见 `stitch_frames` 的"先验后接"）。

    autojunk=False 必须加：聊天里「图片」「动画表情」这类重复内容极多，
    默认的 autojunk 启发式会把它们当垃圾丢掉，导致对齐结果不可用（实测）。
    """
    cands = _overlaps(cur, res, limit=1)
    return cands[0] if cands else None


def splice(cur: List[dict], res: List[dict], m: Tuple[int, int, int]) -> List[dict]:
    """把 cur 按重合段拼进 res，返回新的序列（旧 -> 新）。

    ⚠️ 重合段里的条目**一律保留 res 的版本**，不要用 cur 的覆盖 ——
    res 是更新的那一屏，它的发送者判定更可信。早期版本在这里写成
    `res[:b] + head + cur[a:]`，结果「同一条先记成 ? 、后来判出来」的升级
    会被旧屏的 `?` 覆盖回去（测试专门锁了这条）。

    cur 比重合段更新（cur 的尾巴超出 res 末尾）的部分才接上去。
    """
    a, b, size = m
    head = cur[:a]                      # 比重合段更早的部分（往上翻历史时才有）
    out = res[:b] + head + res[b:]
    if b + size >= len(res):            # res 没有更新的内容了 -> cur 的尾巴是新的
        tail = cur[a + size:]
        if tail:
            out = out + tail
    return out


def collapse_repeats(items: List[dict], min_run: int = COLLAPSE_MIN_RUN):
    """把「同一段连续内容出现两次」折掉，**保留第一次**出现的那份。

    返回 (折叠后的列表, 丢掉的条数)。

    ⚠️ **保留第一次而不是最后一次**：开头那份是**较早的屏前插**建立的，位置更可信；
    结尾那份是后来又被追加了一遍（实测张三：`图片|没吃|哇|挖槽` 在开头是对的 ——
    真实顺序里它后面还跟着 `太火爆了`；而结尾那份后面什么都没有，说明是多余的）。
    曾经写反过，结果把对的那份丢掉、留下错的，顺序更乱。
    """
    if not items:
        return list(items or []), 0
    t = [x.get("text") or "" for x in items]
    n = len(t)
    if n < min_run * 2:
        return list(items), 0
    drop = [False] * n
    for size in range(n // 2, min_run - 1, -1):          # 先找长的
        for i in range(n - size + 1):
            if any(drop[i:i + size]):
                continue
            for j in range(i + size, n - size + 1):
                if any(drop[j:j + size]):
                    continue
                if t[j:j + size] == t[i:i + size]:
                    for k in range(j, j + size):         # 丢掉后面那份
                        drop[k] = True
                    break
    out = [x for k, x in enumerate(items) if not drop[k]]
    return out, n - len(out)


def stitch_frames(frames: List[List[dict]], max_items: int = 60,
                  cut_at_date: bool = True, on_collapse=None
                  ) -> Tuple[List[dict], bool]:
    """把若干屏缝成一条按对话顺序的序列。`frames` **最新在前**。

    返回 (items 旧->新, gap)。gap = True 表示有屏没能缝进去（中间可能有缺口）。

    提成纯函数是为了让查看器（`probes/show_db.py`）能复用这套逻辑，
    而不用以读写模式打开库。

    算法：拿最新一屏当底，把剩下的屏**反复尝试**拼上去 —— 能跟当前序列
    连续重合的就拼，拼不上的留到下一轮（因为别的屏拼进来之后它可能就接得上了）。
    一轮下来谁也拼不上就停。

    ⚠️⚠️ **"先验后接"**（2026-10-14 加）：对每个候选重合段，先按它拼一次，
       然后检查**已经接上的每一屏**是否仍然是结果里的一段**连续窗口**；
       搞乱了就换下一个候选，全都不行就把这屏留成缺口。
       为什么必须这样：OCR 的文本逐拍会变（`11:46`/`11-46`、链接被截），
       重合段退化到 1~4 条时，任意位置都可能"看起来接得上" —— 实测**67% 的屏
       被拼到了错位置**（喂给 AI 的上下文六成是乱的）。加上这道验收后，
       同一批真实数据上错位从 **62% 降到 3%**，代价是 37% 的屏宁可标缺口
       —— 这正是下面那句"宁可不接"要买的东西，只不过原来它**只写在注释里、
       没有任何检查在执行**。

    ⚠️ 不能用「一屏接不上就 break」：用户从历史翻回底部时，屏在时间顺序上
    会有一处跳跃，直接 break 会把跳跃之后那些**本来还能用**的屏全丢掉
    （实测模拟：M1~M20 的一段对话，只 break 的话开头两条会消失）。

    接不上的屏不硬拼 —— 宁可不接，也不要把不相邻的两段凑成连续对话。

    cut_at_date（默认开）：微信的时间分隔里**跨天的那种**（`昨天` / `星期X` /
    `9月20日`）是聊天天然的分段线。取**最后一个**跨天分隔，把它前面的内容切掉 ——
    隔着日期的内容几乎必然是旧话题，留在 prompt 里只是噪音（时效性优先）。
    纯时间（`16:19`）不切，那只是同一天内的锚点。

    拼完之后还会 `collapse_repeats()` 一次：来回滚时同一段可能被前插一次、
    又追加一次（见 `COLLAPSE_MIN_RUN`）。
    """
    if not frames:
        return [], False

    merged = list(frames[0])
    rest = [f for f in frames[1:] if f]
    #: 已经接上的那些屏 —— "先验后接"的验收对象（它们必须始终是连续窗口）
    placed = [list(frames[0])] if frames[0] else []

    changed = True
    while changed:
        changed = False
        for items in list(rest):
            for m in _overlaps(items, merged):
                trial = splice(items, merged, m)
                # ⚠️ 硬判据：已经接上的每一屏 + 这一屏自己，都还得是**连续窗口**。
                #    过不了就换下一个候选（而不是硬拼）。
                if not _is_window(trial, items):
                    continue
                if not all(_is_window(trial, f) for f in placed):
                    continue
                merged = trial
                placed.append(items)
                rest.remove(items)
                changed = True
                break

    gap = bool(rest)          # 还有屏没缝进去 -> 中间可能有缺口

    # 折叠重复段：来回滚时同一段可能被前插一次、又追加一次
    merged, dropped = collapse_repeats(merged)
    if dropped and on_collapse:
        try:
            on_collapse(dropped)
        except Exception:
            pass

    if cut_at_date:
        cut = -1
        for i, it in enumerate(merged):
            if is_time_item(it) and it.get("date"):
                cut = i
        # 切点后面必须还有消息才切，否则会把整段都切没
        if cut > 0 and any(not is_time_item(x) for x in merged[cut + 1:]):
            merged = merged[cut:]

    if max_items and len(merged) > max_items:
        merged = merged[-max_items:]        # 留最新的那一段
    return merged, gap


# ------------------------------------------------------------------ 存储

class ScreenStore:
    """一屏一条的观测存储 + 缝合。线程安全。"""

    def __init__(self, path: str, keep: int = DEFAULT_KEEP,
                 logger: Callable[[str], None] = None):
        self.path = os.path.abspath(path)
        self.keep = int(keep)
        self._log = logger or (lambda m: None)
        self._lock = threading.Lock()
        # 去重效果计数器：written 是真正新增的屏，skipped/updated 是被去重掉的。
        # ⚠️ **不能叫 `stats`** —— 那个名字已经是本类的**方法**（`stats()`，
        #    返回每个会话存了多少屏）。属性会**覆盖同名方法**，于是外部那句
        #    `len(store.stats())` 变成 `'dict' object is not callable`，
        #    异常被 run_watch 的 except 吞掉 -> `screens = None` ->
        #    **屏快照一行都不写，而消息表照写**（实测就这么静默瘫了 8 分钟）。
        self.dedup_stats = {"written": 0, "skipped": 0, "updated": 0}
        self._conn = None
        self._open()

    def _open(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            try:
                if self._conn:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ---------------------------------------------------------- 写入

    def append_screen(self, session_key: str, items, at_bottom: bool = True) -> bool:
        """存一屏。**不做新旧判断、不定位**（顺序留到读取时缝合）。

        去重有三条，看的是**最近 `DEDUP_LOOKBACK` 屏**而不是只看上一屏
        （理由见 `DEDUP_LOOKBACK` 的注释）：

          ① 签名完全相同（含发送者判定）-> 这一屏已经见过，**跳过**
          ② 文本一样、只有发送者判定不同 -> **原地更新那一屏**
             （升级照样生效，但不占窗口位）
          ③ 其余 -> 新的一屏

        返回是否写入了东西（② 算写了）。
        """
        if not session_key or not items:
            return False
        sig = signature(items)
        tsig = text_signature(items)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        payload = json.dumps(items, ensure_ascii=False)
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "SELECT id, sig, items_json FROM screens WHERE session_key = ? "
                    "ORDER BY id DESC LIMIT ?", (session_key, DEDUP_LOOKBACK))
                recent = cur.fetchall()

                for _rid, rsig, _rjson in recent:       # ① 同一屏，跳过
                    if rsig == sig:
                        self.dedup_stats["skipped"] += 1
                        return False

                for rid, _rsig, rjson in recent:        # ② 只有判定变了，原地更新
                    try:
                        same_text = (text_signature(json.loads(rjson)) == tsig)
                    except Exception:
                        same_text = False
                    if not same_text:
                        continue
                    cur.execute("UPDATE screens SET sig = ?, items_json = ? WHERE id = ?",
                                (sig, payload, rid))
                    self._conn.commit()
                    self.dedup_stats["updated"] += 1
                    return True

                cur.execute(                            # ③ 新的一屏
                    "INSERT INTO screens (session_key, seen_at, at_bottom, sig, items_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_key, now, 1 if at_bottom else 0, sig, payload))
                self._trim(cur, session_key)
                self._conn.commit()
                self.dedup_stats["written"] += 1
            except Exception:
                # 一条写不进去不能让连接卡在半开事务里（旧库踩过：
                # 之后每次记录都继续报错，日志每 2 秒刷一条）
                self._conn.rollback()
                raise
        return True

    def _trim(self, cur, session_key: str):
        if self.keep <= 0:
            return
        cur.execute(
            "DELETE FROM screens WHERE session_key = ? AND id NOT IN ("
            "  SELECT id FROM screens WHERE session_key = ? ORDER BY id DESC LIMIT ?"
            ")", (session_key, session_key, self.keep))

    # ---------------------------------------------------------- 读取

    def recent_screens(self, session_key: str, limit: int = 20) -> List[dict]:
        """最近 limit 屏，**最新在前**。"""
        if not session_key or limit <= 0:
            return []
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT id, seen_at, at_bottom, items_json FROM screens "
                        "WHERE session_key = ? ORDER BY id DESC LIMIT ?",
                        (session_key, limit))
            rows = cur.fetchall()
        out = []
        for r in rows:
            try:
                items = json.loads(r[3])
            except Exception:
                continue
            if items:
                out.append({"id": r[0], "seen_at": r[1],
                            "at_bottom": bool(r[2]), "items": items})
        return out

    def stitch(self, session_key: str, max_items: int = 60,
               max_screens: int = 20, cut_at_date: bool = True
               ) -> Tuple[List[dict], bool]:
        """把最近若干屏缝成一条按对话顺序的序列。

        返回 (items 旧 -> 新, gap)。算法说明见模块级的 `stitch_frames()`。
        """
        screens = self.recent_screens(session_key, max_screens)
        return stitch_frames([s["items"] for s in screens], max_items, cut_at_date,
                             on_collapse=self._on_collapse)

    def _on_collapse(self, dropped: int):
        """折叠重复段时记一行 —— 别让它静默发生（用户会问「为什么这次短了」）。"""
        self._log("屏快照：缝合时折叠掉 %d 条重复内容（来回滚造成的）" % dropped)

    # ---------------------------------------------------------- 维护

    def count(self, session_key: str) -> int:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*) FROM screens WHERE session_key = ?",
                        (session_key,))
            return int(cur.fetchone()[0])

    def stats(self) -> List[dict]:
        """每个会话存了多少屏、多少条、最后一次是什么时候。"""
        out = []
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT session_key, COUNT(*), MAX(seen_at) FROM screens "
                        "GROUP BY session_key ORDER BY MAX(id) DESC")
            for sk, n, last in cur.fetchall():
                cur.execute("SELECT items_json FROM screens WHERE session_key = ? "
                            "ORDER BY id DESC LIMIT 1", (sk,))
                r = cur.fetchone()
                cnt = 0
                if r:
                    try:
                        cnt = len(json.loads(r[0]))
                    except Exception:
                        pass
                out.append({"session_key": sk, "screens": n,
                            "last_items": cnt, "last_at": last})
        return out

    def last_seen_map(self) -> dict:
        """`{session_key: 最后一次记录的 seen_at}` —— 给设置面板「按时间排」用。

        ⚠️ `seen_at` 是**字符串**（`"2026-09-27 20:40:52"`，本地时间）。
            这个格式**天生可按字典序比大小**（年月日时分秒都是定宽零填充），
            所以调用方直接拿字符串比就行，**不要再去解析成时间戳** ——
            解析要处理时区/格式，纯属自找麻烦。
        ⚠️ 这里说的是「**我们最后一次看到它有内容**」，不是「消息发送时刻」。
            微信自己那份"按时间排"在 UIA 里拿不全（会话列表是虚拟列表，
            实测 19 个会话只读得到 8 个），所以只能退而求其次用本地的记录时间。
        """
        out = {}
        try:
            with self._lock:
                cur = self._conn.cursor()
                cur.execute("SELECT session_key, MAX(seen_at) FROM screens "
                            "GROUP BY session_key")
                for sk, last in cur.fetchall():
                    if sk and last:
                        out[sk] = str(last)
        except Exception as e:
            self._log("读会话时间失败: %s: %s" % (type(e).__name__, e))
        return out

    def clear(self, session_key: str) -> int:
        """清空某会话的屏（悬浮窗的「清空上下文」用）。"""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM screens WHERE session_key = ?", (session_key,))
            n = cur.rowcount
            self._conn.commit()
        return n
