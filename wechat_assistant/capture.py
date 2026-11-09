# -*- coding: utf-8 -*-
"""把微信窗口内容抓成位图 —— 默认用不受遮挡影响的 PrintWindow。

为什么不能简单用屏幕截图
------------------------
`sender.py` 判断「这条消息是自己发的还是对方发的」靠的是**气泡的左右位置**，
必须先拿到消息行的像素。最初实现用 `PIL.ImageGrab.grab(bbox)` 抓屏幕，
而屏幕截图有致命弱点：**窗口被别的窗口盖住就截到别人的内容**。

实测（2026-09-24，用户真实环境）：按下快捷键时聊天区常常同时被浏览器
（`Chrome_RenderWidgetHostHWND`）和本项目自己的悬浮窗（`TkChild`）盖住，
`SenderResolver` 只好整体放弃 -> 所有消息 `is_self=None`
-> 「同一个人连发的几句话合并成一个回复目标块」这条逻辑完全失效
（表现为「回复目标块」永远只有 1 条）。

`PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` 让**目标窗口自己**把内容画进
位图：不受遮挡影响，也不需要把微信临时置顶去打扰用户。

实测对照（同一时刻，微信处于非前台且被遮挡）：

    flags=2  PW_RENDERFULLCONTENT  非空白；消息左占比 0.01(自己) / 1.00(对方)
    flags=1  PW_CLIENTONLY         整幅空白，不可用
    flags=0  默认                   整幅全黑，不可用

⚠️⚠️ 它是**同步的跨进程消息**，会一直等对方（见 `capture_window` 的说明）
------------------------------------------------------------------------
`PrintWindow` 内部走 `SendMessage(WM_PRINT)` —— 目标窗口的 UI 线程**不处理
消息**时，这个调用**永远不返回**，而且它是**没有超时参数**的。
2026-10-19 实测（`probes/probe_printwindow_hang.py`，一个自建的原生窗口）：

    目标正常泵消息     PrintWindow 31.9 ms 返回
    目标不泵消息       **8 秒仍未返回**（测试主动放弃等待）

这条之所以要写在这里，是因为调用方分布在**三个线程**上，其中最要命的一个
在 **Qt UI 线程**上（`panel._resample_theme_foreground` 每 5 秒重采一次主题）：
一旦微信卡住，整个面板就冻死、Windows 直接给「未响应」。
所以本模块统一加两道保险（`window_responding` + 看门狗线程）。

坐标系
------
`GetWindowRect()` 与 UIA 给出的 rect 都是**屏幕坐标**（本进程设为每显示器 DPI
感知时为物理像素）。PrintWindow 位图的原点却是**窗口左上角**，
所以裁剪前必须减去窗口原点 —— 见 `to_window_coords()`，两种取图方式共用。
"""

import ctypes
import threading
import time
from typing import Optional, Tuple

import win32gui
import win32ui

PW_RENDERFULLCONTENT = 0x00000002
PW_CLIENTONLY = 0x00000001

#: 一次抓图最多等多久（秒）。正常窗口实测 **32ms**，所以给到 2 秒已经很宽；
#: 真超时说明对方卡住了 —— 那时**返回 None**，让这一拍按"抓不到"处理（下一拍再试）。
#: ⚠️ 别调大：这个上限同时也是 **UI 线程**被阻塞的上限（见 `capture_window`）。
CAPTURE_TIMEOUT = 2.0

#: 一次抓图**超时**之后，这么多秒内不再尝试抓图（直接返回 None）。
#: 为什么需要：`IsHungAppWindow` 要"5 秒没泵消息"才为真 —— 微信如果只是
#: **每 2~4 秒卡一下**（渲染大图 / 重排列表），那道闸永远不触发，于是每拍都要
#: 白等 2 秒（UI 线程上就是持续一顿一顿），而且**每拍泄漏一条卡住的线程**。
#: 退避之后最坏也只是一次卡顿 / 10 秒。
HUNG_BACKOFF = 10.0

_user32 = ctypes.windll.user32
try:                                  # pragma: no cover - 取决于运行环境
    _user32.IsHungAppWindow.argtypes = [ctypes.c_void_p]
    _user32.IsHungAppWindow.restype = ctypes.c_bool
