# -*- coding: utf-8 -*-
"""OCR 封装（只读截图识别），带性能优化。

════════════════════════════════════════════════════════════════════
为什么需要这个模块：默认配置慢到不可用

实测（微信 4.1.13.65 / onnxruntime 1.26.0 / i9 20 逻辑核）：
    一张 978x84 的消息行图，默认配置单次 OCR 要 **9.19 秒**，
    并且会把 20 个核心全部拉满 —— 任务管理器 CPU 100%，整机明显卡顿。

两个根因：

  ① onnxruntime 默认 intra_op_num_threads=0（自动模式），在本机是性能灾难。
     实测 rec 模型：显式设线程 10~28 ms ／ 自动模式 233 ms（最慢 434ms）；
     session 初始化：显式 152 ms ／ 自动 1528 ms。

  ② Det 的 limit_type=min 含义是「保证短边 >= 736」。
     聊天消息行是极扁的图（978x84），短边只有 84，于是被放大到
     **736x2944（面积 26 倍）** 才送进检测模型。

修复（本模块采用）：
    intra_op_num_threads = 4     避免线程竞争，也不再吃满全部核心
    inter_op_num_threads = 1
    limit_side_len       = 736
    limit_type           = max   改按长边限制，扁图不再被放大
    use_cls              = False 聊天截图不会倒置，方向分类纯属浪费

效果：单次 9.19s -> **0.33s（约 28 倍）**，
      且识别结果与默认配置逐张一致（12/12 张真实消息行截图验证通过）。

注意：不要为了更快把 limit_side_len 调到 320 —— 实测识别质量会崩
      （'测试'→空、'行'→空、昵称'李四'丢失，仅 2/12 张一致）。
════════════════════════════════════════════════════════════════════
"""

import os
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

DEFAULT_PARAMS = {
    "intra_op_num_threads": 4,
    "inter_op_num_threads": 1,
    # ⚠️ 这个数**保持 736**。2026-10-13 试过提到 1536，**收益被副作用抵消，已回退**：
    #
    #     起因：`wx_1567_single` 那条「嗯嗯」（纯绿气泡白字）**整屏 OCR 认不出来**，
    #     而把那个气泡盒单独裁出来是 **0.99** 的置信度 —— 不是认不出，是
    #     `limit_type=max` 把 1146x661 的整屏**缩小**成 736x424、字从 14px 变 9px，
    #     **检测阶段漏掉了**。
    #
    #     实测（同一张 1146x661）：
    #         limit=736    902 ms    9 块   认不出「嗯嗯」
    #         limit=1536  1212 ms   10 块   认出来了（+34% 时间）
    #
    #     ⚠️ **但 1536 的副作用更大**：整屏不再缩放之后，**图片里的小字也被认出来**了。
    #        实测 `wx_1567_group4` 左上那张小红书截图一下多出 **4 条假消息**
    #        （`对，这是本这人作量的病 时天 1851 广东…` 这种），而**那张图不成盒**，
    #        `_image_regions` 够不着它 → 剔不掉。消息 6 -> 10 条，其中 4 条是噪声。
    #
    #     ✅ 所以改成"**哪里漏了就单独补哪里**"：保持 736（快），
    #        由 `OcrReader._retry_blank_boxes()` 对「**装了 0 块、颜色数又低**」的
    #        气泡盒单独放大重试 —— 那种盒正是"纯色文本气泡"；而**图片和头像的颜色数
    #        都远高于那个阈值**，不会被重试，也就不会把图里的字翻出来。
    "limit_side_len": 736,
    "limit_type": "max",
    "use_cls": False,
}


