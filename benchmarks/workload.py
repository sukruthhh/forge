"""Deterministic synthetic CPU workload; no claim of application representativeness."""
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import socket
import sys
import time

MAX_LIMIT = 5_000_000


def read_optional(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def count_primes(limit):
    if type(limit) is not int or not 0 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 0 through {MAX_LIMIT}")
    count = int(limit >= 2)
    for candidate in range(3, limit + 1, 2):
        for divisor in range(3, math.isqrt(candidate) + 1, 2):
            if candidate % divisor == 0:
                break
        else:
            count += 1
    return count


def reference_counts(limits):
    maximum = max(limits)
    if any(type(n) is not int or not 0 <= n <= MAX_LIMIT for n in limits):
        raise ValueError("invalid reference limits")
    sieve = bytearray(b"\x01") * (maximum + 1)
    sieve[:min(2, len(sieve))] = b"\x00" * min(2, len(sieve))
    for prime in range(2, math.isqrt(maximum) + 1):
        if sieve[prime]:
            start = prime * prime
            sieve[start::prime] = b"\x00" * ((maximum - start) // prime + 1)
    return {n: sum(memoryview(sieve)[:n + 1]) for n in limits}


def main():
    if sys.platform != "linux":
        raise SystemExit("run this workload inside Linux, WSL, or a Linux container")
    import resource
    config = json.loads(Path(sys.argv[1]).read_text())
    limit = config["limit"]
    before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.perf_counter()
    count = count_primes(limit)
    elapsed = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_SELF)
    cpu = read_optional("/proc/cpuinfo") or ""
    cpu_model = next((line.split(":", 1)[1].strip() for line in cpu.splitlines()
                      if line.startswith(("model name", "Hardware")) and ":" in line), "unknown")
    print(json.dumps({
        "schema": 1, "task": config["task"], "limit": limit, "prime_count": count,
        "compute_seconds": elapsed,
        "cpu_seconds": after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
        "host": socket.gethostname(), "worker_pid": os.getppid(),
        "architecture": platform.machine(), "python": platform.python_version(),
        "cpu_model": cpu_model, "cpu_affinity_count": len(os.sched_getaffinity(0)),
        "cgroup_cpu_max": read_optional("/sys/fs/cgroup/cpu.max"),
        "cgroup_memory_max": read_optional("/sys/fs/cgroup/memory.max"),
        "workload_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