except Exception:
    pass

#: 退避到什么时候（`time.monotonic()` 的绝对时间；0 = 没在退避）
_hung_until = 0.0


def _note_hung() -> None:
    """记一次"抓图超时"，开始退避（见 `HUNG_BACKOFF`）。"""
    global _hung_until
    _hung_until = time.monotonic() + HUNG_BACKOFF


def to_window_coords(rect, wrect) -> Optional[Tuple[int, int, int, int]]:
    """屏幕坐标 rect -> PrintWindow 位图内的坐标。

    rect   : (left, top, right, bottom) 屏幕坐标
    wrect  : 窗口的屏幕坐标 rect（`win32gui.GetWindowRect(hwnd)`）
    """
    if not rect or not wrect:
        return None
    ox, oy = wrect[0], wrect[1]
    return (rect[0] - ox, rect[1] - oy, rect[2] - ox, rect[3] - oy)


def window_responding(hwnd) -> bool:
    """这个窗口的 UI 线程还在处理消息吗？（`IsHungAppWindow`：5 秒没泵消息 = 卡住）

    ⚠️ 这是**抓图前必须先问的一句**，不是优化：
        `PrintWindow` 打到卡住的窗口上会一直不返回（见模块说明里的实测），
        而调用方有一个在 **Qt UI 线程**上 —— 那一路挂住就是"程序假死"。
    ⚠️ 判不出来时返回 **True**（fail-open）：宁可去试一次（有看门狗兜底），
        也不要因为一个 API 失败就让所有抓图静默失效。
    ⚠️ 窗口已经销毁也返回 False —— 省掉一次必然失败、还可能卡一下的抓图。
    """
    try:
        if not hwnd:
            return False
        if not win32gui.IsWindow(hwnd):
            return False
        return not bool(_user32.IsHungAppWindow(hwnd))
    except Exception:
        return True


def _capture_once(hwnd: int, flags: int):
    """真正抓一次（**会阻塞**，只在工作线程 / 看门狗里调）。

    只读取窗口内容，不改变窗口状态（不置顶、不激活、不移动）。
    """
    from PIL import Image

    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0 or w > 20000 or h > 20000:
        return None

    hwnd_dc = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        try:
            bmp.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bmp)
            # 用 ctypes 直接调 user32：本机 pywin32 的 win32gui.PrintWindow
            # 成功时返回 None 而不是 1，写成 `if not ok: return None` 会把
            # **成功**的抓图误判成失败（实测踩过：同样的窗口，ctypes 调返回 1、
            # win32gui 调返回 None，结果被判成「PrintWindow 不可用」）。
            # 因此这里不看返回值，是否真的抓到内容交给 is_blank() 判断。
            ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), flags)
            info = bmp.GetInfo()
            bits = bmp.GetBitmapBits(True)
            img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                                   bits, "raw", "BGRX", 0, 1)
            # frombuffer 是只读视图，复制出来避免底层 buffer 被回收后失效
            return img.copy()
        finally:
            # ⚠️ 清理必须在**这个线程**里做：看门狗超时之后调用方已经走了，
            #    而这条线程可能还卡在 PrintWindow 里 —— 它手里的 DC / 位图
            #    只能由它自己释放。
            try:
                win32gui.DeleteObject(bmp.GetHandle())
            except Exception:
                pass
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
    except Exception:
        return None
    finally:
        if hwnd_dc is not None:
            try:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass


