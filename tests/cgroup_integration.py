"""Real cgroup v2 checks; execute only through the private-container wrapper."""
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile

from runtime_fixture import Cluster, binary, environment, frame, receive, require, wait_for

require(os.environ.get('FORGE_PRIVATE_CGROUP_TEST') == '1', 'use scripts/test-cgroup-enforcement.py')
require(Path('/.dockerenv').is_file(), 'fixture must run in its disposable Docker container')
require(Path('/proc/self/cgroup').read_text().strip() == '0::/', 'fixture requires its own fresh cgroup namespace')
root = Path('/sys/fs/cgroup')
subprocess.run(['mount', '-o', 'remount,rw', str(root)], check=True)
manager = root / 'fixture-manager'
manager.mkdir()
for value in (root / 'cgroup.procs').read_text().split():
    require(int(value) != 0, 'private hierarchy unexpectedly contains an invisible process')
    try:
        (manager / 'cgroup.procs').write_text(value)
    except ProcessLookupError:
        pass
require(not (root / 'cgroup.procs').read_text().strip(), 'refusing controller changes while root has processes')
(root / 'cgroup.subtree_control').write_text('+cpu +memory')
jobs = root / 'fixture-jobs'
jobs.mkdir()
(jobs / 'cgroup.subtree_control').write_text('+cpu +memory')


def not_running(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] in ('Z', 'X')
    except FileNotFoundError:
        return True


def active_job(cluster):
    found = []
    def inspect():
        kind, fields = cluster.request(10)
        require(kind == 12, 'job inspection failed')
        found[:] = [fields[i] for i in range(0, len(fields), 5) if fields[i + 1] == 'RUNNING']
        return len(found) == 1
    wait_for(inspect, 'enforced job not running')
    return found[0]