class OcrEngine:
    """RapidOCR 的轻封装：单例、懒加载、可预热，配置经过性能优化。"""

    def __init__(self, params: Optional[Dict] = None, logger: Callable[[str], None] = None):
        self.params = dict(DEFAULT_PARAMS)
        if params:
            for k, v in params.items():
                if v is not None:
                    self.params[k] = v
        self._log = logger or (lambda msg: None)
        self._engine = None
        self._lock = threading.Lock()
        self._load_ms = None

    # ------------------------------------------------------------ 配置

    def _build_config_file(self) -> str:
        """基于包内默认 config.yaml 生成一份改好参数的临时配置。

        为什么要落一个临时文件：RapidOCR 只接受 config_path 或扁平 kwargs，
        而 limit_side_len / limit_type 这些嵌套参数用 kwargs 不好传。
        另外 config_path 指向包外文件时，里面 models/xxx.onnx 这类相对路径
        会解析错，所以这里统一替换成绝对路径。
        """
        import rapidocr_onnxruntime as rr

        pkg = os.path.dirname(rr.__file__)
        with open(os.path.join(pkg, "config.yaml"), "r", encoding="utf-8") as f:
            txt = f.read()

        p = self.params
        txt = txt.replace("&intra_nums -1", "&intra_nums %d" % int(p["intra_op_num_threads"]))
        txt = txt.replace("&inter_nums -1", "&inter_nums %d" % int(p["inter_op_num_threads"]))
        txt = txt.replace("limit_side_len: 736", "limit_side_len: %d" % int(p["limit_side_len"]))
        txt = txt.replace("limit_type: min", "limit_type: %s" % p["limit_type"])
        if not p.get("use_cls", True):
            txt = txt.replace("use_cls: true", "use_cls: false")

        models = os.path.join(pkg, "models")
        for m in (
            "ch_PP-OCRv4_det_infer.onnx",
            "ch_ppocr_mobile_v2.0_cls_infer.onnx",
            "ch_PP-OCRv4_rec_infer.onnx",
        ):
            txt = txt.replace("models/%s" % m, os.path.join(models, m).replace("\\", "/"))

        path = os.path.join(tempfile.gettempdir(), "wechat_ai_assistant_rapidocr.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)
        return path

    # ------------------------------------------------------------ 生命周期

    def load(self):
        """加载引擎（第一次会读模型，约 1~2 秒）。重复调用无开销。"""
        with self._lock:
            if self._engine is not None:
                return self._engine
            from rapidocr_onnxruntime import RapidOCR

            t0 = time.perf_counter()
            cfg = self._build_config_file()
            self._engine = RapidOCR(config_path=cfg)
            self._load_ms = (time.perf_counter() - t0) * 1000
            self._log("OCR 引擎已加载（%.0f ms）  线程=%s limit=%s/%s"
                      % (self._load_ms, self.params["intra_op_num_threads"],
                         self.params["limit_type"], self.params["limit_side_len"]))
            return self._engine

    def warmup(self, size=(84, 978)):
        """预热：跑一张空白图，避免第一次真用时卡住。

        首次推理含 onnxruntime 的图优化，比后续慢好几倍，所以启动时先热一下。
        """
        try:
            self.load()
            h, w = size
            blank = (np.ones((h, w, 3), dtype=np.uint8) * 255)
            t0 = time.perf_counter()
            self._engine(blank)
            ms = (time.perf_counter() - t0) * 1000
            self._log("OCR 预热完成（%.0f ms）" % ms)
            return ms
        except Exception as e:
            self._log("OCR 预热失败: %s: %s" % (type(e).__name__, e))
            return None

    # ------------------------------------------------------------ 识别

    def recognize(self, image, min_score: float = 0.5) -> List[dict]:
        """识别一张图，返回 [{text, score, x1, y1, x2, y2}, ...]。

        image 可以是 PIL.Image 或 numpy 数组。
        """
        self.load()
        arr = np.asarray(image) if not isinstance(image, np.ndarray) else image
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        res, _ = self._engine(arr)
        out: List[dict] = []
        if not res:
            return out
        for box, text, score in res:
            if float(score) < min_score:
                continue
            xs = [pt[0] for pt in box]
            ys = [pt[1] for pt in box]
            out.append({
                "text": text,
                "score": float(score),
                "x1": int(min(xs)), "y1": int(min(ys)),
                "x2": int(max(xs)), "y2": int(max(ys)),
            })
        return out

    @property
    def load_ms(self):
        return self._load_ms


_shared: Optional[OcrEngine] = None


def get_engine(params: Optional[Dict] = None, logger: Callable[[str], None] = None) -> OcrEngine:
    """进程内共享单例 —— 引擎绝不能每次 OCR 都重建（重建要 1~2 秒）。"""
    global _shared
    if _shared is None:
        _shared = OcrEngine(params, logger)
    return _shared
