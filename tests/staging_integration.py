"""Real staged execution and map results, plus hostile manifests and legacy admission."""
import concurrent.futures
import json
import os
import resource
import signal
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

binary = os.path.abspath(sys.argv[1])
env = {k: v for k, v in os.environ.items() if not k.startswith("FORGE_TLS_") and k != "FORGE_TOKEN"}
processes = []


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return str(sock.getsockname()[1])


def command(*args, port=None, seconds=45):
    options = ["--port", port] if port else []
    arguments = [args[0], args[1], *options, *args[2:]] if args[0] == "logs" else [args[0], *options, *args[1:]]
    return subprocess.run([binary, *arguments], env=env,
                          capture_output=True, timeout=seconds)


def start(*args, port):
    process = subprocess.Popen([binary, *args, "--port", port], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    processes.append(process)
    return process


def wait_for(predicate, message):
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError(message)


def frame(kind, fields=()):
    data = bytes((1, kind)) + struct.pack("!I", len(fields))
    for field in fields:
        value = field.encode() if isinstance(field, str) else field
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
    require(6 <= size <= 65536, "response exceeds frame bound")
    data = exact(sock, size)
    count, = struct.unpack("!I", data[2:6])
    fields, offset = [], 6
    for _ in range(count):
        length, = struct.unpack("!I", data[offset:offset + 4])
        offset += 4
        fields.append(data[offset:offset + length])
        offset += length
    require(offset == size, "response framing invalid")
    return data[1], fields


def request(port, kind, fields=()):
    with socket.create_connection(("127.0.0.1", int(port)), timeout=8) as sock:
        sock.sendall(frame(kind, fields))
        return receive(sock)


try:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        port = unused_port()
        server = start("coordinator", port=port)
        wait_for(lambda: command("cluster", port=port).returncode == 0, "coordinator did not start")
        workers = [start("join", "--id", name, port=port) for name in ("staging-a", "staging-b")]
        wait_for(lambda: command("cluster", port=port).stdout.count(b"READY") == 2, "workers did not register")
        kind, capabilities = request(port, 16)
        require(kind == 17 and capabilities[7::8] == [b"staging-v1"] * 2, "staging capabilities missing")
        payload = root / "data.bin"
        payload.write_bytes(b"hello\x00binary\xff")
        outside = root / "keep.txt"
        outside.write_text("preserve")
        script = ("import os,pathlib,sys; print(os.getcwd()); "
                  "print(oct(os.stat('.').st_mode & 511)); print(oct(os.stat('data.bin').st_mode & 511)); "
                  "sys.stdout.flush(); sys.stdout.buffer.write(pathlib.Path('data.bin').read_bytes()); "
                  "pathlib.Path('created').write_text('temporary'); os.symlink(sys.argv[1], 'outside-link')")
        result = command("run", "--file", str(payload), "--", "python3", "-c", script, str(outside), port=port)
        require(result.returncode == 0, result.stderr)
        cwd, directory_mode, file_mode, data = result.stdout.split(b"\n", 3)
        require(data == payload.read_bytes() and directory_mode == b"0o700" and file_mode == b"0o600",
                "staged bytes or permissions damaged")
        require(not Path(cwd.decode()).exists() and outside.read_text() == "preserve", "workspace cleanup followed link or leaked")
        for code, expected in (("import os,sys; print(os.getcwd()); sys.exit(7)", 7),
                               ("import os,time; print(os.getcwd(),flush=True); time.sleep(10)", 124)):
            result = command("run", "--timeout", "1", "--file", str(payload), "--", "python3", "-c", code, port=port)
            require(result.returncode == expected and not Path(result.stdout.strip().decode()).exists(),
                    "failed/timed-out task did not clean its workspace")
        executable = root / "hello.sh"
        executable.write_text("#!/bin/sh\nprintf staged-executable")
        executable.chmod(0o700)
        result = command("run", "--file", str(executable), "--", "./hello.sh", port=port)
        require(result.returncode == 0 and result.stdout == b"staged-executable", "staged executable mode failed")
        boundary = root / "boundary.bin"
        boundary.write_bytes(b"x" * 32768)
        result = command("run", "--file", str(boundary), "--", "python3", "-c",
                         "from pathlib import Path; print(len(Path('boundary.bin').read_bytes()))", port=port)
        require(result.returncode == 0 and result.stdout == b"32768\n", "staging data boundary rejected")
        manifest = ["16"]
        for index in range(16):
            manifest.extend([f"empty-{index}", "0", ""])
        require(request(port, 14, ["1", "0", "1", "linux", "", *manifest, "/bin/true"])[0] == 9,
                "16-file staging boundary rejected")

        shared = root / "analyze.py"
        shared.write_text("import json,os,pathlib,sys,time\n"
                          "print(json.dumps({'pid':os.getppid(),'cwd':os.getcwd(),'data':pathlib.Path(sys.argv[1]).read_bytes().hex()}))\n"
                          "time.sleep(.8)\n")
        inputs = [root / f"input-{i}.bin" for i in range(4)]
        for index, path in enumerate(inputs):
            path.write_bytes(bytes((index, 0, 255)))
        output = root / "results"
        result = command("map", "--arch", "any", "--file", str(shared), "--output", str(output),
                         *map(str, inputs), "--", "python3", "analyze.py", "{}", port=port)
        require(result.returncode == 0, result.stderr)
        rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
        require([row["index"] for row in rows] == [1, 2, 3, 4], "batch result order changed")
        contents = [json.loads((output / row["stdout"]).read_text()) for row in rows]
        require({row["pid"] for row in contents[:2]} == {worker.pid for worker in workers}, "map did not execute in parallel on both workers")
        require(len({row["cwd"] for row in contents}) == 4 and all(not Path(row["cwd"]).exists() for row in contents),
                "batch workspaces shared or leaked")
        for index, row in enumerate(rows):
            require(row["state"] == "SUCCEEDED" and row["exit_code"] == 0 and contents[index]["data"] == inputs[index].read_bytes().hex(),
                    "batch input association or status wrong")
            require(row["elapsed_seconds"] >= .8, "task timing excluded the known execution interval")
            require(json.loads((output / f"{index + 1:04}.json").read_text()) == row, "incremental metadata disagrees with manifest")
            require(command("logs", row["job_id"], port=port).stdout == (output / row["stdout"]).read_bytes(), "retained staged logs differ")
        require(output.stat().st_mode & 0o777 == 0o700 and (output / "0001.stdout").stat().st_mode & 0o777 == 0o600,
                "batch result permissions incorrect")

        mixed = root / "mixed"
        result = command("map", "--output", str(mixed), *map(str, inputs), "--", "python3", "-c",
                         "import pathlib,sys; n=pathlib.Path(sys.argv[1]).read_bytes()[0]; print(n); sys.exit(7 if n==1 else 0)", "{}", port=port)
        rows = [json.loads(line) for line in (mixed / "results.jsonl").read_text().splitlines()]
        require(result.returncode == 1 and [row["exit_code"] for row in rows] == [0, 7, 0, 0], "one failure stopped batch or exit association lost")
        timed = root / "timed"
        result = command("map", "--timeout", "1", "--output", str(timed), str(inputs[0]), "--", "python3", "-c",
                         "import time; time.sleep(10)", "{}", port=port)
        row = json.loads((timed / "results.jsonl").read_text())
        require(result.returncode == 1 and row["state"] == "TIMED_OUT" and row["timed_out"], "batch timeout not recorded")
        binary_output = root / "binary-output"
        result = command("map", "--output", str(binary_output), str(inputs[0]), "--", "python3", "-c",
                         "import sys; sys.stdout.buffer.write(b'a\\x00b'+b'x'*20000)", "{}", port=port)
        row = json.loads((binary_output / "results.jsonl").read_text())
        require(result.returncode == 0 and row["truncated"] and (binary_output / row["stdout"]).read_bytes() == b"a\x00b" + b"x" * 16381,
                "batch binary output or truncation metadata damaged")

        storage_output = root / "storage-failure"
        before_storage = len(command("ps", port=port).stdout.splitlines())
        def limit_result_files():
            resource.setrlimit(resource.RLIMIT_FSIZE, (1024, 1024))
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        result = subprocess.run([binary, "map", "--port", port, "--jobs", "1", "--output", str(storage_output),
                                 *map(str, inputs[:2]), "--", "python3", "-c", "print('x'*5000)", "{}"], env=env,
                                capture_output=True, timeout=15, preexec_fn=limit_result_files)
        storage_rows = [json.loads(line) for line in (storage_output / "results.jsonl").read_text().splitlines()]
        require(result.returncode != 0 and b"storage failed" in result.stderr and storage_rows[1]["state"] == "NOT_SUBMITTED",
                "batch kept submitting after result storage failed")
        require(len(command("ps", port=port).stdout.splitlines()) == before_storage + 1,
                "storage failure submitted or retried additional jobs")

        before = command("ps", port=port).stdout
        oversize = root / "oversize.bin"
        oversize.write_bytes(b"x" * 32769)
        link = root / "link.bin"
        link.symlink_to(payload)
        fifo = root / "fifo.bin"
        os.mkfifo(fifo)
        for invalid in (oversize, link, fifo, root, root / "missing"):
            require(command("run", "--file", str(invalid), "--", "/bin/true", port=port).returncode != 0,
                    "invalid source file accepted")
        too_many = [option for _ in range(17) for option in ("--file", str(payload))]
        require(command("run", *too_many, "--", "/bin/true", port=port).returncode != 0,
                "excess staged file count accepted")
        duplicate = root / "duplicate"
        duplicate.mkdir()
        (duplicate / payload.name).write_text("duplicate")
        require(command("run", "--file", str(payload), "--file", str(duplicate / payload.name), "--", "/bin/true", port=port).returncode != 0,
                "duplicate destination names accepted")
        for options, paths, arguments in (
            (["--output", str(output)], [str(inputs[0])], ["/bin/true", "{}"]),
            (["--output", str(root / "bad-placeholder")], [str(inputs[0])], ["/bin/true"]),
            (["--output", str(root / "bad-input")], [str(inputs[0]), str(oversize)], ["/bin/true", "{}"]),
            (["--output", str(root / "no-worker"), "--arch", "unavailable-architecture"], [str(inputs[0])], ["/bin/true", "{}"]),
            (["--output", str(root / "many")], [str(inputs[0])] * 257, ["/bin/true", "{}"]),
            (["--jobs", "0"], [str(inputs[0])], ["/bin/true", "{}"]),
        ):
            require(command("map", *options, *paths, "--", *arguments, port=port).returncode != 0, "invalid map preflight accepted")
        require(not (root / "bad-input").exists() and not (root / "no-worker").exists(), "preflight created result directory")
        for name in ("../escape", "/tmp/escape", ".", "..", "a/b", "a\\b", "bad\x00name"):
            require(request(port, 14, ["1", "0", "1", "linux", "", "1", name, "0", "data", "/bin/true"])[0] == 6,
                    "unsafe staged path accepted")
        for files in (["0"], ["17"], ["1", "x", "bad", ""], ["1", "x", "0", b"x" * 32769],
                      ["2", "x", "0", "", "x", "0", ""]):
            require(request(port, 14, ["1", "0", "1", "linux", "", *files, "/bin/true"])[0] == 6,
                    "malformed staging manifest accepted")
        fields = ["1", "0", "1", "linux", "", "1", "f", "0", "x", "/bin/true", ""]
        fields[-1] = "x" * (65536 - (len(frame(14, fields)) - 4))
        require(request(port, 14, fields)[0] == 6, "oversized assignment accepted from boundary-sized submission")
        require(command("ps", port=port).stdout == before, "rejected preflight or manifest admitted a job")

        lost_output = root / "lost-worker"
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(command, "map", "--output", str(lost_output), *map(str, inputs), "--",
                                  "python3", "-c", "import time; time.sleep(1.5)", "{}", port=port)
            wait_for(lambda: request(port, 16)[1][6::8].count(b"BUSY") == 2, "batch did not occupy both workers")
            workers[1].terminate()
            workers[1].wait(timeout=7)
            result = pending.result(timeout=15)
        lost_rows = [json.loads(line) for line in (lost_output / "results.jsonl").read_text().splitlines()]
        require(result.returncode == 1 and sum(row["state"] == "ERROR" for row in lost_rows) == 1
                and sum(row["state"] == "SUCCEEDED" for row in lost_rows) == 3,
                "worker loss was retried, hidden, or stopped independent batch tasks")
        wait_for(lambda: request(port, 16)[1][6::8].count(b"READY") == 1, "second worker did not disconnect")
        single = root / "single-worker"
        result = command("map", "--output", str(single), *map(str, inputs[:2]), "--", "python3", "-c",
                         "import time; time.sleep(5.2)", "{}", port=port)
        require(result.returncode == 0, "default parallelism oversubscribed single worker past queue deadline")
        require(server.poll() is None, "malformed staging killed coordinator")

        legacy_port = unused_port()
        legacy_server = start("coordinator", port=legacy_port)
        wait_for(lambda: command("cluster", port=legacy_port).returncode == 0, "legacy coordinator did not start")
        stop = threading.Event()
        assigned = []
        errors = []
        with socket.create_connection(("127.0.0.1", int(legacy_port)), timeout=8) as legacy:
            legacy.sendall(frame(1, ["legacy", "linux", os.uname().machine, "4", "4096", "0"]))
            require(receive(legacy)[0] == 4, "legacy registration rejected")
            def heartbeat():
                try:
                    while not stop.is_set():
                        legacy.sendall(frame(2))
                        kind, fields = receive(legacy)
                        assigned.append(kind)
                        if kind == 8:
                            legacy.sendall(frame(9, [fields[0], "0", "legacy", "", "0", "0"]))
                            require(receive(legacy)[0] == 4, "legacy result not acknowledged")
                        else:
                            require(kind == 4, "staged assignment reached legacy worker")
                        stop.wait(.01)
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=heartbeat)
            thread.start()
            try:
                result = command("run", "--file", str(payload), "--", "/bin/example", port=legacy_port)
                require(result.returncode != 0 and b"no available compatible worker" in result.stderr, "staged job sent to legacy worker")
                result = command("run", "--", "/bin/example", port=legacy_port)
                require(result.returncode == 0 and result.stdout == b"legacy", "legacy plain execution broken")
                result = command("map", "--output", str(root / "legacy-results"), str(payload), "--", "/bin/example", "{}", port=legacy_port)
                require(result.returncode != 0 and not (root / "legacy-results").exists(), "legacy-only cluster passed map preflight")
                require(15 not in assigned and not errors, str(errors))
            finally:
                stop.set()
                thread.join(timeout=6)
        print("Staging/map passed: binary snapshots, permissions, cleanup, parallel placement, stable results, failures, bounds, validation, legacy gating")
finally:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=7)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
