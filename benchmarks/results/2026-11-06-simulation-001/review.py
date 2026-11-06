"""Review raw numerical answers with complex exponentials and recompute statistics."""
import base64
import cmath
import hashlib
import json
import math
from pathlib import Path
import statistics

root = Path(__file__).parent
source = (root / 'workload-source.py').read_bytes().replace(b'\r\n', b'\n')
source_hashes = {hashlib.sha256(source).hexdigest(),
                 hashlib.sha256(source.replace(b'\n', b'\r\n')).hexdigest()}
executions = completed_experiment_tasks = 0
largest_error = 0.0
for directory in (root, root / 'pc-one-worker', root / 'unavailable-fleet'):
    metadata = json.loads((directory / 'metadata.json').read_text())
    summary = json.loads((directory / 'summary.json').read_text())
    trials = [json.loads(line) for line in (directory / 'trials.jsonl').read_text().splitlines()]
    unavailable = directory.name == 'unavailable-fleet'
    assert summary['valid'] is not unavailable and not summary['interrupted']
    assert metadata['workload_sha256'] in source_hashes
    assert metadata['configuration']['steps'] == 2_000_000
    assert metadata['correctness_oracle']['absolute_tolerance'] == 1e-7
    assert metadata['correctness_oracle']['relative_tolerance'] == 1e-7
    for name, report in summary['profiles'].items():
        selected = [trial for trial in trials if trial['profile'] == name]
        assert len(selected) == 6 and sum(trial['warmup'] for trial in selected) == 1
        measured = [trial for trial in selected if not trial['warmup']]
        if unavailable and name == 'forge-2':
            assert not report['valid'] and report['failed_trials'] == 6
            assert report['unobserved_tasks'] == 40 and report['batch_median_seconds'] is None
            assert report['speedup_vs_sequential'] is None and report['speedup_vs_local_parallel'] is None
            assert all(not trial['valid'] and not trial['tasks'] for trial in selected)
            continue
        assert report['valid'] and all(trial['valid'] for trial in selected)
        durations = [trial['batch_seconds'] for trial in measured]
        assert durations == report['batch_seconds']
        assert statistics.median(durations) == report['batch_median_seconds']
        tasks = [task for trial in measured for task in trial['tasks']]
        assert len(tasks) == 40 and report['task_latency_samples'] == 40
        assert math.isclose(report['successful_tasks_per_second'], len(tasks) / sum(durations))
        latency = sorted(task['latency_seconds'] for task in tasks)
        for probability, key in ((.5, 'task_latency_p50_seconds'), (.95, 'task_latency_p95_seconds')):
            assert report[key] == latency[math.ceil(probability * len(latency)) - 1]
        for trial in selected:
            assert len(trial['tasks']) == 8
            if name == 'forge-2':
                assert {task['observed']['host'] for task in trial['tasks']} == {'SukruthPC', 'SukruthsPC'}
            executions += 8
            completed_experiment_tasks += 0 if unavailable else 8
            for task in trial['tasks']:
                assert task['valid'] and task['exit_code'] == 0 and not task['timed_out']
                observed = json.loads(base64.b64decode(task['stdout_base64']))
                assert observed == task['observed']
                assert observed['workload_sha256'] == metadata['workload_sha256']
                for key in ('task', 'workload', 'steps', 'omega', 'damping', 'duration'):
                    assert observed[key] == task[key]
                omega, damping, duration = (task[key] for key in ('omega', 'damping', 'duration'))
                decay = damping * omega
                frequency = omega * math.sqrt(1 - damping * damping)
                exponent = complex(-decay, frequency)
                amplitude = complex(1, -decay / frequency)
                expected = {'position': (amplitude * cmath.exp(exponent * duration)).real,
                            'velocity': (amplitude * exponent * cmath.exp(exponent * duration)).real}
                for key in expected:
                    assert math.isclose(observed[key], expected[key], rel_tol=1e-7, abs_tol=1e-7)
                    assert math.isclose(task['expected_result'][key], expected[key], rel_tol=1e-13, abs_tol=1e-13)
                    largest_error = max(largest_error, abs(observed[key] - expected[key]))
        print(directory.name, name, 'median', report['batch_median_seconds'])
assert executions == 336 and completed_experiment_tasks == 240
print('PASS: 240 executions in completed experiments plus 96 direct baseline executions in invalid fleet run')
print('Largest observed absolute final-state error:', largest_error)
