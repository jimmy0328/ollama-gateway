# Ollama REST API

FastAPI 將外部請求轉送至 `http://192.168.0.105:11434` 的 Ollama 服務。
預設模型為 `qwen3.8:27b-mlx`，呼叫端可以指定伺服器上其他已安裝的模型。
不需要 MLX-LM、GPU 套件、模型轉換或下載模型；模型由遠端 Ollama 載入與執行。

```text
外部程式 → 本專案 FastAPI :8000 → Ollama 192.168.0.105:11434 → 模型
```

## 這台電腦直接啟動

本機已準備好設定與測試環境，在本資料夾執行：

```bash
./start.sh
```

API 文件：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)。
按 Authorize 輸入 `.env` 中的 `API_KEY` 即可測試。Ctrl+C 停止本 API；遠端 Ollama 維持運作。

## 重新安裝

需求為 Python 3.11 以上，且能連線至指定的 Ollama 服務。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[dev]'
cp .env.example .env
python - <<'PY'
from pathlib import Path
import secrets
p = Path('.env')
p.write_text(p.read_text().replace('replace-with-your-random-api-key', secrets.token_urlsafe(32)))
PY
./start.sh
```

ZIP 不包含私密 `.env` 或虛擬環境。也可以直接執行 `python -m local_llm_api`。

## 呼叫方式

在專案資料夾載入 API Key：

```bash
export API_KEY="$(python -c 'from local_llm_api.config import Settings; print(Settings().api_key.get_secret_value())')"
```

取得指定 Ollama 伺服器的模型清單：

```bash
curl http://127.0.0.1:8000/v1/models -H "Authorization: Bearer $API_KEY"
```

JSON 聊天：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.8:27b-mlx",
    "messages": [{"role": "user", "content": "請用繁體中文簡短介紹 REST API。"}],
    "max_tokens": 256,
    "reasoning_effort": "none"
  }'
```

`model` 可以改成模型清單中的名稱，例如 `qwen2.5-coder:32b`；省略時使用 `.env` 的 `MODEL_ID`。
`reasoning_effort` 直接交由 Ollama 處理，實際支援取決於模型及 Ollama 版本。

SSE 串流：

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8:27b-mlx","messages":[{"role":"user","content":"你好"}],"max_tokens":128,"reasoning_effort":"none","stream":true}'
```

Python 範例：`python examples/client.py`。聊天歷史由呼叫端在每次 `messages` 一併傳入。

| 端點 | 功能 | 驗證 |
|---|---|---|
| `POST /v1/chat/completions` | 文字聊天，支援 JSON / SSE | Bearer Key |
| `GET /v1/models` | 遠端 Ollama 的模型清單 | Bearer Key |
| `GET /readyz` | 檢查 Ollama 可達及預設模型已安裝 | Bearer Key |
| `GET /healthz` | 檢查本 API 程序存活 | 無 |
| `GET /docs` | Swagger 文件 | 頁面無；API 呼叫需 Key |

此版提供文字聊天子集，接受 `model`、`messages`、`max_tokens`、`temperature`、`top_p`、
`stop`、`seed`、`reasoning_effort`、`stream` 與 `stream_options.include_usage`。
未知欄位回傳 422；圖片、工具呼叫、embeddings 與 Ollama 原生 `/api/chat` 不在本版範圍內。

## 設定與外部連線

| 設定 | 預設 | 說明 |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://192.168.0.105:11434` | Ollama HTTP(S) 來源地址，不含 `/v1` |
| `MODEL_ID` | `qwen3.8:27b-mlx` | 請求省略模型時的預設值 |
| `API_KEY` | 必填 | 本 API 的 Bearer Key，至少 16 字元 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 本 API 綁定位置 |
| `MAX_TOKENS` | `4096` | 每次輸出 token 上限；請求預設 512 |
| `MAX_PROMPT_CHARS` | `32000` | 全部 messages 的字元上限 |
| `MAX_BODY_BYTES` | `1048576` | HTTP 請求大小上限 |
| `REQUEST_TIMEOUT` | `300` | 後端單次網路讀寫等待秒數，非整次生成總期限 |
| `MAX_INFLIGHT` | `2` | 同時進行的推論上限；超過回傳 429 |

其他電腦需呼叫本 API 時，將 `.env` 改成 `HOST=0.0.0.0` 後重新啟動，使用
`http://執行本API的電腦IP:8000/v1/chat/completions`，並帶上 API Key。
`192.168.0.105:11434` 是後端 Ollama 的位置，`8000` 是本專案對外提供的 API port。
API Key 驗證不會加密 HTTP；跨網際網路使用時，請在本 API 前方設定 HTTPS。

本 API 的 Key 不會轉送給 Ollama。支援單一 FastAPI worker，確保同時請求限制一致。
串流開始後的錯誤會以 SSE error 事件回傳。`readyz` 不會暖機或驗證模型推論所需記憶體。

## 測試

```bash
python -m pytest -q
```

實際區網連線與聊天驗證結果見 `VALIDATION.md`。
官方介面參考：[Ollama HTTP API 相容性](https://docs.ollama.com/api/openai-compatibility)、
[模型清單 API](https://docs.ollama.com/api/tags)。

## 關閉服務

在專案資料夾執行：

```bash
./shutdown.sh
```

腳本從環境變數 `PORT` 或本專案 `.env` 讀取 port（預設 8000），也可以明確指定：

```bash
./shutdown.sh 8000
```

只會對工作目錄屬於本專案、且以 `python -m local_llm_api` 啟動的 API 發送 SIGTERM，
等待正常結束；不會關閉遠端 Ollama，也不會強制終止占用相同 port 的其他專案。
服務未啟動時可重複執行。若要重新啟動，執行 `./start.sh`。
