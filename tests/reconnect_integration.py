"""Reconnect sessions without replaying results or bypassing admission and cleanup."""
from contextlib import contextmanager
import importlib.util
import json
import os
import re
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time

from runtime_fixture import Cluster, binary, environment, frame, receive, require, wait_for


@contextmanager
def listener(port=0):
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', port))
        server.listen()
        server.settimeout(9)
        yield server


@contextmanager
def worker(port, reconnect=True, env=environment):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / 'worker.log'
        with path.open('wb') as log:
            arguments = [binary, 'join', '--id', 'reconnect-test', '--port', str(port)]
            if reconnect:
                arguments.append('--reconnect')
            process = subprocess.Popen(arguments, env=env, stdout=log, stderr=log)
            try:
                yield process, path
            except BaseException:
                print(path.read_text())
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def registration(server, context=None):
    peer, _ = server.accept()
    peer.settimeout(6)
    if context:
        peer = context.wrap_socket(peer, server_side=True)
    kind, fields = receive(peer)
    require(kind == 1 and fields[0] == 'reconnect-test', 'worker replayed a result instead of registering')
    return peer


def admit(server, context=None):
    peer = registration(server, context)
    peer.sendall(frame(4))
    require(receive(peer) == (2, []), 'new session did not start idle')
    return peer


def result(peer):
    for _ in range(40):
        kind, fields = receive(peer)
        if kind == 25:
            return fields
        require((kind, fields) == (2, []), 'unexpected worker message')
        peer.sendall(frame(4))
        time.sleep(.025)
    raise AssertionError('worker result missing')


def fresh_job(peer):
    peer.sendall(frame(23, ['job-1', '1', '5', '/usr/bin/printf', 'fresh']))
    fields = result(peer)
    require(fields[:4] == ['job-1', '1', '0', 'fresh'], 'stale execution state crossed sessions')
    peer.sendall(frame(4))


with listener() as probe:
    port = probe.getsockname()[1]
with worker(port) as (process, log):
    wait_for(lambda: 'reconnecting in' in log.read_text(), 'unavailable coordinator did not trigger backoff')
    with listener(port) as server, admit(server) as peer:
        fresh_job(peer)
        require(process.poll() is None, 'worker failed after coordinator became available')

with listener() as server, worker(server.getsockname()[1]) as (process, log):
    arrivals = []
    for _ in range(8):
        with registration(server) as peer:
            arrivals.append(time.monotonic())
    require(all(b - a >= minimum for a, b, minimum in zip(arrivals, arrivals[1:], (.2, .45, .95, 1.95, 3.95, 4.95, 4.95))),
            'repeated transport failures did not back off')
    wait_for(lambda: log.read_text().count('reconnecting in') >= 8, 'backoff state was not logged')
    waits = [int(value) for value in re.findall(r'reconnecting in (\d+) ms', log.read_text())]
    require(all(250 <= value <= 5100 for value in waits) and all(value >= 5000 for value in waits[-3:]),
            'backoff was not capped at five seconds plus bounded jitter')
    process.terminate()
    require(process.wait(timeout=3) == 0, 'stop did not interrupt reconnect backoff')

with listener() as server, worker(server.getsockname()[1]) as (process, log):
    registration(server).close()
    registration(server).close()
    with admit(server) as peer:
        until = time.monotonic() + 10.3
        peer.sendall(frame(4))
        while time.monotonic() < until:
            require(receive(peer) == (2, []), 'stable session sent unexpected state')
            peer.sendall(frame(4))
            time.sleep(.05)
    wait_for(lambda: log.read_text().count('reconnecting in') >= 3, 'stable session did not reconnect')
    planned = [int(value) for value in re.findall(r'reconnecting in (\d+) ms', log.read_text())]
    require(250 <= planned[-1] <= 350, 'healthy session did not reset accumulated backoff')
    with admit(server) as peer:
        fresh_job(peer)

with listener() as server, worker(server.getsockname()[1], reconnect=False) as (process, log):
    admit(server).close()
    require(process.wait(timeout=5) != 0 and 'reconnecting in' not in log.read_text(),
            'default worker silently enabled reconnect')

