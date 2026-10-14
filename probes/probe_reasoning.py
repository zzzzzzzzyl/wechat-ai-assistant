# -*- coding: utf-8 -*-
"""只读探测：模型的「思考过程」有没有返回？我们现在有没有存？

起因
----
用户问：「这里我问了多次，是有思考记录的吗？」

背景：实测一次生成「入 821 + 出 601 token」，可三条回复加起来才 30 来个字
（约 50 token）。**多出来的 550 token 去哪了？**

可能性：模型带推理（reasoning），思考过程也计入 `completion_tokens`。
而如果它返回在 `reasoning_content` 之类字段里，我们现在**没存也没显示** ——
`AIClient.chat()` 只取了 `message.content`，其余字段直接丢掉了。

本脚本真调一次 API，把响应的完整结构摊开看清楚。
⚠️ 会真实调用一次 API（约 ¥0.002 或更多）。不写库、不改任何状态。
用法： python probes/probe_reasoning.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_assistant.ai import AIClient  # noqa: E402
from wechat_assistant.config import load_config  # noqa: E402


def main():
    cfg = load_config()
    ai = AIClient(cfg)

    print("模型: %s" % ai.model)
    print("max_tokens: %d   temperature: %.1f" % (ai.max_tokens, ai.temperature))
    print()

    # 用一个简单、必定触发「判断」的请求，跟真实场景接近
    system = ("你是聊天助手。先判断对方那句话的真实意思，再写 3 条回复。\n"
              "只输出 JSON：{\"intent\": \"...\", \"replies\": [\"..\",\"..\",\"..\"]}")
    user = ("对方: 你昨天为什么不回我消息\n"
            "对方: 算了 你爱怎么想怎么想吧\n"
            "请生成回复。")

    payload = {
        "model": ai.model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": ai.temperature,
        "max_tokens": ai.max_tokens,
        "stream": False,
    }

    print("=" * 74)
    print("发起一次真实调用，抓原始响应")
    print("=" * 74)
    raw = ai._post("/chat/completions", payload)

    # ---------------------------------------------------------- 响应顶层结构
    print("\n顶层字段: %s" % sorted(raw.keys()))
    choice = (raw.get("choices") or [{}])[0]
    print("choice 字段: %s" % sorted(choice.keys()))
    msg = choice.get("message") or {}
    print("message 字段: %s" % sorted(msg.keys()))

    # ---------------------------------------------------------- 找推理字段
    print("\n" + "=" * 74)
    print("有没有「思考过程」字段？")
    print("=" * 74)
    REASON_KEYS = ("reasoning_content", "reasoning", "thinking",
                   "reasoning_details", "analysis")
    found = []
    for k in msg:
        if k in REASON_KEYS or "reason" in k.lower() or "think" in k.lower():
            found.append(k)
    if found:
        for k in found:
            v = msg.get(k)
            text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            print("  ✓ 发现字段 %r（%d 字）:" % (k, len(text or "")))
            print("    " + "\n    ".join((text or "").splitlines()[:25]))
    else:
        print("  ✗ message 里没有推理字段。")
        print("    现有字段: %s" % sorted(msg.keys()))

    # 顶层和 choice 里也找一遍（有的实现放在别处）
    for obj, tag in ((choice, "choice"), (raw, "顶层")):
        for k in obj:
            if any(w in k.lower() for w in ("reason", "think")):
                print("  ✓ %s 里有 %r" % (tag, k))

    # ---------------------------------------------------------- 内容与用量
    print("\n" + "=" * 74)
    print("实际返回内容与用量")
    print("=" * 74)
    content = msg.get("content") or ""
    print("  content 长度: %d 字" % len(content))
    print("  content 原文:")
    for ln in content.splitlines()[:20]:
        print("    " + ln)

    usage = raw.get("usage") or {}
    print("\n  usage: %s" % json.dumps(usage, ensure_ascii=False))

    # ---------------------------------------------------------- 结论
    print("\n" + "=" * 74)
    print("结论")
    print("=" * 74)
    p = usage.get("prompt_tokens", 0)
    c = usage.get("completion_tokens", 0)
    print("  输入 %d token，输出 %d token" % (p, c))
    print("  content 本身约 %d 字 -> 约 %d token" % (len(content), int(len(content) / 1.6)))
    gap = c - int(len(content) / 1.6)
    if found:
        print("  => **模型确实返回了思考过程**，且计入 completion_tokens。")
        print("     我们现在只取了 content，思考内容被丢掉了（没存、没显示）。")
    elif gap > 100:
        print("  => 没看到推理字段，但输出比 content 大了约 %d token。" % gap)
        print("     可能是模型内部推理（不返回）或 JSON 结构开销 —— 反正照常计费。")
    else:
        print("  => 输出与 content 规模相当，没有额外的隐性开销。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