with concurrent.futures.ThreadPoolExecutor() as pool, Cluster() as cluster, tempfile.TemporaryDirectory() as temporary:
    folder = Path(temporary)
    bad = root / 'fixture-no-controllers'
    bad.mkdir()
    result = cluster.cli('join', '--id', 'bad', '--cgroup-root', str(bad))
    require(result.returncode != 0 and b'bad\t' not in cluster.cli('cluster').stdout,
            'worker without enabled controllers registered')
    fractional = root / 'fixture-half-cpu'
    fractional.mkdir()
    (fractional / 'cpu.max').write_text('50000 100000')
    result = subprocess.run(['/bin/sh', '-c', 'printf "%s\\n" "$$" > "$1/cgroup.procs"; exec "$2" join --id half-cpu --port "$3"',
                             'fixture', str(fractional), binary, cluster.port], env=environment,
                            capture_output=True, timeout=8)
    require(result.returncode != 0 and b'less than one whole CPU' in result.stderr
            and b'half-cpu\t' not in cluster.cli('cluster').stdout, 'fractional quota was overadvertised')
    fractional.rmdir()
    def unprivileged():
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    os.chown(jobs, 65534, 65534)
    try:
        denied = subprocess.run([binary, 'join', '--id', 'cannot-migrate', '--port', cluster.port,
                                 '--cgroup-root', str(jobs)], env=environment, preexec_fn=unprivileged,
                                capture_output=True, timeout=8)
        require(denied.returncode != 0 and b'cannot launch a confined probe' in denied.stderr
                and b'cannot-migrate\t' not in cluster.cli('cluster').stdout,
                'worker advertised enforcement without migration permission')
        os.chown(root / 'cgroup.procs', 65534, 65534)
        os.chown(manager / 'cgroup.procs', 65534, 65534)
        delegated = subprocess.Popen([binary, 'join', '--id', 'unprivileged-enforcer', '--port', cluster.port,
                                      '--cgroup-root', str(jobs)], env=environment, preexec_fn=unprivileged,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cluster.processes.append(delegated)
        wait_for(lambda: b'unprivileged-enforcer\t' in cluster.cli('cluster').stdout,
                 'properly delegated unprivileged worker did not register')
        result = cluster.cli('run', '--enforce-limits', '--memory', str(32 * 1024 * 1024), '--', '/usr/bin/id', '-u')
        require(result.returncode == 0 and result.stdout.strip() == b'65534', 'delegation required a privileged job user')
        delegated.terminate()
        delegated.wait(timeout=7)
        wait_for(lambda: any(row.startswith('unprivileged-enforcer\t') and row.endswith('\tDEAD')
                             for row in cluster.cli('cluster').stdout.decode().splitlines()),
                 'delegated worker did not retire')
    finally:
        os.chown(jobs, 0, 0)
        os.chown(root / 'cgroup.procs', 0, 0)
        os.chown(manager / 'cgroup.procs', 0, 0)
    worker = cluster.start('join', '--id', 'enforcer', '--cgroup-root', str(jobs), '--reconnect')
    wait_for(lambda: b'enforcer\t' in cluster.cli('cluster').stdout, 'enforcing worker did not register')
    row = cluster.cli('cluster', '--details').stdout.decode().splitlines()[1].split('\t')
    require(row[9] == 'cgroup2-v1' and int(row[3]) == 2 and 0 < int(row[4]) <= 512 * 1024 * 1024,
            'worker ignored namespace quotas or failed to advertise enforcement')
    limited = ('--enforce-limits', '--cpu', '1', '--memory', str(128 * 1024 * 1024), '--timeout', '10')
    script = (
        'import json,pathlib,subprocess\n'
        'path=pathlib.Path("/sys/fs/cgroup")/pathlib.Path("/proc/self/cgroup").read_text().strip()[3:].lstrip("/")\n'
        'code="import time; end=time.monotonic()+2\\nwhile time.monotonic()<end: pass"\n'
        'children=[subprocess.Popen(["python3","-c",code]) for _ in range(2)]\n'
        '[p.wait() for p in children]\n'
        'print(json.dumps({"path":str(path),"cpu":(path/"cpu.max").read_text().strip(),'
        '"memory":(path/"memory.max").read_text().strip(),"swap":(path/"memory.swap.max").read_text().strip(),'
        '"cpu_stat":dict(line.split() for line in (path/"cpu.stat").read_text().splitlines())}))\n')
    result = cluster.cli('run', *limited, '--', 'python3', '-c', script)
    require(result.returncode == 0, result.stderr)
    observed = json.loads(result.stdout)
    require(observed['cpu'] == '100000 100000' and observed['memory'] == str(128 * 1024 * 1024)
            and observed['swap'] == '0' and int(observed['cpu_stat']['nr_throttled']) > 0,
            'kernel CPU throttling or memory/swap controls were not applied')
    require(not Path(observed['path']).exists(), 'normal completion leaked its cgroup')
    result = cluster.cli('run', '--enforce-limits', '--memory', str(32 * 1024 * 1024), '--retries', '3',
                         '--', 'python3', '-c', 'data=bytearray(128*1024*1024); print(len(data))')
    require(result.returncode == 137 and b'cgroup reports an OOM kill' in result.stderr, 'memory cap did not cause an observed kernel OOM kill')
    kind, listing = cluster.request(10)
    oom_job = listing[-5]
    require(len(cluster.attempts(oom_job)[1]) == 1 and b'READY' in cluster.cli('cluster').stdout,
            'workload OOM was retried or killed the worker')

    for mode in ('normal', 'timeout', 'cancel'):
        marker = folder / (mode + '.json')
        script = (
            'import json,os,pathlib,subprocess,time\n'
            'child=subprocess.Popen(["/bin/sleep","30"],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
            'path=pathlib.Path("/sys/fs/cgroup")/pathlib.Path("/proc/self/cgroup").read_text().strip()[3:].lstrip("/")\n'
            f'pathlib.Path({str(marker)!r}).write_text(json.dumps({{"pid":child.pid,"path":str(path)}}))\n'
            + ('time.sleep(20)\n' if mode != 'normal' else ''))
        options = ('--enforce-limits', '--memory', str(64 * 1024 * 1024), '--timeout', '1' if mode == 'timeout' else '30')
        pending = pool.submit(cluster.cli, 'run', *options, '--', 'python3', '-c', script)
        wait_for(lambda: marker.exists() and marker.stat().st_size > 0, 'escaped descendant fixture did not start')
        record = json.loads(marker.read_text())
        if mode == 'cancel':
            job_id = active_job(cluster)
            require(cluster.cli('cancel', job_id).returncode == 0, 'enforced cancellation failed')
        result = pending.result(timeout=8)
        require(result.returncode == {'normal': 0, 'timeout': 124, 'cancel': 130}[mode], result.stderr)
        wait_for(lambda: not_running(record['pid']), 'cgroup kill failed to stop a setsid descendant')
        require(not Path(record['path']).exists(), 'lifecycle cleanup left its cgroup')
        if mode == 'cancel':
            require(b'cleanup=reported' in result.stderr, 'successful enforced cleanup was not reported')

    source = folder / 'input'
    source.write_text('staged limits')
    output = folder / 'map-results'
    result = cluster.cli('map', '--enforce-limits', '--memory', str(32 * 1024 * 1024), '--output', str(output),
                         str(source), '--', '/bin/cat', '{}')
    require(result.returncode == 0 and (output / '0001.stdout').read_text() == 'staged limits', 'enforced staged map failed')
    marker = folder / 'must-not-execute'
    (jobs / 'cgroup.subtree_control').write_text('-memory')
    result = cluster.cli('run', *limited, '--', '/usr/bin/touch', str(marker))
    require(result.returncode == 125 and not marker.exists() and b'memory.max' in result.stderr,
            'failed resource setup executed unrestricted work')
    (jobs / 'cgroup.subtree_control').write_text('+memory')
    require(cluster.cli('run', *limited, '--', '/bin/true').returncode == 0, 'clean setup failure poisoned the worker')

    marker = folder / 'nested.json'
    script = ('import pathlib,time\n'
              'path=pathlib.Path("/sys/fs/cgroup")/pathlib.Path("/proc/self/cgroup").read_text().strip()[3:].lstrip("/")\n'
              '(path/"nested").mkdir()\n'
              f'pathlib.Path({str(marker)!r}).write_text(str(path))\n'
              'time.sleep(20)\n')
    pending = pool.submit(cluster.cli, 'run', '--enforce-limits', '--memory', str(32 * 1024 * 1024),
                          '--retries', '3', '--', 'python3', '-c', script)
    wait_for(lambda: marker.exists() and marker.stat().st_size > 0, 'cleanup-failure fixture did not start')
    job_id = active_job(cluster)
    cluster.cli('cancel', job_id)
    result = pending.result(timeout=8)
    require(result.returncode == 130 and b'cleanup=unconfirmed' in result.stderr, 'cleanup failure falsely confirmed cancellation')
    wait_for(lambda: worker.poll() is not None, 'worker reused capacity after cgroup cleanup failure')
    leftover = Path(marker.read_text())
    require(leftover.parent == jobs and leftover.name.startswith('forge-'), 'refusing cleanup outside fixture-owned group')
    (leftover / 'nested').rmdir()
    leftover.rmdir()
    require(not list(jobs.glob('forge-*')), 'fixture leaked owned job cgroups')
    marker = folder / 'disconnect-nested.json'
    script = ('import pathlib,time\n'
              'path=pathlib.Path("/sys/fs/cgroup")/pathlib.Path("/proc/self/cgroup").read_text().strip()[3:].lstrip("/")\n'
              '(path/"nested").mkdir()\n'
              f'pathlib.Path({str(marker)!r}).write_text(str(path))\n'
              'time.sleep(20)\n')
    with socket.socket() as fake:
        fake.bind(('127.0.0.1', 0))
        fake.listen()
        fake.settimeout(8)
        disconnected = subprocess.Popen([binary, 'join', '--id', 'disconnect-enforcer', '--reconnect',
                                         '--cgroup-root', str(jobs), '--port', str(fake.getsockname()[1])],
                                        env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cluster.processes.append(disconnected)
        peer, _ = fake.accept()
        with peer:
            peer.settimeout(6)
            require(receive(peer)[0] == 1, 'limited worker registration missing')
            peer.sendall(frame(4))
            require(receive(peer) == (2, []), 'limited worker heartbeat missing')
            peer.sendall(frame(35, ['job-1', '1', '30', '1', str(32 * 1024 * 1024), 'python3', '-c', script]))
            wait_for(lambda: marker.exists() and marker.stat().st_size > 0, 'disconnect cleanup fixture did not start')
        wait_for(lambda: disconnected.poll() is not None, 'cleanup failure during disconnect reconnected the worker')
        require(disconnected.returncode != 0, 'failed resource cleanup reported successful shutdown')
        fake.settimeout(.5)
        try:
            unexpected, _ = fake.accept()
        except socket.timeout:
            pass
        else:
            unexpected.close()
            raise AssertionError('worker readmitted after failed cgroup cleanup')
    leftover = Path(marker.read_text())
    require(leftover.parent == jobs and leftover.name.startswith('forge-'), 'refusing cleanup outside owned cgroup')
    (leftover / 'nested').rmdir()
    leftover.rmdir()
    subprocess.run(['mount', '--bind', str(jobs), str(jobs)], check=True)
    subprocess.run(['mount', '-o', 'remount,bind,ro', str(jobs)], check=True)
    result = cluster.cli('join', '--id', 'readonly', '--cgroup-root', str(jobs))
    require(result.returncode != 0 and b'readonly\t' not in cluster.cli('cluster').stdout, 'read-only hierarchy advertised enforcement')

print('PASS: real cgroups v2 CPU throttling, memory OOM, staged map, setsid cleanup, timeout/cancel, setup refusal and cleanup-failure retirement')
print('Only the disposable container subtree was changed; unrelated host cgroups were not modified. No performance or sandbox claim.')
