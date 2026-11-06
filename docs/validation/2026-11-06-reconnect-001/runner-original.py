import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

root = Path('/home/sukruth/forge-physical-pc')
binary = '/mnt/c/git/Forge/build/wsl-release/forge'
source = '9e34fac12a60cbb5d2ad24d586a65aca8e96d57f'
environment = dict(os.environ, FORGE_TLS_CA=str(root/'trust.pem'),
                   FORGE_TLS_CERT=str(root/'client.crt'), FORGE_TLS_KEY=str(root/'client.key'))
environment.pop('FORGE_TOKEN', None)
output = Path(__file__).parent / ('reconnect-' + uuid.uuid4().hex[:12])
output.mkdir()
report = {'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'runtime_source': source, 'pc_binary_sha256': hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
          'scope': 'One idle coordinator restart; laptop worker over mutual TLS inside SSH; no active-job recovery or latency benchmark',
          'passed': False}


def cli(*arguments):
    result = subprocess.run([binary, *arguments], env=environment, capture_output=True, text=True, timeout=12)
    return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}


def wait_for(check, seconds=20):
    deadline = time.monotonic() + seconds
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise RuntimeError('Readiness did not recover')
        time.sleep(.1)


def stop_owned(name, role):
    pid = int((root/(name+'.pid')).read_text())
    process = Path('/proc')/str(pid)
    if not process.exists():
        return
    arguments = (process/'cmdline').read_bytes().split(b'\0')
    if arguments[:2] != [binary.encode(), role.encode()]:
        raise RuntimeError('Test process identity changed')
    os.kill(pid, signal.SIGTERM)
    def exited():
        try:
            return (process/'stat').read_text().split(') ', 1)[1][0] == 'Z'
        except FileNotFoundError:
            return True
    wait_for(exited, 8)


def start_owned(name, arguments, env):
    log = (root/(name+'.log')).open('ab')
    process = subprocess.Popen([binary, *arguments], env=env, stdin=subprocess.DEVNULL,
                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (root/(name+'.pid')).write_text(str(process.pid))
    return process


def ready(name):
    result = cli('cluster', '--details')
    return result['returncode'] == 0 and any(row.split('\t')[0] == name and row.split('\t')[6] == 'READY'
                                           for row in result['stdout'].splitlines()[1:])


def probe():
    nonce = uuid.uuid4().hex
    script = '''import hashlib,json,os,pathlib,platform,socket,subprocess,sys
pid=os.getppid()
p=pathlib.Path('/proc')/str(pid)
repo='/mnt/c/git/Forge'
git=['git','-c','safe.directory='+repo,'-c','core.autocrlf=true','-C',repo]
head=subprocess.check_output(git+['rev-parse','HEAD'],text=True).strip()
clean=not subprocess.check_output(git+['status','--porcelain'],text=True).strip()
data=(p/'exe').read_bytes()
installed=pathlib.Path.home()/'forge-build/forge'
args=(p/'cmdline').read_bytes().split(b'\\0')
print(json.dumps({'nonce':sys.argv[1],'host':socket.gethostname(),'worker_pid':pid,
'worker_start_ticks':(p/'stat').read_text().split(') ',1)[1].split()[19],
'binary_sha256':hashlib.sha256(data).hexdigest(),'worker_matches_installed_binary':data==installed.read_bytes(),
'reconnect_enabled':b'--reconnect' in args,'git_head':head,'source_clean':clean,
'architecture':platform.machine(),'kernel':platform.release(),'python':platform.python_version()}))
'''
    result = cli('run', '--timeout', '10', '--', 'python3', '-c', script, nonce)
    if result['returncode'] != 0:
        raise RuntimeError(result['stderr'])
    observed = json.loads(result['stdout'])
    assert observed['nonce'] == nonce and observed['host'] == 'SukruthPC'
    assert observed['reconnect_enabled'] and observed['worker_matches_installed_binary']
    assert observed['git_head'] == source and observed['source_clean']
    return {'cli': result, 'observed': observed, 'expected_nonce': nonce}


coordinator_down = desktop_stopped = False
try:
    report['cluster_before'] = cli('cluster', '--details')
    jobs = cli('ps')
    assert jobs['returncode'] == 0
    assert not any(row.split('\t')[1] in ('QUEUED', 'ASSIGNED', 'RUNNING', 'CANCELLING')
                   for row in jobs['stdout'].splitlines()[1:])
    assert ready('laptop-native')
    stop_owned('desktop-worker', 'join')
    desktop_stopped = True
    report['before'] = probe()
    stop_owned('coordinator', 'coordinator')
    coordinator_down = True
    report['intentional_outage_after_exit_seconds'] = 3
    time.sleep(3)
    server = dict(environment, FORGE_TLS_CERT=str(root/'server-lan.crt'), FORGE_TLS_KEY=str(root/'server.key'))
    start_owned('coordinator', ['coordinator', '--bind', '127.0.0.1', '--port', '9000'], server)
    coordinator_down = False
    restarted_at = time.monotonic()
    wait_for(lambda: ready('laptop-native'))
    report['restart_to_first_ready_observation_seconds'] = time.monotonic() - restarted_at
    report['after'] = probe()
    for key in ('host', 'worker_pid', 'worker_start_ticks', 'binary_sha256'):
        assert report['before']['observed'][key] == report['after']['observed'][key], key
    assert report['before']['expected_nonce'] != report['after']['expected_nonce']
    report['passed'] = True
    print('PASS: same physical laptop worker process rejoined a restarted coordinator without manual join')
finally:
    if coordinator_down:
        server = dict(environment, FORGE_TLS_CERT=str(root/'server-lan.crt'), FORGE_TLS_KEY=str(root/'server.key'))
        start_owned('coordinator', ['coordinator', '--bind', '127.0.0.1', '--port', '9000'], server)
        wait_for(lambda: cli('cluster')['returncode'] == 0)
    if desktop_stopped:
        start_owned('desktop-worker', ['join', '--id', 'desktop-native', '--reconnect'], environment)
        wait_for(lambda: ready('desktop-native'))
    report['cluster_after'] = cli('cluster', '--details')
    (output/'results.json').write_text(json.dumps(report, indent=2)+'\n')
    print('Evidence:', output/'results.json')
