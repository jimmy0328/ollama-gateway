import os

import httpx
from dotenv import load_dotenv

load_dotenv()
response = httpx.post(
    os.getenv("API_URL", "http://127.0.0.1:8000") + "/v1/chat/completions",
    headers={"Authorization": f"Bearer {os.environ['API_KEY']}"},
    json={
        "model": os.getenv("MODEL_ID", "qwen3.8:27b-mlx"),
        "messages": [{"role": "user", "content": "請用繁體中文簡短介紹你自己。"}],
        "max_tokens": 256,
        "reasoning_effort": "none",
    },
    timeout=300,
)
response.raise_for_status()
print(response.json()["choices"][0]["message"]["content"])
