# -*- coding: utf-8 -*-
"""验收提示词模板：**用户给的规则必须一行不少、且没有指向已删除内容的说明**。

    python probes/check_prompt_template.py

⚠️ 基准换过一次（2026-10-11）
--------------------------------
    原来这个探针拿"**改之前那套代码拼出来的提示词**"当基准逐字对比。
    后来用户直接给了**一份新的提示词**（【硬性禁止】+【上下文规则】+【输出要求】），
    默认模板整个换掉了 —— 旧基准**已经不存在**，再拿它比只会满屏差异。

    现在的基准是：**用户写的每一行原文，必须一字不差地出现在渲染结果里。**
    这也是这个探针真正要守的东西：

        重构提示词最容易**悄悄掉一行规则**，而改坏提示词**不报错** ——
        只表现为"AI 写得不如以前"，靠"跑一下看着还行"发现不了。

判据
----
① 用户那 20 行规则**逐行**都在（掉任何一行就失败）
② 占位符都生效：人设 / 关系 / 口吻样本 /「我想说」/ 条数，给了就有、没给整块为空
③ ⚠️ **不许有指向「已不存在的东西」的说法** —— 默认模板换成用户那份之后
   「第一步/第二步」结构没了，任何还提"第二步"的句子都是**悬空引用**
④ 输出格式：模板里**自带**（用户写的【输出要求】）；`OUTPUT_FORMAT` 是**兜底**
   （模板被清掉时代码补上，否则解析会坏）
⑤ 边界：模板清空 / 没有 `{n}` / 有 JSON 花括号 —— 都不能炸
⑥ 给用户看的占位符说明要和实现**对得上**（列了却不起作用 = 白写）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wechat_assistant import prompts  # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    print("  [%s] %s %s" % ("通过" if ok else "失败", name, detail))
    if ok:
        PASS += 1
    else:
        FAIL += 1


#: ⚠️ **用户写的、放在"框里"的那两段规则**（一行不改地列在这里当基准）。
#: 改模板的人掉了哪一行，这里就会报出来。
BOX_RULES = [
    "【硬性禁止，必须遵守】",
    "- 只用第一人称，生成可以直接发送的微信消息",
    "- 不复述、不总结对方的话",
    "- 不加括号备注、不解释回复理由",
    "- 禁止使用：首先、其次、总之、话说回来、亲、您、加油哦、呢",
    "- 禁止排比、工整对仗长句",
    "- 不要AI腔，口语碎片化",
    "- 只输出JSON，严禁输出任何思考过程、额外文字、序号",
    "- 输出JSON不要用```代码块包裹，直接裸JSON文本",
    "【上下文规则】",
    "1. 先看历史消息在聊什么话题、上下文到哪了，再回应当前屏幕这一段；"
    "历史从上到下越来越近。标着 >> 的那条是这次要回应的，"
    "它后面的几句是紧接着的补充，要一起看。",
    "2. 若历史内出现代码、说明文本，忽略，不要当成聊天消息",
    "3. 群聊里多条不同人连续发言：按 >> 标出的那条回应，"
    "同时照顾同一批连续发言（它们往往是同一件事的补充）。",
    "4. 识别对方意图：区分是陈述事情，还是表达情绪。"
    "气话不要讲道理、辩解、翻旧账，优先接住情绪",
    "5.上下文可能会有乱码与识别错误的字段，判断后可忽略。",
    "6.消息可能来自群聊，需要分辨发言角色；标记为「我:」的消息是我方历史发言，"
    "属于背景，不是对方消息，不要当成待回复内容。若整屏只有「我:」的消息，"
    "就顺着上一句往下接。",
]

#: ⚠️ 【输出要求】那几行**已经不在框里了**（挪到代码侧的 `OUTPUT_SPEC`）——
#: 所以它们要出现在**渲染结果**里，但**不能**出现在框里。
OUTPUT_LINES = [
    "【输出要求】",
    '{"intent": "对方这句话的真实意思（一句话）", '
    '"replies": ["回复1", "回复2", "回复3"]}',
    "1. intent字段：一句话概括对方真实意图",
]

#: 用户写过的**全部**文字（框里 16 行 + 代码侧 3 行）——
#: 用来断言"渲染结果里一行都没掉"。
USER_RULES = BOX_RULES + OUTPUT_LINES


def main():
    print("== 1. 用户那一框：**只放他自己的两段规则** ==")
    # ⚠️⚠️ 2026-10-11 用户要求「显示并可改【硬性禁止，必须遵守】【上下文规则】
    #    这里的东西就可以」—— 所以框里**不许**有占位符，**也不许**有【输出要求】。
    import re as _re
    box = prompts.DEFAULT_SYSTEM_TEMPLATE
    check("框里有【硬性禁止，必须遵守】", "【硬性禁止，必须遵守】" in box)
    check("框里有【上下文规则】", "【上下文规则】" in box)
    _ph = _re.findall(r"\{[a-z_]+\}", box)
    check("框里**没有**占位符", not _ph, "还有: %s" % _ph if _ph else "")
    check("框里**没有**【输出要求】（它挪到代码侧了）", "【输出要求】" not in box)
    check("框里没有【第一步】/【第二步】（那个结构早没了）",
          "第一步" not in box and "第二步" not in box)

    print("\n== 2. 用户写的规则**逐行**都还在（渲染结果里）==")
    # 这才是这个探针真正要守的东西：重构提示词最容易**悄悄掉一行规则**，
    # 而改坏提示词**不报错** —— 只表现为"AI 写得不如以前"。
    got = prompts.render_system(prompts.DEFAULT_SYSTEM_TEMPLATE, persona="人设P",
                               relationship="关系R", style_samples=["样本S"],
                               user_intent="意图I", n_replies=4)
    missing = [l for l in USER_RULES if l not in got]
    check("用户写的 %d 行规则全部原样保留" % len(USER_RULES), not missing,
          "掉了: %s" % missing if missing else "")
    check("用户正文整段原样出现在渲染结果里",
          prompts.DEFAULT_SYSTEM_TEMPLATE in got)

    print("\n== 3. 外面拼的那几块：给了就有、没给就整块为空 ==")
    empty = prompts.render_system(prompts.DEFAULT_SYSTEM_TEMPLATE, persona="人设P")
    check("人设拼在最前面", got.startswith("人设P"))
    check("关系拼上了", "【你们的关系】关系R" in got)
    check("口吻样本拼上了（防 AI 腔最有效的一环，**别丢**）", "样本S" in got)
    check("「我想说」拼上了", "意图I" in got)
    check("输出要求在**最后**", got.rstrip().endswith("单语气词"))
    check("没给关系 -> 整块为空", "【你们的关系】" not in empty)
    check("没给样本 -> 整块为空", "照着模仿" not in empty)
    check("没填我想说 -> 整块为空", "最高优先级" not in empty)
    check("没留下未替换的占位符",
          not [k for k in ("{persona}", "{relationship}", "{style}",
                           "{intent}", "{n}", "{step2}") if k in got])

    print("\n== 4. 条数：从代码侧的【输出要求】走（用户不用管占位符）==")
    check("条数进了【输出要求】", "replies：4条候选回复" in got)
    check("正文里没有写死条数（用户框里不该出现数字条数）",
          "3条候选" not in prompts.DEFAULT_SYSTEM_TEMPLATE)
    check("输出要求**永远**在最终提示词里（用户改不到它）",
          "【输出要求】" in got and '"replies"' in got)
    # ⚠️ 把正文清空/换掉，输出格式照样在 —— 这就是"挪到代码侧"买的东西
    check("正文清空时输出要求仍在",
          "【输出要求】" in prompts.render_system("", persona="x"))
    check("正文被换成别的东西时输出要求仍在",
          "【输出要求】" in prompts.render_system("随便写点啥", persona="x"))

    print("\n== 5. 不许有指向「已不存在的东西」的说法 ==")
    check("渲染结果里不出现「第二步」（那个结构已经没了）",
          "第二步" not in got, "出现了" if "第二步" in got else "")
    check("默认正文里没有 {step2}（它只是可选占位符）",
          "{step2}" not in prompts.DEFAULT_SYSTEM_TEMPLATE)
    check("INTENT_BLOCK 不自相矛盾（不引用别处的段）",
          "压过" not in prompts.INTENT_BLOCK)
    # ⚠️ 用户要是在正文里自己写了 {intent}，按他放的位置来，**别拼两份**
    once = prompts.render_system("先看这个：{intent}然后照办", persona="P",
                                 user_intent="我在忙")
    check("正文里自己写了 {intent} -> 只出现一次",
          once.count("最高优先级") == 1, "出现 %d 次" % once.count("最高优先级"))

    print("\n== 6. 边界：不该炸的情况 ==")
    check("正文清空 -> 回落内置默认",
          "【硬性禁止" in prompts.render_system("", persona="x"))
    check("正文只有空白 -> 也回落默认",
          "【硬性禁止" in prompts.render_system("   \n  ", persona="x"))
    try:
        prompts.render_system("写几条候选", persona="x")
        check("正文里没有 {n} 也不炸", True)
    except Exception as e:
        check("正文里没有 {n} 也不炸", False, "%s: %s" % (type(e).__name__, e))
    try:
        ok = '{"intent": 1}' in prompts.render_system('{"intent": 1}', persona="人设")
        check("正文里有 JSON 花括号也不炸（所以实现不能用 format）", ok)
    except Exception as e:
        check("正文里有 JSON 花括号也不炸", False, "%s: %s" % (type(e).__name__, e))
    check("人设为空 -> 兜底角色说明",
          "你是一个聊天助手。" in prompts.render_system("", persona=""))

    print("\n== 7. 给用户看的说明必须和实现对得上 ==")
    check("说明里说了这一框只放规则", "只放" in prompts.PLACEHOLDER_HELP)
    # ⚠️ 2026-10-13 换了措辞（用户要求：说明要面向用户，别说"框里/框外"这种界面内部的话）：
    #    原来是「不在框里」，现在是「会自动拼在它前面，不用写在这里」。
    check("说明里说了其余部分不用自己写",
          ("不用写在这里" in prompts.PLACEHOLDER_HELP)
          or ("不在框里" in prompts.PLACEHOLDER_HELP))
    check("说明里没有 markdown 标记（会原样漏到界面上）",
          "**" not in prompts.PLACEHOLDER_HELP
          and "`" not in prompts.PLACEHOLDER_HELP)
    check("说明里**不再**提占位符（框里根本没有）",
          "占位符：" not in prompts.PLACEHOLDER_HELP)

    print("\n== 8. 设置界面那一项也接上了 ==")
    try:
        from wechat_assistant.settings import SPEC
        item = [s for s in SPEC if s.key == "ai.system_prompt"]
        check("SPEC 里有 ai.system_prompt", len(item) == 1, str(len(item)))
        if item:
            s = item[0]
            check("kind 是 multiline（多行框）", s.kind == "multiline", s.kind)
            check("默认值就是那两段规则", s.default == prompts.DEFAULT_SYSTEM_TEMPLATE)
            check("label / help 里没有 markdown 标记",
                  "**" not in s.label + s.help and "`" not in s.label + s.help)
            check("help 里说了这一框装什么", "只放" in s.help)
    except Exception as e:
        check("能导入 settings.SPEC", False, "%s: %s" % (type(e).__name__, e))

    print("\n" + "=" * 60)
    print("通过 %d，失败 %d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
