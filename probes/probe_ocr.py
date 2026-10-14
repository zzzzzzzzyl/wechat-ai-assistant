# -*- coding: utf-8 -*-
"""
微信 4.0 聊天界面 OCR 探针（与 UIA 方案做对比）。

为什么要单独测 OCR：
    UIA 方案已验证可行，但有两处天生拿不到：
      1) 消息气泡的左右位置（UIA 只给整行 rect），无法区分「对方/自己」
      2) 图片消息内部的文字
    OCR 的输出是「文本 + 像素坐标」，正好补上第 1 点。

引擎选择顺序（自动探测，装哪个用哪个）：
    1. rapidocr_onnxruntime  —— 中文识别好，输出四点坐标框（推荐）
    2. winsdk (Windows.Media.Ocr) —— 系统自带，零模型下载，精度一般
    3. pytesseract           —— 需另装 tesseract.exe
都没装时只截图并保存，供人工查看。

只读性：只截取微信窗口所在屏幕区域，不改动微信任何状态。

用法：
    python probe_ocr.py                  # 截图 + OCR
    python probe_ocr.py --no-focus       # 不把微信提到前台
    python probe_ocr.py --scale 2        # 截图放大 2 倍再 OCR（小字号更准）
"""

import argparse
import ctypes
import datetime as dt
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 关键：必须在导入 PIL 之前声明 DPI 感知，
# 否则 100% 以上缩放时 GetWindowRect 与 ImageGrab 的坐标系不一致，截图会错位。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import win32api
import win32con
import win32gui
import win32process

TARGET_PROC = "weixin.exe"


def find_wechat_hwnds():
    found = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if "QWindowIcon" not in win32gui.GetClassName(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
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


def window_rect(hwnd):
    """优先用 DWM 扩展边框，避开 Windows 10+ 的不可见阴影边框。"""
    try:
        import ctypes.wintypes as wt

        rect = wt.RECT()
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)  # DWMWA_EXTENDED_FRAME_BOUNDS
        )
        if hr == 0:
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    return win32gui.GetWindowRect(hwnd)


# ------------------------------------------------------------------ OCR 引擎

def ocr_rapidocr(img_path):
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    result, elapse = engine(img_path)
    if not result:
        return [], "rapidocr: 未识别到文本"
    items = []
    for box, text, score in result:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append(
            {
                "text": text,
                "score": float(score),
                "x": int(min(xs)),
                "y": int(min(ys)),
                "x2": int(max(xs)),
                "y2": int(max(ys)),
            }
        )
    return items, None


def ocr_winsdk(img_path):
    import asyncio

    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    async def run():
        f = await StorageFile.get_file_from_path_async(os.path.abspath(img_path))
        stream = await f.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bmp = await decoder.get_software_bitmap_async()
        langs = OcrEngine.available_recognizer_languages
        lang = None
        for L in langs:
            if L.language_tag.lower().startswith("zh"):
                lang = L
                break
        if lang is None and langs:
            lang = langs[0]
        engine = OcrEngine.try_create_from_language(lang) if lang else None
        if engine is None:
            raise RuntimeError("没有可用的 OCR 语言包")
        res = await engine.recognize_async(bmp)
        items = []
        for line in res.lines:
            r = line.words[0].bounding_rect
            x1, y1 = int(r.x), int(r.y)
            x2 = int(r.x + r.width)
            y2 = int(r.y + r.height)
            for w in line.words[1:]:
                rr = w.bounding_rect
                x2 = max(x2, int(rr.x + rr.width))
                y2 = max(y2, int(rr.y + rr.height))
            items.append({"text": line.text, "score": 1.0, "x": x1, "y": y1, "x2": x2, "y2": y2})
        return items

    return asyncio.run(run()), None


def ocr_tesseract(img_path):
    import pytesseract
    from PIL import Image

    img = Image.open(img_path)
    data = pytesseract.image_to_data(img, lang="chi_sim+eng", output_type=pytesseract.Output.DICT)
    items = []
    for i, t in enumerate(data["text"]):
        if t.strip():
            items.append(
                {
                    "text": t,
                    "score": float(data["conf"][i]) / 100.0,
                    "x": data["left"][i],
                    "y": data["top"][i],
                    "x2": data["left"][i] + data["width"][i],
                    "y2": data["top"][i] + data["height"][i],
                }
            )
    return items, None


