#!/usr/bin/env bash
set -euo pipefail
control=${1:?Pass the new audited launch directory}
cd "$control"
/home/ubuntu/anaconda3/bin/python - <<'PY'
import hashlib, json
from pathlib import Path
record = json.loads(Path('preparation.json').read_text())
assert all(hashlib.sha256(Path(name).read_bytes()).hexdigest() == value
           for name, value in record['launch_files'].items())
for name in ('local_verification.json', 'docker_verification.json'):
    assert json.loads(Path(name).read_text())['status'] == 'passed'
assert hashlib.sha256(Path('source.tar.gz').read_bytes()).hexdigest() == record['source_archive_sha256']
print('Launch and verification manifests verified', flush=True)
PY
mkdir method_snapshot
tar -xzf source.tar.gz -C method_snapshot
export TRACE_REPAIR_MODEL=gpt-4.1
export TRACE_REPAIR_IMAGE_REGISTRY=127.0.0.1:5555
export PYTHONUTF8=1
export PYTHONDONTWRITEBYTECODE=1
/home/ubuntu/anaconda3/bin/python -u run_batch.py
