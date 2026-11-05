#!/bin/sh
set -eu
task_project_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$task_project_dir"
if [ -x .venv/bin/python ]; then
    task_python=.venv/bin/python
else
    echo "請先依照 README.md 建立 .venv 並安裝套件。" >&2
    exit 1
fi
exec "$task_python" -m local_llm_api "$@"
