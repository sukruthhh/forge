"""Check shared worker budgets, independent attempts, and multi-job cleanup."""
import concurrent.futures
import json
import os
from pathlib import Path
import platform
import socket
import struct
import subprocess
import sys
import tempfile
import time

binary = str(Path(sys.argv[1]).resolve())


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def frame(kind, fields=()):
    payload = bytes((1, kind)) + struct.pack('!I', len(fields))
    for field in fields:
        value = field.encode()
        payload += struct.pack('!I', len(value)) + value
    return struct.pack('!I', len(payload)) + payload


def exact(peer, size):
    data = b''
    while len(data) < size:
        part = peer.recv(size - len(data))
        if not part:
            raise EOFError('peer closed')
        data += part
    return data


def receive(peer):
    size, = struct.unpack('!I', exact(peer, 4))
    require(6 <= size <= 65536, 'invalid frame length')
    data = exact(peer, size)
    count, = struct.unpack('!I', data[2:6])
    fields, offset = [], 6
    for _ in range(count):
        length, = struct.unpack('!I', data[offset:offset + 4])
        offset += 4
        fields.append(data[offset:offset + length].decode())
        offset += length
    require(offset == size, 'invalid fields')
    return data[1], fields


with socket.socket() as probe:
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]


def connect():
    return socket.create_connection(('127.0.0.1', port), timeout=5)


def request(kind, fields=()):
    with connect() as peer:
        peer.sendall(frame(kind, fields))
        return receive(peer)


def cli(*arguments):
    return subprocess.run([binary, *arguments, '--port', str(port)], capture_output=True, text=True, timeout=10)


def wait_for(predicate, message):
    deadline = time.monotonic() + 6
    while not predicate():
        require(time.monotonic() < deadline, message)
        time.sleep(.025)


def jobs():
    return [row.split('\t') for row in cli('ps').stdout.splitlines()[1:]]


def active(count):
    return [row for row in jobs() if row[1] == 'RUNNING'] if sum(row[1] == 'RUNNING' for row in jobs()) == count else []


def submit(cpu='1', memory='0'):
    return request(7, [cpu, memory, '10', 'linux', platform.machine(), '/bin/true'])


