# -*- coding: utf-8 -*-
"""SQLite 消息存储：把每次读到的消息累积起来，当上下文用。


════════════════════════════════════════════════════════════════════
为什么需要

    UIA 只能读到当前**可见**的消息 —— 一屏约 5~6 条（微信的虚拟列表只渲染
    可见部分）。每次读完就丢，于是：
      1. 给 AI 的上下文太短，看不到前因后果（吵架场景特别吃亏：
         不知道矛盾怎么起来的，就判断不出这句是气话）
      2. 口吻样本只有零星几条，模仿不出习惯
      3. 重启后全忘

    做法：每次触发读到的消息都存进本地 SQLite，累积起来就比任何单次可见的都多。
    ⚠️ 注意它**不突破微信的限制** —— 读不到的东西存不下来，它只是把每次读到的攒起来。

去重

    指纹 = hash(is_self, sender_name, content)，配 UNIQUE(session_key, fingerprint)。
    代价是同一会话里一模一样的两条消息只留一条。对"给 AI 当上下文"来说影响很小，
    却省掉了整套"比对-插入"的复杂逻辑。

容量

    每个会话只保留最近 N 条（默认 500），超出的自动删掉，避免库无限膨胀。

成本

    **存储本身不花任何 AI token** —— 只有"喂给 AI 的条数"才花钱，
    而那个由 runtime.max_messages 控制（实测每条中文短消息约 8 token）。
════════════════════════════════════════════════════════════════════
"""
from .prompts import GROUP_UNKNOWN
from .textfilter import is_noise_text


