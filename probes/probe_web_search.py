"""探针：确认服务商"模型自己决定要不要搜"的联网工具长什么样（2026-11-09）。

⚠️ 只作取证用：会**真花钱**、也真把内容发给服务商。产品代码不依赖它。

已验证的结论（2026-11-09，DeepSeek `deepseek-flash`，Chat Completions）：

    请求：
        "tools": [{"type": "deepseek_web_search", "name": "deepseek_web_search"}]
        "thinking": {"type": "disabled"}        # 否则变"深度思考搜索"（最多 20 次工具）

    返回（非流式）：
        message.content        = 普通字符串（**我们现有的解析一行都不用改**）
        message.annotations    = [{"type": "url_citation", "url_citation": {...}}]
        message.server_tool_calls = 服务端工具调用记录
        usage.server_tool_use.tool_search = 联网次数（实测 2 次）

    实测：非思考 + 一次提问 → 3.4 秒，prompt_tokens 2693（搜索把结果拼进了上下文，
          纯文本只有 600 左右），completion 51。

跑法：`python probes\\probe_web_search.py [问题]`
"""

import json
import sys
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("\\", 2)[0] or ".")

from wechat_assistant.config import load_config

QUESTION = (sys.argv[1] if len(sys.argv) > 1
            else "美元兑人民币今天的汇率大概是多少？")


def main() -> int:
    cfg = load_config()
    base = (cfg.get("ai.base_url") or "").rstrip("/")
    key = cfg.get("ai.api_key") or ""
    model = cfg.get("ai.model") or "deepseek-flash"
    if not base or not key:
        print("还没配置接口地址 / API Key（去设置里填）")
        return 1
    print("接口 %s · 模型 %s" % (base, model))

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": QUESTION}],
        "tools": [{"type": "deepseek_web_search", "name": "deepseek_web_search"}],
        "thinking": {"type": "disabled"},
        "stream": False,
        "max_tokens": 300,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": "Bearer " + key})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:600]
        except Exception:
            pass
        print("失败: %s %s %s" % (type(e).__name__, getattr(e, "code", ""), body))
        return 1

    el = time.time() - t0
    usage = data.get("usage") or {}
    stu = usage.get("server_tool_use") or {}
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    print("耗时 %.1fs · 入 %s / 出 %s · 联网 %s 次"
          % (el, usage.get("prompt_tokens"), usage.get("completion_tokens"),
             stu.get("tool_search", 0)))
    print("message 的键:", sorted(msg.keys()))
    print("回答:", str(msg.get("content"))[:300].replace("\n", " "))
    ann = msg.get("annotations") or []
    for a in ann[:3]:
        cite = (a or {}).get("url_citation") or {}
        print("引用:", cite.get("title"), cite.get("url"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
