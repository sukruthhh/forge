"""Verify cancellation ordering, attempt fencing, cleanup evidence and worker reuse."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
import time

from runtime_fixture import Cluster, assigned, complete, frame, receive, require, wait_for


def job_in(cluster, state):
    found = []
    def inspect():
        kind, fields = cluster.request(10)
        require(kind == 12, 'job listing failed')
        found[:] = [fields[i] for i in range(0, len(fields), 5) if fields[i + 1] == state]
        return bool(found)
    wait_for(inspect, 'no job reached ' + state)
    require(len(found) == 1, 'ambiguous active job fixture')
    return found[0]


def cancelled(result, job, cleanup):
    kind, fields = result
    require(kind == 33 and len(fields) == 7 and fields[:2] == [job, '130'] and fields[4] == '0'
            and fields[6] == cleanup, f'invalid cancellation result: {result!r}')


def not_running(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] in ('Z', 'X')
    except FileNotFoundError:
        return True


with concurrent.futures.ThreadPoolExecutor() as pool, Cluster() as cluster:
    for fields in ([], ['missing'], ['a', 'b'], ['bad/id']):
        require(cluster.request(31, fields)[0] == 6, 'invalid or unknown cancellation accepted')
    pending = pool.submit(cluster.submit, 3)
    job = job_in(cluster, 'QUEUED')
    require(cluster.request(31, [job]) == (34, [job, 'CANCELLED', 'not-started']), 'queued cancellation failed')
    cancelled(pending.result(timeout=5), job, 'not-started')
    require(not cluster.attempts(job)[1], 'queued cancellation invented an attempt')
    require(cluster.cli('cancel', job).returncode == 0, 'repeated cancellation not idempotent')
    require(cluster.cli('logs', job).returncode == 0, 'cancelled result not inspectable')

    with cluster.worker('assigned-legacy') as worker:
        pending = pool.submit(cluster.submit, 0)
        job = job_in(cluster, 'ASSIGNED')
        require(cluster.request(31, [job])[1] == [job, 'CANCELLED', 'not-started'], 'undelivered assignment not cancelled')
        cancelled(pending.result(timeout=5), job, 'not-started')
        require(cluster.attempts(job)[1][0][2] == 'CANCELLED', 'assigned attempt state lost')
        worker.sendall(frame(2))
        require(receive(worker) == (4, []), 'cancelled assignment reached worker')
        pending = pool.submit(cluster.submit, 0)
        next_job = assigned(worker)
        require(complete(worker, next_job) == (4, []) and pending.result(timeout=5)[0] == 9,
                'reservation not released after assigned cancellation')
        require(cluster.cli('cancel', next_job[0]).returncode == 1, 'completed success was changed by cancellation')

    with cluster.worker('running', cancellable=True) as worker:
        pending = pool.submit(cluster.submit, 3)
        assignment = assigned(worker)
        job = assignment[0]
        require(cluster.request(31, [job]) == (34, [job, 'CANCELLING', 'pending']), 'running job finished before cleanup')
        require(cluster.request(31, [job])[1] == [job, 'CANCELLING', 'pending'], 'repeated pending cancel changed state')
        require(cluster.attempts(job)[1][0][2] == 'CANCELLING', 'attempt omitted pending cancellation')
        queued = pool.submit(cluster.submit, 0)
        queued_id = job_in(cluster, 'QUEUED')
        worker.sendall(frame(2))
        require(receive(worker) == (32, assignment[:2]), 'stop instruction lost attempt identity')
        require(not queued.done(), 'worker reservation released before completion')
        require(complete(worker, assignment, output='partial output') == (4, []), 'cleanup result rejected')
        result = pending.result(timeout=5)
        cancelled(result, job, 'reported')
        require(result[1][2] == 'partial output', 'cancelled output discarded')
        next_job = assigned(worker)
        require(next_job[0] == queued_id and complete(worker, next_job) == (4, []), 'queued job not dispatched after cleanup')
        require(queued.result(timeout=5)[0] == 9, 'next worker job inherited cancellation')

    with cluster.worker('lost-cancel', cancellable=True) as worker:
        pending = pool.submit(cluster.submit, 3)
        assignment = assigned(worker)
        require(cluster.request(31, [assignment[0]])[1][1] == 'CANCELLING', 'cancel request failed')
    cancelled(pending.result(timeout=5), assignment[0], 'unconfirmed')
    require(len(cluster.attempts(assignment[0])[1]) == 1, 'cancelled worker loss was retried')

    with cluster.worker('stale-cancel-result', cancellable=True) as worker:
        pending = pool.submit(cluster.submit, 3)
        assignment = assigned(worker)
        cluster.request(31, [assignment[0]])
        require(complete(worker, assignment, output='stale', attempt='2')[0] == 6,
                'stale cancellation result was accepted as cleanup')
        result = pending.result(timeout=5)
        cancelled(result, assignment[0], 'unconfirmed')
        require(result[1][2] == '', 'stale cancellation output was retained')

    with cluster.worker('lost-before-cancel') as worker:
        pending = pool.submit(cluster.submit, 3)
        assignment = assigned(worker)
    require(job_in(cluster, 'QUEUED') == assignment[0], 'lost job did not requeue')
    require(cluster.request(31, [assignment[0]])[1][2] == 'unconfirmed', 'old lost execution falsely declared not started')
    cancelled(pending.result(timeout=5), assignment[0], 'unconfirmed')
    require(cluster.attempts(assignment[0])[1][0][2] == 'LOST', 'cancel invented a replacement attempt')

    with cluster.worker('lost-before-replacement') as worker:
        pending = pool.submit(cluster.submit, 3)
        original = assigned(worker)
    with cluster.worker('replacement-cancel', cancellable=True) as worker:
        replacement = assigned(worker)
        require(replacement[:2] == [original[0], '2'], 'replacement identity incorrect')
        cluster.request(31, [original[0]])
        require(complete(worker, replacement) == (4, []), 'replacement completion rejected')
        cancelled(pending.result(timeout=5), original[0], 'unconfirmed')
        attempts = cluster.attempts(original[0])[1]
        require([a[2] for a in attempts] == ['LOST', 'CANCELLED'] and 'reported' in attempts[-1][5],
                'current cleanup report hid earlier unknown execution')

    with cluster.worker('legacy-running') as worker:
        pending = pool.submit(cluster.submit)
        assignment = assigned(worker)
        require(cluster.request(31, [assignment[0]])[0] == 6, 'unsupported running cancellation falsely accepted')
        require(cluster.attempts(assignment[0])[0][1] == 'RUNNING', 'rejected cancellation mutated legacy job')
        require(complete(worker, assignment) == (4, []) and pending.result(timeout=5)[0] == 9, 'legacy result changed')

    with cluster.worker('ignores-stop', cancellable=True) as worker:
        pending = pool.submit(cluster.submit, 3)
        assignment = assigned(worker)
        cluster.request(31, [assignment[0]])
        deadline = time.monotonic() + 8
        while True:
            worker.sendall(frame(2))
            kind, fields = receive(worker)
            if kind == 6:
                break
            require(kind == 32 and fields == assignment[:2], 'invalid repeated stop request')
            require(time.monotonic() < deadline, 'cancellation result deadline not enforced')
            time.sleep(.25)
        cancelled(pending.result(timeout=5), assignment[0], 'unconfirmed')
        require(len(cluster.attempts(assignment[0])[1]) == 1, 'ignored cancellation retried')

    with cluster.worker('race', cancellable=True) as worker:
        for _ in range(8):
            pending = pool.submit(cluster.submit, 0)
            assignment = assigned(worker)
            cancel_response = pool.submit(cluster.request, 31, [assignment[0]])
            require(complete(worker, assignment, output='race output') == (4, []), 'completion race corrupted acknowledgement')
            kind, result = pending.result(timeout=5)
            status_kind, status = cancel_response.result(timeout=5)
            require(status_kind == 34 and result[2] == 'race output', 'race response or output lost')
            if kind == 9:
                require(status[1] == 'SUCCEEDED', 'accepted success overwritten')
            else:
                cancelled((kind, result), assignment[0], 'reported')
                require(status[1] in ('CANCELLING', 'CANCELLED'), 'cancel winner not preserved')
            require(len(cluster.attempts(assignment[0])[1]) == 1, 'completion race created duplicate attempts')

with concurrent.futures.ThreadPoolExecutor() as pool, Cluster() as cluster, tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    marker = root / 'started.json'
    script = root / 'task.py'
    script.write_text(
        'import json,os,pathlib,subprocess,sys,time\n'
        'if pathlib.Path(sys.argv[1]).read_text()=="cancel":\n'
        '    child=subprocess.Popen(["/bin/sleep","30"])\n'
        '    data={"pid":os.getpid(),"child":child.pid,"cwd":os.getcwd()}\n'
        '    print(json.dumps(data),flush=True)\n'
        '    print("partial stderr",file=sys.stderr,flush=True)\n'
        f'    marker=pathlib.Path({str(marker)!r})\n'
        '    temporary=marker.with_suffix(".tmp"); temporary.write_text(json.dumps(data)); temporary.replace(marker)\n'
        '    time.sleep(25)\n'
        'else: print("next job succeeded")\n')
    inputs = [root / 'cancel.txt', root / 'next.txt']
    for path, data in zip(inputs, ('cancel', 'next')):
        path.write_text(data)
    output = root / 'results'
    cluster.start('join', '--id', 'real-cancel')
    wait_for(lambda: b'READY' in cluster.cli('cluster').stdout, 'real worker unavailable')
    owned = None
    try:
        pending = pool.submit(cluster.cli, 'map', '--retries', '3', '--jobs', '1', '--file', str(script),
                              '--output', str(output), *map(str, inputs), '--', 'python3', 'task.py', '{}')
        wait_for(marker.exists, 'staged process did not start')
        owned = json.loads(marker.read_text())
        job = job_in(cluster, 'RUNNING')
        response = cluster.cli('cancel', job)
        require(response.returncode == 0 and b'CANCELLING' in response.stderr, 'cancel CLI failed')
        result = pending.result(timeout=8)
        require(result.returncode == 1, 'partially cancelled map reported success')
        rows = [json.loads(line) for line in (output / 'results.jsonl').read_text().splitlines()]
        first, second = rows
        require(first['job_id'] == job and first['state'] == 'CANCELLED' and first['cancelled']
                and first['exit_code'] == 130 and first['cancellation_cleanup'] == 'reported'
                and not first['timed_out'] and first['storage_complete'], 'map cancellation metadata incorrect')
        require(second['state'] == 'SUCCEEDED' and not second['cancelled'] and second['cancellation_cleanup'] is None
                and (output / second['stdout']).read_text().strip() == 'next job succeeded', 'worker cancel flag leaked into next task')
        require(json.loads((output / first['stdout']).read_text()) == owned
                and b'partial stderr' in (output / first['stderr']).read_bytes(), 'partial streams lost during cancellation')
        wait_for(lambda: not_running(owned['pid']) and not_running(owned['child']), 'cancelled process group survived')
        require(not Path(owned['cwd']).exists(), 'cancelled workspace not removed')
        require(cluster.attempts(job)[1][0][2] == 'CANCELLED', 'cancelled attempt missing')
        logs = cluster.cli('logs', job)
        require(logs.returncode == 0 and b'partial stderr' in logs.stderr and b'CANCELLED cleanup=reported' in logs.stderr,
                'retained cancelled logs failed')
    finally:
        if owned:
            try:
                os.killpg(owned['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
            workspace = Path(owned['cwd'])
            require(workspace.parent == Path(tempfile.gettempdir()) and workspace.name.startswith('forge-job-'),
                    'refusing cleanup outside test-owned workspace')
            shutil.rmtree(workspace, ignore_errors=True)

with concurrent.futures.ThreadPoolExecutor() as pool, Cluster() as cluster:
    worker = cluster.start('join', '--id', 'shutdown-cancel')
    wait_for(lambda: b'READY' in cluster.cli('cluster').stdout, 'shutdown cancellation worker unavailable')
    pending = pool.submit(cluster.cli, 'run', '--retries', '3', '--', '/bin/sleep', '30')
    job = job_in(cluster, 'RUNNING')
    os.kill(worker.pid, signal.SIGSTOP)
    try:
        require(cluster.cli('cancel', job).returncode == 0, 'shutdown cancellation request failed')
        cluster.processes[0].terminate()
        result = pending.result(timeout=5)
        require(result.returncode == 130 and b'cleanup=unconfirmed' in result.stderr,
                'coordinator shutdown lost pending cancellation uncertainty')
        require(cluster.processes[0].wait(timeout=7) == 0, 'pending cancellation blocked coordinator shutdown')
    finally:
        if worker.poll() is None:
            os.kill(worker.pid, signal.SIGCONT)

print('Cancellation checks passed: queued/assigned/running, retries, unknown cleanup, stale attempts, deadline, completion races, process groups, staging, map output, worker reuse and shutdown')