def capture_window(hwnd: int, flags: int = PW_RENDERFULLCONTENT,
                   timeout: float = CAPTURE_TIMEOUT):
    """用 PrintWindow 抓整个窗口，返回 PIL.Image（失败/卡住返回 None）。

    ⚠️⚠️ **为什么要在一条临时线程里调 PrintWindow**
    -------------------------------------------------
    它是同步跨进程消息（内部 SendMessage），**没有超时参数**：目标窗口的 UI
    线程不处理消息时，调用方会**永远**卡在里面（实测一个不泵消息的窗口，
    8 秒都没返回）。而调用方里有一个在 **Qt UI 线程**上
    （`panel._resample_theme_foreground` 每 5 秒重采主题）——
    微信一卡，面板就整块冻死、Windows 直接标「未响应」。

    所以这里把真正的调用丢进一条一次性线程，主调用方最多等 `timeout` 秒。
    超时后**牺牲一条泄漏的线程**（它卡在 SendMessage 里，Python 杀不掉）
    换取调用方（可能是 UI 线程）不被拖死 —— 和 `uia_worker` 里
    「换一条新线程继续干活、旧线程留着」是同一个取舍。

    不做这件事的代价（2026-10-19 实测）：微信在休眠恢复后 / 渲染大图时
    短暂卡住，面板就跟着卡住；`sender.resolve()` 里卡住则表现为
    「点生成之后按钮一直转圈、永远没有结果」。
    """
    # ① 先问一句"对方还活着吗"—— 卡住的窗口连试都不用试
    if not window_responding(hwnd):
        return None
    # ② 刚超时过就退避一会儿：那一刻再抓一次只会再等 `timeout` 秒、
    #    再泄漏一条线程（见 `HUNG_BACKOFF`）
    if time.monotonic() < _hung_until:
        return None

    box = {}
    done = threading.Event()

    def work():
        try:
            box["img"] = _capture_once(hwnd, flags)
        except Exception:
            box["img"] = None
        finally:
            done.set()

    try:
        threading.Thread(target=work, name="printwindow", daemon=True).start()
    except Exception:
        # 起不了线程就退回同步（总比完全不抓强）
        return _capture_once(hwnd, flags)

    if not done.wait(timeout):
        _note_hung()
        return None          # 卡住了：这一拍当"抓不到"，下一拍再来
    return box.get("img")


def is_blank(img, threshold: int = 8) -> bool:
    """整幅图几乎是同一个颜色 -> PrintWindow 什么都没画出来。"""
    if img is None:
        return True
    try:
        small = img.resize((64, 64)).convert("L")
        px = list(small.getdata())
        return (max(px) - min(px)) < threshold
    except Exception:
        return True


#: 发给**视觉模型**的图片：长边上限 / JPEG 质量 / 单张字节软上限。
#:
#: ⚠️ 2026-11-09 用户问「有没有图片大小上限」—— 原来**只有长边 1024**，
#:    没有张数、没有体积上限，而且存成 **PNG**（照片类内容能到 1~2 MB，
#:    base64 再 ×1.33）。视觉模型按图算 token，一张大图就是几千输入 token：
#:    实测纯文本平均输入 **821 token**，一屏 4 张图能顶到 4000+。
IMG_MAX_SIDE = 1024
IMG_JPEG_QUALITY = 85
IMG_MAX_BYTES = 300 * 1024
IMG_MIN_SIDE = 320

#: 默认最多发几张（配置项 `ocr_read.max_images`）。
IMG_MAX_COUNT = 3


def fit_image(img, max_side: int = IMG_MAX_SIDE, quality: int = IMG_JPEG_QUALITY,
              max_bytes: int = IMG_MAX_BYTES):
    """把图缩到长边 `max_side` 并按体积预算压成 JPEG，返回 (PIL.Image, bytes)。

    降体积的顺序：**先降质量（85→70），再缩长边（每次 ×0.8，最低 320）**。
    ⚠️ 缩长边放在降质量之后：降质量几乎不改变模型"看得懂"的程度，
        缩分辨率会丢字；所以先动质量。
    """
    import io

    from PIL import Image

    sub = img.convert("RGB")
    w, h = sub.size
    k = min(1.0, float(max_side) / max(w, h))
    if k < 1.0:
        sub = sub.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)

    def _encode(im, q):
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=int(q), optimize=True)
        return buf.getvalue()

    data = _encode(sub, quality)
    if max_bytes and len(data) > max_bytes:
        data = _encode(sub, max(60, quality - 15))
    while max_bytes and len(data) > max_bytes and max(sub.size) > IMG_MIN_SIDE:
        nw = max(IMG_MIN_SIDE, int(sub.width * 0.8))
        nh = max(IMG_MIN_SIDE, int(sub.height * 0.8))
        if (nw, nh) == sub.size:
            break
        sub = sub.resize((nw, nh), Image.LANCZOS)
        data = _encode(sub, max(60, quality - 15))
    return sub, data


