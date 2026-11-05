# 對話記憶與摘要

這一版提供持久化的對話記錄：每段對話使用一個 `session_id`，同時保存使用者輸入與助理輸出。
程式重啟後可繼續同一段對話。資料預設保存在 `data/conversations.sqlite3`，不依賴外部資料庫。

這對應 ADK 的 Session 概念。本功能的摘要範圍是指定 session；不會跨其他對話自行取用個人偏好。
可參考 [ADK 對 Sessions 與 Memory 的區分](https://github.com/google/adk-docs/blob/main/docs/sessions/index.md)。

## 1. 建立對話

在專案資料夾執行，從現有設定載入 Key：

```bash
export API_KEY="$(.venv/bin/python -c 'from local_llm_api.config import Settings; print(Settings().api_key.get_secret_value())')"
export SESSION_ID="$(curl -fsS http://127.0.0.1:8000/v1/sessions \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"jimmy","system_prompt":"請用繁體中文回答，協助規劃專案。"}' \
  | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
```

請在呼叫端保存回傳的 `id`，之後以 `session_id` 傳入。每個新主題可以另建 session。
`user_id` 是你的應用程式使用者識別碼，例如會員 ID；不是 API Key。

## 2. 持續聊天

```bash
curl -fsS http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"user_id\":\"jimmy\",\"session_id\":\"$SESSION_ID\",\"model\":\"qwen3.8:27b-mlx\",\"messages\":[{\"role\":\"user\",\"content\":\"專案名稱叫海風，請建議三個實作步驟。\"}],\"max_tokens\":256,\"reasoning_effort\":\"none\"}"
```

下一輪只傳新的問題：

```bash
curl -fsS http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"user_id\":\"jimmy\",\"session_id\":\"$SESSION_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"請詳細說明你剛才建議的第一個步驟。\"}],\"max_tokens\":256,\"reasoning_effort\":\"none\"}"
```

伺服器自動組合「session 的 system prompt＋先前使用者訊息＋先前助理輸出＋這次問題」。
對話模式每次只能帶一則 `role=user` 的新訊息，避免重複儲存呼叫端重送的整段歷史。
回應的 `session_id` 與 `X-Session-Id` header 可供呼叫端確認歸屬。

JSON 與 `stream=true` 的 SSE 都支援記憶。串流收到合法完成訊號並成功存檔後，才送出 `[DONE]`。
後端失敗或未完整結束的串流不會留下半輪紀錄；對話中途的 thinking/reasoning 不作為助理答案儲存。

同一 session 同時只能有一個聊天或摘要請求，重疊回傳 409，避免兩輪歷史互相覆蓋。
同一請求重送會視為新的一輪，請由呼叫端避免重複送出已成功的請求。

## 3. 最後產生摘要

```bash
curl -fsS "http://127.0.0.1:8000/v1/users/jimmy/sessions/$SESSION_ID/summary" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"instructions":"請整理完整對話，包含我的需求、你先前提供的步驟與結果、已確認決策及待辦。","max_tokens":1024}'
```

回應的 `content` 是摘要，`source_turns` 是涵蓋的完整輪數，`model_calls` 是摘要實際呼叫模型的次數。
`finish_reason=length` 表示最後摘要碰到輸出 token 上限，可提高 `max_tokens` 後重新產生。

摘要會讀取這個 session 的**全部已保存對話**，包含先前每輪助理輸出。長對話會分段摘要，
再合併成最後摘要；原始對話不會因此被刪除、改寫，也不會把摘要當作新一輪助理回覆。
摘要是模型的整理結果，重點仍可能有遺漏；需要原文時使用歷史 API。

再次讀取最近一份已保存摘要，不重新呼叫模型：

```bash
curl -fsS "http://127.0.0.1:8000/v1/users/jimmy/sessions/$SESSION_ID/summary" \
  -H "Authorization: Bearer $API_KEY"
```

讀取全部歷史時每頁最多 1,000 輪，使用 `limit`、`offset` 翻頁：

```bash
curl -fsS "http://127.0.0.1:8000/v1/users/jimmy/sessions/$SESSION_ID?limit=100&offset=0" \
  -H "Authorization: Bearer $API_KEY"
```

回應 `turns` 每筆包含 `user_content`、`assistant_content`、模型、時間及完成原因。

## 管理與設定

| API | 功能 |
|---|---|
| `POST /v1/sessions` | 建立對話，body 含 `user_id`、可選 `system_prompt` |
| `GET /v1/users/{user_id}/sessions` | 列出使用者對話，支援 `limit` / `offset` |
| `GET /v1/users/{user_id}/sessions/{session_id}` | 對話資料與歷史，支援 `limit` / `offset` |
| `DELETE /v1/users/{user_id}/sessions/{session_id}` | 刪除對話及其所有歷史、摘要 |
| `POST /v1/users/{user_id}/sessions/{session_id}/summary` | 根據完整歷史產生並保存摘要 |
| `GET /v1/users/{user_id}/sessions/{session_id}/summary` | 取得最近一次保存的摘要 |

以上 API 都要求相同的 Bearer Key。`user_id` 用於區分資料，但共用 Key 不是每位使用者的登入驗證；
應由持有 API Key 的可信任後端傳入正確的會員 ID，不能把任意 `user_id` 當成授權證明。

| 設定 | 預設 | 行為 |
|---|---|---|
| `MEMORY_DB_PATH` | `data/conversations.sqlite3` | SQLite 檔案路徑，相對於啟動目錄 |
| `MEMORY_CONTEXT_TURNS` | `20` | 聊天自動帶入的近期完整輪數上限 |
| `MAX_PROMPT_CHARS` | `32000` | 聊天組合後的上下文字元上限，超出時捨棄最舊完整輪；資料庫保留原文 |
| `SUMMARY_CHUNK_CHARS` | `12000` | 長對話每個摘要分段的字元上限 |
| `SUMMARY_MAX_CALLS` | `64` | 一次摘要最多模型呼叫次數；超過回傳 413，不會只摘要部分內容冒充全部 |

即使早期對話未進入近期聊天窗口，最後的摘要仍會從資料庫讀到它。長摘要可能需要多次模型呼叫，
請讓呼叫端的 timeout 留足時間。請保留資料庫檔案，刪除它就會失去記憶。
服務維持單一 worker；SQLite 及 session 請求保護不適用直接增加多個獨立 API worker。

未傳 `session_id` 與 `user_id` 時，仍維持原本由呼叫端傳入 messages 的無狀態聊天模式。
更新前未保存的聊天無法自動補回，需從新的 session 開始，或自行重新提供內容。
