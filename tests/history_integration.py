"""Real coordinator history retention test with a fast deterministic worker stub."""
import socket, struct, subprocess, sys, threading, time

binary = sys.argv[1]
stop = threading.Event()
errors = []

def require(ok, message):
    if not ok:
        raise AssertionError(message)

def frame(kind, fields=()):
    payload = bytes((1, kind)) + struct.pack("!I", len(fields))
    for field in fields:
        data = field.encode()
        payload += struct.pack("!I", len(data)) + data
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
    require(6 <= size <= 65536, "bad response size")
    data = exact(sock, size)
    count, = struct.unpack("!I", data[2:6])
    fields, offset = [], 6
    for _ in range(count):
        length, = struct.unpack("!I", data[offset:offset+4])
        offset += 4
        fields.append(data[offset:offset+length].decode())
        offset += length
    require(offset == size, "bad framing")
    return data[1], fields

with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]

def connect():
    return socket.create_connection(("127.0.0.1", port), timeout=5)

def request(kind, fields=()):
    with connect() as client:
        client.sendall(frame(kind, fields))
        return receive(client)

def worker(sock):
    try:
        while not stop.is_set():
            sock.sendall(frame(2))
            kind, fields = receive(sock)
            if kind == 8:
                sock.sendall(frame(9, [fields[0], "7", "retained out", "retained err", "0", "0"]))
                require(receive(sock)[0] == 4, "result not acknowledged")
            else:
                require(kind == 4, "unexpected heartbeat reply")
            stop.wait(.005)
    except Exception as error:
        if not stop.is_set():
            errors.append(error)

server = subprocess.Popen([binary, "coordinator", "--port", str(port)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
thread, sock = None, None
try:
    deadline = time.monotonic() + 5
    while True:
        try:
            sock = connect()
            break
        except OSError:
            require(time.monotonic() < deadline, "server startup failed")
            time.sleep(.05)
    require(request(10) == (12, []), "new history not empty")
    sock.sendall(frame(1, ["stub", "linux", "x86_64", "4", "4096", "0"]))
    require(receive(sock)[0] == 4, "registration failed")
    thread = threading.Thread(target=worker, args=(sock,))
    thread.start()
    ids = []
    for _ in range(70):
        kind, fields = request(7, ["1", "0", "5", "linux", "x86_64", "/bin/example"])
        require(kind == 9 and fields[1] == "7", "job did not finish")
        ids.append(fields[0])
    kind, rows = request(10)
    require(kind == 12 and len(rows) == 64*5, "history not bounded to 64")
    require(ids[0] not in rows[::5] and ids[-1] in rows[::5], "wrong eviction order")
    require(request(11, [ids[0]])[0] == 6, "evicted logs should fail")
    require(request(11, ["missing"])[0] == 6, "unknown logs should fail")
    require(request(10, ["unexpected"])[0] == 6, "invalid ps schema accepted")
    require(request(11)[0] == 6, "invalid logs schema accepted")
    result = subprocess.run([binary, "logs", ids[-1], "--port", str(port)],
                            capture_output=True, text=True, timeout=5)
    require(result.returncode == 0 and result.stdout == "retained out"
            and "retained err" in result.stderr and "exit=7" in result.stderr,
            "failed-job retrieval should succeed without rerunning")
    require(all(state == "FAILED" for state in rows[1::5]), "failed state missing")
    require(not errors, str(errors))
    print("History checks passed: retention, eviction, failed logs, invalid schemas")
finally:
    stop.set()
    if thread:
        thread.join(timeout=6)
    if sock:
        sock.close()
    server.terminate()
    try:
        server.wait(timeout=7)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()