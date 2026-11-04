"""Mutual TLS admission, endpoint policy, deadlines, execution, and disconnects."""
import concurrent.futures
import importlib.util
import os
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time

binary = os.path.abspath(sys.argv[1])
processes = []
base_env = {k: v for k, v in os.environ.items() if not k.startswith("FORGE_TLS_") and k != "FORGE_TOKEN"}
spec = importlib.util.spec_from_file_location("certificates", Path(__file__).parents[1] / "scripts/create-dev-certificates.py")
certificates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(certificates)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def port_number():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def command(*args, env=base_env):
    return subprocess.run([binary, *args], env=env, capture_output=True, timeout=10)


def start(*args, env):
    process = subprocess.Popen([binary, *args], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    processes.append(process)
    return process


def wait_for(predicate, message):
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError(message)


def environment(folder, role):
    return dict(base_env, FORGE_TLS_CA=str(folder / "ca.crt"),
                FORGE_TLS_CERT=str(folder / f"{role}.crt"), FORGE_TLS_KEY=str(folder / f"{role}.key"))


def exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("peer closed")
        data += chunk
    return data


def receive(sock):
    size, = struct.unpack("!I", exact(sock, 4))
    require(6 <= size <= 65536, "response frame length invalid")
    return exact(sock, size)


def frame(kind):
    return struct.pack("!IBBI", 6, 1, kind, 0)


try:
    with tempfile.TemporaryDirectory() as temporary:
        trusted, rogue = Path(temporary) / "trusted", Path(temporary) / "rogue"
        certificates.generate(trusted, "127.0.0.1")
        certificates.generate(rogue, "127.0.0.1")
        server_env, client_env = environment(trusted, "server"), environment(trusted, "client")
        port = str(port_number())
        server = start("coordinator", "--bind", "0.0.0.0", "--port", port, env=server_env)

        def cluster(env=client_env, host="127.0.0.1"):
            return command("cluster", "--host", host, "--port", port, env=env)

        wait_for(lambda: cluster().returncode == 0, "TLS coordinator did not start")
        require(cluster(base_env).returncode != 0, "plaintext reached TLS coordinator")
        require(cluster(environment(rogue, "client")).returncode != 0, "untrusted server CA accepted")
        rogue_client = dict(client_env, FORGE_TLS_CERT=str(rogue / "client.crt"), FORGE_TLS_KEY=str(rogue / "client.key"))
        require(cluster(rogue_client).returncode != 0, "untrusted client certificate accepted")
        require(cluster(host="127.0.0.2").returncode != 0, "coordinator IP SAN mismatch accepted")
        require(cluster(server_env).returncode != 0, "server-only certificate accepted as client")
        subprocess.run(["openssl", "x509", "-req", "-in", str(trusted / "client.csr"),
                        "-CA", str(trusted / "ca.crt"), "-CAkey", str(trusted / "ca.key"),
                        "-CAcreateserial", "-out", str(trusted / "expired.crt"), "-days", "0",
                        "-extfile", str(trusted / "client.ext")], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        time.sleep(1.1)
        require(cluster(dict(client_env, FORGE_TLS_CERT=str(trusted / "expired.crt"))).returncode != 0,
                "expired client certificate accepted")

        no_certificate = ssl.create_default_context(cafile=str(trusted / "ca.crt"))
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=4) as raw:
                with no_certificate.wrap_socket(raw, server_hostname="127.0.0.1") as peer:
                    peer.sendall(frame(3))
                    require(not peer.recv(1), "client without certificate received application data")
        except (ssl.SSLError, ConnectionError):
            pass

        old_tls = ssl.create_default_context(cafile=str(trusted / "ca.crt"))
        old_tls.maximum_version = ssl.TLSVersion.TLSv1_2
        old_tls.load_cert_chain(str(trusted / "client.crt"), str(trusted / "client.key"))
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=4) as raw:
                with old_tls.wrap_socket(raw, server_hostname="127.0.0.1"):
                    raise AssertionError("TLS 1.2 downgrade accepted")
        except ssl.SSLError:
            pass

        context = ssl.create_default_context(cafile=str(trusted / "ca.crt"))
        context.load_cert_chain(str(trusted / "client.crt"), str(trusted / "client.key"))
        with socket.create_connection(("127.0.0.1", int(port)), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="127.0.0.1") as peer:
                require(peer.version() == "TLSv1.3", "TLS version policy not enforced")
                peer.sendall(frame(3)[:1])
                require(receive(peer)[1] == 6, "TLS partial-frame deadline not enforced")

        with socket.create_connection(("127.0.0.1", int(port)), timeout=5) as stalled:
            started = time.monotonic()
            require(stalled.recv(1) == b"", "silent TLS handshake not closed")
            require(time.monotonic() - started < 4.5, "TLS handshake deadline exceeded bound")

        with socket.socket() as silent_server:
            silent_server.bind(("127.0.0.1", 0))
            silent_server.listen()
            started = time.monotonic()
            result = command("cluster", "--port", str(silent_server.getsockname()[1]), env=client_env)
            require(result.returncode != 0 and time.monotonic() - started < 4.5,
                    "TLS client handshake escaped deadline")

        workers = [start("join", "--host", "127.0.0.1", "--id", name, "--port", port, env=client_env)
                   for name in ("tls-a", "tls-b")]
        wait_for(lambda: cluster().stdout.count(b"READY") == 2, "TLS workers did not register")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: command("run", "--host", "127.0.0.1", "--port", port, "--",
                "python3", "-c", "import os,time; print(os.getppid()); time.sleep(1)", env=client_env), range(2)))
        require(all(r.returncode == 0 for r in results) and
                {int(r.stdout) for r in results} == {w.pid for w in workers}, "TLS placement did not use both workers")
        result = command("run", "--port", port, "--arch", "any", "--", "python3", "-c",
                         "import sys; sys.stdout.buffer.write(b'a\\x00b'+b'x'*20000); sys.stderr.write('err'); sys.exit(7)", env=client_env)
        require(result.returncode == 7 and len(result.stdout) == 16384 and result.stdout.startswith(b"a\x00b")
                and b"err" in result.stderr and b"truncated" in result.stderr, "TLS binary output/exit/bounds damaged")
        result = command("run", "--port", port, "--timeout", "1", "--", "/bin/sleep", "10", env=client_env)
        require(result.returncode == 124, "TLS execution timeout failed")
        require(b"TIMED_OUT" in command("ps", "--port", port, env=client_env).stdout, "TLS ps failed")
        require(command("logs", "job-3", "--port", port, env=client_env).stdout.startswith(b"a\x00b"), "TLS logs damaged")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(command, "run", "--port", port, "--", "/bin/sleep", "10", env=client_env)
            def jobs():
                return command("ps", "--port", port, env=client_env).stdout.decode()
            wait_for(lambda: "\tRUNNING\t" in jobs(), "TLS job did not enter RUNNING")
            running_worker = next(row.split("\t")[2] for row in jobs().splitlines() if "\tRUNNING\t" in row)
            lost = workers[0 if running_worker == "tls-a" else 1]
            lost.terminate()
            require(lost.wait(timeout=7) == 0, "TLS worker shutdown failed")
            result = pending.result(timeout=7)
            require(result.returncode != 0 and b"worker lost" in result.stderr,
                    "TLS worker loss did not fail active job")
        wait_for(lambda: b"DEAD" in cluster().stdout,
                 "TLS disconnection did not release worker session")
        require(command("run", "--port", port, "--", "/bin/true", env=client_env).returncode == 0,
                "remaining TLS worker capacity stuck after loss")

        token_port = str(port_number())
        token = "forge-tls-token-0123456789abcdef0123456789"
        token_server = start("coordinator", "--port", token_port, env=dict(server_env, FORGE_TOKEN=token))
        wait_for(lambda: command("cluster", "--port", token_port, env=dict(client_env, FORGE_TOKEN=token)).returncode == 0,
                 "TLS plus token did not work")
        require(command("cluster", "--port", token_port, env=client_env).returncode != 0, "TLS bypassed token gate")

        for args in (("coordinator", "--bind", "0.0.0.0"), ("cluster", "--host", "192.0.2.1")):
            result = command(*args)
            require(result.returncode != 0 and b"require mutual TLS" in result.stderr, "remote plaintext policy bypassed")
        for env in (dict(base_env, FORGE_TLS_CA=""), dict(base_env, FORGE_TLS_CERT=str(trusted / "client.crt")),
                    dict(client_env, FORGE_TLS_KEY=str(rogue / "client.key"))):
            require(command("coordinator", "--port", str(port_number()), env=env).returncode != 0,
                    "incomplete or mismatched TLS configuration accepted")
        subprocess.run(["openssl", "pkey", "-in", str(trusted / "client.key"), "-out", str(trusted / "encrypted.key"),
                        "-aes-256-cbc", "-passout", "pass:fixture-only"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        result = command("cluster", "--port", port, env=dict(client_env, FORGE_TLS_KEY=str(trusted / "encrypted.key")))
        require(result.returncode != 0 and b"cannot configure TLS" in result.stderr,
                "encrypted private key did not fail without an interactive prompt")
        for args in (("cluster", "--host", "localhost"), ("coordinator", "--host", "127.0.0.1"),
                     ("cluster", "--bind", "127.0.0.1"), ("cluster", "--host", "127.0.0.1", "--host", "127.0.0.1")):
            require(command(*args).returncode != 0, "invalid endpoint option accepted")
        require(server.poll() is None and token_server.poll() is None, "bad TLS clients killed coordinator")
        print("TLS checks passed: certificates, admission, IP verification, deadlines, execution, logs, endpoint policy")
finally:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=7)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
