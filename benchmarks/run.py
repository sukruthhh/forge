"""Compare complete, correctness-checked batches; retain failures and raw observations."""
import argparse
import base64
import concurrent.futures
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import time

from workload import MAX_LIMIT, reference_counts

ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = Path(__file__).with_name("workload.py")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_optional(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def git(*args):
    try:
        result = subprocess.run(["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), *args],
                                env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"), capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def provenance(args):
    cache = read_optional(args.binary.parent / "CMakeCache.txt") or ""
    build_type = next((line.split("=", 1)[1] for line in cache.splitlines()
                       if line.startswith("CMAKE_BUILD_TYPE:STRING=")), "unknown")
    cpu = read_optional("/proc/cpuinfo") or ""
    model = next((line.split(":", 1)[1].strip() for line in cpu.splitlines()
                  if line.startswith(("model name", "Hardware")) and ":" in line), "unknown")
    compiler = {}
    for path in (args.binary.parent / "CMakeFiles").glob("*/CMakeCXXCompiler.cmake"):
        for line in path.read_text().splitlines():
            for key in ("CMAKE_CXX_COMPILER", "CMAKE_CXX_COMPILER_ID", "CMAKE_CXX_COMPILER_VERSION"):
                if line.startswith(f'set({key} "'):
                    compiler[key] = line.split('"')[1]
        break
    status = git("status", "--porcelain")
    return {
        "schema": 1, "started_utc": datetime.now(timezone.utc).isoformat(),
        "label": args.label, "environment_kind": args.environment_kind,
        "cluster_kind": args.cluster_kind if args.host else "local-processes",
        "topology_is_user_declared": bool(args.host), "purpose": args.purpose,
        "git_commit": git("rev-parse", "HEAD"), "git_dirty": None if status is None else bool(status),
        "binary_sha256": digest(args.binary), "workload_sha256": digest(WORKLOAD),
        "source_commit_is_binary_attestation": False,
        "build_type": build_type, "compiler": compiler, "python": sys.version, "python_executable": sys.executable,
        "workload_path": str(WORKLOAD), "platform": platform.platform(),
        "architecture": platform.machine(), "hostname": socket.gethostname(), "cpu_model": model,
        "logical_cpus": os.cpu_count(), "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "meminfo": read_optional("/proc/meminfo"),
        "cgroup_cpu_max": read_optional("/sys/fs/cgroup/cpu.max"),
        "cgroup_memory_max": read_optional("/sys/fs/cgroup/memory.max"),
        "configuration": {key: str(value) if isinstance(value, Path) else value
                          for key, value in vars(args).items()},
        "credential_values_recorded": False,
        "boundary": "batch wall time includes dispatch, process startup, input handling, and result storage; excludes cluster startup, oracle and post-run verification",
        "latency_boundary": "local: subprocess invocation through exit; Forge: map task submission through response, excluding result-file persistence",
    }


def percentile(values, probability):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(probability * len(ordered)) - 1)]


def number(value, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def reject_constant(value):
    raise ValueError("non-finite JSON constant: " + value)


def observe(task, stdout, stderr, exit_code, latency, timed_out, expected, workload_hash):
    row = {"task": task["task"], "limit": task["limit"], "exit_code": exit_code if type(exit_code) is int else None,
           "latency_seconds": latency if number(latency) else None, "timed_out": timed_out if type(timed_out) is bool else None,
           "stdout_base64": base64.b64encode(stdout).decode(), "stderr_base64": base64.b64encode(stderr).decode(),
           "expected_prime_count": expected, "valid": False, "observed": None, "diagnostic": ""}
    try:
        value = json.loads(stdout, parse_float=finite_float, parse_constant=reject_constant)
        row["observed"] = value
        if type(exit_code) is not int or exit_code != 0 or type(timed_out) is not bool or timed_out or not number(latency, positive=True):
            raise ValueError("command failure, timeout, or invalid latency")
        if (type(value) is not dict or type(value.get("schema")) is not int or value["schema"] != 1 or
            type(value.get("task")) is not int or type(value.get("limit")) is not int or value.get("task") != task["task"] or
            value.get("limit") != task["limit"] or type(value.get("prime_count")) is not int or
            value["prime_count"] != expected or value.get("workload_sha256") != workload_hash or
            not number(value.get("compute_seconds")) or not number(value.get("cpu_seconds")) or
            type(value.get("worker_pid")) is not int or value["worker_pid"] <= 0 or
            not all(isinstance(value.get(key), str) and value[key] for key in ("host", "architecture", "python"))):
            raise ValueError("wrong answer, task identity, workload hash, or workload telemetry")
        row["valid"] = True
    except (ValueError, TypeError, KeyError) as error:
        row["diagnostic"] = str(error)
    return row


def execute_local(path, timeout):
    started = time.perf_counter()
    try:
        result = subprocess.run([sys.executable, str(WORKLOAD), str(path)], capture_output=True, timeout=timeout)
        return result.stdout, result.stderr, result.returncode, time.perf_counter() - started, False
    except subprocess.TimeoutExpired as error:
        return error.stdout or b"", error.stderr or b"", None, time.perf_counter() - started, True
    except OSError as error:
        return b"", str(error).encode(), None, time.perf_counter() - started, False


def json_text(value):
    return json.dumps(value, sort_keys=True, allow_nan=False) + "\n"


def local_trial(directory, tasks, paths, jobs, timeout, expected, workload_hash):
    started = time.perf_counter()
    def run(index):
        stdout, stderr, exit_code, latency, timed_out = execute_local(paths[index], timeout)
        stem = f"{index + 1:04}"
        (directory / f"{stem}.stdout").write_bytes(stdout)
        (directory / f"{stem}.stderr").write_bytes(stderr)
        metadata = {"task": tasks[index]["task"], "exit_code": exit_code, "latency_seconds": latency,
                    "timed_out": timed_out, "stdout": stem + ".stdout", "stderr": stem + ".stderr"}
        (directory / f"{stem}.json").write_text(json_text(metadata))
        return metadata, (stdout, stderr, exit_code, latency, timed_out)
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        captures = list(pool.map(run, range(len(tasks))))
    (directory / "results.jsonl").write_text("".join(json_text(metadata) for metadata, _ in captures))
    elapsed = time.perf_counter() - started
    rows = [observe(task, *capture, expected[task["limit"]], workload_hash)
            for task, (_, capture) in zip(tasks, captures)]
    return elapsed, rows


def forge_command(args, host, port, *command):
    if command[0] == "timings":
        return [str(args.binary), *command, "--host", host, "--port", str(port)]
    return [str(args.binary), command[0], "--host", host, "--port", str(port), *command[1:]]


def cluster_snapshot(args, host, port, environment, workers):
    result = subprocess.run(forge_command(args, host, port, "cluster"), env=environment,
                            capture_output=True, text=True, timeout=8)
    if result.returncode:
        raise RuntimeError("cannot inspect coordinator: " + result.stderr.strip())
    records = [row.split("\t") for row in result.stdout.splitlines()[1:]]
    if any(len(row) != 7 for row in records) or sum(row[6] == "READY" for row in records) != workers:
        raise RuntimeError("READY worker count differs from requested benchmark topology")
    jobs = subprocess.run(forge_command(args, host, port, "ps"), env=environment,
                          capture_output=True, text=True, timeout=8)
    if jobs.returncode or any("\t" + state + "\t" in jobs.stdout for state in ("QUEUED", "ASSIGNED", "RUNNING")):
        raise RuntimeError("cluster has active jobs or cannot be inspected; benchmark requires idle Forge capacity")
    return records


@contextmanager
def managed_cluster(args, workers, credentials, log_directory):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = {k: v for k, v in os.environ.items() if not k.startswith("FORGE_TLS_") and k != "FORGE_TOKEN"}
    server_env, client_env = dict(environment), dict(environment)
    if credentials:
        for target, role in ((server_env, "server"), (client_env, "client")):
            target.update(FORGE_TLS_CA=str(credentials / "ca.crt"), FORGE_TLS_CERT=str(credentials / f"{role}.crt"),
                          FORGE_TLS_KEY=str(credentials / f"{role}.key"))
    processes, logs = [], []
    try:
        def start(name, command, env):
            log = (log_directory / f"{name}.log").open("xb")
            logs.append(log)
            process = subprocess.Popen([str(args.binary), *command, "--port", str(port)], env=env, stdout=log, stderr=log)
            processes.append(process)
            return process
        coordinator = start("coordinator", ["coordinator"], server_env)
        deadline = time.monotonic() + 8
        while True:
            if coordinator.poll() is not None:
                raise RuntimeError("managed coordinator exited; see trial logs")
            query = subprocess.run(forge_command(args, "127.0.0.1", port, "cluster"), env=client_env,
                                   capture_output=True, timeout=8)
            if query.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("managed coordinator startup deadline exceeded")
            time.sleep(.05)
        worker_processes = [start(f"worker-{i}", ["join", "--id", f"bench-{i}"], client_env) for i in range(workers)]
        deadline = time.monotonic() + 8
        while True:
            if any(p.poll() is not None for p in worker_processes):
                raise RuntimeError("managed worker exited; see trial logs")
            try:
                snapshot = cluster_snapshot(args, "127.0.0.1", port, client_env, workers)
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
        yield "127.0.0.1", port, client_env, snapshot, {p.pid for p in worker_processes}
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()


def forge_trial(args, directory, tasks, paths, jobs, host, port, environment, expected, workload_hash):
    result_directory = directory / "map-results"
    command = forge_command(args, host, port, "map", "--arch", "any", "--jobs", str(jobs),
                            "--timeout", str(args.timeout), "--file", str(WORKLOAD), "--output", str(result_directory),
                            *map(str, paths), "--", args.worker_python or ("python3" if args.host else sys.executable), "workload.py", "{}")
    started = time.perf_counter()
    try:
        result = subprocess.run(command, env=environment, capture_output=True, timeout=len(tasks) * 45 + 15)
        elapsed = time.perf_counter() - started
        (directory / "cli.stdout").write_bytes(result.stdout)
        (directory / "cli.stderr").write_bytes(result.stderr)
    except subprocess.TimeoutExpired:
        raise RuntimeError("map CLI deadline exceeded; accepted external jobs may still be running")
    metadata = [json.loads(line) for line in (result_directory / "results.jsonl").read_text().splitlines()]
    if len(metadata) != len(tasks):
        raise RuntimeError("map manifest omitted or added inputs")
    rows = []
    job_ids = set()
    for index, (task, item) in enumerate(zip(tasks, metadata)):
        stem = f"{index + 1:04}"
        if (item.get("index") != index + 1 or item.get("input") != paths[index].name or
            item.get("stdout") != stem + ".stdout" or item.get("stderr") != stem + ".stderr"):
            raise RuntimeError("map manifest input order or artifact association changed")
        row = observe(task, (result_directory / item["stdout"]).read_bytes(), (result_directory / item["stderr"]).read_bytes(),
                      item.get("exit_code"), item.get("elapsed_seconds"), item.get("timed_out"), expected[task["limit"]], workload_hash)
        row.update(job_id=item.get("job_id"), map_state=item.get("state"))
        if item.get("state") != "SUCCEEDED" or item.get("storage_complete") is not True or item.get("truncated") is not False:
            row.update(valid=False, diagnostic="map task failed, truncated output, or incomplete storage")
        job_id = item.get("job_id")
        if not isinstance(job_id, str) or not job_id.startswith("job-") or not job_id[4:].isdigit() or job_id in job_ids:
            row.update(valid=False, diagnostic="missing, invalid, or duplicate job identity")
        if isinstance(job_id, str):
            job_ids.add(job_id)
        if args.collect_phases and not getattr(args, "comparison_profile", False):
            timing = subprocess.run(forge_command(args, host, port, "timings", job_id), env=environment,
                                    capture_output=True, text=True, timeout=8)
            (directory / f"{stem}.timings.json").write_text(timing.stdout)
            value = json.loads(timing.stdout, parse_float=finite_float, parse_constant=reject_constant)
            if (timing.returncode or value.get("job_id") != job_id or value.get("state") != "SUCCEEDED"
                or not all(number(value.get(key)) for key in PHASES)):
                raise RuntimeError("missing or invalid requested phase measurements")
            row["phases"] = value
        rows.append(row)
    return elapsed, rows, result.returncode, command


PHASES = ("queue_seconds", "dispatch_wait_seconds", "dispatch_write_seconds", "assigned_to_terminal_seconds",
          "worker_prepare_seconds", "worker_execute_seconds", "worker_cleanup_seconds", "worker_result_wait_seconds")


def profiles(workers, comparison=False):
    result = [{"name": "local-sequential", "kind": "local", "jobs": 1}]
    result.extend({"name": f"local-parallel-{n}", "kind": "local", "jobs": n} for n in workers if n != 1)
    result.extend({"name": f"forge-{n}", "kind": "forge", "jobs": n} for n in workers)
    if comparison:
        result.extend({"name": f"comparison-forge-{n}", "kind": "comparison", "jobs": n} for n in workers)
    return result


def summarize(trials, configurations, repetitions, warmups):
    summaries = {}
    for profile in configurations:
        matching = [trial for trial in trials if trial["profile"] == profile["name"]]
        measured = [trial for trial in matching if not trial["warmup"]]
        valid = (len(measured) == repetitions and sum(t["warmup"] for t in matching) == warmups
                 and all(trial["valid"] and number(trial["batch_seconds"], positive=True) for trial in matching))
        durations = [trial["batch_seconds"] for trial in measured]
        tasks = [task for trial in measured for task in trial["tasks"]]
        latencies = [task["latency_seconds"] for task in tasks if task["valid"]]
        summaries[profile["name"]] = {
            "valid": valid, "measured_trials": len(measured), "warmup_trials": sum(t["warmup"] for t in matching),
            "failed_tasks": sum(not task["valid"] for task in tasks), "failed_trials": sum(not t["valid"] for t in matching),
            "unobserved_tasks": sum(max(0, t.get("expected_tasks", len(t["tasks"])) - len(t["tasks"])) for t in measured),
            "batch_seconds": durations, "batch_median_seconds": statistics.median(durations) if valid else None,
            "batch_min_seconds": min(durations) if valid else None,
            "batch_max_seconds": max(durations) if valid else None,
            "batch_stdev_seconds": statistics.stdev(durations) if valid and len(durations) > 1 else None,
            "successful_tasks_per_second": len(tasks) / sum(durations) if valid else None,
            "task_latency_p50_seconds": percentile(latencies, .5) if valid else None,
            "task_latency_p95_seconds": percentile(latencies, .95) if valid else None,
            "task_latency_samples": len(latencies),
            "compute_median_seconds": statistics.median(t["observed"]["compute_seconds"] for t in tasks) if valid else None,
            "process_cpu_seconds_total": sum(t["observed"]["cpu_seconds"] for t in tasks) if valid else None,
            "phase_samples": sum("phases" in task for task in tasks),
            "phase_median_seconds": {key: statistics.median(task["phases"][key] for task in tasks) for key in PHASES}
                if valid and tasks and all("phases" in task for task in tasks) else None,
        }
    baseline = summaries["local-sequential"]
    for profile in configurations:
        report = summaries[profile["name"]]
        parallel = summaries["local-sequential" if profile["jobs"] == 1 else f"local-parallel-{profile['jobs']}"]
        for label, comparison in (("sequential", baseline), ("local_parallel", parallel)):
            usable = report["valid"] and comparison["valid"]
            report[f"speedup_vs_{label}"] = comparison["batch_median_seconds"] / report["batch_median_seconds"] if usable else None
            report[f"time_reduction_pct_vs_{label}"] = 100 * (1 - report["batch_median_seconds"] / comparison["batch_median_seconds"]) if usable else None
        previous = summaries.get(f"comparison-forge-{profile['jobs']}") if profile["kind"] == "forge" else None
        if previous is not None:
            usable = report["valid"] and previous["valid"]
            report["speedup_vs_comparison"] = previous["batch_median_seconds"] / report["batch_median_seconds"] if usable else None
            report["time_reduction_pct_vs_comparison"] = 100 * (1 - report["batch_median_seconds"] / previous["batch_median_seconds"]) if usable else None
    return {"schema": 1, "valid": all(report["valid"] for report in summaries.values()), "profiles": summaries,
            "percentile_method": "nearest rank, ceil(p*n), task submission-to-result samples; not dispatch latency",
            "comparison_method": "ratio of median measured batch durations; negative time reductions are retained"}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--compare-binary", type=Path, help="interleave an older complete runtime in managed mode")
    parser.add_argument("--comparison-source", help="user-declared source revision for the comparison binary; not an attestation")
    parser.add_argument("--collect-phases", action="store_true", help="inspect current-runtime job phases after each timed batch")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment-kind", required=True, choices=("native-linux", "wsl", "container", "vm"))
    parser.add_argument("--label", required=True)
    parser.add_argument("--purpose", choices=("smoke", "measurement"), default="measurement")
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2])
    parser.add_argument("--tasks", type=int, default=8)
    parser.add_argument("--limit", type=int, default=500000)
    parser.add_argument("--step", type=int, default=10000)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261103)
    parser.add_argument("--transport", choices=("tls", "plaintext"), default="tls")
    parser.add_argument("--host", help="use an existing cluster instead of starting local worker processes")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--cluster-kind", choices=("local-processes", "same-host-containers", "physical-machines", "unspecified"), default="unspecified")
    parser.add_argument("--worker-python", help="interpreter available on all workers; defaults to this interpreter locally, python3 externally")
    args = parser.parse_args()
    args.binary, args.output = args.binary.resolve(), args.output.resolve()
    if args.compare_binary:
        args.compare_binary = args.compare_binary.resolve()
        if not args.compare_binary.is_file() or args.host or not args.comparison_source:
            parser.error("comparison needs an existing binary, --comparison-source, and managed mode")
    elif args.comparison_source:
        parser.error("--comparison-source requires --compare-binary")
    if sys.platform != "linux":
        parser.error("run the benchmark inside Linux, WSL, or the development container")
    if not args.binary.is_file() or args.output.exists():
        parser.error("binary must exist and output directory must be new")
    if (not args.workers or len(set(args.workers)) != len(args.workers) or any(not 1 <= n <= 16 for n in args.workers)
        or not max(args.workers) <= args.tasks <= 64 or args.limit < 0 or args.step < 0
        or args.limit + (args.tasks - 1) * args.step > MAX_LIMIT or not 1 <= args.repetitions <= 30
        or not 0 <= args.warmups <= 5 or not 1 <= args.timeout <= 30 or not 1 <= args.port <= 65535):
        parser.error("invalid worker/task/workload/repetition/timeout bounds")
    if args.host and (len(args.workers) != 1 or args.cluster_kind == "unspecified"):
        parser.error("external mode needs one --workers count and an explicit --cluster-kind declaration")
    if args.host:
        tls = [os.environ.get(key) for key in ("FORGE_TLS_CA", "FORGE_TLS_CERT", "FORGE_TLS_KEY")]
        if (args.transport == "tls" and not all(tls)) or (args.transport == "plaintext" and any(v is not None for v in tls)):
            parser.error("external transport must match the configured TLS environment")
    return args


