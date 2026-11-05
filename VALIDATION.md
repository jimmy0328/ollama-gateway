# 驗證紀錄

日期：2026-11-05（Asia/Taipei）。

本版依新規格直接串接 Ollama `http://192.168.0.105:11434`。

- 新建獨立 Python 3.13.5 環境，未安裝 MLX、MLX-LM 或任何模型權重。
- API 單元與整合測試：30 passed。
- pip check：No broken requirements found。
- 真實區網連線：成功讀取指定 Ollama 的模型清單。
- 測試模型：`qwen3.8:27b-mlx`，由遠端 Ollama 執行。
- HTTP JSON 聊天成功回覆「你好」。
- HTTP SSE 串流成功回覆「你好」並收到 `[DONE]`。
- 未提供 API Key 的請求回傳 401。
- 測試用本機 API 綁定 127.0.0.1:18000，驗證結束後已關閉；未停止遠端 Ollama。

真實回應紀錄見 `http-smoke-result.json`。
本次涵蓋文字聊天與模型清單，未驗證圖片、工具呼叫、長上下文或同時大量推論。
