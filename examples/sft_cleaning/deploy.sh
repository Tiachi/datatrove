#!/usr/bin/env bash
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then return 2; fi
set -Eeuo pipefail
trap 'echo "[sft-deploy] STOP status=$? line=$LINENO; existing data/config preserved" >&2' ERR
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python_bin=${SFT_DEPLOY_PYTHON:-}
if [[ -z "$python_bin" ]]; then
  for candidate in /usr/local/python3.12.13/bin/python3 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3,10))' >/dev/null 2>&1; then
      python_bin=$candidate
      break
    fi
  done
fi
[[ -n "$python_bin" ]] || { echo '[sft-deploy] FAIL Python3.10+ with venv required' >&2; exit 2; }
"$python_bin" -B "$here/scripts/deploy_datatrove_sft.py" --config "$here/config/deployment.json" --from-current-fork "$@"
echo '[sft-deploy] PASS standalone fork deployment; production calls not started'
