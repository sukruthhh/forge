"""Exercise pending heartbeats, legacy results, timing validation and worker wakeups."""
import concurrent.futures
import json
import platform
import select
import socket
import struct
import subprocess
import sys
import time

binary = sys.argv[1]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def frame(kind, fields=()):
    payload = bytes((1, kind)) + struct.pack("!I", len(fields))
    for field in fields:
        data = field.encode()
        payload += struct.pack("!I", len(data)) + data
    return struct.pack("!I", len(payload)) + payload


def exact(sock, length):
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise EOFError("peer closed")
        data += chunk
    return data


def receive(sock):
    size, = struct.unpack("!I", exact(sock, 4))
    require(6 <= size <= 65536, "invalid frame size")
    data = exact(sock, size)
    count, = struct.unpack("!I", data[2:6])
    fields, offset = [], 6
    for _ in range(count):
        length, = struct.unpack("!I", data[offset:offset + 4])
        offset += 4
        fields.append(data[offset:offset + length].decode())
        offset += length
    require(data[0] == 1 and offset == size, "invalid frame")
    return data[1], fields


with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]


def connect():
    return socket.create_connection(("127.0.0.1", port), timeout=5)


def request(kind, fields=()):
    with connect() as sock:
        sock.sendall(frame(kind, fields))
        return receive(sock)


def cli(*arguments):
    return subprocess.run([binary, *arguments, "--port", str(port)], capture_output=True, text=True, timeout=8)


def submit():
    return request(7, ["1", "0", "5", "linux", platform.machine(), "/bin/true"])


server = subprocess.Popen([binary, "coordinator", "--port", str(port)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
worker = None
try:
    deadline = time.monotonic() + 5
    while True:
        try:
            request(3)
            break
        except OSError:
            require(time.monotonic() < deadline, "coordinator failed to start")
            time.sleep(.025)
    require(request(18)[0] == 6 and request(18, ["missing"])[0] == 6, "bad timing request accepted")
    with concurrent.futures.ThreadPoolExecutor() as pool:
        for profiling in (False, True):
            with connect() as sock:
                caps = ["stub-" + str(profiling), "linux", platform.machine(), "1", "4096", "0", "staging-v1", "longpoll-v1"]
                if profiling:
                    caps.append("timing-v1")
                sock.sendall(frame(1, caps))
                require(receive(sock) == (4, []), "feature registration failed")
                sock.sendall(frame(2))
                require(not select.select([sock], [], [], .1)[0], "idle heartbeat was not held")
                pending = pool.submit(submit)
                kind, assignment = receive(sock)
                require(kind == 8, "pending heartbeat did not receive assignment")
                kind, active = request(18, [assignment[0]])
                require(kind == 19 and active[1] == "RUNNING" and active[5:] == [""] * 5,
                        "active timing intervals fabricated")
                result = [assignment[0], "0", "done", "", "0", "0"]
                sock.sendall(frame(20 if profiling else 9, result + (["10", "20", "30", "40"] if profiling else [])))
                require(receive(sock) == (4, []), "result not acknowledged")
                require(pending.result(timeout=5) == (9, result), "timing extension leaked into ordinary result")
                kind, timing = request(18, [assignment[0]])
                require(kind == 19 and timing[1] == "SUCCEEDED" and all(x.isdigit() for x in timing[2:6]),
                        "coordinator intervals missing")
                require(timing[6:] == (["10", "20", "30", "40"] if profiling else [""] * 4),
                        "legacy or reported worker intervals changed")
                if profiling:
                    pending = pool.submit(submit)
                    sock.sendall(frame(2))
                    kind, assignment = receive(sock)
                    require(kind == 8, "second assignment missing")
                    sock.sendall(frame(20, [assignment[0], "0", "", "", "0", "0", "-1", "0", "0", "0"]))
                    require(receive(sock)[0] == 6 and pending.result(timeout=5)[0] == 6,
                            "invalid timing result was accepted")
    worker = subprocess.Popen([binary, "join", "--id", "real", "--port", str(port)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 5
    while "real\t" not in cli("cluster").stdout:
        require(time.monotonic() < deadline, "real worker did not register")
        time.sleep(.025)
    kind, result = request(7, ["1", "0", "5", "linux", platform.machine(), "/bin/sleep", "1.2"])
    require(kind == 9 and result[1] == "0", "running heartbeats or completion failed")
    report = cli("timings", result[0])
    timing = json.loads(report.stdout)
    require(report.returncode == 0 and timing["state"] == "SUCCEEDED"
            and timing["worker_execute_seconds"] >= 1.2
            and all(isinstance(value, (float, int)) and value >= 0 for key, value in timing.items() if key.endswith("_seconds")),
            "real worker phase measurements invalid")
    for _ in range(4):
        require(submit()[0] == 9, "completion notification lost between successive tasks")
    worker.terminate()
    require(worker.wait(timeout=4) == 0, "worker did not stop during pending heartbeat")
    print("Dispatch checks passed: pending heartbeat, legacy results, active/terminal timings, invalid telemetry, running heartbeats, repeated completion, shutdown")
finally:
    for process in (worker, server):
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
