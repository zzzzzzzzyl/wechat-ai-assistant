# -*- coding: utf-8 -*-
"""会话管理与白名单。

设计要点（见《需求与设计文档.md》5.3）：
    **白名单模式** —— 新发现的会话默认 mode=off，完全不被处理
    （不读内容、不记录上下文、不提醒、不请求 AI）。
    用户在管理界面（P1）或配置白名单（P0）里开启后才会工作。

P0 用 JSON 存状态，P1 换成 SQLite。
"""

import json
import os
import time
import re
from typing import Callable, Dict, List, Optional

# 会话模式
MODE_OFF = "off"          # 不处理（默认）
MODE_ON = "on"            # 提醒 + 记录 + 生成建议
MODE_OBSERVE = "observe"  # 只记录上下文，不生成建议（预留）

#: 「这个会话要不要用知识库」的**出厂默认值**（2026-10-17 用户要求改成 `True`）。
#:
#: ⚠️ 语义是"默认打开"，不是"默认就查"：真正要不要检索还要过**全局开关**
#:    （`kb.enabled`，出厂是关的）。两级都开才会查 —— 出厂状态下什么都没发生。
#: ⚠️ 老的 `state.json` 里没有这个字段的会话也按这个默认值算（见 `kb_enabled()`），
#:    否则升级之后老会话会"突然全都不用知识库"，而且界面上看不出原因。
KB_DEFAULT = True


def normalize_mode(v) -> str:
    """把配置里的 mode 归一化成 'off' / 'on' / 'observe'。

    ⚠️ **必须归一化，不能直接 str()**：YAML 1.1 会把裸写的
    `on` / `off` / `yes` / `no` 解析成**布尔值**，所以 config 里写
    `default_mode: on` 读出来是 `True` 而不是 `"on"`。

    实测踩过：写成 on 之后 `default_mode` 是 True，永远不等于 MODE_ON，
    于是**所有会话都不启用，而且不报任何错** —— 非常隐蔽。
    （配置里现在也加了引号，双保险。）
    """
    if v is True:
        return MODE_ON
    if v is False or v is None:
        return MODE_OFF
    s = str(v).strip().lower()
    if s in ("on", "true", "yes", "1", "enabled"):
        return MODE_ON
    if s == MODE_OBSERVE:
        return MODE_OBSERVE
    if s in ("off", "false", "no", "0", "disabled", ""):
        return MODE_OFF
    return MODE_OFF


#: OCR 把**图形**认成的字（emoji / 未读圆点 / 头像边框读成了字符）。
#:
#: ⚠️⚠️ 2026-10-11 用户报「开启后会出现一些不知道从哪里来的 X、G、0 这些」——
#:     一部分就是**纯图形标题**被认成了字。实测证据：
#:         '😝😜🤪😛😋'      -> '00000'
#:         '♥️示例群…🏠'       -> '口口示例群…口'
#:         '●示例群…'          -> 多了个未读圆点
#:     这些字符在**判断"是不是同一个会话"时**该被忽略（下面的 `_is_decoration`）。
#:
#: ⚠️ **只加"图形类"的，绝不要把 `0` / `O` / `G` / `X` 加进来** ——
#:     会话名里数字和字母很正常（'示例23班'、'G'），
#:     删掉它们会把**本来不同的会话合并**，比不合并更糟。
#:     （`0`/`O` 是 OCR 认错的结果，只能靠"垃圾标题不建会话"那条挡，不能靠删。）
#: 群标题末尾的**成员数后缀**（`(50)` / `（141）`）。
#: ⚠️ 只匹配**末尾**：标题中间的括号数字可能是名字的一部分
#:    （`示例(2024)同学群`），去了会把两个不同的群并成一个。
_GROUP_COUNT_RE = re.compile(r"[（(]\d{1,5}[)）]\s*$")

