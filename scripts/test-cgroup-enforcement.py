"""Run real kernel enforcement checks in one disposable private cgroup namespace."""
import argparse
from pathlib import Path
import subprocess
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', default='build/release/forge')
    parser.add_argument('--image', default='forge-dev')
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    relative = Path(args.binary)
    if relative.is_absolute() or '..' in relative.parts or not (repository / relative).is_file():
        parser.error('--binary must name a Linux executable within the repository')
    name = 'forge-cgroup-test-' + uuid.uuid4().hex[:12]
    command = ['docker', 'run', '--rm', '--init', '--name', name, '--privileged', '--cgroupns', 'private',
               '--cpus', '2', '--memory', '512m', '--memory-swap', '512m',
               '--mount', f'type=bind,source={repository},target=/src,readonly',
               '-e', 'FORGE_PRIVATE_CGROUP_TEST=1', args.image,
               'python3', '/src/tests/cgroup_integration.py', '/src/' + relative.as_posix()]
    try:
        result = subprocess.run(command, timeout=150)
        return result.returncode
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)


if __name__ == '__main__':
    raise SystemExit(main())
