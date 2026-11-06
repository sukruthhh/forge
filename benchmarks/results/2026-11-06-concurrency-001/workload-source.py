"""Independent damped-oscillator simulations with an analytic correctness oracle."""
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import socket
import sys
import time

MIN_STEPS = 10_000
MAX_STEPS = 2_000_000
TOLERANCE = 1e-7
PARAMETERS = ('steps', 'omega', 'damping', 'duration')


def validate(config):
    if (config.get('workload') != 'oscillator' or type(config.get('task')) is not int
            or not 0 <= config['task'] < 64 or type(config.get('steps')) is not int
            or not MIN_STEPS <= config['steps'] <= MAX_STEPS):
        raise ValueError('invalid oscillator task or integration steps')
    for key, lower, upper in (('omega', .25, 5), ('damping', 0, .9), ('duration', 1, 20)):
        value = config.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError('invalid oscillator parameter: ' + key)


def make_task(index, steps):
    config = {'workload': 'oscillator', 'task': index, 'steps': steps,
              'omega': 1 + index / 16, 'damping': .02 + .02 * (index % 8), 'duration': 20.0}
    validate(config)
    return config


def reference(config):
    validate(config)
    omega, damping, duration = (config[key] for key in ('omega', 'damping', 'duration'))
    decay = damping * omega
    frequency = omega * math.sqrt(1 - damping * damping)
    sine, cosine = math.sin(frequency * duration), math.cos(frequency * duration)
    envelope = math.exp(-decay * duration)
    return {'position': envelope * (cosine + decay / frequency * sine),
            'velocity': -envelope * omega * omega / frequency * sine}


def solve(config):
    validate(config)
    stiffness = config['omega'] ** 2
    drag = 2 * config['damping'] * config['omega']
    step = config['duration'] / config['steps']
    half, sixth = step / 2, step / 6
    position, velocity = 1.0, 0.0
    for _ in range(config['steps']):
        dx1 = velocity
        dv1 = -drag * velocity - stiffness * position
        dx2 = velocity + half * dv1
        dv2 = -drag * dx2 - stiffness * (position + half * dx1)
        dx3 = velocity + half * dv2
        dv3 = -drag * dx3 - stiffness * (position + half * dx2)
        dx4 = velocity + step * dv3
        dv4 = -drag * dx4 - stiffness * (position + step * dx3)
        position += sixth * (dx1 + 2 * dx2 + 2 * dx3 + dx4)
        velocity += sixth * (dv1 + 2 * dv2 + 2 * dv3 + dv4)
    return {'position': position, 'velocity': velocity}


def check_result(config, observed, expected):
    if observed.get('workload') != 'oscillator':
        raise ValueError('wrong oscillator workload identity')
    for key in PARAMETERS:
        value = observed.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value != config[key]:
            raise ValueError('wrong oscillator parameter: ' + key)
    if type(observed['steps']) is not int:
        raise ValueError('integration steps must be an integer')
    for key in ('position', 'velocity'):
        value = observed.get(key)
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not math.isclose(value, expected[key], rel_tol=TOLERANCE, abs_tol=TOLERANCE)):
            raise ValueError('oscillator answer outside analytic tolerance: ' + key)


def read_optional(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def main():
    if sys.platform != 'linux':
        raise SystemExit('run inside Linux, WSL, or a Linux container')
    import resource
    config = json.loads(Path(sys.argv[1]).read_text())
    validate(config)
    before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.perf_counter()
    answer = solve(config)
    elapsed = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_SELF)
    cpu = read_optional('/proc/cpuinfo') or ''
    model = next((line.split(':', 1)[1].strip() for line in cpu.splitlines()
                  if line.startswith(('model name', 'Hardware')) and ':' in line), 'unknown')
    print(json.dumps(dict(config, **answer, schema=1, compute_seconds=elapsed,
                          cpu_seconds=after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
                          host=socket.gethostname(), worker_pid=os.getppid(),
                          architecture=platform.machine(), python=platform.python_version(),
                          cpu_model=model, cpu_affinity_count=len(os.sched_getaffinity(0)),
                          cgroup_cpu_max=read_optional('/sys/fs/cgroup/cpu.max'),
                          cgroup_memory_max=read_optional('/sys/fs/cgroup/memory.max'),
                          workload_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()),
                     sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
