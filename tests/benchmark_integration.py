"""Check benchmark accounting and failure censorship; never gate CI on speedup."""
import base64
import copy
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "benchmarks"))
import workload
import oscillator

spec = importlib.util.spec_from_file_location("benchmark", root / "benchmarks/run.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)
binary = str(Path(sys.argv[1]).resolve())


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def invoke(output, *arguments):
    return subprocess.run([sys.executable, str(root / "benchmarks/run.py"), "--binary", binary,
                           "--output", str(output), "--environment-kind", "container", "--label", "CTest synthetic smoke",
                           "--purpose", "smoke", *arguments], capture_output=True, text=True, timeout=65)


known = {0: 0, 1: 0, 2: 1, 3: 2, 10: 4, 100: 25, 1000: 168, 7919: 1000, 10000: 1229}
require(workload.reference_counts(list(known)) == known, "sieve oracle fails known answers")
require(all(workload.count_primes(limit) == expected for limit, expected in known.items()), "CPU kernel fails known answers")
undamped = dict(oscillator.make_task(0, 10000), damping=0, duration=math.pi / 2)
reference = oscillator.reference(undamped)
require(abs(reference['position']) < 1e-14 and abs(reference['velocity'] + 1) < 1e-14,
        'analytic oscillator oracle fails the quarter-period known answer')
oscillator.check_result(undamped, dict(undamped, **oscillator.solve(undamped)), reference)
for omega in (.25, 5):
    for damping in (0, .9):
        for duration in (1, 20):
            edge = dict(undamped, omega=omega, damping=damping, duration=duration)
            oscillator.check_result(edge, dict(edge, **oscillator.solve(edge)), oscillator.reference(edge))
for index in range(64):
    task = oscillator.make_task(index, 10000)
    answer = oscillator.solve(task)
    oscillator.check_result(task, dict(task, **answer), oscillator.reference(task))
    require(answer['velocity'] ** 2 + task['omega'] ** 2 * answer['position'] ** 2 < task['omega'] ** 2,
            'damped oscillator increased total mechanical energy')
for key, value in [('steps', 0), ('steps', True), ('omega', float('nan')), ('damping', 1), ('duration', 0)]:
    invalid = dict(undamped, **{key: value})
    try:
        oscillator.solve(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError('invalid oscillator input accepted')
require(benchmark.percentile([1, 2, 3, 4], .5) == 2 and benchmark.percentile([1, 2, 3, 4], .95) == 4,
        "percentile method does not match nearest-rank contract")

with tempfile.TemporaryDirectory() as temporary:
    simulations = Path(temporary) / 'simulations'
    result = invoke(simulations, '--workload', 'oscillator', '--steps', '10000', '--workers', '1', '2',
                    '--tasks', '2', '--repetitions', '1', '--warmups', '0', '--timeout', '5', '--collect-phases')
    require(result.returncode == 0, result.stdout + result.stderr)
    simulation_metadata = json.loads((simulations / 'metadata.json').read_text())
    simulation_summary = json.loads((simulations / 'summary.json').read_text())
    require(simulation_summary['valid'] and simulation_metadata['correctness_oracle']['absolute_tolerance'] == 1e-7,
            'oscillator benchmark omitted correctness policy or failed')
    simulation_trials = [json.loads(line) for line in (simulations / 'trials.jsonl').read_text().splitlines()]
    require(len(simulation_trials) == 4, 'oscillator profiles omitted')
    good_simulation = simulation_trials[0]['tasks'][0]
    task = {key: good_simulation[key] for key in ('workload', 'task', *oscillator.PARAMETERS)}
    for key, value in [('position', good_simulation['observed']['position'] + .01),
                       ('velocity', float('inf')), ('steps', 10001), ('omega', 4.0)]:
        wrong = dict(good_simulation['observed'], **{key: value})
        rejected = benchmark.observe(task, json.dumps(wrong).encode(), b'', 0, .1, False,
                                     good_simulation['expected_result'], simulation_metadata['workload_sha256'])
        require(not rejected['valid'], 'wrong oscillator answer or task parameters accepted')
    for options in (('--workload', 'oscillator', '--steps', '0'),
                    ('--workload', 'oscillator', '--limit', '1000'), ('--steps', '10000')):
        invalid_output = Path(temporary) / 'invalid-simulation'
        require(invoke(invalid_output, *options).returncode == 2 and not invalid_output.exists(),
                'invalid workload options accepted or created benchmark output')
    output = Path(temporary) / "successful"
    result = invoke(output, "--workers", "1", "2", "--tasks", "2", "--limit", "1000", "--step", "100",
                    "--repetitions", "2", "--warmups", "1", "--timeout", "5", "--collect-phases",
                    "--compare-binary", binary, "--comparison-source", "same-binary-test")
    require(result.returncode == 0, result.stdout + result.stderr)
    metadata = json.loads((output / "metadata.json").read_text())
    summary = json.loads((output / "summary.json").read_text())
    trials = [json.loads(line) for line in (output / "trials.jsonl").read_text().splitlines()]
    configurations = benchmark.profiles([1, 2], True)
    require(summary["valid"] and len(trials) == 18, "missing warmups or measured trials")
    require(metadata["comparison_binary"]["binary_sha256"] == metadata["binary_sha256"], "comparison provenance missing")
    for name in ("forge-1", "forge-2"):
        report = summary["profiles"][name]
        require(report["phase_samples"] == 4 and all(benchmark.number(v) for v in report["phase_median_seconds"].values()),
                "requested phase observations missing")
        previous = summary["profiles"]["comparison-" + name]
        require(abs(report["speedup_vs_comparison"] - previous["batch_median_seconds"] / report["batch_median_seconds"]) < 1e-9,
                "comparison ratio changed samples")
    require(metadata["cluster_kind"] == "local-processes" and not metadata["topology_is_user_declared"],
            "managed process test mislabeled as physical cluster")
    require(metadata["configuration"]["transport"] == "tls" and metadata["credential_values_recorded"] is False,
            "transport or credential metadata wrong")
    for profile in configurations:
        report = summary["profiles"][profile["name"]]
        selected = [t for t in trials if t["profile"] == profile["name"] and not t["warmup"]]
        require(report["valid"] and report["measured_trials"] == 2 and report["warmup_trials"] == 1,
                "warmup excluded incorrectly or valid trials rejected")
        require(report["batch_seconds"] == [t["batch_seconds"] for t in selected] and report["task_latency_samples"] == 4,
                "summary silently changed samples")
        require(abs(report["successful_tasks_per_second"] - 4 / sum(report["batch_seconds"])) < 1e-9,
                "throughput is not completed tasks per measured batch second")
    for trial in trials:
        for row in trial["tasks"]:
            require(row["valid"] and json.loads(base64.b64decode(row["stdout_base64"])) == row["observed"],
                    "raw observations omitted or altered")
    good = trials[0]["tasks"][0]
    wrong = dict(good["observed"], prime_count=good["observed"]["prime_count"] + 1)
    rejected = benchmark.observe({"task": good["task"], "limit": good["limit"]}, json.dumps(wrong).encode(), b"", 0,
                                 good["latency_seconds"], False, good["expected_prime_count"], metadata["workload_sha256"])
    require(not rejected["valid"], "fast wrong answer accepted as useful work")
    for malformed in (b'{"compute_seconds":NaN}', b'{"compute_seconds":1e999}'):
        invalid = benchmark.observe({"task": 0, "limit": 1000}, malformed, b"", 0, float("nan"), False, 168, metadata["workload_sha256"])
        require(not invalid["valid"] and invalid["latency_seconds"] is None,
                "non-finite timing accepted")
        benchmark.json_text(invalid)
    corrupt = copy.deepcopy(trials)
    failed = next(t for t in corrupt if t["profile"] == "forge-2" and not t["warmup"])
    failed["valid"] = False
    failed["tasks"][0]["valid"] = False
    report = benchmark.summarize(corrupt, configurations, 2, 1)["profiles"]["forge-2"]
    require(not report["valid"] and report["failed_tasks"] == 1 and report["speedup_vs_sequential"] is None
            and report["batch_median_seconds"] is None, "failed repetition discarded to improve headline metrics")
    corrupt = copy.deepcopy(trials)
    next(t for t in corrupt if t["profile"] == "local-sequential" and t["warmup"])["valid"] = False
    summary = benchmark.summarize(corrupt, configurations, 2, 1)
    require(summary["profiles"]["forge-2"]["speedup_vs_sequential"] is None, "failed baseline warmup hidden")
    incomplete = benchmark.summarize(trials[:-1], configurations, 2, 1)
    require(not incomplete["valid"], "incomplete experiment reported as complete")
    corrupt = copy.deepcopy(trials)
    next(t for t in corrupt if t["profile"] == "comparison-forge-2")["valid"] = False
    require(benchmark.summarize(corrupt, configurations, 2, 1)["profiles"]["forge-2"]["speedup_vs_comparison"] is None,
            "failed comparison trial hidden")
    previous = (output / "metadata.json").read_bytes()
    require(invoke(output, "--workers", "1").returncode != 0 and (output / "metadata.json").read_bytes() == previous,
            "existing benchmark results overwritten")

    failed_output = Path(temporary) / "failure"
    result = invoke(failed_output, "--workers", "1", "--tasks", "1", "--limit", "1000", "--step", "0",
                    "--repetitions", "1", "--warmups", "0", "--worker-python", "/forge-benchmark-missing-python")
    failed = json.loads((failed_output / "summary.json").read_text())
    require(result.returncode == 1 and not failed["valid"] and failed["profiles"]["local-sequential"]["valid"],
            "real remote task failure did not invalidate experiment")
    require(failed["profiles"]["forge-1"]["speedup_vs_sequential"] is None,
            "failed remote jobs produced speedup metrics")
    raw = [json.loads(line) for line in (failed_output / "trials.jsonl").read_text().splitlines()]
    forge = next(t for t in raw if t["profile"] == "forge-1")
    require(forge["tasks"][0]["exit_code"] == 127 and forge["tasks"][0]["stderr_base64"], "task failure evidence lost")
    print("Benchmark checks passed: oracle, raw samples, timing/accounting, warmups, invalidation, actual task failure, no overwrite")