#: 微信**自己的界面文案** —— 它们会出现在标题栏，但**不是会话名**。
#: ⚠️ 实测（2026-10-13，用户报「会话设置存储里有很多奇奇怪怪的记录」）：
#:    停在“收藏”页时标题栏显示「全部收藏」、停在标签页时显示
#:    「新建标签页」。它们**都是正常汉字**，所以 `is_junk_title`
#:    的“空/单字/纯数字”那几条**认不出来**；而且它们是**稳定文本**
#:    （每拍读出来都一样），所以 `discover` 里那个“两次确认”
#:    **也挡不住** —— 只能靠白名单。
#: ⚠️ 这一组是微信的**固定功能名**，不是开放集合；
#:    别往里塞“像人话”的词（会误杀真会话）。
#: ⚠️ 比对用的是 **`norm_title` 之后的形式**（casefold 过）。
_UI_TITLES = (
    "全部收藏", "新建标签页", "搜索", "通讯录", "收藏", "朋友圈",
    "聊天文件", "订阅号消息", "微信团队", "文件传输助手",
)

_BOX_GLYPHS = "口囗〇○◯●◉◇◆□■▢▪▫"
#: 纯图形的标题归一化之后会变成空 —— 但**全 emoji 也是空**，两者都该丢。

#: 同一个标题要在**不同的拍里看到几次**才认为"这是个真会话"（见 `discover`）。
#:
#: ⚠️ 2 是有意的：OCR 的乱码是**偶发**的（下一拍就变了），真会话会**一直在**。
#:    调大更稳，但"刚打开的会话要等更久才开始记录"；调小就挡不住。
SESSION_CONFIRM_N = 2

#: 两次确认的**有效窗口**（秒）—— 超过它就重新数。
#: ⚠️ 为什么要有它（2026-10-13）：原来计数器**永不过期**，
#:    一个偶发乱码只要在**任意两拍**被撞见就会建档
#:    （哪怕中间隔了十分钟），而真会话是**一直在**的。
#: ⚠️ 取 10 秒：一拍约 1 秒，真会话连着两拍就够；
#:    “隔了十秒以上又出现”就当新的一次数。
_CONFIRM_WINDOW = 10.0

#: 两次确认的**有效窗口**（秒）—— 超过它就重新数。
#: ⚠️ 为什么要有它（2026-10-13）：原来计数器**永不过期**，
#:    一个偶发乱码只要在**任意两拍**被撞见就会建档
#:    （哪怕中间隔了十分钟），而真会话是**一直在**的。
#: ⚠️ 取 10 秒：一拍约 1 秒，真会话连着两拍就够；
#:    “隔了十秒以上又出现”就当新的一次数。


def _is_decoration(ch: str) -> bool:
    """这个字符是不是「装饰」—— emoji / 符号 / 图形误认 / 零宽 / 空白。

    判断"同一个会话名"时要把它们忽略掉，见 `norm_title`。
    """
    o = ord(ch)
    return (
        ch in _BOX_GLYPHS                 # ⚠️ OCR 把图形认成的字（见上面的说明）
        or ch.isspace()
        or o == 0x200D                    # 零宽连接符（emoji 组合用）
        or 0xFE00 <= o <= 0xFE0F          # 变体选择符（♥️ 后面那个）
        or 0x1F000 <= o <= 0x1FAFF        # emoji 主体
        or 0x2600 <= o <= 0x27BF          # 杂项符号 / dingbats
        or 0x2B00 <= o <= 0x2BFF
        or 0x2190 <= o <= 0x21FF          # 箭头
        or 0x1F1E6 <= o <= 0x1F1FF        # 区域指示符（国旗）
    )


def norm_title(title: str) -> str:
    """把会话名归一化，用来判断「这两个名字是不是同一个会话」。

    ⚠️⚠️ 为什么需要它（2026-10-11，用户问「换回 UIA 会不会因为颜文字重复记录」）：

        **会。** 两个后端读同一个标题可能得到不同的字符串：

            真值 '♥️示例群名🏠'
              UIA -> '♥️示例群名🏠'   （读控件的 Name，原样）
              OCR -> '口口示例群名口'   （认字形，emoji 认不出）

        而 `session_key` 就是**标题原文**，于是同一个会话被劈成两个 key ——
        历史库 / 屏快照库 / 关系设定 / 人设 / 黑名单**各认各的**，
        上下文只剩一半。实测（`probes/probe_title_variants.py`）：
        6 个真实会话名里 **3 个**两个后端给出的字符串不一致。

    归一化只去掉**装饰**（emoji / 符号 / 零宽 / 空白），**保留所有文字和数字**：
        · 目的是让「只差装饰」的两个名字合并
        · ⚠️ **不做**全角半角转换、不做繁简、不删普通标点 ——
          那些改动会误合并本来不同的会话，而收益很小。

    ⚠️ 全 emoji 的名字（实测 `😝😜🤪😛😋`）归一化之后是**空串** ——
        调用方必须把空串当成"不能用来合并"（见 `key_for`），
        否则所有纯 emoji 的会话会被合并成一条。
    """
    s = "".join(ch for ch in (title or "") if not _is_decoration(ch)).strip().casefold()
    # ⚠️ 再去掉**群成员数后缀**（2026-10-13）：群有人进退它就变
    #    （实测 `(50)` -> `(54)`、`(280)` -> `(279)`），一变就是另一个
    #    `session_key` -> **历史断掉、设置丢**。
    #    ⚠️ 代价：**恰好同名**的两个群会被当成一个（罕见）——
    #    而“有人进退就断历史”是**经常**发生的。
    return _GROUP_COUNT_RE.sub("", s).strip()