with listener() as server, worker(server.getsockname()[1]) as (process, log):
    with admit(server) as stalled:
        with admit(server) as peer:
            require(stalled.recv(1) == b'', 'timed-out application connection stayed open')
            fresh_job(peer)

with listener() as server, worker(server.getsockname()[1]) as (process, log):
    with admit(server) as expired:
        expired.sendall(frame(6, ['I/O deadline exceeded']))
    with admit(server) as peer:
        fresh_job(peer)

with listener() as server, worker(server.getsockname()[1]) as (process, log):
    admit(server).close()
    with registration(server) as busy:
        busy.sendall(frame(6, ['worker ID already connected']))
    with admit(server) as peer:
        fresh_job(peer)

with listener() as server, worker(server.getsockname()[1]) as (process, log):
    registration(server).close()
    with registration(server) as busy:
        busy.sendall(frame(6, ['worker ID already connected']))
    with admit(server) as peer:
        fresh_job(peer)

with listener() as server, worker(server.getsockname()[1]) as (process, log):
    with admit(server) as peer:
        peer.sendall(frame(23, ['bad']))
        require(process.wait(timeout=5) != 0 and 'reconnecting in' not in log.read_text(),
                'protocol violation was retried as a transport failure')

with listener() as server, worker(server.getsockname()[1]) as (process, log):
    admit(server).close()
    os.sched_setaffinity(process.pid, {min(os.sched_getaffinity(process.pid))})
    peer, _ = server.accept()
    with peer:
        peer.settimeout(6)
        kind, fields = receive(peer)
        require(kind == 1 and fields[3] == '1', 'reconnect reused stale CPU capacity')
        peer.sendall(frame(4))
        require(receive(peer) == (2, []), 'refreshed worker was not idle')
        fresh_job(peer)

with listener() as server, worker(server.getsockname()[1]) as (process, log):
    with admit(server) as peer:
        peer.sendall(frame(23, ['job-1', '1', '5', '/usr/bin/printf', 'old']))
        require(result(peer)[:4] == ['job-1', '1', '0', 'old'], 'initial result missing')
    with admit(server) as peer:
        fresh_job(peer)

with tempfile.TemporaryDirectory() as temporary, listener() as server:
    marker = Path(temporary) / 'started.json'
    script = ('import json,os,pathlib,time\n'
              f'pathlib.Path({str(marker)!r}).write_text(json.dumps({{"pid":os.getpid(),"cwd":str(pathlib.Path.cwd())}}))\n'
              'time.sleep(20)\n')
    with worker(server.getsockname()[1]) as (process, log):
        with admit(server) as peer:
            peer.sendall(frame(24, ['job-1', '1', '30', '1', 'probe.py', '0', script, 'python3', 'probe.py']))
            wait_for(lambda: marker.exists() and marker.stat().st_size > 0, 'staged job did not start')
            old = json.loads(marker.read_text())
        with admit(server) as peer:
            status = Path(f'/proc/{old["pid"]}/stat')
            require(not status.exists() or status.read_text().split(') ', 1)[1][0] in ('Z', 'X'),
                    'worker registered before stopping the old ordinary process group')
            require(not Path(old['cwd']).exists(), 'worker registered before workspace cleanup')
            fresh_job(peer)

with Cluster() as cluster:
    original = cluster.start('join', '--id', 'reconnect-test', '--reconnect')
    wait_for(lambda: b'reconnect-test' in cluster.cli('cluster').stdout, 'worker failed initial admission')
    duplicate = cluster.cli('join', '--id', 'reconnect-test', '--reconnect')
    require(duplicate.returncode != 0 and b'already connected' in duplicate.stderr and original.poll() is None,
            'new duplicate worker retried forever or evicted the live session')
    coordinator = cluster.processes[0]
    coordinator.terminate()
    coordinator.wait(timeout=7)
    cluster.start('coordinator')
    wait_for(lambda: b'reconnect-test' in cluster.cli('cluster').stdout, 'worker did not rejoin a restarted coordinator')
    require(original.poll() is None and cluster.cli('run', '--', '/bin/true').returncode == 0,
            'restarted coordinator could not use the same live worker process')

