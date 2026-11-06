"""Independently verify retained concurrency observations and summary arithmetic."""
import base64
import cmath
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent


def check(directory):
    metadata = json.loads((directory / 'metadata.json').read_text())
    trials = [json.loads(line) for line in (directory / 'trials.jsonl').read_text().splitlines()]
    summary = json.loads((directory / 'summary.json').read_text())
    config = metadata['configuration']
    assert summary['valid'] and not summary['interrupted']
    assert metadata['git_dirty'] is False
    assert config['workers'] == [1] and config['worker_slots'] == 2 and config['comparison_slots'] == 1
    assert config['transport'] == 'tls' and config['steps'] == 2000000
    assert metadata['binary_sha256'] == metadata['comparison_binary']['binary_sha256']
    source = (root / 'workload-source.py').read_bytes().replace(b'\r\n', b'\n')
    assert hashlib.sha256(source).hexdigest() == metadata['workload_sha256']
    expected_profiles = {'forge-1', 'comparison-forge-1', 'local-sequential', 'local-parallel-2'}
    assert set(summary['profiles']) == expected_profiles
    assert len(trials) == 24 and all(t['valid'] and not t['error'] for t in trials)
    errors, warmups, measured = [], 0, 0
    for trial in trials:
        assert len(trial['tasks']) == trial['expected_tasks'] == 8
        assert math.isfinite(trial['batch_seconds']) and trial['batch_seconds'] > 0
        if trial['warmup']:
            warmups += 8
        else:
            measured += 8
        assert {row['task'] for row in trial['tasks']} == set(range(8))
        for row in trial['tasks']:
            assert row['valid'] and row['exit_code'] == 0 and not row['timed_out'] and not row['diagnostic']
            observed = row['observed']
            assert observed['host'] == metadata['hostname']
            assert json.loads(base64.b64decode(row['stdout_base64'])) == observed
            assert not base64.b64decode(row['stderr_base64'])
            assert observed['workload_sha256'] == metadata['workload_sha256']
            for key in ('task', 'omega', 'damping', 'duration', 'steps'):
                assert observed[key] == row[key]
            w, z, t = row['omega'], row['damping'], row['duration']
            q = math.sqrt(1-z*z)
            pole = cmath.exp(complex(-z*w, w*q)*t)
            oracle = {'position': pole.real + z/q*pole.imag, 'velocity': -w/q*pole.imag}
            for key, value in oracle.items():
                assert math.isfinite(observed[key]) and math.isclose(observed[key], value, abs_tol=1e-7, rel_tol=1e-7)
                errors.append(abs(observed[key]-value))
        if trial['profile'].startswith(('forge-', 'comparison-')):
            assert trial['worker_slots'] == (2 if trial['profile'] == 'forge-1' else 1)
            assert trial['binary_sha256'] == metadata['binary_sha256']
            assert len(trial['worker_identities']) == 1
            assert {tuple(pair) for pair in trial['worker_identities']} == {
                (row['observed']['host'], row['observed']['worker_pid']) for row in trial['tasks']}
    assert warmups == 32 and measured == 160
    for profile, report in summary['profiles'].items():
        selected = [t for t in trials if t['profile'] == profile]
        assert sum(t['warmup'] for t in selected) == 1
        observations = [t['batch_seconds'] for t in selected if not t['warmup']]
        assert len(observations) == 5 and observations == report['batch_seconds']
        assert statistics.median(observations) == report['batch_median_seconds']
        assert math.isclose(40/sum(observations), report['successful_tasks_per_second'])
        assert report['task_latency_samples'] == 40 and report['failed_trials'] == report['failed_tasks'] == 0
        latencies = sorted(row['latency_seconds'] for trial in selected if not trial['warmup'] for row in trial['tasks'])
        assert latencies[math.ceil(.5*len(latencies))-1] == report['task_latency_p50_seconds']
        assert latencies[math.ceil(.95*len(latencies))-1] == report['task_latency_p95_seconds']
    new = summary['profiles']['forge-1']['batch_median_seconds']
    old = summary['profiles']['comparison-forge-1']['batch_median_seconds']
    parallel = summary['profiles']['local-parallel-2']['batch_median_seconds']
    assert math.isclose(100*(1-new/old), summary['profiles']['forge-1']['time_reduction_pct_vs_comparison'])
    assert math.isclose(parallel/new, summary['profiles']['forge-1']['speedup_vs_local_parallel'])
    print(directory.name, json.dumps({'medians': {k:v['batch_median_seconds'] for k,v in summary['profiles'].items()},
          'time_reduction_pct': 100*(1-new/old), 'parallel_overhead_pct': 100*(new/parallel-1),
          'max_absolute_state_error': max(errors), 'measured_tasks': measured, 'warmup_tasks': warmups}))


check(root)
if (root/'initial').is_dir():
    check(root/'initial')
