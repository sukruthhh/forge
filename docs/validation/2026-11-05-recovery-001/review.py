"""Check saved outcomes, reconstructed input hashes and observation intervals."""
import hashlib
import json
from pathlib import Path
import re
import statistics

root = Path(__file__).parent
report = json.loads((root / 'results.json').read_text())
assert report['passed'] and len(report['trials']) == 5
for trial in report['trials']:
    assert trial['passed'] and trial['result']['returncode'] == 0
    assert trial['cancelled_submission']['returncode'] == 130
    attempts = [row.split('\t') for row in trial['attempts']['stdout'].splitlines()[1:]]
    assert len(attempts) == 2
    assert attempts[0][1:3] == ['desktop-native', 'LOST']
    assert attempts[1][1:4] == ['laptop-native', 'SUCCEEDED', '0']
    assignment = next(row for row in trial['assignment_rows'] if row[0] == trial['job_id'])
    task = int(re.search(r'task-(\d+)\.json$', assignment[4]).group(1))
    observed = json.loads(trial['result']['stdout'])
    encoded = json.dumps({'nonce': observed['nonce'], 'task': task}).encode()
    assert observed['host'] == 'SukruthPC'
    assert hashlib.sha256(encoded).hexdigest() == observed['sha256']
    assert 0 < trial['signal_to_first_loss_observation_seconds'] < trial['signal_to_submission_result_seconds']
for key in ('signal_to_first_loss_observation_seconds', 'signal_to_submission_result_seconds'):
    values = [trial[key] for trial in report['trials']]
    print(key, 'median', statistics.median(values), 'min', min(values), 'max', max(values))
for name in ('preflight-main-thread.json', 'preflight-history.json'):
    preflight = json.loads((root / name).read_text())
    assert not preflight['passed']
    assert all('signal_call_seconds' not in trial for trial in preflight['trials'])
print('PASS: 5/5 controlled worker-crash recoveries; both no-fault preflight failures retained')
