"""Real subprocess/TCP integration checks. No external Python dependencies."""
import concurrent.futures
import socket
import struct
import subprocess
import sys
import time

binary = sys.argv[1]
processes = []
sockets = []


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wait_for(predicate, message, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(0.05)
    raise AssertionError(message)


def frame(kind, fields=()):
    payload = bytes((1, kind)) + struct.pack("!I", len(fields))
    for field in fields:
        value = field.encode()
        payload += struct.pack("!I", len(value)) + value
    return struct.pack("!I", len(payload)) + payload


def exact(sock, count):
    result = b""
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise EOFError("peer closed")
        result += chunk
    return result


def response(sock):
    size, = struct.unpack("!I", exact(sock, 4))
    require(6 <= size <= 65536, "invalid response length")
    payload = exact(sock, size)
    require(payload[0] == 1, "invalid version")
    count, = struct.unpack("!I", payload[2:6])
    fields = []
    offset = 6
    for _ in range(count):
        length, = struct.unpack("!I", payload[offset:offset + 4])
        offset += 4
        fields.append(payload[offset:offset + length].decode())
        offset += length
    require(offset == len(payload), "invalid response framing")
    return payload[1], fields


with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]


def start(*args):
    process = subprocess.Popen([binary, *args, "--port", str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    processes.append(process)
    return process


def connect():
    sock = socket.create_connection(("127.0.0.1", port), timeout=4)
    sockets.append(sock)
    return sock


def cluster():
    return subprocess.check_output([binary, "cluster", "--port", str(port)],
                                   text=True, timeout=5)


try:
    server = start("coordinator")
    wait_for(lambda: "STATE" in cluster(), "coordinator did not start")
    workers = [start("join", "--id", name) for name in ("worker-a", "worker-b")]
    wait_for(lambda: cluster().count("READY") == 2, "workers did not register")
    time.sleep(2.2)
    require(cluster().count("READY") == 2, "heartbeats did not keep workers alive")

    duplicate = start("join", "--id", "worker-a")
    require(duplicate.wait(timeout=5) != 0, "duplicate live ID accepted")
    require(cluster().count("READY") == 2, "duplicate damaged original worker")

    # Fragmented input must be assembled; coalesced messages stay separate.
    raw = connect()
    registration = frame(1, ["raw", "linux", "x86_64", "2", "4096", "0"])
    for byte in registration:
        raw.sendall(bytes((byte,)))
    require(response(raw)[0] == 4, "fragmented registration failed")
    raw.sendall(frame(2) + frame(2))
    require(response(raw)[0] == 4 and response(raw)[0] == 4, "coalesced frames failed")
    wait_for(lambda: "raw\t" in cluster() and "SUSPECT" in cluster(), "missing heartbeat not suspected")
    wait_for(lambda: any(row.startswith("raw\t") and row.endswith("DEAD")
                         for row in cluster().splitlines()), "silent worker not declared dead")
    raw.close()

    for malformed in (
        struct.pack("!I", 65537),
        struct.pack("!I", 0),
        frame(99),
        b"\x00\x00\x00\x06\x02\x03\x00\x00\x00\x00",  # wrong version
        b"\x00\x00\x00\x06\x01\x03\x00\x00\x04\x01",  # too many fields
        b"\x00\x00\x00\x07\x01\x03\x00\x00\x00\x00x",  # trailing byte
        b"\x00\x00\x00\x0a\x01\x01\x00\x00\x00\x01\x00\x00\x00\x05",  # truncated field
        frame(1, ["bad", "linux", "x86_64", "0", "4096", "0"]),
        frame(7),  # incomplete submission
        frame(7, ["-1", "0", "1", "linux", "x86_64", "/bin/true"]),
        frame(7, ["1", "0", "31", "linux", "x86_64", "/bin/true"]),
    ):
        sock = connect()
        sock.sendall(malformed)
        require(response(sock)[0] == 6, "malformed message not rejected")
        sock.close()
    require(server.poll() is None, "malformed client killed server")

    # A partial frame cannot retain a handler indefinitely.
    partial = connect()
    partial.sendall(b"\x00")
    require(response(partial)[0] == 6, "partial-header deadline not enforced")
    partial.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        require(all("worker-a" in output for output in pool.map(lambda _: cluster(), range(120))),
                "concurrent inspection or slot reuse failed")

    workers[0].kill()
    workers[0].wait(timeout=3)
    wait_for(lambda: any(row.startswith("worker-a\t") and row.endswith("DEAD")
                         for row in cluster().splitlines()), "worker death not observed")
    replacement = start("join", "--id", "worker-a")
    wait_for(lambda: cluster().count("READY") == 2, "worker ID could not reconnect")
    require(replacement.poll() is None, "replacement worker exited")

    server.terminate()
    require(server.wait(timeout=7) == 0, "coordinator did not shut down cleanly")
    print("TCP integration passed: registration, heartbeats, inspection, malformed frames, deadlines, death, reconnect")
finally:
    for sock in sockets:
        sock.close()
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=7)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
