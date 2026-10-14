# -*- coding: utf-8 -*-
"""
UIA 深度验证：FindAll(Descendants) + LegacyIAccessible(MSAA) 双重确认。

背景：
    probe_uia.py 已经证明微信 4.0 的 UIA 树在「会话列表 / 消息区」处断掉
    （ChatMessagePage 和 session_list 下 0 个子节点，全窗口 Text=0 ListItem=0）。
    但 uiautomation 的 GetChildren() 走的是 RawViewWalker 的「逐个遍历」路径，
    为排除「provider 实现了 FindAll 却没实现 walker」这种可能，这里改用
    IUIAutomation::FindAll(TreeScope_Descendants, TrueCondition) 做一次性全量扫描，
    并额外尝试 LegacyIAccessiblePattern（MSAA 桥）看是否还有隐藏文本。

仍然是纯只读：只调用查询接口。
"""

import datetime as dt
import os
import sys
import threading

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import win32gui
import win32process

TARGET_PROC = "weixin.exe"
TIMEOUT = 40.0


def _build_type_names():
    """把 uiautomation.ControlType 里的数值反向映射成可读名字。"""
    try:
        from uiautomation import uiautomation as core

        m = {}
        for k, v in vars(core.ControlType).items():
            if not k.startswith("_") and isinstance(v, int):
                m.setdefault(v, k)
        return m
    except Exception:
        return {}


_TYPE_NAMES = _build_type_names()


def find_wechat_hwnd():
    found = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            cls = win32gui.GetClassName(hwnd)
            if "QWindowIcon" not in cls:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            import win32api
            import win32con

            h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            try:
                path = win32process.GetModuleFileNameEx(h, 0)
            finally:
                win32api.CloseHandle(h)
            if os.path.basename(path).lower() == TARGET_PROC:
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return found


def work(hwnd, out):
    import comtypes
    comtypes.CoInitialize()

    import uiautomation as auto
    from uiautomation import uiautomation as core

    with auto.UIAutomationInitializerInThread(debug=False):
        uia = core._AutomationClient.instance().IUIAutomation
        elem = auto.ControlFromHandle(hwnd)
        if elem is None:
            out["error"] = "ControlFromHandle 返回 None"
            return
        el = elem.Element

        # ---- 1) FindAll(Descendants, TrueCondition) —— Raw 视图全量扫描
        true_cond = uia.CreateTrueCondition()
        arr = el.FindAll(4, true_cond)  # TreeScope_Descendants = 4
        n = arr.Length
        out["findall_total"] = n
        type_names = {}
        named = []
        for i in range(n):
            e = arr.GetElement(i)
            ctype = e.CurrentControlType
            tn = _TYPE_NAMES.get(ctype, "Type%d" % ctype)
            type_names[tn] = type_names.get(tn, 0) + 1
            nm = e.CurrentName or ""
            if nm.strip():
                named.append((tn, nm, e.CurrentClassName or "", e.CurrentAutomationId or ""))
        out["findall_types"] = type_names
        out["findall_named"] = named

        # ---- 2) 只找 Text 控件
        try:
            cv = uia.CreatePropertyCondition(30005, 50020)  # UIA_ControlTypePropertyId=30005, Text=50020
            arr2 = el.FindAll(4, cv)
            texts = []
            for i in range(arr2.Length):
                e = arr2.GetElement(i)
                texts.append(e.CurrentName or "")
            out["text_count"] = arr2.Length
            out["texts"] = texts
        except Exception as ex:
            out["text_error"] = "%s: %s" % (type(ex).__name__, ex)

        # ---- 3) 消息区元素的 LegacyIAccessible(MSAA) 名称
        try:
            def walk_find(ctrl, cls_name, depth=0):
                if depth > 20:
                    return None
                try:
                    if ctrl.ClassName == cls_name:
                        return ctrl
                    for c in ctrl.GetChildren():
                        r = walk_find(c, cls_name, depth + 1)
                        if r:
                            return r
                except Exception:
                    pass
                return None

            for target_cls in ("mmui::ChatMessagePage", "mmui::ChatSessionList", "mmui::XTableView"):
                c = walk_find(elem, target_cls)
                if not c:
                    out.setdefault("msaa", {})[target_cls] = "未找到"
                    continue
                try:
                    pat = c.GetPattern(core.PatternId.LegacyIAccessiblePattern)
                    if pat is None:
                        out.setdefault("msaa", {})[target_cls] = "不支持 LegacyIAccessiblePattern"
                    else:
                        out.setdefault("msaa", {})[target_cls] = {
                            "name": pat.Name,
                            "value": pat.Value,
                            "childId": pat.ChildId,
                            "role": pat.Role,
                            "state": pat.State,
                            "description": pat.Description,
                        }
                except Exception as ex:
                    out.setdefault("msaa", {})[target_cls] = "读取失败: %s" % ex
        except Exception as ex:
            out["msaa_error"] = str(ex)


def main():
    hwnds = find_wechat_hwnd()
    buf = []
    def rec(s=""):
        print(s, flush=True)
        buf.append(s)

    rec("=" * 78)
    rec("UIA 深度验证 (FindAll Descendants + MSAA)")
    rec("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    rec("微信窗口 hwnd: %s" % [hex(h) for h in hwnds])
    rec("=" * 78)

    for h in hwnds:
        out = {}
        t = threading.Thread(target=_safe_work, args=(h, out), daemon=True)
        t.start()
        t.join(TIMEOUT)
        if t.is_alive():
            rec("hwnd=%s 超时 %.0fs" % (hex(h), TIMEOUT))
            continue

        if out.get("error"):
            rec("hwnd=%s 错误: %s" % (hex(h), out["error"]))
            continue

        rec("")
        rec("hwnd=%s" % hex(h))
        rec("FindAll(Descendants) 元素总数: %s" % out.get("findall_total"))
        rec("控件类型分布:")
        tn = out.get("findall_types", {})
        for k in sorted(tn, key=lambda x: -tn[x]):
            rec("   %-24s %d" % (k, tn[k]))
        rec("")
        rec("非空 Name 的元素 (%d 个):" % len(out.get("findall_named", [])))
        for ctype, nm, cls, aid in out.get("findall_named", []):
            rec("   [%s] name=%r class=%r id=%r" % (ctype, nm[:120], cls, aid))
        rec("")
        rec("Text 控件数量: %s" % out.get("text_count", out.get("text_error")))
        for tx in (out.get("texts") or [])[:50]:
            rec("   text=%r" % tx[:200])
        rec("")
        rec("MSAA (LegacyIAccessible):")
        for k, v in (out.get("msaa") or {}).items():
            rec("   %s -> %s" % (k, v))

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "uia_deep_%s.txt" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(buf))
    print("\n[已保存] %s" % path)


def _safe_work(hwnd, out):
    try:
        work(hwnd, out)
    except Exception as e:
        import traceback

        out["error"] = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-800:])


if __name__ == "__main__":
    main()