ENGINES = [
    ("rapidocr", ocr_rapidocr),
    ("winsdk", ocr_winsdk),
    ("tesseract", ocr_tesseract),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-focus", action="store_true", help="不把微信窗口提到前台")
    ap.add_argument("--scale", type=float, default=2.0, help="截图放大倍数（默认 2.0）")
    ap.add_argument("--engine", default="auto", choices=["auto"] + [e[0] for e in ENGINES])
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(os.path.dirname(here), "out")
    os.makedirs(outdir, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 78)
    print("微信 4.0 聊天界面 OCR 探针")
    print("时间: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78)

    hwnds = find_wechat_hwnds()
    if not hwnds:
        print("!! 未找到微信主窗口（需已登录且主窗口可见）")
        return
    hwnd = hwnds[0]
    print("微信主窗口 hwnd = %s" % hex(hwnd))

    if not args.no_focus:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            import time

            time.sleep(0.6)
        except Exception as e:
            print("!! 提到前台失败（继续截当前画面）: %s" % e)

    rect = window_rect(hwnd)
    print("窗口区域 = %s  尺寸 = %dx%d" % (rect, rect[2] - rect[0], rect[3] - rect[1]))

    from PIL import ImageGrab

    img = ImageGrab.grab(bbox=rect, all_screens=True)
    raw_path = os.path.join(outdir, "wechat_shot_%s.png" % ts)
    img.save(raw_path)
    print("[截图已保存] %s  实际像素 %s" % (raw_path, img.size))

    ocr_path = raw_path
    if args.scale and args.scale != 1.0:
        scaled = img.resize(
            (int(img.width * args.scale), int(img.height * args.scale)), 3  # BICUBIC
        )
        ocr_path = os.path.join(outdir, "wechat_shot_%s_x%.0f.png" % (ts, args.scale))
        scaled.save(ocr_path)
        print("[放大图已保存] %s %s" % (ocr_path, scaled.size))

    chosen = None
    items = None
    if args.engine == "auto":
        for name, fn in ENGINES:
            try:
                items, err = fn(ocr_path)
                chosen = name
                print("\n[OCR 引擎] %s  -> 识别到 %d 个文本块" % (name, len(items)))
                break
            except ImportError as e:
                print("[跳过引擎 %s] 未安装: %s" % (name, e))
            except Exception as e:
                print("[引擎 %s 失败] %s: %s" % (name, type(e).__name__, e))
    else:
        fn = dict(ENGINES)[args.engine]
        items, err = fn(ocr_path)
        chosen = args.engine

    if items is None:
        print("\n!! 没有可用的 OCR 引擎。当前只完成了截图。")
        print("   在 venv 里装一个即可，例如：")
        print("   .venv\\Scripts\\python -m pip install rapidocr-onnxruntime")
        return

    # 按 y 再按 x 排序，模拟阅读顺序
    items = sorted(items, key=lambda d: (d["y"] // 12, d["x"]))
    print("")
    print("-" * 78)
    print("识别结果（按阅读顺序，坐标已还原到原始窗口尺度）")
    print("-" * 78)
    print("%-6s %-6s %-6s %s" % ("x", "y", "score", "text"))
    for d in items:
        x = int(d["x"] / args.scale)
        y = int(d["y"] / args.scale)
        print("%-6d %-6d %-6.2f %s" % (x, y, d["score"], d["text"]))

    txt_path = os.path.join(outdir, "ocr_result_%s.txt" % ts)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("engine=%s scale=%s window=%s\n" % (chosen, args.scale, rect))
        f.write("x\ty\tscore\ttext\n")
        for d in items:
            f.write(
                "%d\t%d\t%.2f\t%s\n" % (int(d["x"] / args.scale), int(d["y"] / args.scale), d["score"], d["text"])
            )
    print("\n[结果已保存] %s" % txt_path)


if __name__ == "__main__":
    main()
