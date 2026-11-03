"""Verify Forge over a private Docker bridge; this is not a physical-machine benchmark."""
import argparse
import concurrent.futures
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
        plaintext = docker("exec", client, binary, "cluster", "--host", host, check=False)
        if plaintext.returncode == 0 or "require mutual TLS" not in plaintext.stderr:
            raise RuntimeError("remote plaintext policy bypassed")
        docker("stop", "--time", "2", worker_a)
        wait_for(lambda: "DEAD" in cluster().stdout, "worker-container loss not detected")
        execute(client, "client", binary, "run", "--host", host, "--", "/bin/true")
        print("PASS: mutual TLS across four containers on a private bridge; two-worker placement, results, ps/logs, loss, remaining capacity.")
        print("This verifies separate network namespaces on one Docker host, not separate physical machines or performance.")
    finally:
        for name in reversed(created):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", network, check=False)


if __name__ == "__main__":
    main()
