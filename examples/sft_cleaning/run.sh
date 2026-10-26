#!/usr/bin/env bash
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo '[sft-clean] FAIL execute this script; do not source' >&2
  return 2
fi
set -Eeuo pipefail
trap 'echo "[sft-clean] STOP status=$? line=$LINENO; sources unchanged; checkpoints retained" >&2' ERR
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" PYTHONDONTWRITEBYTECODE=1
python_bin=${SFT_CLEAN_PYTHON:-/mnt/sfs/ltj/runtime/datatrove-sft-cleaner/bin/python}
config=${SFT_CLEAN_CONFIG:-/mnt/sfs/ltj/text_cleaning/config/server.json}
echo '[sft-clean] READ explicit text annotations only; no media, original mutation, downloads or model launch.'
echo '[sft-clean] WRITE configured run/scratch roots; API generation only for explicit pilot/run commands.'
"$python_bin" -B -m sft_cleaner --config "$config" "$@"
echo '[sft-clean] PASS requested command; model labels are candidates, not independent correctness certification'