def main():
    args = parse_args()
    metadata = provenance(args)
    if args.compare_binary:
        previous = copy.copy(args)
        previous.binary = args.compare_binary
        metadata["comparison_binary"] = {key: value for key, value in provenance(previous).items()
                                         if key in ("binary_sha256", "build_type", "compiler", "source_commit_is_binary_attestation")}
        metadata["comparison_binary"]["declared_source"] = args.comparison_source
    metadata["phase_boundary"] = "optional job inspection occurs after the batch clock; worker phases are reported durations; coordinator phases overlap and must not be summed"
    args.output.mkdir(mode=0o700)
    (args.output / "metadata.json").write_text(json_text(metadata))
    input_directory = args.output / "inputs"
    input_directory.mkdir()
    tasks = [{"task": index, "limit": args.limit + index * args.step} for index in range(args.tasks)]
    expected = reference_counts([task["limit"] for task in tasks])
    paths = [input_directory / f"task-{task['task']:04}.json" for task in tasks]
    for task, path in zip(tasks, paths):
        path.write_text(json_text(task))
    configurations = profiles(args.workers, bool(args.compare_binary))
    trials = []
    randomizer = random.Random(args.seed)
    workload_hash = digest(WORKLOAD)
    interrupted = False
    with tempfile.TemporaryDirectory(prefix="forge-bench-") as private, (args.output / "trials.jsonl").open("x") as journal:
        credentials = None
        if not args.host and args.transport == "tls":
            spec = importlib.util.spec_from_file_location("certificates", ROOT / "scripts/create-dev-certificates.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            credentials = Path(private) / "credentials"
            module.generate(credentials, "127.0.0.1")
        try:
            for round_index in range(args.warmups + args.repetitions):
                order = list(configurations)
                randomizer.shuffle(order)
                for profile in order:
                    directory = args.output / f"round-{round_index:02}-{profile['name']}"
                    directory.mkdir()
                    trial = {"profile": profile["name"], "round": round_index, "warmup": round_index < args.warmups,
                             "valid": False, "batch_seconds": None, "tasks": [], "error": "",
                             "expected_tasks": len(tasks),
                             "load_before": os.getloadavg(), "started_utc": datetime.now(timezone.utc).isoformat()}
                    try:
                        if profile["kind"] == "local":
                            trial["batch_seconds"], trial["tasks"] = local_trial(directory, tasks, paths, profile["jobs"], args.timeout, expected, workload_hash)
                            trial["valid"] = all(row["valid"] for row in trial["tasks"])
                        else:
                            trial_args = copy.copy(args)
                            trial_args.comparison_profile = profile["kind"] == "comparison"
                            if trial_args.comparison_profile:
                                trial_args.binary = args.compare_binary
                            trial["binary_sha256"] = digest(trial_args.binary)
                            def measure(host, port, environment, snapshot, expected_pids):
                                elapsed, rows, exit_code, command = forge_trial(trial_args, directory, tasks, paths, profile["jobs"], host, port, environment, expected, workload_hash)
                                trial.update(batch_seconds=elapsed, tasks=rows, cli_exit_code=exit_code, command=command, cluster_snapshot=snapshot)
                                identities = {(row["observed"]["host"], row["observed"]["worker_pid"]) for row in rows if row["valid"]}
                                trial["worker_identities"] = sorted(identities)
                                trial["valid"] = exit_code == 0 and all(row["valid"] for row in rows) and len(identities) == profile["jobs"]
                                if expected_pids and {pid for _, pid in identities} != expected_pids:
                                    trial["valid"] = False
                                if not trial["valid"]:
                                    trial["error"] = "task validation, worker participation, or map completion failed"
                            if args.host:
                                snapshot = cluster_snapshot(args, args.host, args.port, os.environ.copy(), profile["jobs"])
                                measure(args.host, args.port, os.environ.copy(), snapshot, None)
                            else:
                                with managed_cluster(trial_args, profile["jobs"], credentials, directory) as cluster:
                                    measure(*cluster)
                    except Exception as error:
                        trial["valid"] = False
                        trial["error"] = str(error)
                    trial["load_after"] = os.getloadavg()
                    trials.append(trial)
                    journal.write(json_text(trial))
                    journal.flush()
                    print(f"[benchmark] {profile['name']} round={round_index} warmup={trial['warmup']} valid={trial['valid']} batch_seconds={trial['batch_seconds']}", flush=True)
        except KeyboardInterrupt:
            interrupted = True
    summary = summarize(trials, configurations, args.repetitions, args.warmups)
    summary.update(interrupted=interrupted, workload="synthetic trial-division prime counting; independent sieve oracle", label=args.label)
    (args.output / "summary.json").write_text(json_text(summary))
    print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
    return 130 if interrupted else 0 if summary["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
