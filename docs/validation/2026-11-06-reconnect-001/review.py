"""Check process continuity, fresh results and final capacity in the saved report."""
import json
from pathlib import Path
import re

root = Path(__file__).parent
report = json.loads((root / 'results.json').read_text())
assert report['passed'] and report['intentional_outage_after_exit_seconds'] == 3
assert len(report['pc_binary_sha256']) == 64
for name in ('before', 'after'):
    probe = report[name]
    assert probe['cli']['returncode'] == 0
    observed = json.loads(probe['cli']['stdout'])
    assert observed == probe['observed']
    assert observed['host'] == 'SukruthPC'
    assert observed['nonce'] == probe['expected_nonce']
    assert observed['reconnect_enabled'] and observed['worker_matches_installed_binary']
    assert observed['source_clean'] and observed['git_head'] == report['runtime_source']
    assert len(observed['binary_sha256']) == 64 and observed['worker_pid'] > 0
    assert re.search(r'\[forge\] job-\d+ exit=0', probe['cli']['stderr'])
for key in ('host', 'worker_pid', 'worker_start_ticks', 'binary_sha256'):
    assert report['before']['observed'][key] == report['after']['observed'][key]
assert report['before']['expected_nonce'] != report['after']['expected_nonce']
assert 0 < report['restart_to_first_ready_observation_seconds']
for name in ('cluster_before', 'cluster_after'):
    snapshot = report[name]
    assert snapshot['returncode'] == 0
    rows = [row.split('\t') for row in snapshot['stdout'].splitlines()[1:]]
    assert {row[0] for row in rows if row[6] == 'READY'} == {'desktop-native', 'laptop-native'}
print('PASS: same laptop worker PID/start identity/binary; fresh jobs before and after; both workers READY')