def strip_group_count(title: str) -> str:
    """去掉群标题末尾的**成员数后缀**（`(50)` / `（141）`）。

    ⚠️ 2026-10-13 用户要求：**会话名里不该带人数** ——
        · 人数会变（实测 `(50)` -> `(54)`、`(280)` -> `(279)`），
          带着它**看起来就像另一个会话**；
        · 而且它**不是用户起的名字**，是微信自己加的。
    ⚠️ **只去末尾**：中间的括号数字可能真是名字的一部分
        （`示例(2024)同学群`），去了会把两个不同的群并成一个。
    ⚠️ 名字**只有人数**时（极端情况）返回原文，别返回空串。
    """
    s = (title or "").strip()
    out = _GROUP_COUNT_RE.sub("", s).strip()
    return out or s


def merge_known_sessions(sessions: Optional[Dict[str, dict]],
                         db_names=None,
                         default_mode: str = MODE_OFF) -> Dict[str, dict]:
    """「设置 → 会话」那份**名单** = 注册表 ∪ 屏快照库。**纯函数**，好单测。

    ⚠️⚠️ 为什么必须并上库（2026-10-11，用户报「重新启动程序后设置中的
        会话历史会不见」）：

        注册表（`state.json`）**只在正常退出时**才落盘（`run_watch` 的
        `finally`）。实测证据：

            `state.json` 最后写入 **21:35**，而 `screens.db` 里的记录一直到 **23:18**
            —— 说明那一轮**根本没走到保存就被结束了**（强杀 / 崩溃 / 直接关
            控制台窗口）。于是它发现的十来个会话**全丢**，
            重启后设置里只剩上次干净退出时的 **1** 个，而库里其实有 **16** 个。

        `screens.db` 是**连续写**的，它一直知道有哪些会话 —— 并进来之后
        列表就**自愈**了：注册表整个丢了也不怕。

    ⚠️ 补进来的条目带 `from_db=True`，是**临时的**，不会写回注册表。
        只有当用户真的在面板上改了模式，`set_mode()` 才会把它建成正式条目
        （那边用的是 `setdefault`，不会覆盖已有的）。
    ⚠️ `db_names` 只用来取**键**（有哪些会话）。带时间的那些由调用方另行合并 ——
        这个函数不碰时间，免得两处各算一遍。
    """
    out = dict(sessions or {})
    for k in (db_names or ()):
        if k and k not in out:
            out[k] = {"mode": default_mode, "is_new": False, "from_db": True}
    return out


def is_junk_title(raw_title: str) -> bool:
    """这个标题**明显不是会话名**，不该为它建会话（源头挡掉）。

    ⚠️⚠️ 2026-10-11 用户报「开启后会出现一些不知道从哪里来的 X、G、0 这些」，
        并要求「可不可以**从源头上解决**」。判据（都在归一化之后看）：

        · 归一化后**为空**   —— 纯 emoji / 纯图形的标题（'😝😜' -> '00000' -> ''）
        · **只剩一个字**     —— 'X' / 'G' / '0' / '口'（OCR 认错的碎片）
        · **全是数字并且很短** —— '0' / '999994'（不是会话名，是乱码或计数）

    ⚠️ 判据只敢用"**明显**不是名字"的形态。像 '示例23班'、
       'G' 这种**只有长度能区分**的，宁可放过（漏一个脏名字只是列表里多一条，
       而**误杀一个真会话**会让它的历史整个断掉）——
       所以这里**不做**"短的名字就丢"，只丢"归一化后空/单字/纯数字"。
    """
    n = norm_title(raw_title)
    if not n:
        return True                       # 纯 emoji / 纯图形
    if n.isdigit():
        return True                       # 0 / 999994
    if n in _UI_TITLES:
        return True                       # 微信自己的界面文案（见 `_UI_TITLES`）
    if len(n) == 1:
        # ⚠️ 单个**汉字**是**正常**昵称（'白' / '周'），不能杀；
        #    而单个**拉丁字母**几乎都是 OCR 的碎片（用户点名的就是 X / G / 0）。
        #    所以这里按"是不是汉字"分，**别一刀切按长度**。
        return not ("\u4e00" <= n <= "\u9fff")
    return False


