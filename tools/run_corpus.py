# -*- coding: utf-8 -*-
"""OCR 读屏链路的**语料验收台**（离线跑，只读 PNG）。

为什么必须有它
==============
这条链路的唯一裁判是「真机截图 + 期望输出」，而

  · `out/` 下的截图被 `.gitignore` 排除 —— 清一次就**永久没了**；
  · 旧版后端有**已知损失**（图片上的字变假消息、长文本被切碎、
    `_top_cutoff_image` 会整片吃掉真消息），所以"它现在读出来的"
    **不等于**"正确答案"。

于是「改好了还是改坏了」不能靠肉眼看微信，必须有一份**跟着仓库走**的
语料 + 人工核对过的期望值。原地重写 `ocr_reader.py` 期间它是唯一的安全网。

三个文件分工
============
    cases.json    语料清单 + 每张的 scale（缩放）—— 机器可再生成
    golden.json   **人工核对过**的期望（只覆盖代表性用例）
    baseline.json 旧版后端的输出（动代码**之前**跑一次）

用法
====
    python tools\\run_corpus.py --calibrate           # 逐张标定 DPI 缩放（只打印）
    python tools\\run_corpus.py --calibrate --write    # 把标定结果固化进 cases.json
    python tools\\run_corpus.py --write-baseline      # 存当前后端输出为基线
    python tools\\run_corpus.py                       # 跑全部用例，和 golden 对比（验收）
    python tools\\run_corpus.py --trace --only <id>    # 接出产品日志排查某一张

⚠️ `scale` 是「位图像素 / 逻辑像素」。结果对它**极其敏感** ——
   `chat_top` / `input_fallback` / `chat_left` 全都要乘它，算小了会把第一条
   消息整条切掉。
⚠️ 每条用例必须**新建** reader —— 几何、标题缓存、日志节流标记都是实例状态，
   复用同一个实例会让上一条用例污染下一条（`_left_accepted` 尤其致命：
   标题会跟着上一条图的左边界走）。
⚠️ 纯只读：只读 PNG，绝不碰微信窗口。
"""

import argparse
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image                              # noqa: E402

from wechat_assistant import ocr_reader as R       # noqa: E402
from wechat_assistant.config import load_config    # noqa: E402

CORPUS_DIR = os.path.join(ROOT, "tests", "corpus")
CASES_PATH = os.path.join(CORPUS_DIR, "cases.json")
GOLDEN_PATH = os.path.join(CORPUS_DIR, "golden.json")
BASELINE_PATH = os.path.join(CORPUS_DIR, "baseline.json")

#: 标定用的缩放档位。1.0 / 1.25 / 1.5 / 1.75 / 2.0 是 Windows 常见的
#: 100%/125%/150%/175%/200%，2.5 / 3.0 给"2 倍图当作 1 倍图量"兜底。
SCALE_GRID = (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)


# ------------------------------------------------------------------ 读写

def _load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_cases():
    return _load_json(CASES_PATH, {"cases": []})


def load_golden():
    """人工核对的期望。**允许缺**（还没标的用例只做回归）。"""
    return (_load_json(GOLDEN_PATH, {}) or {}).get("cases", {})


