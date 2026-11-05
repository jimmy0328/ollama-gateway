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

## 0.3.0 對話記憶與摘要（2026-11-05）

- 實際專案環境完整測試：44 passed。
- 新增 SQLite 持久化 session、user/assistant 完整回合與獨立摘要。
- 真實 Ollama 驗證：重啟 API 後仍能回答第一輪的專案代號與上線月份。
- 真實 SSE 驗證：完整模型輸出保存成功，完成後才回傳 [DONE]。
- 真實摘要驗證：摘要包含第一輪助理提供的建議，且摘要後原始三輪對話仍完整保留。
- 長對話分段摘要與合併、同 session 併發保護、資料依 user/session 分開、刪除 cascade，均有測試。
- 已於 8000 啟用新版，並驗證保存、讀取、摘要與刪除 API；測試對話已清除。
- API Key 與原有 .env 內容維持不變。
