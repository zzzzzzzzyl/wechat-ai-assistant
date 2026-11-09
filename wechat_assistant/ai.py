# -*- coding: utf-8 -*-
"""AI 接口模块：组装上下文、调用大模型、解析出多条备选回复。


设计要点：
    - 只用标准库 urllib，不引入 requests 等额外依赖（本机 pip 装不进全局包，踩过坑）
    - 上下文用「结构化消息 + 滑动窗口」，不是把聊天记录整段拼成文本
    - 输出要求返回 JSON 数组，便于稳定拿到 N 条备选回复；解析带容错

红线：本模块只**生成文本**，不碰微信。
     复制、粘贴、发送全部由用户手动完成。
"""
from .textfilter import is_noise_text


import json
import random
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional

from . import prompts

# ---------------------------------------------------------------- 重试与错误码
#
# ⚠️ 2026-11-09 用户问「有没有失败重试、错误码处理」—— 原来两样都没有：
#    一次 `urlopen`，任何失败都直接变成 `AIError("HTTP %s: %s")` 甩到界面上。
#    429 限流 / 5xx / 连接被掐，全都当**最终失败**，用户只能自己再点一次
#    （点的时候 busy 可能还没清）。

#: 这些状态码**值得重试**（服务端瞬态 / 限流）。
#: ⚠️ 400/401/403/404/422 一律**不重试**：参数或鉴权错了，重试只是再错一遍。
RETRY_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: 第 1 / 2 次重试前等多久（秒）。3 次尝试之后就放弃。
RETRY_BACKOFF = (0.6, 1.8)

#: 一次调用（含重试）的**总时间预算**。超过就不再重试 ——
#: 否则 `ai.timeout=60` × 3 次 = 3 分钟，界面看起来像卡死。
RETRY_BUDGET_S = 90.0

#: 只有**这次尝试本身很快失败**（< 这个秒数）才重试。
#:
#: ⚠️ 反例：`timeout=60` 的超时说明链路很差，再等 60 秒 ×2 只会让界面像卡死。
#:    429 / 5xx 都是"服务端立刻回一个错误"，耗时才几百毫秒 → 值得重试。
RETRY_FAST_S = 15.0

#: 单次重试最多等多久（服务端给了 `Retry-After` 也不能把我们挂太久）。
RETRY_WAIT_MAX = 8.0

#: HTTP 状态码 → **给用户看的一句话**（≤20 字，不含内部细节）。
#: ⚠️ 界面只显示这句；原始 body 放在 `AIError.detail` 里**只进日志**
#:    （body 里可能有 Key 片段、超长 JSON、服务商内部 id）。
HTTP_HINTS = {
    400: "请求被拒（内容可能太长）",
    401: "API Key 无效或已过期",
    402: "余额不足或未开通",
    403: "没有权限（Key 或地区）",
    404: "接口地址或模型名不对",
    408: "请求超时",
    413: "请求太大（图太多或太长）",
    422: "参数不被接受（模型不支持看图？）",
    429: "请求太频繁，稍等再试",
    500: "服务端出错",
    502: "服务端网关错误",
    503: "服务暂时不可用",
    504: "服务端超时",
}


def explain_http_error(code, body: str = ""):
    """把 HTTP 状态码翻成 (给用户看的一句话, 原始细节)。

    ⚠️ `settings.test_api`（「测试连接」按钮）和生成路径**共用这一个函数** ——
        原来两套写法：按钮有中文分类，生成路径直接甩 `HTTP 500: {...}`。
    """
    try:
        c = int(code)
    except Exception:
        c = 0
    hint = HTTP_HINTS.get(c) or ("接口返回 %s" % (code,))
    return hint, (body or "")

# ⚠️ **不再有"默认接口地址 / 默认模型"这种兜底**（2026-10-13 用户要求：
#    「接口地址与模型默认为空」）。
#    原来这里写着 `DEFAULT_BASE_URL = "https://api.deepseek.com"` /
#    `DEFAULT_MODEL = "deepseek-flash"`，配置里留空时会**静默兜底**到它们 ——
#    后果是用户换了一家服务商、只填了 Key，请求还是打到 DeepSeek 上，
#    报错信息也说不清"其实是地址没填"。
#    现在的语义：**留空 = 没配置**，由 `missing()` 说清缺哪几项。
#    （DeepSeek 那两行只作为"例子"出现在设置的说明文字和 config.yaml 注释里。）