def save_cases(data):
    with open(CASES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def case_path(case) -> str:
    return os.path.join(CORPUS_DIR, case["file"])


# ------------------------------------------------------------------ 跑一例

def make_reader(img, scale, cfg, logger=None):
    """构造一个**只为这张图服务**的 reader（理由见文件头 ⚠️）。"""
    rd = R.OcrReader(cfg, logger=logger)
    # ⚠️ 只替换"取图"和"缩放"两个外部依赖，其余**全走产品代码** ——
    #    这样验收台量到的就是产品行为，而不是这里另写的一份逻辑。
    rd.grab = lambda _hwnd: img
    rd._scale = lambda: float(scale)
    return rd


def luma(img) -> float:
    """整图平均亮度 —— 把语料自动分成深色/浅色两组（别手写主题标注，会漂）。"""
    px = list(img.convert("L").resize((48, 48)).getdata())
    return sum(px) / float(len(px))


def summarize(rd, snap, img) -> dict:
    """把一屏结果压成**可 JSON 化、可比较**的摘要。"""
    out = {
        "geom_bad": bool(getattr(rd, "_geom_bad", False)),
        "left_accepted": getattr(rd, "_left_accepted", None),
        "luma": round(luma(img), 1),
        "title": None,
        "is_group": False,
        "messages": [],
        "markers": [],
    }
    if snap is None:
        return out
    out["title"] = snap.title
    out["is_group"] = bool(getattr(snap, "is_group", False))
    for m in snap.messages:
        r = m.rect or (0, 0, 0, 0)
        out["messages"].append({
            "text": m.text,
            "is_self": m.is_self,
            # ⚠️ `kind` 由 `MessageItem.cls` 推出来（UIA 靠控件类名；OCR 后端
            #    只能硬编码成 text）—— 新架构的"气泡分类"就是要替换掉它。
            "kind": getattr(m, "kind", None),
            "sender": m.sender_name,
            "y": int(r[1]),
            "x1": int(r[0]),
            "x2": int(r[2]),
        })
    out["markers"] = [t.text for t in snap.markers]
    return out


def run_case(case, cfg, scale=None, logger=None) -> dict:
    path = case_path(case)
    img = Image.open(path).convert("RGB")
    sc = float(scale if scale is not None else case.get("scale") or 1.0)
    rd = make_reader(img, sc, cfg, logger=logger)
    snap = rd.read_chat(0)
    s = summarize(rd, snap, img)
    s["scale"] = sc
    s["window"] = [img.width, img.height]
    return s


# ------------------------------------------------------------------ 标定

def do_calibrate(cfg, data, cases, write: bool) -> int:
    """逐张在 SCALE_GRID 上跑一遍，看**哪个缩放真的读得出东西**。

    ⚠️ 不要只看"消息条数最多"就选 —— 几何错的时候 OCR 照样会"编"出块
       （那是假消息，而旧版恰恰有把真消息整片丢掉的 bug，两个方向都不干净）。
       所以脚本给的是**建议**，最终值要人过一眼（尤其新语料）。
    """
    changed = 0
    for case in cases:
        print("\n=== %s  (%s)" % (case["id"], case["file"]))
        rows = []
        for sc in SCALE_GRID:
            s = run_case(case, cfg, scale=sc)
            rows.append(s)
            print("   scale=%-5s 消息=%-2d 分隔=%-2d 标题=%-16r 左边界=%-5s 几何坏=%s"
                  % (sc, len(s["messages"]), len(s["markers"]),
                     (s["title"] or "")[:16], s["left_accepted"], s["geom_bad"]))
        ok = [s for s in rows if not s["geom_bad"] and (s["title"] or "").strip()]
        pool = ok or rows
        # ⚠️ 并列时取**更小的那个 scale**：大 scale 是"把图放大"的解释，
        #    而 scale 猜大了一律会把几何算歪（chat_top 乘大了会切掉第一条消息）。
        best = sorted(pool, key=lambda s: (-len(s["messages"]), s["scale"]))[0]
        print("   -> 建议 scale=%s（消息 %d 条）"
              % (best["scale"], len(best["messages"])))
        if write and case.get("scale") != best["scale"]:
            case["scale"] = best["scale"]
            changed += 1
    if write:
        save_cases(data)
        print("\n已写回 cases.json（%d 条 scale 有变化）" % changed)
    return 0


# ------------------------------------------------------------------ 基线

def do_write_baseline(cfg, data, cases) -> int:
    """把**当前后端**的输出存下来。

    ⚠️ 必须在动代码**之前**跑 —— 这是"有没有回归"的唯一对照组，
       也是"准确率是否超过旧版"的分母。
    """
    out = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "旧版（原地重写前）后端在各语料上的输出。golden 是人工核对的"
                "正确答案，baseline 是旧版的答案，两者之差就是旧版的误差。",
        "cases": {},
    }
    for case in cases:
        s = run_case(case, cfg)
        out["cases"][case["id"]] = s
        print("%-30s 消息 %-2d 分隔 %-2d 标题 %r"
              % (case["id"], len(s["messages"]), len(s["markers"]), s["title"]))
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("\n基线已写入 %s" % os.path.relpath(BASELINE_PATH, ROOT))
    return 0