import hashlib
import os
import sqlite3
import threading
import time
from typing import Callable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT    NOT NULL,
    is_self     INTEGER,              -- 1=我发的 0=别人发的 NULL=未判定
    sender_name TEXT,                 -- 群聊昵称
    content     TEXT    NOT NULL,
    msg_type    TEXT,                 -- text / refer / ...
    fingerprint TEXT    NOT NULL,     -- 去重用
    from_ai     INTEGER DEFAULT 0,    -- 1=这条是「AI 建议过的原文」被用户发出去的
    created_at  TEXT    NOT NULL,
    UNIQUE(session_key, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_msg_sess ON messages(session_key, id);
"""


def make_fingerprint(is_self, sender_name: str, content: str) -> str:
    """消息指纹：同一会话内判断是否同一条。"""
    raw = "%s|%s|%s" % (
        "1" if is_self is True else ("0" if is_self is False else "?"),
        sender_name or "",
        (content or "").strip(),
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class MessageStore:
    """消息历史存储。线程安全（自用一个锁，供多线程调用）。"""

    def __init__(self, path: str, max_per_session: int = 500,
                 logger: Callable[[str], None] = None):
        self.path = os.path.abspath(path)
        self.max_per_session = int(max_per_session)
        self._log = logger or (lambda msg: None)
        self._lock = threading.Lock()
        # 最近 AI 生成过的建议原文 —— 写入时用它给消息打 from_ai 标记，
        # 取样口吻时排除，避免 AI 模仿自己的输出（见 note_ai_suggestions）
        self._ai_recent: set = set()
        self._conn = None
        self._open()

    # ------------------------------------------------------------ 连接

    def _open(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self):
        """给老库补上新增的列。

        `CREATE TABLE IF NOT EXISTS` 对**已存在**的表不会改结构，
        所以新加的列得自己 ALTER。踩过：加 `from_ai` 时如果只改 SCHEMA，
        老库上查询会直接报 no such column。
        """
        try:
            cur = self._conn.cursor()
            cur.execute("PRAGMA table_info(messages)")
            have = set(r[1] for r in cur.fetchall())
            if "from_ai" not in have:
                cur.execute("ALTER TABLE messages ADD COLUMN from_ai INTEGER DEFAULT 0")
                self._log("历史库：已补上 from_ai 列（老库升级）")
        except Exception as e:
            self._log("历史库：迁移失败（可忽略，会按新库处理）: %s: %s"
                      % (type(e).__name__, e))

    def close(self):
        with self._lock:
            try:
                if self._conn:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ------------------------------------------------------------ 写入

    def note_ai_suggestions(self, texts) -> None:
        """记下「这批文本是 AI 生成过的建议」。

        为什么需要（用户发现的自我污染）：
            AI 生成建议 -> 用户复制粘贴发出去 -> 这条进了历史库 ->
            下一轮被当成「我平时说话的样子」喂回去 -> **AI 开始模仿自己的输出**。
            几轮下来风格会漂移，AI 腔越来越重。

        所以写入时给这些消息打 `from_ai=1` 标记，取样口吻时排除掉。
        注意：它们**仍然进对话历史**（那确实是用户发出去的真实消息，
        上下文里必须有），只是不当口吻样本。
        """
        for t in (texts or []):
            s = (t or "").strip()
            if s:
                self._ai_recent.add(s)
        # 别无限涨：只留最近若干条
        while len(self._ai_recent) > 500:
            self._ai_recent.pop()

    def ai_suggestion_texts(self) -> set:
        """最近 AI 生成过的建议原文（给「窗口取样」那条路径排除用）。"""
        return set(self._ai_recent)

    def save_messages(self, session_key: str, messages) -> int:
        """把一批消息存进库（自动去重、自动裁剪容量），返回**新增**条数。

        messages 是 reader.MessageItem 列表，或带同样字段的对象。
        """
        if not session_key or not messages:
            return 0
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for m in messages:
            content = (getattr(m, "text", "") or "").strip()
            if not content:
                continue
            is_self = getattr(m, "is_self", None)
            sender = getattr(m, "sender_name", None)
            kind = None
            try:
                kind = m.kind
            except Exception:
                pass
            # 这条是不是「AI 建议过的原文」被发出去的？是就打标记，
            # 之后取样口吻时跳过它（否则 AI 会模仿自己的输出）。
            from_ai = 1 if content in self._ai_recent else 0
            rows.append((
                session_key,
                1 if is_self is True else (0 if is_self is False else None),
                sender,
                content,
                kind,
                make_fingerprint(is_self, sender, content),
                from_ai,
                now,
            ))
        if not rows:
            return 0

        added = 0
        upgraded = 0
        cleaned = 0
        with self._lock:
            cur = self._conn.cursor()
            try:
                for r in rows:
                    sk, is_self, sender, content, kind, fp, from_ai, _created = r
                    if is_self is not None:
                        # ① 这条「带作者」的记录是不是已经在库里了？
                        #    必须先查这一步，否则下面那句 UPDATE 会把另一行的指纹
                        #    改成已经存在的值，直接撞 UNIQUE 报
                        #    IntegrityError: UNIQUE constraint failed
                        #    （实机踩过 —— 序列是：可见时记下 -> 遮挡时又记成 ? -> 再次可见）。
                        cur.execute(
                            "SELECT 1 FROM messages "
                            "WHERE session_key = ? AND fingerprint = ?", (sk, fp))
                        if cur.fetchone():
                            # 库里已经有「带作者」的正确版本了。
                            # 但如果窗口被最小化过，可能还留着一条**同内容、作者未知**
                            # 的孪生行（那时判不出作者，指纹是 fp("?|内容")）。
                            # 必须把它删掉，否则那个 ? 会永远留着，
                            # 进 prompt 时就成了「?: xxx」的噪声 —— 实测就是这样
                            # 留下了 '有'/'还真'/'图片' 等 5 组重复（id 867~913）。
                            cur.execute(
                                "DELETE FROM messages WHERE session_key = ? AND content = ? "
                                "AND is_self IS NULL", (sk, content))
                            cleaned += cur.rowcount
                            continue
                        # ② 有没有「同一条内容、但发送者当时没判出来」的旧记录？
                        #    有的话**升级它**，而不是新增一份。
                        #
                        #    为什么需要：发送者判定依赖截图，窗口被遮挡时会判不出来。
                        #    同一条消息可能先被记成 is_self=NULL，后来重读时判出来了；
                        #    而指纹含 is_self，两次指纹不同 -> UNIQUE 去重失效 -> 存成两份。
                        #
                        #    ⚠️ 必须连 fingerprint 一起更新。否则这行的指纹还停留在
                        #    「is_self 未知」时的值，下次再读到同一条，新指纹对不上，
                        #    又会插一份 —— 等于没修（这个坑是测试抓出来的）。
                        cur.execute(
                            "UPDATE messages SET is_self = ?, sender_name = ?, "
                            "msg_type = ?, fingerprint = ? "
                            "WHERE session_key = ? AND content = ? AND is_self IS NULL",
                            (is_self, sender, kind, fp, sk, content),
                        )
                        if cur.rowcount:
                            upgraded += cur.rowcount
                            continue
                        # ②b ⚠️ 同内容、**发送者当时没读出来**的旧行 -> 升级它
                        #     （2026-10-14 加）
                        #
                        #     为什么需要：UIA 的群昵称是**后补**的
                        #     （`SenderResolver.resolve()` 逐拍 OCR 昵称那一小块），
                        #     窗口最小化/遮挡那一拍读不出来 -> 先写成空（或「群友」），
                        #     下一拍补上真名。而指纹含 `sender_name` -> 两次指纹不同
                        #     -> `UNIQUE(session_key, fingerprint)` 去重失效 -> 同一句
                        #     话存 3~4 份（实测：用户快速上翻时 `'动画表情'` 存了 3 份，
                        #     sender 分别是 `李四/王五/(空)`，库尾一段话连写两遍）。
                        #
                        #     ⚠️⚠️ **只在"旧行没有名字"时升级**：
                        #        两个**已知的**不同发送者说同一句话，必须留成**两条**
                        #        （用户 2026-10-14 明确要求）—— 那种情况指纹不同、
                        #        这条 SQL 也匹配不到（旧行 sender 非空），走下面的 INSERT ✓
                        #     ⚠️ 候选**不唯一**时也不碰（宁可多一条，也不要合并错人）。
                        if sender and sender != GROUP_UNKNOWN:
                            cur.execute(
                                "SELECT id FROM messages WHERE session_key = ? AND content = ? "
                                "AND (is_self = ? OR is_self IS NULL) "
                                "AND (sender_name IS NULL OR sender_name = '' "
                                "     OR sender_name = ?) "
                                "ORDER BY id DESC LIMIT 2",
                                (sk, content, is_self, GROUP_UNKNOWN),
                            )
                            cands = cur.fetchall()
                            if len(cands) == 1:
                                cur.execute(
                                    "UPDATE messages SET is_self = ?, sender_name = ?, "
                                    "msg_type = ?, fingerprint = ? WHERE id = ?",
                                    (is_self, sender, kind, fp, cands[0][0]),
                                )
                                upgraded += cur.rowcount
                                continue
                    # INSERT OR IGNORE 靠 UNIQUE(session_key, fingerprint) 去重
                    cur.execute(
                        "INSERT OR IGNORE INTO messages "
                        "(session_key, is_self, sender_name, content, msg_type, "
                        " fingerprint, from_ai, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", r,
                    )
                    added += cur.rowcount
                self._trim(cur, session_key)
                self._conn.commit()
            except Exception:
                # 一条出错不能让整个连接卡在半开的事务里，
                # 否则后续每次记录都会继续报错（实机表现就是每 2 秒刷一条异常）。
                self._conn.rollback()
                raise
        if upgraded:
            self._log("历史库：把 %d 条此前未判定发送者的记录补全了（而不是重复存一份）"
                      % upgraded)
        if cleaned:
            self._log("历史库：清掉 %d 条「作者未知」的重复行（正确版本已在库里）"
                      % cleaned)
        return added

    def _recent_contents(self, session_key: str, limit: int) -> set:
        """该会话最近 limit 条的**内容**集合。

        为什么按内容而不是指纹匹配：指纹含 is_self / 昵称，而发送者判定
        是会变的 —— 同一条消息先记成 `?`、后来判出来了，指纹就变了。
        用指纹找重叠点会**找不到**，升级路径随之断掉（踩过）。
        而「这屏消息我见过没有」本质是个内容问题，与谁发的无关。
        """
        if not session_key or limit <= 0:
            return set()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT content FROM messages WHERE session_key = ? "
                        "ORDER BY id DESC LIMIT ?", (session_key, limit))
            return set((r[0] or "").strip() for r in cur.fetchall())

    def save_new_only(self, session_key: str, messages, at_bottom: bool = True,
                      lookback: int = 80, bottom_known: bool = True) -> int:
        """只写入**库尾之后**的新消息；判据只有一个 —— 「和库里已有的重叠点」，
        滚动位置（`at_bottom`）**不参与**决定。

        为什么不用「列表在底部」当主判据（用户指出那个判据别扭，确实如此）：
            「在底部」代理的是「屏幕上的最后一条是不是最新的」，两个毛病：
              · 虚拟化列表上有盲区：最后一条滚出渲染区后会换成上一条，
                gap 在「滚动距离 == 消息高度」时归零 -> 误判成在底部；
              · 会误伤：翻历史时即便真有新消息，也会整轮跳过。
            而我们真正想知道的是**另一件事**：「这些可见消息里哪几条还没记过」。

        主判据：找**最后一个内容已经在库里的**可见消息作为重叠点，
        它之后的才是新的：
            在底部来了 F、G : 可见 [E,F,G]，重叠点 E(0) -> 写入 F、G
            往上翻历史      : 可见 [A,B,C,D]，重叠点 D(3) 是最后一条 -> 不写

        ⚠️⚠️ 重叠点找不到（cut = -1）时**一律不写**（2026-10-14 改）。
            曾经用 `at_bottom` 兜底（"在底部就当新消息全写"），理由是"用户离开一会儿
            来了 30 条消息，回来只看得到最后一屏"；但实测那个信号在**快速上翻历史**
            时不可靠（虚拟列表盲区），于是同一屏被反复追加（38 行里 24 行重复）。
            代价（明确接受）：程序没在跑时来的消息、之后只看得到最后一屏时，
            这几条不进消息表 —— 但**同一屏已经无条件进了屏快照**，
            喂给 AI 的上下文来自 `screens.stitch()`，不靠这张表。

        ⚠️⚠️ `bottom_known` / `at_bottom` **已经不再影响写入决定**（2026-10-14）：
            `cut < 0`（这屏一条都没记过）时**一律不写** —— 那两个信号在快速
            上翻历史时都不可靠，实测把同一屏反复当新消息追加（38 行里 24 行重复）。
            参数保留只是**为了不打断调用点**，别再用它们做判据。

        重叠点**之前**但内容已知的消息也一并交给 save_messages：它们要么指纹
        没变被忽略、要么是「? 现在判出来了」被升级，两种都不会新增行、不会乱序。
        """
        if not session_key or not messages:
            return 0

        rows = []
        for m in messages:
            text = (getattr(m, "text", "") or "").strip()
            if text:
                rows.append((m, text))
        if not rows:
            return 0

        known = self._recent_contents(session_key, lookback)
        if not known:
            # 这个会话库里还没有东西 -> 全当新消息（首次记录 / 刚清空）
            return self.save_messages(session_key, messages)

        cut = -1
        for i in range(len(rows) - 1, -1, -1):
            if rows[i][1] in known:
                cut = i
                break

        if cut < 0:
            # ⚠️⚠️ 2026-10-14：**这里一律不写**（原来会看 `at_bottom` 决定"整屏当新消息写"）。
            #
            # 实测（用户快速上翻历史，日志 + 库一起验的）：
            #   · 屏快照 40 屏里 **15 屏**判成 `at_bottom=True`，而用户明明在往上翻；
            #     UIA 那条 gap 判据有**已知盲区** —— 最后一条滚出渲染区后
            #     "最后一条"换成上一条，gap 归零（`reader.at_bottom()` 的注释里写着）；
            #   · 于是同一屏 5~7 条被反复当成"新消息"追加：最近 10 分钟 38 行里
            #     **24 行**是库里已经有的内容，尾部同一句话存了 3 份。
            #
            # 为什么可以一律不写（代价是明确接受的）：
            #   · **屏快照是无条件记录的**（`Recorder` 每拍都 append 一屏），
            #     喂给 AI 的上下文来自 `screens.stitch()`，**不靠这张表**；
            #   · 这张表只管口吻样本 / AI 自我污染标记 / 清空 —— 少几行没有损失，
            #     而**多出来的重复行会污染口吻样本**（AI 会学着自己重复说话）。
            #   · 代价：程序没在跑时来的消息、之后只看得到最后一屏时，这几条不进
            #     消息表（同一屏已经进了屏快照）。首次见到某会话（`known` 为空）
            #     那条路不受影响，仍然整屏写。
            self._log("历史库：这屏消息一条都没记过，位置无法确定 -> 本次不写"
                      "（上下文由屏快照负责；宁可不写，也不要把旧消息接到库尾）")
            return 0

        to_save = [m for i, (m, text) in enumerate(rows)
                   if i > cut or text in known]
        if not to_save:
            return 0
        return self.save_messages(session_key, to_save)

    def _trim(self, cur, session_key: str):
        """只保留该会话最近 max_per_session 条，多余的删掉。"""
        if self.max_per_session <= 0:
            return
        cur.execute(
            "DELETE FROM messages WHERE session_key = ? AND id NOT IN ("
            "  SELECT id FROM messages WHERE session_key = ? ORDER BY id DESC LIMIT ?"
            ")",
            (session_key, session_key, self.max_per_session),
        )

    # ------------------------------------------------------------ 读取

    def get_recent(self, session_key: str, limit: int = 20) -> List[dict]:
        """取该会话最近的 limit 条（按时间正序返回，便于直接拼进 prompt）。"""
        if not session_key or limit <= 0:
            return []
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT is_self, sender_name, content, msg_type, from_ai, created_at "
                "FROM messages WHERE session_key = ? ORDER BY id DESC LIMIT ?",
                (session_key, limit),
            )
            rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()      # 变成时间正序
        return rows

    def own_style_texts(self, session_key: str, limit: int = 40,
                        max_len: int = 60) -> List[str]:
        """取「我」发过的短消息当口吻样本，**排除掉 AI 建议过的那些**。

        为什么要排除（用户发现的自我污染）：AI 生成建议 -> 用户复制发出去 ->
        这条进了库 -> 下一轮被当成「我平时说话的样子」-> **AI 开始模仿自己的
        输出**，几轮下来 AI 腔越来越重。所以 from_ai=1 的一律不取。

        （它们仍然进对话历史 —— 那确实是用户发出去的真实消息，上下文里要有。）
        """
        if not session_key or limit <= 0:
            return []
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT content FROM messages "
                "WHERE session_key = ? AND is_self = 1 AND from_ai = 0 "
                "ORDER BY id DESC LIMIT ?",
                (session_key, max(limit * 4, 40)),
            )
            rows = [r[0] for r in cur.fetchall()]
        out = []
        for t in rows:                       # rows 是倒序的，从头挑最新的
            t = (t or "").strip()
            if not t or len(t) > max_len:
                continue
            if "http" in t:
                continue
            # ⚠️ 2026-10-12：再挡一道**噪声**（`职位详情` / `D` / `KI` / `小程序` …）。
            #    OCR 会把图标/卡片上的字读成「我」发的消息，而它们**是 is_self=1** ——
            #    原来只挡 from_ai=1，所以它们**真的进了口吻样本**，AI 会去模仿 ✗
            #    ⚠️ 这里只做"值不值得当样本"，**不要**拿它去过滤正文（正文一条都不能丢）。
            if is_noise_text(t):
                continue
            if t in out:
                continue
            out.append(t)
            if len(out) >= limit:
                break
        return list(reversed(out))           # 恢复时间顺序

    def get_before(self, session_key: str, before_id: int, limit: int = 20) -> List[dict]:
        """取某条消息之前的 limit 条（给「回复历史某条」用）。"""
        if not session_key or limit <= 0:
            return []
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT is_self, sender_name, content, msg_type, created_at "
                "FROM messages WHERE session_key = ? AND id < ? ORDER BY id DESC LIMIT ?",
                (session_key, before_id, limit),
            )
            rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows

    def last_row(self, session_key: str) -> Optional[dict]:
        """该会话库里最后一条（用于判断「翻历史期间是否有新消息」）。"""
        if not session_key:
            return None
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT id, is_self, sender_name, content, msg_type, created_at "
                "FROM messages WHERE session_key = ? ORDER BY id DESC LIMIT 1",
                (session_key,),
            )
            r = cur.fetchone()
        return dict(r) if r else None

    def count(self, session_key: str) -> int:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*) FROM messages WHERE session_key = ?", (session_key,))
            return int(cur.fetchone()[0])

    def stats(self) -> List[dict]:
        """每个会话存了多少条。"""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT session_key, COUNT(*) AS n, MAX(created_at) AS last_at "
                        "FROM messages GROUP BY session_key ORDER BY n DESC")
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------ 清理

    def clear(self, session_key: str) -> int:
        """清空某个会话的历史（悬浮窗的「清空上下文」用）。"""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM messages WHERE session_key = ?", (session_key,))
            n = cur.rowcount
            self._conn.commit()
        return n
