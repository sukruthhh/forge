import concurrent.futures
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

credentials = Path('/home/sukruth/forge-physical-pc')
binary = '/mnt/c/git/Forge/build/wsl-release/forge'
environment = dict(os.environ, FORGE_TLS_CA=str(credentials/'trust.pem'),
                   FORGE_TLS_CERT=str(credentials/'client.crt'),
                   FORGE_TLS_KEY=str(credentials/'client.key'))
environment.pop('FORGE_TOKEN', None)
output = Path(__file__).parent / ('recovery-' + uuid.uuid4().hex[:12])
output.mkdir()
report = {'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'runtime_source': 'c5ba40197cf10e6f0cb85508255877320c57abec',
          'pc_binary_sha256': hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
          'fault': 'SIGKILL of PC worker process, not physical power loss or a network partition',
          'transport': 'PC loopback TLS; replacement laptop over mutual TLS inside SSH',
          'observation_boundary': 'PC monotonic clock: signal request to first observed LOST inspection / completed submission result; inspection delay included',
          'workload': 'retry-safe staged input hash and nonce; eight-second sleep per attempt; not application recovery latency',
          'passed': False, 'trials': []}


def cli(*arguments):
    result = subprocess.run([binary, *arguments], env=environment,
                            capture_output=True, text=True, timeout=25)
    return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}


def wait_for(check, seconds=5):
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise RuntimeError('Condition did not become true')
        time.sleep(.025)


def worker_ready():
    result = cli('cluster', '--details')
    return result['returncode'] == 0 and any(
        row.startswith('desktop-native\t') and row.split('\t')[6] == 'READY'
        for row in result['stdout'].splitlines()[1:])


def restart_desktop():
    log = (credentials/'desktop-worker.log').open('ab')
    process = subprocess.Popen([binary, 'join', '--id', 'desktop-native'], env=environment,
                               stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
    (credentials/'desktop-worker.pid').write_text(str(process.pid))
    wait_for(worker_ready)


try:
    with tempfile.TemporaryDirectory(prefix='forge-recovery-measure-') as temporary:
        root = Path(temporary)
        script = root/'failover_probe.py'
        script.write_text('''import hashlib,json,pathlib,socket,sys,time
data=pathlib.Path(sys.argv[1]).read_bytes()
value=json.loads(data)
time.sleep(8)
print(json.dumps({"host":socket.gethostname(),"nonce":value["nonce"],"sha256":hashlib.sha256(data).hexdigest()}))
''')
        for index in range(5):
            trial = {'index': index, 'passed': False}
            report['trials'].append(trial)
            sources = []
            for task in range(2):
                source = root/f'{root.name}-trial-{index}-task-{task}.json'
                source.write_text(json.dumps({'nonce': uuid.uuid4().hex, 'task': task}))
                sources.append(source)

            def submit(source):
                return cli('run', '--retries', '1', '--timeout', '15', '--file', str(script),
                           '--file', str(source), '--', 'python3', script.name, source.name)

            workspace = child = child_identity = None
            crashed = False
            futures = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                try:
                    futures = [pool.submit(submit, source) for source in sources]

                    def assigned():
                        rows = [row.split('\t') for row in cli('ps')['stdout'].splitlines()[1:]]
                        rows = [row for row in rows if any(row[4].endswith(source.name) for source in sources)]
                        return rows if len(rows) == 2 and all(row[1] == 'RUNNING' for row in rows) else None

                    rows = wait_for(assigned)
                    victim = next(row for row in rows if row[2] == 'desktop-native')
                    blocker = next(row for row in rows if row[2] == 'laptop-native')
                    victim_index = next(i for i, source in enumerate(sources) if victim[4].endswith(source.name))
                    trial.update(job_id=victim[0], cancelled_job_id=blocker[0], assignment_rows=rows)
                    trial['cancel_result'] = cli('cancel', blocker[0])
                    assert trial['cancel_result']['returncode'] == 0
                    trial['cancelled_submission'] = futures[1-victim_index].result(timeout=8)
                    assert trial['cancelled_submission']['returncode'] == 130
                    pid = int((credentials/'desktop-worker.pid').read_text())
                    arguments = (Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
                    assert arguments[:2] == [binary.encode(), b'join'] and b'desktop-native' in arguments
                    children = sorted({child for path in (Path('/proc')/str(pid)/'task').glob('*/children')
                                       for child in path.read_text().split()})
                    assert len(children) == 1, children
                    child = int(children[0])
                    child_path = Path('/proc')/str(child)
                    assert b'failover_probe.py' in (child_path/'cmdline').read_bytes()
                    child_identity = (child_path/'stat').read_text().split(') ', 1)[1].split()[19]
                    assert os.getpgid(child) == child
                    workspace = (child_path/'cwd').resolve()
                    assert workspace.parent == Path('/tmp') and workspace.name.startswith('forge-job-')
                    fault_at = time.monotonic()
                    os.kill(pid, signal.SIGKILL)
                    crashed = True
                    trial['signal_call_seconds'] = time.monotonic() - fault_at

                    def loss_observed():
                        snapshot = cli('attempts', victim[0])
                        return snapshot if '\tdesktop-native\tLOST\t' in snapshot['stdout'] else None

                    trial['first_loss_snapshot'] = wait_for(loss_observed)
                    trial['signal_to_first_loss_observation_seconds'] = time.monotonic() - fault_at
                    trial['old_child_visible_after_loss'] = child_path.exists()
                    result = futures[victim_index].result(timeout=20)
                    trial['signal_to_submission_result_seconds'] = time.monotonic() - fault_at
                    trial['result'] = result
                    assert result['returncode'] == 0, result
                    actual = json.loads(result['stdout'])
                    expected_bytes = sources[victim_index].read_bytes()
                    assert actual['host'] == 'SukruthPC'
                    assert actual['nonce'] == json.loads(expected_bytes)['nonce']
                    assert actual['sha256'] == hashlib.sha256(expected_bytes).hexdigest()
                    trial['attempts'] = cli('attempts', victim[0])
                    attempts = [row.split('\t') for row in trial['attempts']['stdout'].splitlines()[1:]]
                    assert len(attempts) == 2
                    assert attempts[0][1:3] == ['desktop-native', 'LOST']
                    assert attempts[1][1:4] == ['laptop-native', 'SUCCEEDED', '0']
                    trial['passed'] = True
                finally:
                    owned = [row.split('\t') for row in cli('ps')['stdout'].splitlines()[1:]]
                    owned = [row for row in owned if any(row[4].endswith(source.name) for source in sources)]
                    for row in owned:
                        cli('cancel', row[0])
                    if child is not None:
                        child_path = Path('/proc')/str(child)
                        try:
                            if (child_path/'stat').read_text().split(') ', 1)[1].split()[19] == child_identity:
                                os.killpg(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        except FileNotFoundError:
                            pass
                    if workspace is not None and workspace.exists():
                        assert workspace.parent == Path('/tmp') and workspace.name.startswith('forge-job-')
                        assert (workspace/script.name).read_bytes() == script.read_bytes()
                        shutil.rmtree(workspace)
                    if crashed:
                        restart_desktop()
            print('Trial', index+1, 'passed:', trial['passed'],
                  'signal-to-result', trial.get('signal_to_submission_result_seconds'), flush=True)
            (output/'results.json').write_text(json.dumps(report, indent=2)+'\n')
    report['passed'] = all(trial['passed'] for trial in report['trials'])
finally:
    (output/'results.json').write_text(json.dumps(report, indent=2)+'\n')
    print('Evidence:', output/'results.json', flush=True)
