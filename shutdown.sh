#!/bin/sh
set -eu

task_project_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$task_project_dir"

if [ "$#" -gt 1 ]; then
    echo "用法：$0 [PORT]" >&2
    exit 1
fi
if [ ! -x .venv/bin/python ]; then
    echo "找不到本專案的 .venv/bin/python。" >&2
    exit 1
fi
if ! command -v lsof >/dev/null 2>&1; then
    echo "需要 lsof 才能確認服務程序。" >&2
    exit 1
fi

# 與啟動設定相同：指定參數 > 環境變數 PORT > .env > 8000。
task_port="$(.venv/bin/python - "${1-}" <<'PY'
import os
import sys
from dotenv import dotenv_values

value = sys.argv[1] or os.getenv('PORT') or dotenv_values('.env').get('PORT') or '8000'
try:
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError
except (TypeError, ValueError):
    sys.exit('PORT 必須是 1 到 65535 的整數。')
print(port)
PY
)"

task_pids="$(lsof -nP -t -iTCP:"$task_port" -sTCP:LISTEN 2>/dev/null || true)"
if [ -z "$task_pids" ]; then
    echo "服務未啟動（port ${task_port}）。"
    exit 0
fi

task_matched=0
for task_pid in $task_pids; do
    task_cwd="$(lsof -a -p "$task_pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' || true)"
    task_command="$(ps -p "$task_pid" -o command= 2>/dev/null || true)"
    if [ "$task_cwd" != "$task_project_dir" ]; then
        echo "略過其他專案的程序 PID ${task_pid}（port ${task_port}）。" >&2
        continue
    fi
    case " $task_command " in
        *" -m local_llm_api "*) ;;
        *)
            echo "略過非本 API 的程序 PID ${task_pid}（port ${task_port}）。" >&2
            continue
            ;;
    esac

    task_matched=1
    echo "正在關閉 API（PID ${task_pid}，port ${task_port}）…"
    if ! kill -TERM "$task_pid" 2>/dev/null; then
        if kill -0 "$task_pid" 2>/dev/null; then
            echo "無法關閉 PID ${task_pid}，請確認程序權限。" >&2
            exit 1
        fi
    fi

    task_attempt=0
    while kill -0 "$task_pid" 2>/dev/null; do
        task_state="$(ps -p "$task_pid" -o stat= 2>/dev/null || true)"
        case "$task_state" in *Z*) break ;; esac
        task_attempt=$((task_attempt + 1))
        if [ "$task_attempt" -ge 100 ]; then
            echo "程序尚未結束，請稍後再確認；未強制終止。" >&2
            exit 1
        fi
        sleep 0.2
    done
    echo "API 已關閉。"
done

if [ "$task_matched" -eq 0 ]; then
    echo "port $task_port 沒有屬於本專案的 API，未關閉其他服務。" >&2
    exit 1
fi
