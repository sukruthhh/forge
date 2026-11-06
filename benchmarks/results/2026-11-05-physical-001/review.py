"""Independently check published trial arithmetic and raw workload observations."""
import base64
import hashlib
import json
import math
from pathlib import Path
import statistics

root = Path(__file__).parent
total = warmup_tasks = 0
for directory in (root, root / 'pc-one-worker'):
    metadata = json.loads((directory / 'metadata.json').read_text())
    summary = json.loads((directory / 'summary.json').read_text())
    trials = [json.loads(line) for line in (directory / 'trials.jsonl').read_text().splitlines()]
    assert summary['valid'] and not summary['interrupted']
    for name, report in summary['profiles'].items():
        selected = [trial for trial in trials if trial['profile'] == name]
        assert len(selected) == 6 and all(trial['valid'] for trial in selected)
        assert sum(trial['warmup'] for trial in selected) == 1
        measured = [trial for trial in selected if not trial['warmup']]
        durations = [trial['batch_seconds'] for trial in measured]
        assert report['batch_seconds'] == durations
        assert report['batch_median_seconds'] == statistics.median(durations)
        tasks = [task for trial in measured for task in trial['tasks']]
        assert len(tasks) == 40 and report['task_latency_samples'] == 40
        assert math.isclose(report['successful_tasks_per_second'], len(tasks) / sum(durations))
        latency = sorted(task['latency_seconds'] for task in tasks)
        for probability, key in ((.5, 'task_latency_p50_seconds'), (.95, 'task_latency_p95_seconds')):
            assert report[key] == latency[math.ceil(probability * len(latency)) - 1]
        for trial in selected:
            assert len(trial['tasks']) == 8
            total += 8
            warmup_tasks += 8 if trial['warmup'] else 0
            for task in trial['tasks']:
                assert task['valid'] and task['exit_code'] == 0 and not task['timed_out']
                observed = json.loads(base64.b64decode(task['stdout_base64']))
                assert observed == task['observed']
                assert observed['task'] == task['task'] and observed['limit'] == task['limit']
                limit = task['limit']
                sieve = bytearray(b'\x01') * (limit + 1)
                sieve[:2] = b'\x00\x00'
                for prime in range(2, math.isqrt(limit) + 1):
                    if sieve[prime]:
                        start = prime * prime
                        sieve[start::prime] = b'\x00' * ((limit - start) // prime + 1)
                assert observed['prime_count'] == task['expected_prime_count'] == sum(sieve)
                assert observed['workload_sha256'] == metadata['workload_sha256']
            if name == 'forge-2':
                assert {task['observed']['host'] for task in trial['tasks']} == {'SukruthPC', 'SukruthsPC'}
        print(directory.name, name, 'median', statistics.median(durations), 'validated', len(tasks))
assert total == 240 and warmup_tasks == 40
workload = (root.parents[1] / 'workload.py').read_bytes().replace(b'\r\n', b'\n')
assert metadata['workload_sha256'] in {
    hashlib.sha256(workload).hexdigest(),
    hashlib.sha256(workload.replace(b'\n', b'\r\n')).hexdigest(),
}
print('PASS: 240 executions, 40 warmup and 200 measured; raw answers and summary arithmetic checked')
