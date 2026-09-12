#!/usr/bin/env bash
set -euo pipefail
root=${1:?Pass the fresh directory containing source.tar.gz and runtime-deps}
image=sha256:75ccfce5854288fb3af22c71880c8fafa09a4763e85c170b1c89705501f60590
cd "$root"
/home/ubuntu/anaconda3/envs/swebench/bin/python - <<'PY'
import hashlib,json
from pathlib import Path
state=json.loads(Path('source_state.json').read_text())
assert hashlib.sha256(Path('source.tar.gz').read_bytes()).hexdigest()==state['archive_sha256']
print('Verified archive:',state['archive_sha256'])
PY
mkdir method results
tar -xzf source.tar.gz -C method
/home/ubuntu/anaconda3/envs/swebench/bin/python - <<'PY'
import hashlib,json
from pathlib import Path
expected=json.loads(Path('source_files.sha256.json').read_text())
assert all(hashlib.sha256((Path('method')/p).read_bytes()).hexdigest()==h for p,h in expected.items())
print('Verified source files:',len(expected))
PY
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" --cpus 2 --memory 4g --pids-limit 256 \
  --tmpfs /tmp:rw,nosuid,nodev,size=1g \
  --mount "type=bind,src=$root/method,dst=/method,readonly" \
  --mount "type=bind,src=$root/results,dst=/verification" \
  --mount "type=bind,src=$root/runtime-deps,dst=/deps,readonly" \
  --mount type=bind,src=/home/ubuntu/anaconda3/envs/swebench,dst=/runtime,readonly \
  -e HOME=/tmp -e PYTHONUTF8=1 -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/deps \
  --entrypoint /bin/sh -w /method "$image" \
  -c 'umask 0002; exec /runtime/bin/python verification/20260911_interface_v3/run_checks.py --output /verification/checks'