server = subprocess.Popen([binary, 'coordinator', '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
worker = None
try:
    def listening():
        try:
            return request(3)[0] == 5
        except OSError:
            return False
    wait_for(listening, 'coordinator did not start')
    caps = ['stub', 'linux', platform.machine(), '2', '1000', '0', 'staging-v1', 'longpoll-v1',
            'timing-v1', 'attempt-v1', 'cancel-v1']
    for suffix in (['slots-v1', '0'], ['slots-v1', '17'], ['slots-v2', '2'], ['bad', 'slots-v1', '2']):
        require(request(1, caps + suffix)[0] == 6, 'invalid slot capability accepted')
    for cpu, memory in [('2', '0'), ('1', '600')]:
        with concurrent.futures.ThreadPoolExecutor() as pool, connect() as peer:
            peer.sendall(frame(1, caps + ['slots-v1', '2']))
            require(receive(peer) == (4, []), 'slot registration refused')
            first = pool.submit(submit, cpu, memory)
            peer.sendall(frame(2))
            kind, assigned = receive(peer)
            require(kind == 23, 'first assignment missing')
            second = pool.submit(submit, cpu, memory)
            wait_for(lambda: any(row[1] == 'QUEUED' for row in jobs()), 'second job overcommitted worker resources')
            kind, snapshot = request(41)
            require(kind == 42 and snapshot[10:14] == ['2', '1', cpu, memory], 'shared reservations incorrect')
            peer.sendall(frame(25, [assigned[0], assigned[1], '0', '', '', '0', '0', '0', '0', '0', '0']))
            require(receive(peer) == (4, []), 'first result rejected')
            require(first.result(timeout=5)[0] == 9, 'first task failed')
            peer.sendall(frame(2))
            kind, assigned = receive(peer)
            require(kind == 23, 'released resources were not reused')
            peer.sendall(frame(25, [assigned[0], assigned[1], '0', '', '', '0', '0', '0', '0', '0', '0']))
            require(receive(peer) == (4, []) and second.result(timeout=5)[0] == 9, 'second task failed')
        wait_for(lambda: request(3)[1][6] == 'DEAD', 'stub session did not close')

    with concurrent.futures.ThreadPoolExecutor() as pool:
        peer = connect()
        peer.sendall(frame(1, caps + ['slots-v1', '2']))
        require(receive(peer) == (4, []), 'retry stub registration refused')
        pending = [pool.submit(request, 21, ['1', '0', '10', 'linux', platform.machine(), '1', '/bin/true']) for _ in range(2)]
        originals = []
        for _ in range(2):
            peer.sendall(frame(2))
            kind, assigned = receive(peer)
            require(kind == 23 and assigned[1] == '1', 'concurrent first attempt missing')
            originals.append(assigned[0])
        peer.close()
        wait_for(lambda: sum(row[1] == 'QUEUED' for row in jobs()) == 2, 'worker loss did not requeue both jobs')
        with connect() as replacement:
            replacement.sendall(frame(1, caps + ['slots-v1', '2']))
            require(receive(replacement) == (4, []), 'replacement refused')
            recovered = []
            for _ in range(2):
                replacement.sendall(frame(2))
                kind, assigned = receive(replacement)
                require(kind == 23 and assigned[1] == '2', 'concurrent retry attempt incorrect')
                recovered.append(assigned[0])
                replacement.sendall(frame(25, [assigned[0], assigned[1], '0', '', '', '0', '0', '0', '0', '0', '0']))
                require(receive(replacement) == (4, []), 'retried result refused')
            require(set(recovered) == set(originals) and all(future.result(timeout=5)[0] == 9 for future in pending),
                    'concurrent retries changed job identity or failed')
        wait_for(lambda: request(3)[1][6] == 'DEAD', 'replacement session did not close')

    available = sorted(os.sched_getaffinity(0))
    if len(available) >= 2:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            script = directory / 'task.py'
            script.write_text('import os, pathlib, sys, time\np=pathlib.Path(sys.argv[1]); '
                              'p.write_text(str(os.getpid())); time.sleep(float(sys.argv[2])); print(p.name)\n')
            def start_worker():
                return subprocess.Popen([binary, 'join', '--id', 'multi', '--slots', '2', '--port', str(port)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        preexec_fn=lambda: os.sched_setaffinity(0, available[:2]))
            worker = start_worker()
            wait_for(lambda: 'multi\t' in cli('cluster').stdout, 'real worker did not register')
            def run(name, seconds):
                return subprocess.Popen([binary, 'run', '--port', str(port), '--', sys.executable, str(script),
                                         str(directory / name), str(seconds)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            first, second = run('first', 4), run('second', 1.5)
            try:
                wait_for(lambda: (directory / 'first').exists() and (directory / 'second').exists(), 'jobs did not overlap')
                wait_for(lambda: bool(active(2)), 'two jobs not running on one worker')
                rows = active(2)
                require({row[2] for row in rows} == {'multi'}, 'concurrency used multiple worker identities')
                first_pid = int((directory / 'first').read_text())
                victim = next(row[0] for row in rows if str(directory / 'first') in row[4])
                require(cli('cancel', victim).returncode == 0, 'concurrent cancellation failed')
                first.communicate(timeout=6)
                output, error = second.communicate(timeout=6)
                require(first.returncode == 130 and second.returncode == 0 and output.strip() == 'second',
                        'cancellation affected sibling task: ' + error)
                require(not Path(f'/proc/{first_pid}').exists(), 'cancelled child survived')
                kind, snapshot = request(41)
                offset = snapshot.index('multi')
                require(kind == 42 and snapshot[offset + 11:offset + 14] == ['0', '0', '0'], 'reservations leaked')
            finally:
                for process in (first, second):
                    if process.poll() is None:
                        process.kill()
                    process.communicate()

            inputs = []
            for index in range(4):
                path = directory / f'{index}.txt'
                path.write_text(str(index))
                inputs.append(str(path))
            output = directory / 'results'
            result = subprocess.run([binary, 'map', '--port', str(port), '--jobs', '2', '--output', str(output),
                                     *inputs, '--', sys.executable, '-c',
                                     'import pathlib, sys, time; time.sleep(.2); print(pathlib.Path(sys.argv[1]).read_text())', '{}'],
                                    capture_output=True, text=True, timeout=12)
            require(result.returncode == 0, 'concurrent staged map failed: ' + result.stderr)
            manifest = [json.loads(line) for line in (output / 'results.jsonl').read_text().splitlines()]
            require(len(manifest) == 4 and all(row['state'] == 'SUCCEEDED' for row in manifest), 'map results invalid')
            require([(output / f'{i + 1:04}.stdout').read_text().strip() for i in range(4)] == list(map(str, range(4))),
                    'concurrent staging mixed inputs')

            first, second = run('shutdown-a', 10), run('shutdown-b', 10)
            try:
                wait_for(lambda: (directory / 'shutdown-a').exists() and (directory / 'shutdown-b').exists(), 'shutdown jobs not started')
                child_pids = [int((directory / name).read_text()) for name in ('shutdown-a', 'shutdown-b')]
                worker.terminate()
                worker.wait(timeout=6)
                for process in (first, second):
                    process.communicate(timeout=6)
                    require(process.returncode != 0, 'worker shutdown reported success')
                require(all(not Path(f'/proc/{pid}').exists() for pid in child_pids), 'shutdown left concurrent children')
            finally:
                for process in (first, second):
                    if process.poll() is None:
                        process.kill()
                    process.communicate()
    print('Concurrent worker budgets, cancellation, staging and cleanup passed')
finally:
    for process in (worker, server):
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