# ------------------------------------------------------------------ 验收

def _norm(t) -> str:
    """比较文本前先归一化空白（OCR 的空格/换行不值得当成差异）。"""
    return " ".join((t or "").split())


def match_messages(expected, got):
    """期望消息 vs 实际消息：一对一贪心匹配。

    每条期望的匹配规则：
      · 给了 `kind`（如 image）-> 只要求**类型**命中，文本不参与；
      · `match: "contains"`   -> 实际文本**包含**期望子串（长文本/卡片用，
        OCR 一定有错字，全等会永远失败）；
      · 否则 -> 归一化文本**全等**。
    `is_self` 给了就一起校验（三态 True/False/None，None = 不校验）。

    返回 `(命中, 漏掉, 多余, 身份不符)`。
    """
    used = [False] * len(got)
    hit, miss, side_bad = 0, [], []
    for e in expected:
        want = _norm(e.get("text"))
        mode = e.get("match", "exact")
        ekind = e.get("kind")
        want_side = e.get("is_self")
        found = None
        for i, g in enumerate(got):
            if used[i]:
                continue
            gtext, gkind = _norm(g.get("text")), g.get("kind")
            if ekind:
                ok = (gkind == ekind)
            elif mode == "contains":
                ok = bool(want) and want in gtext
            else:
                ok = bool(want) and gtext == want
            if not ok:
                continue
            if want_side is not None and g.get("is_self") != want_side:
                # 文本对了、身份错了 —— 单独记，别和"漏了"混在一起
                if found is None:
                    found = ("side", i)
                continue
            found = ("ok", i)
            break
        label = want or ("<%s>" % ekind)
        if found is None:
            miss.append(label)
        elif found[0] == "side":
            side_bad.append(label)
        else:
            used[found[1]] = True
            hit += 1
    extra = [_norm(g.get("text")) or ("<%s>" % g.get("kind"))
             for i, g in enumerate(got) if not used[i]]
    return hit, miss, extra, side_bad


def do_sessions(cfg, cases) -> int:
    """读每张语料的**会话列表**（`read_sessions`）并打印，人工核对。

    ⚠️ 为什么单独一条路：会话列表和聊天正文读的是**屏幕的不同区域**，
       `golden.json` 那套（title / messages / markers）覆盖不到它。
       所以先"看读出来像不像"，攒够用例再考虑给它也做 golden。
    """
    for case in cases:
        path = case_path(case)
        img = Image.open(path).convert("RGB")
        sc = float(case.get("scale") or 1.0)
        rd = make_reader(img, sc, cfg)
        items = rd.read_sessions(0)
        print("\n=== %s  (%dx%d)  会话 %d 项"
              % (case["id"], img.width, img.height, len(items)))
        for it in items:
            print("   %-24s | %-26s | %s"
                  % (it.raw_title[:24], it.preview[:26], it.time_text))
    return 0


