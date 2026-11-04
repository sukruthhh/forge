"""Verify Forge over a private Docker bridge; this is not a physical-machine benchmark."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="build/recovery/forge", help="Linux binary relative to the repository")
    parser.add_argument("--image", default="forge-dev")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    relative = Path(args.binary)
    if relative.is_absolute() or ".." in relative.parts or not (repository / relative).is_file():
        parser.error("--binary must name an existing Linux binary inside the repository")
    binary = "/src/" + relative.as_posix()
    network = "forge-test-" + uuid.uuid4().hex[:12]
    created = []

    def docker(*arguments, check=True):
        result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=60)
        if check and result.returncode:
            raise RuntimeError(f"docker {arguments[0]} failed: {result.stderr.strip()}")
        return result

    def execute(container, role, *arguments, check=True):
        folder = "/tmp/credentials" if role == "server" else "/tmp"
        return docker("exec", "-e", f"FORGE_TLS_CA={folder}/ca.crt",
                      "-e", f"FORGE_TLS_CERT={folder}/{role}.crt",
                      "-e", f"FORGE_TLS_KEY={folder}/{role}.key", container, *arguments, check=check)

    def wait_for(predicate, message):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.1)
        raise RuntimeError(message)

    docker("network", "create", network)
    try:
        names = [network + suffix for suffix in ("-coordinator", "-worker-a", "-worker-b", "-cli")]
        for name in names:
            docker("run", "--rm", "-d", "--init", "--name", name, "--network", network,
                   "--mount", f"type=bind,source={repository},target=/src,readonly",
                   args.image, "sleep", "300")
            created.append(name)
        coordinator, worker_a, worker_b, client = names
        host = docker("inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", coordinator).stdout.strip()
        docker("exec", coordinator, "python3", "/src/scripts/create-dev-certificates.py",
               "--output", "/tmp/credentials", "--server-ip", host)
        with tempfile.TemporaryDirectory(prefix="forge-test-") as temporary:
            for filename in ("ca.crt", "client.crt", "client.key"):
                local = str(Path(temporary) / filename)
                docker("cp", f"{coordinator}:/tmp/credentials/{filename}", local)
                for name in (worker_a, worker_b, client):
                    docker("cp", local, f"{name}:/tmp/{filename}")
        docker("exec", "-d", "-e", "FORGE_TLS_CA=/tmp/credentials/ca.crt",
               "-e", "FORGE_TLS_CERT=/tmp/credentials/server.crt", "-e", "FORGE_TLS_KEY=/tmp/credentials/server.key",
               coordinator, binary, "coordinator", "--bind", "0.0.0.0")

        def cluster():
            return execute(client, "client", binary, "cluster", "--host", host, check=False)

        wait_for(lambda: cluster().returncode == 0, "coordinator unavailable across bridge")
        for name, worker_id in ((worker_a, "bridge-a"), (worker_b, "bridge-b")):
            docker("exec", "-d", "-e", "FORGE_TLS_CA=/tmp/ca.crt", "-e", "FORGE_TLS_CERT=/tmp/client.crt",
                   "-e", "FORGE_TLS_KEY=/tmp/client.key", name, binary, "join", "--host", host, "--id", worker_id)
        wait_for(lambda: cluster().stdout.count("READY") == 2, "bridge workers did not register")
        expected = {docker("inspect", "--format", "{{.Config.Hostname}}", name).stdout.strip()
                    for name in (worker_a, worker_b)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: execute(client, "client", binary, "run", "--host", host, "--",
                "python3", "-c", "import socket,time; print(socket.gethostname()); time.sleep(2)"), range(2)))
        if {result.stdout.strip() for result in results} != expected:
            raise RuntimeError("jobs did not execute in both worker containers")
        if "SUCCEEDED" not in execute(client, "client", binary, "ps", "--host", host).stdout:
            raise RuntimeError("bridge job inspection failed")
        if execute(client, "client", binary, "logs", "job-1", "--host", host).stdout.strip() not in expected:
            raise RuntimeError("bridge log retrieval failed")
        analysis = ("import json,os,pathlib,socket,sys,time\n"
                    "print(json.dumps({'host':socket.gethostname(),'cwd':os.getcwd(),"
                    "'value':pathlib.Path(sys.argv[1]).read_text(),'source_visible':pathlib.Path('/tmp/batch-inputs').exists()}))\n"
                    "time.sleep(.8)\n")
        docker("exec", client, "python3", "-c",
               "import sys; from pathlib import Path; p=Path('/tmp/batch-inputs'); p.mkdir(); "
               "[(p/f'{i}.txt').write_text(str(i)) for i in range(4)]; "
               "(p/'analyze.py').write_text(sys.argv[1])", analysis)
        execute(client, "client", binary, "map", "--host", host, "--arch", "any",
                "--file", "/tmp/batch-inputs/analyze.py", "--output", "/tmp/batch-results",
                *[f"/tmp/batch-inputs/{i}.txt" for i in range(4)], "--", "python3", "analyze.py", "{}")
        batch = json.loads(docker("exec", client, "python3", "-c",
            "import json,pathlib; p=pathlib.Path('/tmp/batch-results'); "
            "rows=[json.loads(s) for s in (p/'results.jsonl').read_text().splitlines()]; "
            "assert all(r['state']=='SUCCEEDED' for r in rows); "
            "print(json.dumps([json.loads((p/r['stdout']).read_text()) for r in rows]))").stdout)
        if {row["host"] for row in batch} != expected or [row["value"] for row in batch] != ["0", "1", "2", "3"]:
            raise RuntimeError("staged map did not distribute or preserve input/result association")
        if any(row["source_visible"] for row in batch):
            raise RuntimeError("worker could see CLI fixture directory; staging test is invalid")
        for name in (worker_a, worker_b):
            hostname = docker("inspect", "--format", "{{.Config.Hostname}}", name).stdout.strip()
            paths = [row["cwd"] for row in batch if row["host"] == hostname]
            docker("exec", name, "python3", "-c",
                   "import pathlib,sys; assert not any(pathlib.Path(p).exists() for p in sys.argv[1:])", *paths)
        execute(client, "client", "python3", "/src/benchmarks/run.py", "--binary", binary,
                "--output", "/tmp/benchmark-results", "--environment-kind", "container", "--label", "CI private-bridge smoke",
                "--purpose", "smoke", "--host", host, "--cluster-kind", "same-host-containers", "--workers", "2",
                "--tasks", "2", "--limit", "1000", "--step", "100", "--repetitions", "1", "--warmups", "0")
        benchmark = json.loads(docker("exec", client, "cat", "/tmp/benchmark-results/summary.json").stdout)
        if not benchmark["valid"] or not benchmark["profiles"]["forge-2"]["valid"]:
            raise RuntimeError("external TLS benchmark smoke did not validate its results")
        plaintext = docker("exec", client, binary, "cluster", "--host", host, check=False)
        if plaintext.returncode == 0 or "require mutual TLS" not in plaintext.stderr:
            raise RuntimeError("remote plaintext policy bypassed")
        cancel_script = ("import json,os,pathlib,time\n"
                         "pathlib.Path('/tmp/forge-cancel-started.json').write_text(json.dumps({'cwd':os.getcwd()}))\n"
                         "time.sleep(20)\n")
        docker("exec", client, "python3", "-c",
               "import pathlib,sys; pathlib.Path('/tmp/cancel.py').write_text(sys.argv[1])", cancel_script)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(execute, client, "client", binary, "run", "--host", host,
                                  "--retries", "1", "--file", "/tmp/cancel.py", "--", "python3", "cancel.py", check=False)
            started = []
            def cancellation_started():
                started[:] = [name for name in (worker_a, worker_b)
                              if docker("exec", name, "test", "-s", "/tmp/forge-cancel-started.json", check=False).returncode == 0]
                return len(started) == 1
            wait_for(cancellation_started, "staged cancellation fixture never started")
            owner = started[0]
            workspace = json.loads(docker("exec", owner, "cat", "/tmp/forge-cancel-started.json").stdout)["cwd"]
            listing = execute(client, "client", binary, "ps", "--host", host).stdout
            active = [row.split("\t") for row in listing.splitlines()[1:] if "\tRUNNING\t" in row]
            if len(active) != 1:
                raise RuntimeError("cancellation fixture did not own exactly one running job")
            job_id = active[0][0]
            execute(client, "client", binary, "cancel", job_id, "--host", host)
            cancelled = pending.result(timeout=15)
            if cancelled.returncode != 130 or "CANCELLED cleanup=reported" not in cancelled.stderr:
                raise RuntimeError("mutual-TLS cancellation did not report completed cleanup")
            docker("exec", owner, "python3", "-c", "import pathlib,sys; assert not pathlib.Path(sys.argv[1]).exists()", workspace)
            attempts = execute(client, "client", binary, "attempts", job_id, "--host", host)
            if "attempts=1/2" not in attempts.stderr or "\tCANCELLED\t" not in attempts.stdout:
                raise RuntimeError("cancelled TLS attempt was retried or not retained")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(execute, client, "client", binary, "run", "--host", host,
                                  "--retries", "1", "--timeout", "10", "--", "python3", "-c",
                                  "import socket,time; time.sleep(4); print(socket.gethostname())")
            active = []
            def active_job():
                listing = execute(client, "client", binary, "ps", "--host", host).stdout
                active[:] = [row.split("\t") for row in listing.splitlines()[1:] if "\tRUNNING\t" in row]
                return len(active) == 1
            wait_for(active_job, "retry fixture never entered RUNNING")
            job = active[0]
            victim = {"bridge-a": worker_a, "bridge-b": worker_b}[job[2]]
            survivor = worker_b if victim == worker_a else worker_a
            docker("stop", "--time", "1", victim)
            recovered = pending.result(timeout=25)
            survivor_host = docker("inspect", "--format", "{{.Config.Hostname}}", survivor).stdout.strip()
            if recovered.stdout.strip() != survivor_host:
                raise RuntimeError("lost-worker job did not complete on the surviving container")
            attempts = execute(client, "client", binary, "attempts", job[0], "--host", host)
            if "attempts=2/2" not in attempts.stderr or "\tLOST\t" not in attempts.stdout or "\tSUCCEEDED\t" not in attempts.stdout:
                raise RuntimeError("cross-container recovery did not retain both execution attempts")
        wait_for(lambda: "DEAD" in cluster().stdout, "worker-container loss not detected")
        execute(client, "client", binary, "run", "--host", host, "--", "/bin/true")
        print("PASS: mutual TLS across four containers; staged map, ordered results, cancellation and cleanup, external benchmark smoke, ps/logs, active-job recovery, attempt history, remaining capacity.")
        print("This verifies separate network namespaces on one Docker host, not separate physical machines or performance.")
    finally:
        for name in reversed(created):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", network, check=False)


if __name__ == "__main__":
    main()