class SessionRegistry:
    """会话表 + 白名单 + 增量识别游标。"""

    def __init__(
        self,
        state_path: str,
        bootstrap_whitelist: Optional[List[str]] = None,
        default_mode: str = MODE_OFF,
        disabled_sessions: Optional[List[str]] = None,
        disabled_contains: Optional[List[str]] = None,
        merge_variants: bool = True,
        logger: Callable[[str], None] = None,
    ):
        self.state_path = os.path.abspath(state_path)
        # 归一化：YAML 会把裸写的 on/off 解析成布尔值（见 normalize_mode）
        self.default_mode = normalize_mode(default_mode)
        self._log = logger or (lambda msg: None)
        self._bootstrap = [w for w in (bootstrap_whitelist or []) if w]
        self._disabled = [d for d in (disabled_sessions or []) if d]
        #: 系统会话的**关键词**（子串匹配）—— 公众号 / 微信支付 / 服务通知 …
        #: 它们不是聊天，但看起来像，所以要用名字把它们挡掉（见 `is_system_session`）。
        self._disabled_contains = [d for d in (disabled_contains or []) if d]
        #: 是不是把「只差 emoji/符号」的会话名当成同一个会话（见 `norm_title`）。
        #: 关掉的话，OCR / UIA 两种读法会给同一个会话建**两条**记录。
        self._merge_variants = bool(merge_variants)
        #: 「两次确认」的计数器（见 `discover`）。
        #: ⚠️ **只在内存里**，故意不落盘 —— 落盘的话一个只出现过一次的乱码标题
        #:    会跨重启累积成"确认过"，那两道人墙就等于没有了。
        self._pending: Dict[str, int] = {}
        #: 已经抱怨过的垃圾标题（只记一次日志，免得每拍刷屏）。
        self._junk_seen: set = set()
        self.sessions: Dict[str, dict] = {}
        self.sync: Dict[str, dict] = {}

        self.load()
        self._migrate_kb_default()
        self._apply_bootstrap()
        self._rebuild_norm_index()

    # ------------------------------------------------------------ 持久化

    def load(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            self.sessions = data.get("sessions", {}) or {}
            self.sync = data.get("sync", {}) or {}
        except Exception as e:
            self._log("读取状态文件失败（将重新开始）: %s" % e)
            self.sessions, self.sync = {}, {}

    def save(self) -> None:
        d = os.path.dirname(self.state_path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"sessions": self.sessions, "sync": self.sync},
                f, ensure_ascii=False, indent=2,
            )
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------ 白名单

    def _migrate_kb_default(self) -> None:
        """把**旧默认值写进去的** `kb_enabled=False` 改成新默认（开）。

        ⚠️⚠️ 为什么必须迁移（2026-10-17 用户实测）：会话级默认从 `False` 改成 `True`
            之后，**只有新建的会话**用新默认；老 `state.json` 里 20 个会话存的是
            旧默认写进去的 `False` —— 用户看到的就是"还是默认关闭"。

        ⚠️ 怎么区分"用户显式关掉的"和"旧默认写进去的"：靠 `kb_by_user` 标记。
            `set_kb_enabled()` 会打这个标记；没有标记的一律视为旧默认，重新套用
            `KB_DEFAULT`。用户真正手动关掉的会带标记，**不会被覆盖**。
        """
        n = 0
        for _key, s in self.sessions.items():
            if s.get("kb_by_user"):
                continue
            if bool(s.get("kb_enabled")) is not KB_DEFAULT:
                s["kb_enabled"] = KB_DEFAULT
                n += 1
        if n:
            self._log("按新的默认值打开 %d 个会话的知识库（老配置里是旧默认）" % n)
            try:
                self.save()
            except Exception as e:
                self._log("保存会话表失败（下次启动会再迁移一次）: %s: %s"
                          % (type(e).__name__, e))

    def _apply_bootstrap(self) -> None:
        """套用配置里的名单：白名单启用、黑名单强制关闭。

        默认是**全部启用**（`default_mode: on`），所以白名单通常是空的；
        黑名单 `sessions.disabled` 是留给「不想被处理的会话」的安全阀 ——
        银行、工作群之类，加进去它就完全不读不记。
        黑名单优先级更高，写在两个名单里的以黑名单为准。
        """
        # default_mode 是 on 时，把「从未被显式配置过」的会话也拉到 on ——
        # 否则改 default_mode 对**已经发现过**的会话不起作用：它们的 mode
        # 早就写死在 state.json 里了（实测 11 个会话有 8 个卡在 off）。
        # 判据用 enabled_by：只有白名单 / 黑名单 / 用户显式设置过才会有这个字段，
        # 自动发现的没有 —— 所以「没有 enabled_by 的 off」= 只是沿用了当时的默认值。
        if self.default_mode == MODE_ON:
            pulled = 0
            for s in self.sessions.values():
                if s.get("mode") == MODE_OFF and not s.get("enabled_by"):
                    s["mode"] = MODE_ON
                    s["is_new"] = False
                    pulled += 1
            if pulled:
                self._log("default_mode=on：把 %d 个此前自动发现、从未配置过的会话也启用了"
                          % pulled)

        for name in self._bootstrap:
            s = self.sessions.setdefault(name, self._new_session(name))
            if s.get("mode") != MODE_ON:
                s["mode"] = MODE_ON
                s["is_new"] = False
                s["enabled_by"] = "config"
                self._log("按配置白名单启用会话: %s" % name)

        for name in self._disabled:
            s = self.sessions.setdefault(name, self._new_session(name))
            if s.get("mode") != MODE_OFF:
                s["mode"] = MODE_OFF
                s["enabled_by"] = "config-disabled"
                self._log("按配置**排除**会话（完全不读不记）: %s" % name)

        # 系统会话（公众号 / 微信支付 …）—— 关键词子串匹配，**在已有的表上也过一遍**：
        # ⚠️ 光靠 `discover()` 新建时设 off 是不够的 —— 那些会话可能**早就**在
        #    state.json 里了，而且当时（没有这个功能之前）是以 `default_mode: on`
        #    建进去的。不在这里刷一遍的话，加了这个配置对它们**不生效**。
        pinned = 0
        for key, s in self.sessions.items():
            if self.is_system_session(key) and s.get("mode") != MODE_OFF:
                s["mode"] = MODE_OFF
                s["enabled_by"] = "system-pattern"
                pinned += 1
        if pinned:
            self._log("按「系统会话」关键词排除 %d 个会话（公众号 / 微信支付 这类）"
                      % pinned)

    def _new_session(self, key: str) -> dict:
        return {
            "key": key,
            "display_name": key,
            "raw_names": [key],
            "mode": self.default_mode,
            "is_new": True,
            "persona": None,
            "kb_enabled": KB_DEFAULT,
            "kb_id": None,
        }

    # ------------------------------------------------------------ 会话操作

    def _rebuild_norm_index(self) -> None:
        """重建「归一化名 -> 已有 key」的索引（合并变体用）。

        ⚠️ 归一化名**为空**的（全 emoji 的会话名）**不进索引** ——
            不然它们会互相合并成一条（实测 `😝😜🤪😛😋` 就归一化成空串）。
        ⚠️ 同一个归一化名对应多个 key 时，**先来的赢**（第一个进索引的）——
            后建的那条会留在表里（不删，删了会丢它的历史），
            但从现在起新读到的变体都会认到先来那个。
        """
        self._norm_index: Dict[str, str] = {}
        if not self._merge_variants:
            return
        for key in self.sessions:
            n = norm_title(key)
            if n and n not in self._norm_index:
                self._norm_index[n] = key

    def key_for(self, raw_title: str) -> str:
        """原始会话名 -> 唯一 session_key。

        ⚠️⚠️ **不能只是 `return raw_title`**（2026-10-11 修）。

            UIA 读控件的 Name（emoji / 颜文字**原样**），OCR 认字形
            （emoji 认不出，实测变成 `口` / `0`）。同一句「只差装饰」的名字
            于是成了两个 key —— 同一个会话的**历史被劈成两半**，
            关系设定 / 人设 / 黑名单也各认各的。

        规则（**保守**）：
            ① 精确命中已有 key -> 就用它
            ② 归一化之后命中已有 key -> **复用那个 key**，并把这次的原名
               记进它的 `raw_names`（日志里说一声）
            ③ 都不命中 -> 用原文当新 key

        ⚠️ 归一化名**为空**时绝不合并（全 emoji 的名字会全挤成一条）。
        ⚠️ 关掉 `merge_variants` 就退回旧行为（只做 ①）。
        ⚠️ 这是**有副作用**的 getter：命中变体时会往 `raw_names` 里追加。
           故意的 —— 调用方（`discover` / `mode_of` / 各处取 key）都不该
           自己去维护别名表，堵在这一个出口才不会漏。
        """
        t = raw_title or ""
        # ⚠️⚠️ 2026-10-13：**key 用“去掉群成员数”的名字**。
        #    人数会变（`(50)` -> `(54)`），带着它在界面上看起来就像
        #    **另一个会话**（用户直接问过「群聊后缀人数没有去吗」）。
        #    ⚠️ **不能拿 `norm_title` 当 key**：它还会 casefold + 去装饰，
        #       那会把用户看到的字（emoji）也改掉。这里**只去末尾的 `(N)`**。
        t = strip_group_count(t)
        if t in self.sessions or not self._merge_variants:
            return t
        n = norm_title(t)
        if not n:
            return t
        hit = getattr(self, "_norm_index", {}).get(n)
        if not hit or hit == t:
            return t
        s = self.sessions.get(hit)
        if s is None:
            return t
        names = s.setdefault("raw_names", [])
        if t not in names:
            names.append(t)
            self._log("会话名变体合并: %r 归到 %r（只差 emoji/符号，"
                      "不合并的话历史会被劈成两份）" % (t, hit))
        return hit

    def discover(self, raw_title: str) -> bool:
        """登记一个新发现的会话。返回是否**首次**发现（= 要不要落盘）。

        ⚠️⚠️ 2026-10-11 用户要求「**两次确定后再加新的会话**」+「统一去噪」。
            原因：OCR 会**偶发**吐出一个乱码标题（'X' / 'G' / '0'），
            一见到就建会话的话，列表里会冒出一堆**不知道从哪来的**条目 ——
            而且它们会各自攒出空记录、还会被算进 `default_mode`。

        两道人墙（都在建之前）：

        ① `is_junk_title()` —— 归一化后空 / 单字 / 纯数字的，**永远不建**
           （连"看到两次"也不建：那种形态不是会话名，看到一百次也不是）。
        ② **`TITLE_CONFIRM_N` 次确认** —— 同一个标题要**在不同的拍里看到 N 次**
           才建。OCR 的乱码是**偶发**的（下一拍就变了），而真会话会**一直在**，
           所以"两次"就能把它们分开。

        ⚠️ 计数器是**内存里**的（`self._pending`），重启就清 ——
            这是有意的：重启后你多半还在同一个会话上，再看两次就登记了，
            代价只是"刚开程序那两拍不记录"。**别挪进 state.json**：
            那会让一个只出现一次的乱码跨重启累积成"确认过"。
        ⚠️ `discover()` 返回 False **不等于**出错 —— 调用方只该在 True 时落盘。
        """
        key = self.key_for(raw_title)
        # ⚠️ 系统会话（公众号 / 微信支付 …）**根本不登记**（2026-10-13 用户要求：
        #    「微信收款这些改为不记录。不要默认关」）。
        #    以前是“建了再设 mode=off”，于是它们**占着会话列表**、
        #    显示成“关闭”，看起来像是用户自己关的 ——
        #    其实它们根本不是“会话”。
        #    现在：**建之前就挡掉**，而且**已经在库里的也移除**。
        #    ⚠️ 只删系统自动关的那些（`enabled_by == "system-pattern"`）——
        #       用户手动设过的（`enabled_by == "user"`）不动。
        if self.is_system_session(key):
            _s0 = self.sessions.get(key)
            if _s0 is not None and _s0.get("enabled_by") == "system-pattern":
                self.sessions.pop(key, None)
                self._log("从登记表里移除系统会话 %r（它不是会话）"
                          % raw_title)
            return False
        s = self.sessions.get(key)
        if s is not None:
            names = s.setdefault("raw_names", [])
            if raw_title not in names:
                names.append(raw_title)
            self._pending.pop(key, None)      # 已经登记过，不用再数
            return False
        # ---- ① 垃圾标题：永远不建
        if is_junk_title(key):
            self._junk_seen = getattr(self, "_junk_seen", set())
            if key not in self._junk_seen:
                self._junk_seen.add(key)
                self._log("忽略一个不像会话名的标题 %r（归一化后是空/单字/纯数字）"
                          % raw_title)
            return False
        # ---- ② 两次确认：第一次只记数，不建
        # ⚠️ 2026-10-13：改成**连续**确认。
        #    原来只“累计”而 `_pending` **永不过期**，于是
        #        第 1 拍撞见一次、第 200 拍又撞见一次
        #    也会建档 —— 而两次之间隔了几分钟，根本不是“同一个会话”。
        #    现在：超过 `_CONFIRM_WINDOW` 秒没再见到 -> **重新数**。
        now = time.time()
        n, last = self._pending.get(key, (0, 0.0))
        if now - last > _CONFIRM_WINDOW:
            n = 0
        n += 1
        self._pending[key] = (n, now)
        if n < SESSION_CONFIRM_N:
            return False
        self._pending.pop(key, None)
        # ⚠️ 这里**不再**有“建好了再设 mode=off”那一步 ——
        #    系统会话在上面就直接 return 了（见那段注释）。
        s = self._new_session(key)
        self.sessions[key] = s
        # ⚠️ 新建的会话要**立刻进归一化索引** —— 不然同一轮里紧接着读到的
        #    另一个变体（比如 UIA 切 OCR 时先读到带 emoji 的、再读到不带的）
        #    会找不到它，于是又建一条。
        nk = norm_title(key)
        if self._merge_variants and nk and nk not in getattr(self, "_norm_index", {}):
            self._norm_index[nk] = key
        return True

    def is_system_session(self, raw_title: str) -> bool:
        """是不是「系统会话」（公众号 / 微信支付 / 服务通知 …）。

        用途：这些东西**不是聊天**，但微信里长得跟聊天一样（有标题栏、
        有内容区），所以像素这条路照样会把它们读成会话、把内容读成消息。
        `sessions.disabled_contains` 里配的就是这类名字的**关键词**（子串匹配）。

        ⚠️ 用**子串**而不是全等：公众号有成千上万个（`人民日报`、`新华社`…），
           全等没法穷举；而它们的归属页/标题里会带「公众号」这类词。
        ⚠️ 副作用：名字里**碰巧**含这些词的正常会话也会被排除
           （比如一个叫「XX公众号运营交流群」的群）。所以列表要短、要具体，
           用户发现误伤时把它删掉即可 —— 这比"漏掉一堆公众号"划算。
        """
        t = raw_title or ""
        return any(p and p in t for p in self._disabled_contains)

    def mode_of(self, raw_title: str) -> str:
        # ⚠️ 系统会话**每次都判一次**（不是只在登记时判）：
        #    这样即使用户在会话面板里手动把它设成「开」，也仍然不生效 ——
        #    和黑名单一样的"安全阀优先级最高"语义。
        if self.is_system_session(raw_title):
            return MODE_OFF
        s = self.sessions.get(self.key_for(raw_title))
        return s.get("mode", self.default_mode) if s else self.default_mode

    def is_registered(self, raw_title: str) -> bool:
        """这个会话**已经通过两道人墙、登记在册**了吗？

        用途：记录器拿它当「**只给确认过的会话写库**」的闸门（2026-10-13）。

        ⚠️ 为什么库这边也得有闸：`is_junk_title` / 两次确认原来**只挡
            `state.json`**（设置里那份会话列表），而记录器是直接
            `key_for(title)` 就写 `screens.db` / `history.db` 的。

            实测证据（2026-10-13 查库）：两个库里躺着 **15** 个会话名，
            而 `state.json` 里只有 **2** 个（`张三` / `示例23班甲群`）。
            多出来的那些根本不是会话：

                'X'      -> 微信设置页的文案（'字体大小' / '将文字翻译为…'）
                '简历'    -> 资源管理器 + PyCharm + 我们自己的面板（OCR 抓错了画面）
                '+' / '①' -> OCR 认错的碎片

            用户看到的就是「翻记录时冒出一堆不知道从哪来的记录」。
        """
        return self.key_for(raw_title) in self.sessions

    def is_enabled(self, raw_title: str) -> bool:
        """是否处于「启用」状态（需要提醒 + 生成建议）。"""
        return self.mode_of(raw_title) == MODE_ON

    def set_mode(self, raw_title: str, mode: str) -> None:
        key = self.key_for(raw_title)
        s = self.sessions.setdefault(key, self._new_session(key))
        s["mode"] = mode
        # 标记成「用户显式设置过」：以后改 default_mode 不会再把这条覆盖掉
        s["enabled_by"] = "user"
        if mode != MODE_OFF:
            s["is_new"] = False

    # ---- 知识库（2026-10-17）
    #
    # ⚠️ 这是**会话级**的第二个开关，和 `mode` 是两件事，别合并：
    #    `mode` 管"要不要提醒/记录/生成"；`kb_enabled` 管"生成时要不要查资料"。
    #    一个会话可以是 `observe`（只记录不生成）却仍然开着知识库（以后生成时用得上）。
    #
    # ⚠️ 字段早就留好了（`_new_session` 里的 `kb_enabled: False`，一直没人读）。
    #    默认 **False**：私聊里突然冒出公司资料是灾难，所以必须一处处显式打开。

    def kb_enabled(self, raw_title: str) -> bool:
        """这个会话要不要用知识库（**出厂默认开**，见 `KB_DEFAULT`）。

        ⚠️ 老 `state.json` 里没有这个字段 → 按 `KB_DEFAULT` 算，别默认成 False。
        """
        key = self.key_for(raw_title)
        return bool((self.sessions.get(key) or {}).get("kb_enabled", KB_DEFAULT))

    def set_kb_enabled(self, raw_title: str, on: bool) -> None:
        key = self.key_for(raw_title)
        s = self.sessions.setdefault(key, self._new_session(key))
        s["kb_enabled"] = bool(on)
        # ⚠️ 打标记：以后改 `KB_DEFAULT` 时**不要**覆盖用户手动关掉的那个
        #    （`_migrate_kb_default()` 只迁移没有这个标记的）。
        s["kb_by_user"] = True

    def touch(self, raw_title: str) -> None:
        """记下「**刚刚看到这个会话**」的时刻（设置面板按时间排要用）。

        ⚠️ 时间用**字符串** `"2026-09-27 20:40:52"`，跟 `screens.seen_at` 同一个格式 ——
            这样两个来源可以直接按**字典序**比大小，不用解析（见
            `ScreenStore.last_seen_map()` 的说明）。
        ⚠️ 它记的是「你**打开过**它」，不是「里面来了新消息」。
            这两件事微信自己也是混在一起的：微信的会话列表就是"你点过它"
            也会往上顶。所以语义是对的。
        ⚠️ **故意不在这里 save()**：调用方是记录器，每切一次会话就落一次盘太浪费。
            落盘复用现成的 `save()`（退出时、改 mode 时都会调）。
        """
        key = self.key_for(raw_title)
        s = self.sessions.get(key)
        if s is None:
            return
        s["last_seen_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def enabled_sessions(self) -> List[str]:
        return [k for k, v in self.sessions.items() if v.get("mode") == MODE_ON]

    def new_sessions(self) -> List[str]:
        """尚未被用户处理过的会话（管理界面标「新」）。"""
        return [k for k, v in self.sessions.items() if v.get("is_new")]

    # ------------------------------------------------------------ 增量游标

    def get_tail(self, key: str) -> list:
        """读取增量比对游标。

        ⚠️ 必须把 JSON 读回来的 list 规范成 tuple：
        JSON 没有元组类型，tuple 存进去、读出来就变成 list，
        而内存里 to_keys() 产出的是 tuple。Python 里 (1,2) != [1,2]，
        若不做规范化，比对会永远不相等 —— 表现就是「每轮都误报有新消息」。
        """
        raw = (self.sync.get(key) or {}).get("tail", []) or []
        out = []
        for item in raw:
            out.append(tuple(item) if isinstance(item, (list, tuple)) else item)
        return out

    def set_tail(self, key: str, tail: list) -> None:
        self.sync.setdefault(key, {})["tail"] = list(tail)