def encode_for_model(img, max_side: int = IMG_MAX_SIDE,
                     quality: int = IMG_JPEG_QUALITY,
                     max_bytes: int = IMG_MAX_BYTES) -> str:
    """把一张 PIL 图压成**能直接喂视觉模型**的 base64（JPEG，带体积上限）。

    ⚠️ 原来两处调用各自 `save(format="PNG")` —— 这就是"图片没有上限"的来源：
        PNG + 照片 = 巨大；现在统一走这里（JPEG + 体积预算），**两处共用一份实现**。
    """
    import base64

    _img, data = fit_image(img, max_side=max_side, quality=quality,
                           max_bytes=max_bytes)
    return base64.b64encode(data).decode("ascii")


#: 两张图**判定为同一张**的汉明距离上限（64 位 dHash）。
#:
#: ⚠️ 6 是实测选出来的：同一张图裁两次（±1px 偏移、重新缩放）通常差 0~3 位；
#:    不同但相似的图（两张不同的表情包）一般差 12 位以上。
#:    调大就会把"像但不相同"的两张图并成一张，那是**丢信息**，比多花 token 糟。
DUPE_TOLERANCE = 6


def dhash(img, size: int = 8) -> int:
    """64 位差值哈希（dHash）——**不引入 imagehash 依赖**，PIL 就够。

    做法：缩成 (size+1)×size 的灰度图，逐行比较"左像素 > 右像素"，
    得到一个 64 位整数。对尺寸变化、JPEG 噪声、±1px 偏移都不敏感。
    """
    from PIL import Image

    small = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(small.getdata())
    bits = 0
    k = 0
    for y in range(size):
        row = px[y * (size + 1):(y + 1) * (size + 1)]
        for x in range(size):
            if row[x] > row[x + 1]:
                bits |= (1 << k)
            k += 1
    return bits


def is_same_picture(h1: int, h2: int, tol: int = DUPE_TOLERANCE) -> bool:
    """两个 dHash 是不是同一张图（汉明距离 ≤ tol）。"""
    try:
        return bin(int(h1) ^ int(h2)).count("1") <= tol
    except Exception:
        return False


def dedupe_crops(images, tol: int = DUPE_TOLERANCE):
    """把一批 PIL 图**按感知哈希去重**，返回 (保留的图, 去掉几张)。

    ⚠️ 2026-11-09 用户要求「处理图片去重」：聊天里同一张图被发两遍
        （重复转发的表情包、同一张截图连发）会让同一次请求里带两张一模一样的图，
        **视觉模型按图算 token，等于白花一份钱**。
    ⚠️ 只去掉**完全重复**的（dHash 距离 ≤ 6），宁可少去也别误删：
        两张不同的图被并成一张是丢信息，比多花点 token 严重得多。
    ⚠️ 保留**首次出现**的那张（顺序与调用方给的一致）。
    """
    kept = []
    hashes = []
    dropped = 0
    for img in images or []:
        try:
            h = dhash(img)
        except Exception:
            kept.append(img)          # 算不出哈希就别去重，宁可多发一张
            continue
        if any(is_same_picture(h, other, tol) for other in hashes):
            dropped += 1
            continue
        hashes.append(h)
        kept.append(img)
    return kept, dropped


def kb_of(b64_list) -> int:
    """一串 base64 大约多少 KB（给日志用，不解码整串）。"""
    try:
        return int(sum(len(s) for s in b64_list) * 3 / 4 / 1024)
    except Exception:
        return 0


def crop_window_region(img, wrect, rect):
    """从窗口位图里裁出屏幕坐标 rect 对应的区域；越界自动收敛，无效返回 None。"""
    if img is None:
        return None
    box = to_window_coords(rect, wrect)
    if box is None:
        return None
    x1 = max(0, box[0])
    y1 = max(0, box[1])
    x2 = min(img.width, box[2])
    y2 = min(img.height, box[3])
    if x2 - x1 < 1 or y2 - y1 < 1:
        return None
    return img.crop((x1, y1, x2, y2))
