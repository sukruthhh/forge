import concurrent.futures
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import uuid

credentials = Path('/home/sukruth/forge-physical-pc')
binary = Path('/mnt/c/git/Forge/build/wsl-release/forge')
environment = dict(os.environ, FORGE_TLS_CA=str(credentials / 'trust.pem'),
                   FORGE_TLS_CERT=str(credentials / 'client.crt'),
                   FORGE_TLS_KEY=str(credentials / 'client.key'))
environment.pop('FORGE_TOKEN', None)
output = Path(__file__).parent / ('run-' + uuid.uuid4().hex[:12])
output.mkdir()
report = {'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'source_commit': 'c5ba40197cf10e6f0cb85508255877320c57abec',
          'pc_binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
          'transport': 'Forge mutual TLS over laptop-initiated restricted SSH tunnel',
          'scope': 'Two physical Windows hosts, native Ubuntu WSL2 workers; correctness only',
          'passed': False, 'tasks': []}


def cli(*arguments):
    started = time.monotonic()
    result = subprocess.run([str(binary), *arguments, '--host', '127.0.0.1']
                            if arguments[0] in ('cluster', 'ps', 'attempts')
                            else [str(binary), *arguments],
                            env=environment, capture_output=True, text=True, timeout=20)
    return {'returncode': result.returncode, 'stdout': result.stdout,
            'stderr': result.stderr, 'elapsed_seconds': time.monotonic() - started}


try:
    report['cluster_before'] = cli('cluster', '--details')
    rows = report['cluster_before']['stdout'].splitlines()[1:]
    ready = {row.split('\t')[0] for row in rows if row.split('\t')[6] == 'READY'}
    assert ready == {'desktop-native', 'laptop-native'}, rows
    with tempfile.TemporaryDirectory(prefix='forge-physical-inputs-') as temporary:
        root = Path(temporary)
        script = root / 'physical_probe.py'
        script.write_text('''import hashlib,json,pathlib,platform,socket,sys,time
path=pathlib.Path(sys.argv[1])
data=path.read_bytes()
value=json.loads(data)
time.sleep(3)
print(json.dumps({"hostname":socket.gethostname(),"architecture":platform.machine(),
"input_sha256":hashlib.sha256(data).hexdigest(),"nonce":value["nonce"],
"cwd":str(pathlib.Path.cwd())}))
''')
        barrier = threading.Barrier(2)

        def task(index):
            source = root / f'input-{index}.json'
            nonce = uuid.uuid4().hex
            source.write_text(json.dumps({'nonce': nonce, 'index': index}))
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            barrier.wait(timeout=5)
            result = cli('run', '--host', '127.0.0.1', '--timeout', '10',
                         '--file', str(script), '--file', str(source), '--',
                         'python3', script.name, source.name)
            result.update(index=index, expected_nonce=nonce, expected_input_sha256=digest)
            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            report['tasks'] = list(pool.map(task, (1, 2)))
    report['ps'] = cli('ps')
    jobs = {row.split('\t')[0]: row.split('\t') for row in report['ps']['stdout'].splitlines()[1:]}
    workers = set()
    hosts = set()
    for task in report['tasks']:
        assert task['returncode'] == 0, task
        actual = json.loads(task['stdout'])
        assert actual['nonce'] == task['expected_nonce'], task
        assert actual['input_sha256'] == task['expected_input_sha256'], task
        assert actual['cwd'].startswith('/tmp/forge-job-'), task
        match = re.search(r'\[forge\] (job-\d+) exit=0', task['stderr'])
        assert match, task
        job_id = match.group(1)
        assert jobs[job_id][1] == 'SUCCEEDED', jobs[job_id]
        task['job_id'] = job_id
        task['worker_id'] = jobs[job_id][2]
        task['attempts'] = cli('attempts', job_id)
        workers.add(task['worker_id'])
        hosts.add(actual['hostname'])
    assert workers == {'desktop-native', 'laptop-native'}, workers
    assert len(hosts) == 2, hosts
    report['cluster_after'] = cli('cluster', '--details')
    report['passed'] = True
    print('PASS: two concurrent staged jobs executed on desktop-native and laptop-native')
    print(json.dumps(report['tasks'], indent=2))
finally:
    (output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print('Evidence:', output / 'results.json')
