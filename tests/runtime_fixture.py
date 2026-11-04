"""Linux runtime test fixture and bounded protocol helpers."""
from contextlib import contextmanager
import os
import platform
import socket
import struct
import subprocess
import sys
import time

binary = os.path.abspath(sys.argv[1])
environment = {k: v for k, v in os.environ.items() if not k.startswith('FORGE_TLS_') and k != 'FORGE_TOKEN'}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wait_for(predicate, message, seconds=7):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.025)
    raise AssertionError(message)


def frame(kind, fields=()):
    payload = bytes((1, kind)) + struct.pack('!I', len(fields))
    for field in fields:
        data = field.encode() if isinstance(field, str) else field
        payload += struct.pack('!I', len(data)) + data
    return struct.pack('!I', len(payload)) + payload


def exact(sock, length):
    data = b''
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise EOFError('peer closed')
        data += chunk
    return data


def receive(sock):
    size, = struct.unpack('!I', exact(sock, 4))
    require(6 <= size <= 65536, 'invalid frame size')
    data = exact(sock, size)
    count, = struct.unpack('!I', data[2:6])
    fields, offset = [], 6
    for _ in range(count):
        length, = struct.unpack('!I', data[offset:offset + 4])
        offset += 4
        fields.append(data[offset:offset + length].decode())
        offset += length
    require(data[0] == 1 and offset == size, 'invalid frame')
    return data[1], fields


class Cluster:
    def __enter__(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = str(probe.getsockname()[1])
        self.processes = []
        self.start('coordinator')
        wait_for(lambda: self.cli('cluster').returncode == 0, 'coordinator startup failed')
        return self

    def __exit__(self, *unused):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def start(self, *arguments):
        process = subprocess.Popen([binary, *arguments, '--port', self.port], env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return process

    def cli(self, *arguments, seconds=20):
        offset = 2 if arguments[0] in ('logs', 'timings', 'attempts', 'cancel') else 1
        return subprocess.run([binary, *arguments[:offset], '--port', self.port, *arguments[offset:]],
                              env=environment, capture_output=True, timeout=seconds)

    def connect(self):
        return socket.create_connection(('127.0.0.1', int(self.port)), timeout=10)

    def request(self, kind, fields=()):
        with self.connect() as sock:
            sock.sendall(frame(kind, fields))
            return receive(sock)

    @contextmanager
    def worker(self, name, features=True, cancellable=False):
        sock = self.connect()
        fields = [name, 'linux', platform.machine(), '1', '4096', '0', 'staging-v1', 'longpoll-v1', 'timing-v1']
        if features:
            fields.append('attempt-v1')
        if cancellable:
            require(features, 'cancellation requires attempt support')
            fields.append('cancel-v1')
        sock.sendall(frame(1, fields))
        require(receive(sock) == (4, []), 'worker registration failed')
        try:
            yield sock
        finally:
            sock.close()
            wait_for(lambda: any(row.startswith(name + '\t') and row.endswith('\tDEAD')
                                 for row in self.cli('cluster').stdout.decode().splitlines()),
                     'closed worker session was not retired')

    def submit(self, retries=1, staged=False):
        fields = ['1', '0', '5', 'linux', platform.machine()]
        if retries:
            fields.append(str(retries))
        if staged:
            fields.extend(['1', 'input', '0', 'original staged bytes'])
        fields.append('/bin/true')
        return self.request((22 if staged else 21) if retries else (14 if staged else 7), fields)

    def attempts(self, job):
        kind, fields = self.request(26, [job])
        require(kind == 27 and len(fields) >= 3 and (len(fields) - 3) % 6 == 0, 'bad attempt snapshot')
        return fields[:3], [fields[i:i + 6] for i in range(3, len(fields), 6)]


def assigned(sock, staged=False):
    for _ in range(12):
        sock.sendall(frame(2))
        kind, fields = receive(sock)
        if kind != 4:
            require(kind == (24 if staged else 23), 'wrong assignment type')
            return fields
    raise AssertionError('assignment did not arrive')


def complete(sock, assignment, output='accepted', attempt=None):
    result = [assignment[0], attempt or assignment[1], '0', output, '', '0', '0', '1', '2', '3', '4']
    sock.sendall(frame(25, result))
    return receive(sock)