# ⚠️ 提示词的**文本**全在 `prompts.py`（唯一一份，`settings.py` 也要用它的默认值）。
#    这里只是转出来给老代码/文档一个引用点，**不要再在这里写第二份**。
ANTI_TEMPLATE_RULES = prompts.ANTI_TEMPLATE_RULES
FOCUS_RULES = prompts.FOCUS_RULES

# ⚠️ 「反 AI 腔」和「上下文里可能有无关内容」这两段规则的**原文**现在在
#    `prompts.DEFAULT_SYSTEM_TEMPLATE` 里（用户可改的那一整段模板中）。
#    这里不再保留副本 —— 留副本必然和模板里的那份漂。


def collect_style_samples(messages, limit: int = 12, max_len: int = 60,
                          exclude=None) -> List[str]:
    """从消息里挑出「我」最近发过的短消息，用来给模型当口吻样本。

    为什么要取样而不是只靠人设描述：
        「语气自然、简短」这种描述模型只能猜；给它几条你真实说过的话，
        它照着模仿的用词、句长、标点习惯，比任何形容词都准。

    只取短消息：长段落在聊天里本来就是少数，不代表日常口吻，还会挤占上下文。

    `exclude`：要跳过的文本集合 —— 传「AI 生成过的建议」进来，
        避免 AI 模仿自己的输出（自我污染，见 store.note_ai_suggestions）。
    """
    skip = exclude or ()
    out = []
    for m in reversed(messages or []):
        if getattr(m, "is_self", None) is not True:
            continue
        t = (getattr(m, "text", "") or "").strip()
        if not t or len(t) > max_len:
            continue
        if t.startswith("http") or "://" in t:
            continue
        # ⚠️ 2026-10-12：和 `store.own_style_texts` **同一道闸**（`textfilter`）——
        #    两条取样路（库 / 当前屏）必须用同一个判据，否则换个后端口吻样本就变脏。
        if is_noise_text(t):
            continue
        if t in skip:
            continue
        if t in out:          # 同一句反复说不算样本
            continue
        out.append(t)
        if len(out) >= limit:
            break
    return list(reversed(out))       # 恢复时间顺序


class AIError(RuntimeError):
    """AI 调用失败。

    属性（2026-11-09 加）：
        code      HTTP 状态码；网络层错误是 None
        detail    **原始**响应 body / 异常文本 —— **只进日志，不上界面**
        retriable 这类错误重试过也失败了吗（决定日志措辞，不影响界面）
    """

    def __init__(self, message, code=None, detail: str = "", retriable: bool = False):
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.retriable = retriable


