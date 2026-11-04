"""Credential checks must precede every coordinator operation."""
import os
import socket
import struct
import subprocess
import sys
import time

binary = sys.argv[1]
token = "forge-test-credential-0123456789abcdef"
environment = dict(os.environ, FORGE_TOKEN=token)
processes = []


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def frame(kind, fields=()):
    payload = bytes((1, kind)) + struct.pack("!I", len(fields))
    for field in fields:
        value = field.encode()
        payload += struct.pack("!I", len(value)) + value
    return struct.pack("!I", len(payload)) + payload


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
    require(6 <= size <= 65536, "invalid response size")
    return exact(sock, size)


with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]


def command(*args, env=environment):
    return subprocess.run([binary, *args], env=env, capture_output=True, timeout=8)


def start(*args):
    process = subprocess.Popen([binary, *args, "--port", str(port)], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    processes.append(process)
    return process


def wait_for(predicate, message):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError(message)


try:
    server = start("coordinator")
    wait_for(lambda: command("cluster", "--port", str(port)).returncode == 0,
             "authenticated coordinator did not start")
    for kind, fields in (
        (1, ["intruder", "linux", "x86_64", "2", "4096", "0"]),
        (3, []), (7, ["1", "0", "1", "linux", "x86_64", "/bin/true"]),
        (10, []), (11, ["job-1"]), (13, []),
        (14, ["1", "0", "1", "linux", "", "1", "input", "0", "data", "/bin/true"]),
        (16, []),
        (13, [token, "extra"]), (13, ["x" * len(token)]),
    ):
        with socket.create_connection(("127.0.0.1", port), timeout=4) as sock:
            sock.sendall(frame(kind, fields))
            reply = receive(sock)
            require(reply[1] == 6 and token.encode() not in reply,
                    "unauthenticated operation accepted or credential disclosed")
            require(sock.recv(1) == b"", "rejected connection remained open")

    missing = dict(os.environ)
    missing.pop("FORGE_TOKEN", None)
    for env in (missing, dict(environment, FORGE_TOKEN="x" * len(token))):
        require(command("cluster", "--port", str(port), env=env).returncode != 0,
                "missing or incorrect client credential accepted")
        require(command("join", "--id", "intruder", "--port", str(port), env=env).returncode != 0,
                "unauthorized worker registered")
    require(b"intruder" not in command("cluster", "--port", str(port)).stdout,
            "rejected registration changed registry")

    with socket.create_connection(("127.0.0.1", port), timeout=4) as sock:
        sock.sendall(frame(13, [token]))
        require(receive(sock)[1] == 4, "valid handshake rejected")
        sock.sendall(frame(13, [token]))
        require(receive(sock)[1] == 6, "repeated authentication accepted as operation")

    with socket.create_connection(("127.0.0.1", port), timeout=4) as sock:
        sock.sendall(frame(13, [token]) + frame(3))
        require(receive(sock)[1] == 4 and receive(sock)[1] == 5,
                "coalesced authentication and operation lost framing")

    with socket.create_connection(("127.0.0.1", port), timeout=4) as sock:
        sock.sendall(frame(13, [token]))
        require(receive(sock)[1] == 4, "valid handshake rejected")
        require(receive(sock)[1] == 6, "authenticated idle session escaped request deadline")

    worker = start("join", "--id", "authenticated-worker")
    wait_for(lambda: b"READY" in command("cluster", "--port", str(port)).stdout,
             "authenticated worker did not register")
    result = command("run", "--port", str(port), "--", "/usr/bin/printf", "authenticated output")
    require(result.returncode == 0 and result.stdout == b"authenticated output",
            "authenticated execution failed")
    require(b"SUCCEEDED" in command("ps", "--port", str(port)).stdout,
            "authenticated job inspection failed")
    require(command("logs", "job-1", "--port", str(port)).stdout == b"authenticated output",
            "authenticated logs failed")
    for invalid in ("", "short", "x" * 257, "x" * 31 + " ", "x" * 31 + "\n"):
        result = command("coordinator", "--port", str(port), env=dict(environment, FORGE_TOKEN=invalid))
        require(result.returncode != 0 and b"FORGE_TOKEN must contain" in result.stderr,
                "invalid credential configuration did not fail before binding")
    require(server.poll() is None and worker.poll() is None, "authentication checks damaged session")
    print("Authentication checks passed: gate, rejection, registration, execution, inspection, configuration")
finally:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=7)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
