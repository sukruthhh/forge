"""Placement against simulated architecture advertisements, not cross-architecture execution."""
import os
import platform
import socket
import struct
import subprocess
import sys
import threading
import time

binary = sys.argv[1]
environment = {k: v for k, v in os.environ.items() if not k.startswith("FORGE_TLS_") and k != "FORGE_TOKEN"}
native = platform.machine()
foreign = "aarch64" if native != "aarch64" else "x86_64"
stopped = threading.Event()
errors = []
threads, sockets = [], []
assignments = []


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def frame(kind, fields=()):
    data = bytes((1, kind)) + struct.pack("!I", len(fields))
    for field in fields:
        value = field.encode()
        data += struct.pack("!I", len(value)) + value
    return struct.pack("!I", len(data)) + data


def exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise EOFError("peer closed")
        data += chunk
    return data


def receive(sock):
    size, = struct.unpack("!I", exact(sock, 4))
    require(6 <= size <= 65536, "invalid response length")
    data = exact(sock, size)
    count, = struct.unpack("!I", data[2:6])
    fields, offset = [], 6
    for _ in range(count):
        length, = struct.unpack("!I", data[offset:offset + 4])
        offset += 4
        fields.append(data[offset:offset + length].decode())
        offset += length
    require(offset == size, "invalid response framing")
    return data[1], fields


with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = str(probe.getsockname()[1])


def connect():
    return socket.create_connection(("127.0.0.1", int(port)), timeout=5)


def request(kind, fields=()):
    with connect() as sock:
        sock.sendall(frame(kind, fields))
        return receive(sock)


def worker(sock, worker_id):
    try:
        while not stopped.is_set():
            sock.sendall(frame(2))
            kind, fields = receive(sock)
            if kind == 8:
                assignments.append(worker_id)
                sock.sendall(frame(9, [fields[0], "0", worker_id, "", "0", "0"]))
                require(receive(sock)[0] == 4, "result not acknowledged")
            else:
                require(kind == 4, "unexpected heartbeat response")
            stopped.wait(.01)
    except Exception as error:
        if not stopped.is_set():
            errors.append(error)


def run(*options):
    return subprocess.run([binary, "run", "--port", port, *options, "--", "/bin/example"],
                          env=environment, capture_output=True, text=True, timeout=10)


server = subprocess.Popen([binary, "coordinator", "--port", port], env=environment,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    deadline = time.monotonic() + 5
    while True:
        try:
            require(request(3)[0] == 5, "startup query failed")
            break
        except OSError:
            require(time.monotonic() < deadline, "coordinator did not start")
            time.sleep(.05)
    for worker_id, architecture in (("a-foreign", foreign), ("z-native", native)):
        sock = connect()
        sockets.append(sock)
        sock.sendall(frame(1, [worker_id, "linux", architecture, "4", "4096", "0"]))
        require(receive(sock)[0] == 4, "stub registration failed")
        thread = threading.Thread(target=worker, args=(sock, worker_id))
        threads.append(thread)
        thread.start()

    for options, expected in (((), "z-native"), (("--arch", "native"), "z-native"),
                              (("--arch", foreign), "a-foreign"), (("--arch", "any"), "a-foreign"),
                              (("--arch", native), "z-native")):
        result = run(*options)
        require(result.returncode == 0 and result.stdout == expected,
                f"incorrect placement for {options}: {result.stderr} {result.stdout}")
    before = len(assignments)
    result = run("--arch", "forge-unsupported-arch")
    require(result.returncode != 0 and "no available compatible worker" in result.stderr,
            "unsupported architecture was placed on an incompatible worker")
    for invalid in ("", "bad arch", "x" * 65, "arm64/../../", "*", "\n"):
        result = run("--arch", invalid)
        require(result.returncode != 0 and "architecture must be" in result.stderr,
                "invalid architecture not rejected by CLI")
        require(request(7, ["1", "0", "1", "linux", invalid or "bad arch", "/bin/example"])[0] == 6,
                "invalid architecture not rejected by coordinator")
    require(run("--arch", native, "--arch", foreign).returncode != 0,
            "repeated architecture option accepted")
    require(len(assignments) == before, "rejected architecture requests reached a worker")
    require(not errors, str(errors))
    require(server.poll() is None, "invalid client killed coordinator")
    print("Architecture checks passed: default/native/exact/any placement, incompatibility, validation")
finally:
    stopped.set()
    for thread in threads:
        thread.join(timeout=6)
    for sock in sockets:
        sock.close()
    server.terminate()
    try:
        server.wait(timeout=7)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