def do_verify(cfg, cases, golden) -> int:
    print("%-30s %-4s %-4s %-12s %-7s %-7s %s"
          % ("用例", "标题", "群", "命中/期望/多", "召回", "精确", "分隔"))
    print("-" * 92)
    tot_hit = tot_exp = tot_got = 0
    failed = []
    for case in cases:
        s = run_case(case, cfg)
        got = s["messages"]
        exp = golden.get(case["id"])
        if exp is None:
            print("%-30s %-4s %-4s %-12s %-7s %-7s %s"
                  % (case["id"], "?" if not s["title"] else "有", "-",
                     "%d（未标 golden）" % len(got), "-", "-", "-"))
            continue

        title_ok = (_norm(exp.get("title")) in _norm(s["title"])
                    if exp.get("title") else True)
        group_ok = (bool(s["is_group"]) == bool(exp["is_group"])
                    if "is_group" in exp else None)
        mk_exp = exp.get("markers")
        mk_ok = (set(mk_exp) == set(s["markers"])) if mk_exp is not None else None

        hit, miss, extra, side_bad = match_messages(exp.get("messages") or [], got)
        n_exp, n_got = len(exp.get("messages") or []), len(got)
        tot_hit += hit
        tot_exp += n_exp
        tot_got += n_got
        recall = hit / n_exp if n_exp else 1.0
        prec = hit / n_got if n_got else 0.0
        print("%-30s %-4s %-4s %-12s %-7.0f%% %-7.0f%% %s"
              % (case["id"], "OK" if title_ok else "错",
                 "-" if group_ok is None else ("OK" if group_ok else "错"),
                 "%d/%d/%d" % (hit, n_exp, len(extra)),
                 recall * 100, prec * 100,
                 "-" if mk_ok is None else ("OK" if mk_ok else "错")))
        if miss or extra or side_bad or not title_ok or group_ok is False or mk_ok is False:
            failed.append(case["id"])
            for t in miss:
                print("      漏: %r" % t[:70])
            for t in extra:
                print("      多: %r" % t[:70])
            for t in side_bad:
                print("      身份错（文本对了、我/对方判反）: %r" % t[:70])
            if not title_ok:
                print("      标题: 期望含 %r，实际 %r" % (exp.get("title"), s["title"]))
            if mk_ok is False:
                print("      分隔: 期望 %s，实际 %s" % (mk_exp, s["markers"]))
    print("-" * 92)
    if tot_exp:
        print("合计：召回 %.1f%%（%d/%d）  精确 %.1f%%（%d/%d）"
              % (tot_hit / tot_exp * 100, tot_hit, tot_exp,
                 tot_hit / tot_got * 100 if tot_got else 0, tot_hit, tot_got))
    print("有差异的用例：%s" % (", ".join(failed) if failed else "无"))
    return 0


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", action="store_true", help="逐张标定 DPI 缩放")
    ap.add_argument("--write", action="store_true", help="配合 --calibrate 写回 cases.json")
    ap.add_argument("--write-baseline", action="store_true", help="存当前后端输出为基线")
    ap.add_argument("--only", default="", help="只跑 id 含这个子串的用例")
    ap.add_argument("--trace", action="store_true", help="把产品日志接出来（排查用）")
    ap.add_argument("--sessions", action="store_true",
                    help="改读**会话列表**（read_sessions）而不是聊天正文")
    args = ap.parse_args()

    # ⚠️ 语料**不进版本库**（那是真机截图，里面有聊天内容，见 `tests/corpus/README.md`）。
    #    这里先说清楚是什么情况，否则会打印成「没有匹配的用例（--only ''）」，
    #    看起来像是筛选写错了。
    if not os.path.exists(CASES_PATH):
        print("没有语料：%s 不存在。" % os.path.relpath(CASES_PATH, ROOT))
        print("语料不进版本库。自己跑验收：照 tests/corpus/README.md 放截图并登记 cases.json。")
        return 1

    cfg = load_config()
    data = load_cases()
    cases = [c for c in data["cases"] if args.only in c["id"]]
    if not cases:
        print("没有匹配的用例（--only %r）" % args.only)
        return 1

    if args.trace:
        logger = lambda m: print("   [log] %s" % m)   # noqa: E731
        for c in cases:
            s = run_case(c, cfg, logger=logger)
            print("%s: 消息 %d 标题 %r" % (c["id"], len(s["messages"]), s["title"]))
        return 0

    if args.sessions:
        return do_sessions(cfg, cases)
    if args.calibrate:
        return do_calibrate(cfg, data, cases, args.write)
    if args.write_baseline:
        return do_write_baseline(cfg, data, cases)
    return do_verify(cfg, cases, load_golden())


if __name__ == "__main__":
    sys.exit(main())