class AIClient:
    def __init__(self, cfg, logger: Callable[[str], None] = None):
        # ⚠️ 留空就是**空**（不兜底，见文件开头那段说明）；`available` 要求三样齐全。
        self.base_url = (cfg.get("ai.base_url") or "").strip().rstrip("/")
        self.api_key = (cfg.get("ai.api_key") or "").strip()
        self.model = (cfg.get("ai.model") or "").strip()
        # ⚠️ 视觉模型：**留空 = 就用上面那个 `ai.model`**（2026-10-13 用户要求）。
        #    单独一项是为了“日常对话用便宜的、带图时才换贵的” ——
        #    有些服务商的视觉模型按图片分辨率另算钱。
        self.vision_model = (cfg.get("ai.vision_model") or "").strip() or self.model
        #: “要不要把整屏图带给模型”。
        #: ⚠️ 它属于**读取**而不是生成，所以 key 在 `ocr_read.*` 下
        #:    而不是 `ai.*`（2026-10-13 用户指出：“每次带整屏
        #:    应该不是在这里吧，是不是要加到读取方式？”）。
        #: 关着的时候**一行代码都不走图**（老行为）。
        self.vision = bool(cfg.get("ocr_read.attach_image", False))
        self.timeout = float(cfg.get("ai.timeout", 60))
        self.temperature = float(cfg.get("ai.temperature", 1.0))
        self.max_tokens = int(cfg.get("ai.max_tokens", 800))
        self.n_replies = int(cfg.get("ai.n_replies", 3))
        # 上下文取最近多少条消息。注意这是**消息条数**，不是对话轮数。
        # 之前这里写死用 render_history 的默认值 10，配置没接上 ——
        # 无论有多少历史都只用 10 条。同时兼容旧的 max_turns 键名。
        self.max_messages = int(cfg.get("runtime.max_messages")
                                or cfg.get("runtime.max_turns") or 20)
        # ⚠️ 长度闸。**只限条数是防不住"长内容"的**——实测用户把提示词全文
        # 粘进微信，一条 2000 字就把 user 从 57 撑到 2071，每次调用贵 3 倍，
        # 而条数上限对此完全无感。见 render_history 的说明。
        self.max_chars = int(cfg.get("runtime.max_chars", 4000) or 0)
        self.max_msg_chars = int(cfg.get("runtime.max_msg_chars", 500) or 0)
        self._log = logger or (lambda msg: None)
        # token 用量统计（用于核算成本）
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.total_usage = {"calls": 0, "prompt_tokens": 0,
                            "completion_tokens": 0, "total_tokens": 0}
        # 模型对「对方这句话真实意思」的判断（每次生成后更新）
        self.last_intent = ""
        # ⚠️ **system prompt 的模板**（用户在「设置 → AI」里改的那一整段）。
        #    在构造时读一次 —— 设置保存会**重建 AIClient**，所以改完立即生效。
        #    空串 = 用内置默认（见 `prompts.render_system`）。
        self.system_template = (cfg.get("ai.system_prompt") or "").strip()
        # 最近一次**实际提交**的提示词（悬浮窗可查看 —— 能看见到底发了什么）
        self.last_system = ""
        self.last_user = ""
        # 最近一次的「思考过程」（模型返回的 reasoning_content；会计费）
        self.last_reasoning = ""

    # ------------------------------------------------------------ 基础

    @property
    def missing(self) -> List[str]:
        """还差哪几项才能真的调 API（中文名，直接给界面/日志用）。

        ⚠️ 2026-10-13 起「能调」要求**三样齐全**：接口地址 + API Key + 模型。
            以前只看 api_key（地址和模型有硬编码兜底），
            现在留空就是没配置 —— 所以判据也得跟着严。
        """
        miss = []
        if not self.base_url:
            miss.append("接口地址")
        if not self.api_key:
            miss.append("API Key")
        if not self.model:
            miss.append("模型名")
        return miss

    @property
    def available(self) -> bool:
        return not self.missing

    def _retry_wait(self, err, attempt: int) -> float:
        """这次重试要等多久：优先听服务端 `Retry-After`，否则指数退避 + 抖动。"""
        wait = float(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
        try:
            if err is not None:
                ra = err.headers.get("Retry-After") if getattr(err, "headers", None) else None
                if ra:
                    wait = float(str(ra).strip())
        except Exception:
            pass
        wait = max(0.0, min(wait, RETRY_WAIT_MAX))
        return wait + random.uniform(0, 0.25)      # 抖动：避免多实例同时重试

    def _post(self, path: str, payload: dict) -> dict:
        """POST 一次；对**瞬态**失败自动重试（见 `RETRY_CODES` / `RETRY_BUDGET_S`）。

        ⚠️ 2026-11-09 **新增重试**：原来一次 `urlopen` 失败就直接抛，429/5xx
            只能说"请求失败"，用户得自己再点一次。
        ⚠️ 重试要讲规矩：只重试**幂等且瞬态**的（429/5xx/网络），
            400/401/403/404/422 重试一遍还是同样的错，立刻抛。
        ⚠️ 总预算 `RETRY_BUDGET_S`：`timeout=60` 时 3 次尝试能到 3 分钟，
            界面看起来像卡死 —— 超过预算就不再重试。
        """
        url = self.base_url + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        started = time.monotonic()
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json; charset=utf-8")
            req.add_header("Authorization", "Bearer " + self.api_key)
            req.add_header("User-Agent", "wechat-ai-assistant (local)")
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "ignore")[:400]
                except Exception:
                    pass
                hint, detail = explain_http_error(e.code, body)
                took = time.monotonic() - t0
                left = RETRY_BUDGET_S - (time.monotonic() - started)
                if (e.code in RETRY_CODES and attempt < len(RETRY_BACKOFF)
                        and took < RETRY_FAST_S and left > 1.0):
                    wait = self._retry_wait(e, attempt)
                    self._log("[AI] HTTP %s（%s），%.1fs 后重试（第 %d 次）"
                              % (e.code, hint, wait, attempt + 1))
                    time.sleep(wait)
                    attempt += 1
                    continue
                raise AIError(hint, code=e.code, detail=body,
                              retriable=e.code in RETRY_CODES) from e
            except Exception as e:
                # URLError（DNS/连接被拒/TLS）/ TimeoutError / socket 层错误
                retriable = isinstance(
                    e, (urllib.error.URLError, TimeoutError, OSError))
                took = time.monotonic() - t0
                left = RETRY_BUDGET_S - (time.monotonic() - started)
                if (retriable and attempt < len(RETRY_BACKOFF)
                        and took < RETRY_FAST_S and left > 1.0):
                    wait = self._retry_wait(None, attempt)
                    self._log("[AI] 网络错误（%s），%.1fs 后重试（第 %d 次）"
                              % (type(e).__name__, wait, attempt + 1))
                    time.sleep(wait)
                    attempt += 1
                    continue
                raise AIError(
                    ("网络不通（已重试 %d 次）" % attempt) if attempt else "网络不通",
                    detail="%s: %s" % (type(e).__name__, e),
                    retriable=retriable) from e

    def _get(self, path: str) -> dict:
        url = self.base_url + path
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            raise AIError("%s: %s" % (type(e).__name__, e)) from e

    def list_models(self) -> List[str]:
        data = self._get("/models")
        return [m.get("id") for m in (data.get("data") or []) if m.get("id")]

    def chat(self, system: str, user: str, temperature: Optional[float] = None,
             images=None) -> str:
        """最基础的一问一答。`images` 是一串 base64（**不带 data: 前缀**）。

        ⚠️ 带图时 `content` 要换成**数组**（OpenAI 兼容协议的多模态格式），
           而且**模型换成 `vision_model`** —— 见 `__init__` 里的说明。
        ⚠️ 不带图时行为**完全不变**（model / content 都跟以前一模一样）。
        """
        if not self.available:
            # ⚠️ 说清**缺哪几项**（以前只说 api_key，现在地址/模型也可能是空的）
            raise AIError("还没配置：" + "、".join(self.missing))
        content = user
        if images:
            content = [{"type": "text", "text": user}]
            for i, b64 in enumerate(images, 1):
                # ⚠️ 每张图前先说一句它是第几张：
                #    否则一屏多图时模型对不上“哪张图对应哪条消息”
                #    —— 上文里只有 N 个 `[图片]` 占位符，没有别的线索。
                content.append({
                    "type": "text",
                    "text": "【第 %d 张图（对应上面第 %d 个 [图片]）】"
                            % (i, i)})
                content.append({"type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64," + b64}})
        payload = {
            "model": self.vision_model if images else self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        data = self._post("/chat/completions", payload)
        # 记录 token 用量，便于核算成本
        usage = data.get("usage") or {}
        _details = usage.get("completion_tokens_details") or {}
        self.last_usage = {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            # 其中有多少是「思考」花的 —— 实测能占到输出的八成
            "reasoning_tokens": _details.get("reasoning_tokens", 0),
            # ⚠️ 2026-11-09：**前缀缓存**是否命中（DeepSeek 这类服务商会在
            #    usage 里回这两个字段；不支持的厂商就是 0）。
            #    为什么值得记：输入占成本约 64%，而缓存命中价通常只有 1/10 ——
            #    没有这两个数，就永远不知道"把口吻样本挪到最后"到底赚了没有。
            "cache_hit_tokens": usage.get("prompt_cache_hit_tokens", 0) or 0,
            "cache_miss_tokens": usage.get("prompt_cache_miss_tokens", 0) or 0,
        }
        self.total_usage["calls"] += 1
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self.total_usage[k] += self.last_usage.get(k, 0)
        try:
            msg = data["choices"][0]["message"]
        except Exception as e:
            raise AIError("响应结构异常: %s" % json.dumps(data, ensure_ascii=False)[:300]) from e

        # 模型带推理：它会先「想」再答，思考过程放在 reasoning_content 里。
        # ⚠️ 这部分**照样计费**，实测能占到输出 token 的八成（540/680）。
        # 之前这里只取 content，思考内容被整个丢掉了 —— 既看不见，
        # 也没法复盘「它为什么这么判」。现在留下来给悬浮窗展示。
        self.last_reasoning = (msg.get("reasoning_content") or "").strip()
        return (msg.get("content") or "").strip()

    # ------------------------------------------------------------ 上下文组装

    @staticmethod
    def render_history(messages: List[dict], max_turns: int = 10,
                       max_chars: int = 0, max_msg_chars: int = 0) -> str:
        """把结构化消息渲染成对话文本。

        messages 里每项形如 {"who": "我" / "李四", "text": "..."}

        ⚠️ **两道闸：条数 + 长度。只限条数是不够的。**
        实测踩过：用户把提示词全文粘进微信（2000+ 字），那**一条**就把 user
        从 57 字撑到 2071 字，每次调用贵 3 倍（¥0.0010 -> ¥0.0024）。
        条数上限（max_messages=20）对这种情况**完全无感** —— 它还是"20 条以内"。

        max_msg_chars: 单条超过就截断。一条粘贴不该把整段上下文挤掉。
        max_chars:     渲染后总量超过就从**最旧**的开始丢。
        两者都为 0 表示不限制（保持旧行为）。
        """
        if not messages:
            return "（暂无历史）"
        recent = list(messages[-max_turns:]) if max_turns > 0 else list(messages)

        rows = []
        for m in recent:
            who = m.get("who") or "?"
            text = (m.get("text") or "").strip()
            if not text:
                continue
            if max_msg_chars > 0 and len(text) > max_msg_chars:
                text = text[:max_msg_chars] + "…（本条过长已截断）"
            rows.append((who, text))

        if max_chars > 0:
            total = sum(len(w) + len(t) + 2 for w, t in rows)
            dropped = 0
            while rows and total > max_chars:
                w, t = rows.pop(0)
                total -= len(w) + len(t) + 2
                dropped += 1
            if dropped and not rows:
                return "（历史太长，按长度上限全部略去）"

        lines = [("%s: %s" % (w, t)) if w else "（%s）" % t for w, t in rows]
        return "\n".join(lines) if lines else "（暂无历史）"

    def build_prompts(self, persona: str, history: List[dict], current: str,
                      n_replies: int, extra_context: str = "",
                      style_samples: Optional[List[str]] = None,
                      relationship: str = "",
                      user_intent: str = "",
                      max_turns: Optional[int] = None) -> (str, str):
        """组装 (system, user) 两个 prompt。

        persona       : 全局或会话级人设
        history       : 历史消息（结构化）
        current       : 当前待回复的消息
        extra_context : 知识库检索片段等附加信息（P2 用）
        style_samples : 「我」最近发过的短消息，用来模仿口吻（见 collect_style_samples）
        relationship  : 你们的关系（恋人/朋友/同事/家人/自定义），影响称呼和分寸
        user_intent   : 「我」自己想表达的意思（悬浮窗里填的）。给了它就按它写，
                        不再让模型猜立场 ——「我」的真实想法只有「我」知道，
                        模型从对话里推不出来；猜错时三条可能一条都用不上。

        ⚠️⚠️ **system 那段现在是"模板 + 占位符"**（2026-10-11 用户要求
            「提示词要能在设置里改」，选了整段可改）：

                「设置 → AI → 系统提示词」那个多行框 = `ai.system_prompt`
                里面的 `{persona}` / `{relationship}` / `{style}` / `{intent}`
                / `{n}` 由这里填。默认值见 `prompts.DEFAULT_SYSTEM_TEMPLATE`。

            渲染全在 `prompts.render_system()` 里（含"框被清空就回落默认"
            和"压掉多余空行"）。**这里只负责追加 `OUTPUT_FORMAT`**。

        ⚠️ **【输出格式】必须由代码追加、不进模板**：解析器依赖那个 JSON 结构，
            让用户能改它 = 给了一个"改完就静默生成失败"的开关。
        """
        system = prompts.render_system(
            self.system_template, persona=persona, relationship=relationship,
            style_samples=style_samples, user_intent=user_intent,
            n_replies=n_replies)
        # ⚠️ **输出格式现在由 `render_system` 在代码侧拼上**（`prompts.OUTPUT_SPEC`），
        #    用户那一框里只有他自己的规则 —— 所以这段**通常不会触发**。
        #    留着它是**防将来**：谁哪天把 `OUTPUT_SPEC` 从拼接里删掉，
        #    这里会补上并且**记一行日志**，而不是让解析器静默失败。
        #    （解析器 `ai.parse_replies` 依赖那个 JSON 结构，
        #      "改完就静默生成失败"是这个项目里最贵的一类 bug。）
        if not ('"intent"' in system and '"replies"' in system):
            self._log("提示词里没有输出格式，已由代码补上（否则解析会失败）")
            system = system + "\n\n" + prompts.OUTPUT_FORMAT

        parts = []
        if extra_context:
            parts.append("【参考资料】\n%s\n" % extra_context.strip())
        parts.append("【对话历史】\n%s"
                     % self.render_history(
                         history,
                         self.max_messages if max_turns is None else max_turns,
                         self.max_chars, self.max_msg_chars))
        # ⚠️⚠️ **这段的标题原来叫「需要回复的最新消息」，是名不副实的**
        #    （2026-10-11 用户实测报的困惑）：
        #
        #        `run_watch` 默认走 `collect_visible_block()` ——
        #        **当前屏幕上的全部对话**都是回复目标，不是"最后一条"。
        #        于是标题承诺"最新消息"、内容却给了四行不同人的话，
        #        用户和模型都会问"我到底要回哪句"。
        #
        #    ⚠️ 现在改成如实描述 + 把"哪条最新"说出来。**别再改回"最新消息"**，
        #       除非回复目标的取法也一起改成"只取最后一条"。
        _blk = (current or "").strip()
        if _blk:
            # ⚠️⚠️ 2026-10-17 **改标题**（用户反馈：明明有上下文，它只盯着最后一两条
            #    无关紧要的说）。旧标题是「当前屏幕上的对话 —— 要回复的就是这一段」，
            #    等于要求模型对**整屏每一行**都回应；而实测当前屏常常只有 2-5 条、
            #    一半是 `[图片]`、末尾还是我自己刚说的话（`probes/diag_context_focus.py`）。
            #    现在真正要回的那条由 `run_watch` 用 `>> ` 标出来
            #    （见 `context.render_current_block` / `pick_reply_anchor`），
            #    这里只解释标记的含义。
            has_anchor = any(l.startswith(">> ") for l in _blk.splitlines())
            head = "\n【当前屏幕上的对话】\n" + _blk + "\n\n"
            if has_anchor:
                head += ("（标着 >> 的那条是**这次要回应的**；它后面的是紧接着的补充，"
                         "前面几条是同一段对话的背景 —— 看懂整段再回，别只抓最后一句。）")
            else:
                head += ("（这一屏没有别人发的正文消息 —— 可以顺着我们自己刚才那句话往下说。）")
            # ⚠️ 群聊里认不出昵称的成员显示成「群友」—— **它们不一定是同一个人**。
            #    不说明的话模型会把 `群友: A` / `群友: B` 当成同一人的两句话，
            #    写出来的回复张冠李戴（用户实测就是这个场景：
            #    `对方: 好吃` 和 `对方: 这个叫做碱水包…` 其实是两个人）。
            if prompts.GROUP_UNKNOWN in _blk:
                head += ("\n（⚠️ 里面的「%s」表示**还没认出昵称的群成员** —— "
                         "它们**不一定是同一个人**，别把这段当成一对一的对话。）"
                         % prompts.GROUP_UNKNOWN)
            parts.append(head)
        return system, "\n".join(parts)

    # ------------------------------------------------------------ 生成回复

    def generate_replies(self, persona: str, history: List[dict], current: str,
                         n_replies: Optional[int] = None,
                         extra_context: str = "",
                         style_samples: Optional[List[str]] = None,
                         relationship: str = "",
                         user_intent: str = "",
                         max_turns: Optional[int] = None,
                         images=None) -> List[str]:
        """生成 N 条备选回复。

        user_intent 非空时按它写（同一个立场的不同说法）；为空时由模型自己判断
        「对方那句话什么意思」，并顺着那个判断写。
        """
        n = int(n_replies or self.n_replies)
        system, user = self.build_prompts(persona, history, current, n, extra_context,
                                          style_samples, relationship, user_intent,
                                          max_turns)
        # 存下来供悬浮窗展示：用户要能看见「到底发了什么给模型」
        self.last_system = system
        self.last_user = user
        # ⚠️ `images` 直接透给 `chat`：有图时它会把 content 换成数组、
        #    并把模型切到 `vision_model`（见那边的说明）。
        raw = self.chat(system, user, images=images)
        replies = parse_replies(raw, n)
        # 把模型对「对方真实意思」的判断也捞出来，展示给用户看 —— 判错了一眼能发现
        self.last_intent = parse_intent(raw)
        if not replies:
            # ⚠️ 解析不出来时把原始输出打出来，否则只能看到「生成 0 条」无从排查
            _think = (getattr(self, "last_reasoning", "") or "").strip()
            if not (raw or "").strip() and _think:
                # ⚠️⚠️ **正文空、但模型“想了很长一段”** ——
                #    2026-10-13 用户实测踩到：输出 0 字，而「看提示词」里
                #    能看到长长一段 —— 那是 reasoning_content，**不是正文**。
                #    原因：**思考也算在 max_tokens 里**，而默认值不够
                #    （项目里就记过“一次 680 token 里 540 是思考”）。
                #    → 直接告诉用户该调哪里，别让人对着“生成 0 条”猜。
                self._log("[AI] **正文是空的**，但模型输出了 %d 字思考"
                          "（思考也占输出额度，当前上限 %d）"
                          "—— 把「设置 → 高级 → 最长输出」调大"
                          % (len(_think), self.max_tokens))
            else:
                self._log("[AI] 解析不出回复；原始输出(%d 字)前 300 字: %r"
                          % (len(raw or ""), (raw or "")[:300]))
        else:
            self._log("[AI] 原始输出长度 %d，解析出 %d 条" % (len(raw or ""), len(replies)))
        return replies


def parse_intent(raw: str) -> str:
    """从模型输出里取出 intent —— 即它对「对方真实意思」的判断。

    展示这个判断的价值：判错的时候用户一眼就能发现，
    比默默生成一堆奇怪的回复要好排查得多。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    candidates = [text]
    if "```" in text:
        for seg in text.split("```"):
            s = seg.strip()
            if s.startswith("json"):
                s = s[4:].strip()
            if s.startswith("{"):
                candidates.append(s)
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        candidates.append(text[i:j + 1])

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict):
            for key in ("intent", "意图", "判断", "meaning", "reading"):
                v = obj.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return ""


def parse_replies(raw: str, want: int) -> List[str]:
    """从模型输出里解析回复列表，尽量容错。

    优先按 JSON 解析；失败时退化为按行/按序号切分 —— 模型偶尔会多说几句
    或忘记输出 JSON，这里不能让整条链路直接失败。
    """
    text = (raw or "").strip()
    if not text:
        return []

    # 1) 直接当 JSON 解析
    candidates = [text]
    # 2) 从 ```json ... ``` 代码块里抠出来
    if "```" in text:
        for seg in text.split("```"):
            s = seg.strip()
            if s.startswith("json"):
                s = s[4:].strip()
            if s.startswith("{") or s.startswith("["):
                candidates.append(s)
    # 3) 抠出第一个 { ... } 或 [ ... ]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i, j = text.find(open_ch), text.rfind(close_ch)
        if 0 <= i < j:
            candidates.append(text[i:j + 1])

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        arr = None
        if isinstance(obj, dict):
            for key in ("replies", "reply", "回复", "answers", "data", "result"):
                if isinstance(obj.get(key), list):
                    arr = obj[key]
                    break
            if arr is None:
                # 退一步：取第一个 list 类型的值
                for v in obj.values():
                    if isinstance(v, list):
                        arr = v
                        break
        elif isinstance(obj, list):
            arr = obj
        if arr:
            out = []
            for x in arr:
                if isinstance(x, str) and x.strip():
                    out.append(x.strip())
                elif isinstance(x, dict):
                    for k in ("text", "content", "reply", "回复"):
                        if isinstance(x.get(k), str) and x[k].strip():
                            out.append(x[k].strip())
                            break
            if out:
                return out[:want] if want > 0 else out

    # 4) 兜底：按行切，去掉序号和空行
    import re

    lines = []
    for ln in text.splitlines():
        s = re.sub(r"^\s*(?:[-*•]|\d+[\.、)）])\s*", "", ln.strip())
        if s and not s.startswith("```"):
            lines.append(s)
    return lines[:want] if want > 0 else lines
