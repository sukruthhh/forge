"""Exercise bounded worker-loss retries, attempt fencing and real staged recovery."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
import threading

from runtime_fixture import Cluster, assigned, complete, frame, receive, require, wait_for

with concurrent.futures.ThreadPoolExecutor() as pool, Cluster() as cluster:
    require(cluster.request(26)[0] == 6 and cluster.request(26, ['missing'])[0] == 6, 'bad inspection accepted')
    for value in ('0', '4', '-1', '18446744073709551616', ''):
        require(cluster.request(21, ['1', '0', '1', 'linux', '', value, '/bin/true'])[0] == 6,
                'invalid retry bound accepted')
    require(cluster.request(21, ['1', '0', '1', 'linux', '', '1'])[0] == 6, 'missing retry argv accepted')
    for arguments in (('--retries', '4'), ('--retries', '-1'), ('--retries', '1', '--retries', '1')):
        result = cluster.cli('run', *arguments, '--', '/bin/true')
        require(result.returncode != 0, 'invalid CLI retry option accepted')
    require(cluster.request(10) == (12, []), 'invalid submissions mutated job state')

    with cluster.worker('recovery-a') as first:
        pending = pool.submit(cluster.submit, 1, True)
        initial = assigned(first, staged=True)
        header, attempts = cluster.attempts(initial[0])
        require(header[1:] == ['RUNNING', '2'] and len(attempts) == 1 and attempts[0][4] == '',
                'active attempt not observable')
    with cluster.worker('recovery-b') as replacement:
        recovered = assigned(replacement, staged=True)
        require(recovered[:2] == [initial[0], '2'] and recovered[2:] == initial[2:],
                'retry changed job identity, input bytes or command')
        require(complete(replacement, recovered) == (4, []), 'recovered result rejected')
        require(pending.result(timeout=5) == (9, [initial[0], '0', 'accepted', '', '0', '0']), 'job did not recover')
        header, attempts = cluster.attempts(initial[0])
        require(header[1] == 'SUCCEEDED' and [a[2] for a in attempts] == ['LOST', 'SUCCEEDED'],
                'attempt history lost recovery evidence')
        require(all(a[4].isdigit() for a in attempts), 'completed attempt duration missing')
        result = cluster.cli('attempts', initial[0])
        require(result.returncode == 0 and b'attempts=2/2' in result.stderr and b'LOST' in result.stdout,
                'attempt CLI omitted history')
        require(cluster.cli('logs', initial[0]).stdout == b'accepted', 'final logs changed during recovery')

    with cluster.worker('default-loss') as first:
        pending = pool.submit(cluster.submit, 0)
        initial = assigned(first)
    require(pending.result(timeout=5)[0] == 6, 'default worker loss silently retried')
    require(len(cluster.attempts(initial[0])[1]) == 1, 'default job created additional attempts')

    with cluster.worker('exhaust-1') as first:
        pending = pool.submit(cluster.submit, 3)
        initial = assigned(first)
    for index in range(2, 5):
        with cluster.worker('exhaust-' + str(index)) as replacement:
            current = assigned(replacement)
            require(current[:2] == [initial[0], str(index)], 'attempt counter changed across losses')
    kind, failure = pending.result(timeout=5)
    require(kind == 30 and failure[0] == initial[0] and 'exhausted' in failure[1], 'retry budget did not terminate job')
    header, attempts = cluster.attempts(initial[0])
    require(header[1:] == ['LOST', '4'] and [a[2] for a in attempts] == ['LOST'] * 4, 'retry budget exceeded')

    with cluster.worker('reuse') as first:
        pending = pool.submit(cluster.submit, 2)
        initial = assigned(first)
    wait_for(lambda: any(row.startswith('reuse\t') and row.endswith('\tDEAD')
                        for row in cluster.cli('cluster').stdout.decode().splitlines()), 'closed worker not removed')
    with cluster.worker('reuse') as replacement:
        current = assigned(replacement)
        require(current[1] == '2', 'reconnected worker did not receive a new attempt')
        require(complete(replacement, current, output='stale', attempt='1')[0] == 6, 'stale result accepted')
    with cluster.worker('fresh') as replacement:
        current = assigned(replacement)
        require(current[1] == '3' and complete(replacement, current, output='fresh') == (4, []), 'fenced job did not recover')
        require(pending.result(timeout=5)[1][2] == 'fresh', 'stale result escaped into final output')

    with cluster.worker('silent') as first:
        pending = pool.submit(cluster.submit)
        initial = assigned(first)
        require(receive(first)[0] == 6, 'silent assigned worker escaped receive deadline')
    with cluster.worker('after-silent') as replacement:
        current = assigned(replacement)
        require(current[:2] == [initial[0], '2'] and complete(replacement, current) == (4, []), 'silent worker job not recovered')
        require(pending.result(timeout=5)[0] == 9, 'heartbeat expiry recovery failed')

    with cluster.worker('no-capacity') as first:
        pending = pool.submit(cluster.submit)
        initial = assigned(first)
    kind, failure = pending.result(timeout=8)
    header, attempts = cluster.attempts(initial[0])
    require(kind == 30 and failure[0] == initial[0] and header[1] == 'UNSCHEDULABLE' and len(attempts) == 1,
            'replacement queue deadline did not terminate without fabricated attempts')

    stop, errors = threading.Event(), []
    with cluster.worker('legacy-profile', features=False) as legacy:
        def heartbeat():
            try:
                while not stop.is_set():
                    legacy.sendall(frame(2))
                    require(receive(legacy) == (4, []), 'retry job reached non-attempt worker')
            except Exception as error:
                if not stop.is_set():
                    errors.append(str(error))
        thread = threading.Thread(target=heartbeat)
        thread.start()
        try:
            kind, failure = cluster.submit()
            header, attempts = cluster.attempts(failure[0])
            require(kind == 30 and header[1] == 'UNSCHEDULABLE' and not attempts and not errors,
                    'retry capability was not enforced')
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'input'
                path.write_text('value')
                output = Path(temporary) / 'out'
                result = cluster.cli('map', '--retries', '1', '--output', str(output), str(path), '--', '/bin/cat', '{}')
                require(result.returncode != 0 and not output.exists() and b'attempt-v1' in result.stderr,
                        'map did not preflight retry-capable capacity')
        finally:
            stop.set()
            thread.join(timeout=3)
            require(not thread.is_alive() and not errors, 'legacy heartbeat thread failed or leaked')

    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / 'input'
        path.write_text('value')
        output = Path(temporary) / 'out'
        with cluster.worker('map-exhaust-a') as first:
            pending = pool.submit(cluster.cli, 'map', '--retries', '1', '--output', str(output),
                                  str(path), '--', '/bin/cat', '{}')
            initial = assigned(first, staged=True)
        with cluster.worker('map-exhaust-b') as replacement:
            current = assigned(replacement, staged=True)
            require(current[:2] == [initial[0], '2'], 'map retry identity changed')
        result = pending.result(timeout=5)
        row = json.loads((output / 'results.jsonl').read_text())
        require(result.returncode == 1 and row['job_id'] == initial[0] and row['state'] == 'ERROR'
                and row['exit_code'] is None and row['storage_complete'] and 'exhausted' in row['diagnostic'],
                'structured retry failure lost map job identity or invented a command exit')

with concurrent.futures.ThreadPoolExecutor() as pool, Cluster() as cluster, tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    marker, effects = root / 'started.json', root / 'effects'
    script = root / 'task.py'
    script.write_text(
        'import json,os,pathlib,sys,time\n'
        f'marker=pathlib.Path({str(marker)!r})\n'
        f'with open({str(effects)!r}, "a") as stream: stream.write("attempt\\n")\n'
        'if not marker.exists():\n'
        '    marker.write_text(json.dumps({"pid":os.getpid(),"cwd":os.getcwd()}))\n'
        '    time.sleep(25)\n'
        'print(os.getcwd(),flush=True)\n'
        'sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())\n')
    payload = root / 'input.bin'
    payload.write_bytes(b'original\x00bytes\xff')
    output = root / 'out'
    worker = cluster.start('join', '--id', 'real-a')
    wait_for(lambda: b'real-a\t' in cluster.cli('cluster').stdout, 'real worker did not register')
    old_process = None
    try:
        pending = pool.submit(cluster.cli, 'map', '--retries', '1', '--file', str(script), '--output', str(output),
                              str(payload), '--', 'python3', 'task.py', '{}')
        wait_for(lambda: marker.exists() and marker.stat().st_size > 0, 'first attempt never started')
        old_process = json.loads(marker.read_text())
        payload.write_bytes(b'changed after initial admission')
        worker.kill()
        worker.wait(timeout=5)
        cluster.start('join', '--id', 'real-b')
        result = pending.result(timeout=12)
        require(result.returncode == 0, result.stdout + result.stderr)
        manifest = [json.loads(line) for line in (output / 'results.jsonl').read_text().splitlines()]
        require(len(manifest) == 1 and manifest[0]['state'] == 'SUCCEEDED', 'recovered map result was not saved')
        row = manifest[0]
        cwd, data = (output / row['stdout']).read_bytes().split(b'\n', 1)
        require(data == b'original\x00bytes\xff' and cwd.decode() != old_process['cwd'], 'staged snapshot or fresh workspace was lost')
        require(not Path(cwd.decode()).exists(), 'successful retry workspace leaked')
        require(effects.read_text().splitlines() == ['attempt', 'attempt'], 'repeated side effects were hidden')
        os.kill(old_process['pid'], 0)
        header, attempts = cluster.attempts(row['job_id'])
        require(header[1] == 'SUCCEEDED' and [a[1] for a in attempts] == ['real-a', 'real-b']
                and [a[2] for a in attempts] == ['LOST', 'SUCCEEDED'], 'real execution attempts not retained')
        for command, expected in ((['/bin/sh', '-c', 'exit 7'], 7), (['/bin/sleep', '5'], 124),
                                  (['/forge-no-such-executable'], 127)):
            result = cluster.cli('run', '--retries', '3', '--timeout', '1', '--', *command)
            require(result.returncode == expected,
                    f'expected exit {expected}, got {result.returncode}: {result.stderr!r}')
        kind, rows = cluster.request(10)
        require(kind == 12, 'job list failed')
        for job_id in rows[::5][-3:]:
            header, attempts = cluster.attempts(job_id)
            require(len(attempts) == 1, 'ordinary command failure or timeout was retried')
    finally:
        if old_process is not None:
            try:
                os.killpg(old_process['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
            workspace = Path(old_process['cwd'])
            require(workspace.parent == Path(tempfile.gettempdir()) and workspace.name.startswith('forge-job-'),
                    'refusing cleanup outside test-owned job workspace')
            shutil.rmtree(workspace, ignore_errors=True)

with concurrent.futures.ThreadPoolExecutor() as pool, Cluster() as cluster, tempfile.TemporaryDirectory() as temporary:
    cluster.start('join', '--id', 'shutdown-worker')
    wait_for(lambda: b'READY' in cluster.cli('cluster').stdout, 'shutdown worker not ready')
    pending = pool.submit(cluster.request, 21, ['1', '0', '5', 'linux', '', '3', '/bin/sleep', '30'])
    wait_for(lambda: b'RUNNING' in cluster.cli('ps').stdout, 'shutdown job did not start')
    marker = Path(temporary) / 'must-not-run'
    queued = pool.submit(cluster.request, 21, ['1', '0', '5', 'linux', '', '3', '/usr/bin/touch', str(marker)])
    wait_for(lambda: b'QUEUED' in cluster.cli('ps').stdout, 'shutdown queued job was not admitted')
    cluster.processes[0].terminate()
    for future in (pending, queued):
        kind, failure = future.result(timeout=5)
        require(kind == 30 and 'coordinator stopping' in failure[1], 'shutdown retried or lost its terminal response')
    require(cluster.processes[0].wait(timeout=7) == 0, 'coordinator shutdown hung with retries enabled')
    require(not marker.exists(), 'coordinator shutdown dispatched queued work')

print('Recovery checks passed: opt-in retries, bounded attempts, stale-result fencing, expiry, no capacity, legacy gating, staged map recovery, repeated side effects, terminal command failures')
