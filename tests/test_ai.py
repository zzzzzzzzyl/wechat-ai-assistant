# -*- coding: utf-8 -*-
"""AI 模块测试：配置加载、真实调用、输出解析容错。

用法：python tests/test_ai.py          # 含真实 API 调用
      python tests/test_ai.py --offline # 只测解析逻辑，不联网
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.ai import AIClient, AIError, parse_replies
from wechat_assistant.config import load_config

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [通过] %s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, detail))


def test_parse():
    print("\n1. 输出解析容错（不联网）")
    check("标准 JSON", parse_replies('{"replies": ["A", "B"]}', 2) == ["A", "B"])
    check("代码块包裹",
          parse_replies('```json\n{"replies": ["A", "B"]}\n```', 2) == ["A", "B"])
    check("前后有废话",
          parse_replies('好的，我来写：\n{"replies": ["A", "B"]}\n以上。', 2) == ["A", "B"])
    check("数组形式", parse_replies('["A", "B"]', 2) == ["A", "B"])
    check("对象数组",
          parse_replies('{"replies": [{"text": "A"}, {"text": "B"}]}', 2) == ["A", "B"])
    check("超量截断", parse_replies('{"replies": ["A","B","C","D"]}', 2) == ["A", "B"])
    check("纯文本兜底", parse_replies("1. 好的\n2. 行", 2) == ["好的", "行"])
    check("空输入", parse_replies("", 3) == [])
    check("中文键", parse_replies('{"回复": ["A", "B"]}', 2) == ["A", "B"])


def test_prompt_template():
    """⭐ system prompt 现在是「模板 + 占位符」，用户在设置里能整段改。

    （2026-10-11 用户要求「提示词部分要可给用户在设置修改」，选的整段可改。）

    ⚠️ 这里只放**最容易悄悄坏掉**的几条；逐字对比改之前那套拼接的完整验收
       在 `probes/check_prompt_template.py`（它会打印差异）。
    """
    print("\n1b. 提示词模板（设置里可改的那一整段）")
    from wechat_assistant import prompts

    check("默认正文非空且够长", len(prompts.DEFAULT_SYSTEM_TEMPLATE) > 400,
          "%d 字" % len(prompts.DEFAULT_SYSTEM_TEMPLATE))
    # ⚠️⚠️ 用户那一框**只放他自己的两段规则**（2026-10-11 要求
    #    「显示并可改【硬性禁止，必须遵守】【上下文规则】这里的东西就可以」）。
    #    **不许出现占位符**，**也不许出现【输出要求】** ——
    #    后者挪到代码侧（`OUTPUT_SPEC`）了，那样用户根本改不到，
    #    解析器依赖的 JSON 结构就丢不了。
    import re as _re_ai
    box = prompts.DEFAULT_SYSTEM_TEMPLATE
    check("框里有【硬性禁止】和【上下文规则】",
          "【硬性禁止，必须遵守】" in box and "【上下文规则】" in box)
    check("框里**没有**占位符", not _re_ai.findall(r"\{[a-z]+\}", box),
          str(_re_ai.findall(r"\{[a-z]+\}", box)))
    check("框里**没有**【输出要求】（它在代码侧）", "【输出要求】" not in box)
    check("代码侧 OUTPUT_SPEC 里有 JSON 结构和【输出要求】",
          "【输出要求】" in prompts.OUTPUT_SPEC
          and '"intent"' in prompts.OUTPUT_SPEC
          and '"replies"' in prompts.OUTPUT_SPEC)
    check("条数占位符 {n} 在代码侧的 OUTPUT_SPEC 里（用户不用管）",
          "{n}" in prompts.OUTPUT_SPEC)

    # 外面那几块真的拼上去了
    got = prompts.render_system(prompts.DEFAULT_SYSTEM_TEMPLATE, persona="人设P",
                                relationship="关系R", style_samples=["样本S"],
                                user_intent="意图I", n_replies=4)
    for want in ("人设P", "【你们的关系】关系R", "样本S", "意图I"):
        check("  外面拼上了: %s" % want, want in got)
    check("  条数进了【输出要求】", "replies：4条候选回复" in got)
    check("  用户的正文原样在里面", "【硬性禁止，必须遵守】" in got)
    check("  没留下未替换的占位符",
          all(k not in got for k in ("{persona}", "{relationship}", "{style}",
                                     "{intent}", "{n}", "{step2}")))
    # 顺序：人设在最前、输出要求在最后
    check("  人设在最前面", got.startswith("人设P"))
    check("  输出要求在最后", got.rstrip().endswith("单语气词"), repr(got[-24:]))

    # 没给的那几块要**整块为空**（不是留下空标题）
    bare = prompts.render_system(prompts.DEFAULT_SYSTEM_TEMPLATE, persona="人设")
    check("  没给关系 -> 不出现「你们的关系」", "【你们的关系】" not in bare)
    check("  没给样本 -> 不出现口吻样本那段", "照着模仿" not in bare)
    check("  没填我想说 -> 不出现最高优先级", "最高优先级" not in bare)

    # ⚠️ 模板换了之后**不许再有指向「第二步」的说明** —— 那个结构没了，
    #    任何还提它的句子都是**悬空引用**。
    check("  渲染结果里不出现「第二步」（悬空引用）", "第二步" not in got)
    check("  INTENT_BLOCK 不自相矛盾（不引用别处）",
          "压过" not in prompts.INTENT_BLOCK)

    # ⚠️ 边界：清空 / 花括号 / 没有 {n} —— 都不能炸
    check("  正文清空 -> 回落内置默认",
          "【硬性禁止" in prompts.render_system("", persona="x"))
    check("  正文里有 JSON 花括号也不炸（所以实现用 replace 不用 format）",
          '{"a": 1}' in prompts.render_system('{"a": 1}', persona="P"))
    # ⚠️ 现在的拼接是「人设 + 正文 + 输出要求」，所以正文**不再**是开头 ——
    #    判据只能是"它还在、且没炸"（第一版我沿用了旧断言 startswith，白红了一次）。
    _nod = prompts.render_system("就写几条", persona="P")
    check("  正文里删掉 {n} 也不炸（条数在代码侧）",
          "就写几条" in _nod and "replies：" in _nod)
    check("  人设为空 -> 有兜底角色说明",
          "你是一个聊天助手。" in prompts.render_system("", persona=""))

    # ⚠️ 用户要是自己在正文里写了 {intent}，就按他放的位置来，别拼两份
    once = prompts.render_system("先看这个：{intent}然后照办", persona="P",
                                 user_intent="我在忙")
    check("  正文里自己写了 {intent} -> 只出现一次",
          once.count("最高优先级") == 1, "出现 %d 次" % once.count("最高优先级"))

    check("OUTPUT_FORMAT 兜底常量还在（防 OUTPUT_SPEC 被误删）",
          '"intent"' in prompts.OUTPUT_FORMAT
          and '"replies"' in prompts.OUTPUT_FORMAT)


def test_missing_config():
    """⭐ 2026-10-13：**接口地址与模型出厂是空的**（用户要求「默认为空吧」）。

    这条守的是三件事：
      1. 留空 = **没配置**（不再有硬编码兜底悄悄替用户选好服务商）；
      2. "能不能调"的判据是**三样齐全**（地址 + Key + 模型），
         以前只看 Key —— 那样只填 Key 会静默打到 DeepSeek 上；
      3. 报错/状态行要说清**缺哪几项**（用户只补 Key 时得知道还差东西）。
    """

    class Cfg:
        def __init__(self, **kw):
            self._d = kw

        def get(self, key, default=None):
            return self._d.get(key, default)

    print("\n1d. 没配全就别发请求（接口地址/模型留空 = 没配置）")
    empty = AIClient(Cfg())
    check("三样都空 -> available 为假", empty.available is False)
    check("而且能说清缺哪三样",
          empty.missing == ["接口地址", "API Key", "模型名"], repr(empty.missing))

    only_key = AIClient(Cfg(**{"ai.api_key": "sk-x"}))
    check("⚠️ 只填 Key 也**不算**配好（地址/模型照样缺）",
          only_key.available is False and only_key.missing == ["接口地址", "模型名"],
          repr(only_key.missing))

    only_url = AIClient(Cfg(**{"ai.base_url": "https://example.com/"}))
    check("只填地址也不行", only_url.available is False, repr(only_url.missing))
    check("地址末尾的斜杠被去掉（拼路径不会出现 //）",
          only_url.base_url == "https://example.com", repr(only_url.base_url))

    full = AIClient(Cfg(**{"ai.api_key": "sk-x",
                           "ai.base_url": "https://example.com",
                           "ai.model": "some-model"}))
    check("三样齐全才算能调", full.available is True and full.missing == [],
          repr(full.missing))

    # ⚠️ 没配全时**不能**走到网络那一步，而且要给出人话
    args = dict(persona="人设", history=[{"who": "对方", "text": "在吗"}],
                current="在吗")
    try:
        full.generate_replies(**args)
        raised = ""
    except AIError as e:
        raised = str(e)
    except Exception as e:                        # 网络错误也可以（说明它真发了请求）
        raised = ""
    check("配全时会真的发请求（这里没网也不该报「还没配置」）",
          "还没配置" not in raised, raised[:60])

    try:
        empty.generate_replies(**args)
        msg = "(没有抛异常)"
    except AIError as e:
        msg = str(e)
    check("没配全时抛的是 AIError 而且点名叫缺什么",
          "还没配置" in msg and "接口地址" in msg, msg[:80])


def test_build_prompts_appends_format():
    """⚠️ **输出格式永远在 final system 里** —— 而且现在**用户改不到**。

    2026-10-11 改：用户那一框只装他自己的规则（【硬性禁止】+【上下文规则】），
    【输出要求】挪到**代码侧**（`prompts.OUTPUT_SPEC`）由 `render_system` 拼上。
    这比"模板里自带"更硬 —— 用户**根本改不到**它，
    所以 `parse_replies` 依赖的 JSON 结构不可能因为改提示词而丢。

    `ai.build_prompts` 里那段"没有就补上"保留着，是**防将来**有人把
    `OUTPUT_SPEC` 从拼接里删掉（到时会补上并记一行日志，而不是静默失败）。
    """
    print("\n1c. 输出格式：代码侧拼上、用户改不到")
    from wechat_assistant.prompts import OUTPUT_SPEC
    cfg = load_config()
    ai = AIClient(cfg)
    system, user = ai.build_prompts("人设", [{"who": "对方", "text": "在吗"}],
                                    "在吗", 3)
    check("system 里有 JSON 结构",
          '"intent"' in system and '"replies"' in system)
    check("system 里有【输出要求】", "【输出要求】" in system)
    check("**不该**再额外追加一份 OUTPUT_FORMAT（会重复）",
          "【输出格式】只输出 JSON" not in system,
          "重复了" if "【输出格式】只输出 JSON" in system else "")
    check("【输出要求】来自代码侧的 OUTPUT_SPEC", OUTPUT_SPEC[:20] in system)
    check("user 里有对话历史和要回复的那段",
          "【对话历史】" in user and "要回复的就是这一段" in user)

    # ⚠️⚠️ **用户把提示词框改成什么都没关系** —— 输出格式照样在。
    #    这正是"挪到代码侧"要买的东西：改坏提示词**不会**让解析失败。
    for junk in ("", "你就是个聊天助手，随便写。", "只回我一句就行"):
        ai2 = AIClient(cfg)
        ai2.system_template = junk
        s2, _ = ai2.build_prompts("人设", [], "在吗", 3)
        check("提示词框改成 %r 时输出格式仍在" % junk[:14],
              '"intent"' in s2 and '"replies"' in s2 and "【输出要求】" in s2)
    check("AIClient 从配置读到了那一段（空=用内置默认）",
          ai.system_template == (cfg.get("ai.system_prompt") or "").strip(),
          repr(ai.system_template[:20]))


def test_reply_target_wording():
    """⚠️⚠️ 回复目标那段的**措辞必须和实际取法一致**（2026-10-11 用户实测报的）。

    用户看到的是：

        【需要回复的最新消息】
        对方: 好吃
        细姨: 肿儿做年甲潘薯平样
        对方: 这个叫做碱水包…
        思驷: 皮韧咸香

    他的困惑完全合理：**标题承诺"最新消息"、内容却给了四行不同人的话**
    ——「我到底要回哪一句？」

    真相（有代码依据）：`run_watch` 默认走 `collect_visible_block()`，
    **当前屏幕上的全部对话**都是回复目标，不是最后一条。
    所以标题是**名不副实**的 —— 现在改成如实描述 + 把"哪条最新"说出来。

    ⚠️ 顺带那个更要命的：那两句 `对方:` 其实**是两个人**（群昵称没认出来时的
       兜底名，而「对方」在中文里暗示"只有两个人"）—— 模型会被带偏成一对一。
    """
    print("\n1d. 「要回复哪一段」的措辞 + 群聊里认不出昵称的人怎么标")
    from wechat_assistant.context import block_to_text
    from wechat_assistant.prompts import GROUP_UNKNOWN
    from wechat_assistant.reader import (ChatSnapshot, MessageItem,
                                         label_unknown_senders)

    def make(group, rows):
        s = ChatSnapshot(title="某群", is_group=group)
        for who, text, me in rows:
            s.messages.append(MessageItem(text=text, cls="mmui::ChatTextItemView",
                                          sender_name=who, is_self=me))
        label_unknown_senders(s)
        return s

    rows = [(None, "好吃", False), ("细姨", "肿儿做年甲潘薯平样", False),
            (None, "这个叫做碱水包", False), ("思驷", "皮韧咸香", False),
            (None, "没判出发送者的", None)]

    g = block_to_text(make(True, rows).messages)
    one = block_to_text(make(False, rows).messages)

    check("群聊里认不出昵称的 -> 群友（不是「对方」）",
          g.count(GROUP_UNKNOWN + ":") == 2, repr(g.splitlines()[:2]))

    # ⚠️⚠️ 用户报（2026-10-11）：「**如果是私聊，就是不会有上面的名称，
    #     就不要是群友**」。
    #     `is_group` 在 **OCR 后端**是"看到任何一条消息带昵称"就置真的，
    #     而昵称判定是几何 + 像素、**会误判**。私聊里误判一次，
    #     整屏的"别人"就全变「群友」—— 而私聊里那个词是错的。
    #     所以**只看 `is_group` 不够**：还要有"这一屏真的认出了别人的昵称"这个证据
    #     （私聊永远不会有昵称行）。
    no_names = [(None, "好吃", False), (None, "嗯", False)]
    one2one = block_to_text(make(False, no_names).messages)
    check("私聊（没人有昵称）-> 仍是「对方」",
          one2one.count("对方:") == 2 and GROUP_UNKNOWN not in one2one,
          repr(one2one.splitlines()[:2]))
    # 这就是用户遇到的那种：私聊被误判成群
    faked = block_to_text(make(True, no_names).messages)
    check("  ⚠️ 私聊被**误判成群**（is_group=True）时也不许出现「群友」",
          GROUP_UNKNOWN not in faked and faked.count("对方:") == 2,
          repr(faked.splitlines()[:2]))
    # 反过来：群聊但一个昵称都没认出来 -> 也没证据说这是群，保持「对方」
    check("  群聊但一个昵称都没认出来 -> 也不硬贴「群友」",
          GROUP_UNKNOWN not in faked)
    check("一对一里**仍然**叫「对方」（那里是对的）",
          one.count("对方:") == 2, repr(one.splitlines()[:2]))
    check("认出来的昵称原样保留", "细姨:" in g and "思驷:" in g)
    check("is_self 没判出来（None）的**不伪造** —— 保持 ?",
          "?: 没判出发送者的" in g, repr(g.splitlines()[-1]))

    cfg = load_config()
    ai = AIClient(cfg)
    _, user = ai.build_prompts("人设", [], g, 3)
    check("标题如实描述（不再是「需要回复的最新消息」）",
          "需要回复的最新消息" not in user and "要回复的就是这一段" in user)
    check("明确说了「最后一条是最新的」", "最后一条是最新的" in user)
    check("有「群友」时给出警告：它们不一定是同一个人",
          "不一定是同一个人" in user)

    _, user2 = ai.build_prompts("人设", [], "对方: 在吗", 3)
    check("块里没有「群友」时**不加**那句警告（省 token）",
          "不一定是同一个人" not in user2)
    check("但标题和「最后一条」那句永远有",
          "要回复的就是这一段" in user2 and "最后一条是最新的" in user2)

    _, user3 = ai.build_prompts("人设", [], "", 3)
    check("current 为空时不拼那段标题", "要回复的就是这一段" not in user3)


def main():
    offline = "--offline" in sys.argv
    print("=" * 80)
    print("AI 模块测试")
    print("=" * 80)

    test_parse()
    test_prompt_template()
    test_missing_config()
    test_build_prompts_appends_format()
    test_reply_target_wording()

    print("\n2. 配置加载（含 secrets.yaml 合并）")
    cfg = load_config()
    ai = AIClient(cfg)
    # ⚠️ 2026-10-13：接口地址/模型**出厂就是空的**（用户要求「默认为空吧」），
    #    所以这两条**不再断言"已配置"** —— 只如实报出当前值，
    #    有没有配好由 `available` / `missing` 判（§1d）。
    check("接口地址读出来了（出厂为空是正常的）", True, repr(ai.base_url))
    check("模型读出来了（出厂为空是正常的）", True, repr(ai.model))
    check("没配全时 available 为假（这条才是判据）",
          ai.available is False or not ai.missing,
          "缺 %r" % (ai.missing,))
    # ⚠️ 没有 `secrets.yaml` 时这条**跳过**，不算失败（2026-10-15）：
    #    这个文件不在版本库里（里面有本机的 API Key），刚克隆下来就是没有它。
    _sec = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "secrets.yaml")
    if os.path.exists(_sec):
        check("api_key 已从 secrets.yaml 读到", bool(ai.api_key),
              "长度 %d，前缀 %s…" % (len(ai.api_key), ai.api_key[:8]) if ai.api_key else "缺失")
    else:
        print("   [跳过] api_key 来自 secrets.yaml（本机没有这个文件）")
    check("人设已配置", bool(cfg.get("persona.default")))

    if offline or not ai.available:
        print("\n（跳过联网测试）")
        print("\n结果: %d 通过 / %d 失败" % (PASS, FAIL))
        return 1 if FAIL else 0

    print("\n3. 接口连通性")
    try:
        models = ai.list_models()
        check("能列出模型", len(models) > 0, str(models))
    except AIError as e:
        check("能列出模型", False, str(e))

    print("\n4. 真实生成回复")
    history = [
        {"who": "李四", "text": "我可是正规的学习资料你在想什么"},
        {"who": "我", "text": "行"},
    ]
    current = "看点文献论文什么的"
    persona = cfg.get("persona.default")
    try:
        replies = ai.generate_replies(persona, history, current, n_replies=3)
        check("返回了回复", len(replies) > 0, "共 %d 条" % len(replies))
        for i, r in enumerate(replies, 1):
            print("      %d. %s" % (i, r))
        check("条数符合预期", len(replies) <= 3)
    except AIError as e:
        check("生成回复", False, str(e))

    print("\n" + "=" * 80)
    print("结果: %d 通过 / %d 失败" % (PASS, FAIL))
    print("=" * 80)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
