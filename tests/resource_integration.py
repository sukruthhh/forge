"""Verify unconfigured worker discovery and fail-closed enforced-job admission."""
import os
from pathlib import Path
import tempfile
import subprocess

from runtime_fixture import Cluster, binary, environment, require, wait_for

with Cluster() as cluster, tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    invalid = cluster.cli('join', '--id', 'invalid-root', '--cgroup-root', str(root))
    require(invalid.returncode != 0 and b'cgroup v2' in invalid.stderr, 'ordinary directory enabled enforcement')
    require(b'invalid-root' not in cluster.cli('cluster').stdout, 'invalid enforcing worker registered')
    affinity = sorted(os.sched_getaffinity(0))
    worker = cluster.start('join', '--id', 'capacity-worker')
    wait_for(lambda: b'capacity-worker' in cluster.cli('cluster').stdout, 'worker discovery failed')
    row = cluster.cli('cluster', '--details').stdout.decode().splitlines()[1].split('\t')
    require(len(row) == 10 and 1 <= int(row[3]) <= len(affinity) and int(row[4]) > 0 and row[9] == '',
            'unconfigured worker claims enforcement or impossible capacity')
    restricted = subprocess.Popen([binary, 'join', '--id', 'affinity-worker', '--port', cluster.port], env=environment,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  preexec_fn=lambda: os.sched_setaffinity(0, {affinity[0]}))
    cluster.processes.append(restricted)
    wait_for(lambda: b'affinity-worker\t' in cluster.cli('cluster').stdout, 'affinity-restricted worker did not join')
    rows = cluster.cli('cluster').stdout.decode().splitlines()[1:]
    require(next(row.split('\t')[3] for row in rows if row.startswith('affinity-worker\t')) == '1',
            'worker ignored a real affinity restriction')
    restricted.terminate()
    restricted.wait(timeout=7)
    quota_path = Path('/sys/fs/cgroup/cpu.max')
    quota = quota_path.read_text().split() if quota_path.exists() else ['max', '100000']
    if quota[0] != 'max':
        require(int(row[3]) <= int(quota[0]) // int(quota[1]), 'worker ignored visible CPU quota')
    memory_path = Path('/sys/fs/cgroup/memory.max')
    memory = memory_path.read_text().strip() if memory_path.exists() else 'max'
    if memory != 'max':
        require(int(row[4]) <= int(memory), 'worker ignored visible memory limit')
    invalid = cluster.cli('run', '--enforce-limits', '--', '/bin/true')
    require(invalid.returncode != 0, 'enforcement accepted without explicit memory limit')
    result = cluster.cli('run', '--enforce-limits', '--memory', '33554432', '--', '/bin/true')
    require(result.returncode != 0 and b'no available compatible worker' in result.stderr,
            'enforced job silently fell back to an unconfigured worker')
    path = root / 'input'
    path.write_text('value')
    output = root / 'out'
    result = cluster.cli('map', '--enforce-limits', '--memory', '33554432', '--output', str(output),
                         str(path), '--', '/bin/cat', '{}')
    require(result.returncode != 0 and b'cgroup2-v1' in result.stderr and not output.exists(),
            'map did not preflight enforcing capacity')
    require(cluster.cli('run', '--', '/bin/true').returncode == 0, 'ordinary execution regressed')
    for kind in (37, 38):
        require(cluster.request(kind, ['1', '0', '5', 'linux', '', '0', '/bin/true'])[0] == 6,
                'raw enforced zero-memory submission accepted')
    worker.terminate()
    worker.wait(timeout=7)

print('Resource integration passed: bounded discovery, invalid delegation, enforcement gating, map preflight and ordinary execution')
