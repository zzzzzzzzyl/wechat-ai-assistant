# 语料

真机截图与语料数据**不进版本库**（里面是真实的聊天窗口内容）。
要自己跑语料验收：

1. 把你的截图放进 `images/`（`tests/corpus/images/` 已在 `.gitignore` 里）；
2. 按下例登记到 `cases.json`：

```json
[
  {"id": "my_case", "file": "images/my_case.png", "scale": 1.5,
   "expected": null, "note": "备注"}
]
```

3. 人工核对过的期望写进 `golden.json`（格式见 `tools/run_corpus.py` 的 docstring）；
4. `python tools\run_corpus.py`。
