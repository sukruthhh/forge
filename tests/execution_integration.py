"""End-to-end job placement, execution, output, timeout, and worker-loss checks."""
import concurrent.futures
import re
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

binary = os.path.abspath(sys.argv[1])
processes = []


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wait_for(predicate, message, seconds=7):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(0.05)
    raise AssertionError(message)


with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]


def start(*args):
    process = subprocess.Popen([binary, *args, "--port", str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    processes.append(process)
    return process


def cluster():
    return subprocess.check_output([binary, "cluster", "--port", str(port)], text=True, timeout=5)


def run(*args, options=()):
    return subprocess.run([binary, "run", "--port", str(port), *options, "--", *args],
                          capture_output=True, timeout=40)


try:
    server = start("coordinator")
    wait_for(lambda: "STATE" in cluster(), "server did not start")
    worker = start("join", "--id", "executor-a")
    wait_for(lambda: "READY" in cluster(), "worker did not join")

    result = run("/usr/bin/printf", "%s", "hello ; $(echo not-a-shell)")
    require(result.returncode == 0 and result.stdout == b"hello ; $(echo not-a-shell)",
            "argument boundaries or literal shell syntax damaged")
    result = run("/bin/sh", "-c", "printf stdout; printf stderr >&2; exit 7")
    require(result.returncode == 7 and result.stdout == b"stdout" and b"stderr" in result.stderr,
            "stdout, stderr, or nonzero exit lost")
    result = run("forge-nonexistent-executable")
    require(result.returncode == 127 and b"cannot execute" in result.stderr, "spawn error not returned")
    result = run("python3", "-c", "import sys; sys.stdout.write('x'*100000); sys.stderr.write('y'*100000)")
    require(result.returncode == 0 and result.stdout == b"x" * 16384 and b"truncated" in result.stderr,
            "output limit or pipe draining failed")
    result = run("python3", "-c", "import sys; sys.stdout.buffer.write(b'a\\x00b')")
    require(result.stdout == b"a\x00b", "binary output damaged")

    job_id = re.search(rb"\[forge\] (job-\d+)", result.stderr).group(1).decode()
    retained = subprocess.run([binary, "logs", job_id, "--port", str(port)], capture_output=True, timeout=5)
    require(retained.returncode == 0 and retained.stdout == b"a\x00b", "binary log retrieval failed")
    listed = subprocess.check_output([binary, "ps", "--port", str(port)], text=True, timeout=5)
    require(job_id + "\tSUCCEEDED\texecutor-a\t0" in listed, "completed job not listed")

    # A long-running job must not stop worker heartbeats or coordinator inspection.
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pending = pool.submit(run, "/bin/sleep", "2")
        time.sleep(1.5)
        require("READY" in cluster(), "execution starved heartbeat")
        listed = subprocess.check_output([binary, "ps", "--port", str(port)], text=True, timeout=5)
        require("\tRUNNING\texecutor-a\t" in listed, "running job not visible")
        running_id = next(row.split("\t")[0] for row in listed.splitlines() if "\tRUNNING\t" in row)
        unavailable = subprocess.run([binary, "logs", running_id, "--port", str(port)],
                                     capture_output=True, timeout=5)
        require(unavailable.returncode != 0 and b"not finished" in unavailable.stderr,
                "running job logs must explain that streaming is unavailable")
        require(pending.result().returncode == 0, "sleep failed")

    result = run("/bin/sleep", "10", options=("--timeout", "1"))
    require(result.returncode == 124 and b"timed out" in result.stderr, "runtime deadline failed")
    job_id = re.search(rb"\[forge\] (job-\d+)", result.stderr).group(1).decode()
    listed = subprocess.check_output([binary, "ps", "--port", str(port)], text=True, timeout=5)
    require(job_id + "\tTIMED_OUT\t" in listed, "timeout state not retained")
    retained = subprocess.run([binary, "logs", job_id, "--port", str(port)], capture_output=True, timeout=5)
    require(retained.returncode == 0 and b"timed out" in retained.stderr, "timeout logs unavailable")
    result = run("python3", "-c",
                 "import subprocess; subprocess.Popen(['/bin/sleep','10'])",
                 options=("--timeout", "1"))
    require(result.returncode == 124, "descendant output pipes escaped runtime deadline")
    require(run("/bin/true").returncode == 0, "reservation not released after timeout")
    result = run("/bin/true", options=("--cpu", "1048576"))
    require(result.returncode != 0 and b"no available compatible worker" in result.stderr,
            "impossible job not rejected after queue deadline")

    listed = subprocess.check_output([binary, "ps", "--port", str(port)], text=True, timeout=5)
    require("\tUNSCHEDULABLE\t" in listed, "queue expiry state not retained")

    # One worker must not overlap jobs even if advertised CPU capacity is large.
    with tempfile.TemporaryDirectory() as folder:
        lock = os.path.join(folder, "lock")
        script = ("import os,sys,time; p=sys.argv[1]; "
                  "os.mkdir(p); time.sleep(.3); os.rmdir(p)")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: run("python3", "-c", script, lock), range(4)))
        require(all(r.returncode == 0 for r in results), "worker jobs overlapped or queue stuck")

    second = start("join", "--id", "executor-b")
    wait_for(lambda: cluster().count("READY") == 2, "second executor did not join")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run("python3", "-c",
            "import os,time; print(os.getppid()); time.sleep(1)"), range(2)))
    require(all(r.returncode == 0 for r in results) and
            {int(r.stdout) for r in results} == {worker.pid, second.pid},
            "concurrent jobs were not distributed across both worker processes")
    second.terminate()
    second.wait(timeout=7)
    wait_for(lambda: any(row.startswith("executor-b\t") and row.endswith("DEAD")
                         for row in cluster().splitlines()), "second worker did not disconnect")

    # Kill a worker after its child has started. No automatic retry is promised.
    with tempfile.TemporaryDirectory() as folder:
        marker = os.path.join(folder, "started")
        script = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(20)"
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending = pool.submit(run, "python3", "-c", script, marker)
            wait_for(lambda: os.path.exists(marker) and os.path.getsize(marker) > 0, "job never started")
            with open(marker) as source:
                child_pid = int(source.read())
            worker.kill()
            worker.wait(timeout=3)
            result = pending.result(timeout=7)
            require(result.returncode != 0 and b"worker lost" in result.stderr, "worker loss left job waiting")
            listed = subprocess.check_output([binary, "ps", "--port", str(port)], text=True, timeout=5)
            require("\tLOST\texecutor-a\t" in listed, "lost-worker state not retained")
            # SIGKILL cannot run worker cleanup. Clean the test child explicitly.
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    replacement = start("join", "--id", "executor-a")
    wait_for(lambda: "READY" in cluster(), "replacement did not join")
    require(run("/usr/bin/printf", "recovered").stdout == b"recovered", "capacity stuck after worker loss")

    print("Execution integration passed: argv, output, exits, bounds, heartbeats, reservations, worker loss")
finally:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=7)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