spec = importlib.util.spec_from_file_location('certificates', Path(__file__).parents[1] / 'scripts/create-dev-certificates.py')
certificates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(certificates)
with tempfile.TemporaryDirectory() as temporary:
    trusted, rogue = Path(temporary) / 'trusted', Path(temporary) / 'rogue'
    certificates.generate(trusted, '127.0.0.1')
    certificates.generate(rogue, '127.0.0.1')
    tls_env = dict(environment, FORGE_TLS_CA=str(trusted/'ca.crt'),
                   FORGE_TLS_CERT=str(trusted/'client.crt'), FORGE_TLS_KEY=str(trusted/'client.key'))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(str(trusted/'ca.crt'))
    context.load_cert_chain(str(trusted/'server.crt'), str(trusted/'server.key'))
    with listener() as server, worker(server.getsockname()[1], env=tls_env) as (process, log):
        admit(server, context).close()
        with admit(server, context) as peer:
            fresh_job(peer)
    rotated = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    rotated.minimum_version = ssl.TLSVersion.TLSv1_3
    rotated.verify_mode = ssl.CERT_REQUIRED
    rotated.load_verify_locations(str(rogue/'ca.crt'))
    rotated.load_cert_chain(str(rogue/'server.crt'), str(rogue/'server.key'))
    active = Path(temporary)/'active'
    active.symlink_to(trusted, target_is_directory=True)
    rotating_env = dict(environment, FORGE_TLS_CA=str(active/'ca.crt'),
                        FORGE_TLS_CERT=str(active/'client.crt'), FORGE_TLS_KEY=str(active/'client.key'))
    with listener() as server, worker(server.getsockname()[1], env=rotating_env) as (process, log):
        admit(server, context).close()
        active.unlink()
        active.symlink_to(rogue, target_is_directory=True)
        with admit(server, rotated) as peer:
            fresh_job(peer)
    for variable in ('FORGE_TLS_CA', 'FORGE_TLS_CERT', 'FORGE_TLS_KEY'):
        invalid = dict(tls_env, **{variable: str(Path(temporary)/'missing')})
        with worker(1, env=invalid) as (process, log):
            require(process.wait(timeout=5) != 0 and variable in log.read_text()
                    and 'reconnecting in' not in log.read_text(), 'local TLS error was obscured or retried')
    with listener() as server, worker(server.getsockname()[1], env=dict(tls_env, FORGE_TLS_CA=str(rogue/'ca.crt'))) as (process, log):
        raw, _ = server.accept()
        try:
            with context.wrap_socket(raw, server_side=True):
                pass
        except ssl.SSLError:
            raw.close()
        require(process.wait(timeout=5) != 0 and 'reconnecting in' not in log.read_text(),
                'server certificate rejection was retried')
    wrong_client = dict(tls_env, FORGE_TLS_CERT=str(rogue/'client.crt'), FORGE_TLS_KEY=str(rogue/'client.key'))
    with listener() as server, worker(server.getsockname()[1], env=wrong_client) as (process, log):
        raw, _ = server.accept()
        try:
            with context.wrap_socket(raw, server_side=True) as peer:
                peer.recv(1)
        except ssl.SSLError:
            raw.close()
        require(process.wait(timeout=5) != 0 and 'reconnecting in' not in log.read_text(),
                'client certificate rejection was retried')

with listener() as server, worker(server.getsockname()[1], env=dict(environment, FORGE_TOKEN='a'*32)) as (process, log):
    peer, _ = server.accept()
    with peer:
        peer.settimeout(5)
        require(receive(peer)[0] == 13, 'token admission was bypassed')
        peer.sendall(frame(6, ['authentication failed']))
    require(process.wait(timeout=5) != 0 and 'reconnecting in' not in log.read_text()
            and 'a'*32 not in log.read_text(), 'authentication rejection was retried or credential leaked')

print('Reconnect checks passed: offline start, backoff/stop, idle timeout, busy ID, no replay, active cleanup, restart, TLS and fatal errors')
