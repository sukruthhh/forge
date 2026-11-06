# Numerical parameter-sweep workload

```sh
python3 benchmarks/run.py --binary build/wsl-release/forge \
  --output build/physical-validation/new-simulation-run \
  --environment-kind wsl --label 'describe hardware, transport and background load' \
  --host 127.0.0.1 --workers 2 --cluster-kind physical-machines \
  --workload oscillator --steps 2000000 --tasks 8 \
  --repetitions 5 --warmups 1 --seed 20261106 --collect-phases
```

Configure the ordinary client TLS environment and idle external workers first.
Omit the host/cluster options for a managed local comparison. The output directory
must be new, with an existing parent. Timing, baseline and invalidation rules are
the same as [the benchmark harness](README.md). Prime counting remains the default;
its original workload file is unchanged so historical source hashes remain valid.

## What it models

Each task numerically integrates an independent linear oscillator with unit mass:

```text
x'' + 2*zeta*omega*x' + omega^2*x = 0
x(0) = 1, x'(0) = 0
```

The two first-order position/velocity equations use fourth-order Runge–Kutta,
with a fixed step size for the requested interval. Generated cases have angular
frequency omega = 1 + task_index/16, damping ratio zeta = .02 + .02*(task_index%8),
and duration 20 seconds. Changing --steps changes numerical resolution and CPU
work, not simulated duration or the expected solution. Each task returns final
position and velocity, parameter identity, source hash and execution telemetry.

This is a reproducible small scientific-simulation example that exercises the
independent parameter-sweep workflow. It uses generated model inputs, not external
user data, an industrial simulator or a validated customer workload. The model has
an analytic answer; numerical integration is intentionally chosen to demonstrate
the runtime workflow and is not the most efficient way to evaluate this equation.
It should not be described as production simulation performance.

The workload and one JSON input fit the existing flat-file staging bounds. Python
must already be installed on each eligible worker; no additional package is needed.
The helper stays self-contained so it can be staged as one file. Telemetry fields
match the prime workload, but the numerical implementation and oracle are separate.

## Correctness contract

The analytic oracle uses gamma = zeta*omega, wd = omega*sqrt(1-zeta^2):

```text
x(t) = exp(-gamma*t) * (cos(wd*t) + gamma/wd*sin(wd*t))
v(t) = -exp(-gamma*t) * omega^2/wd*sin(wd*t)
```

This is the underdamped oscillator solution with the stated initial conditions;
see the [MIT differential-equations notes](https://math.mit.edu/~poonen/03/notes.pdf).
The harness evaluates the closed-form oracle before timing, independently of the
RK4 loop. Both position and velocity must match using math.isclose with absolute
and relative tolerances of 1e-7. Parameters, task identity, integer step count,
workload SHA-256, finite timings and successful result storage must also match.
An incorrect or non-finite answer invalidates the task and entire profile; it
cannot contribute a performance improvement. Tolerances are recorded in metadata.

CLI bounds are 1–64 tasks and 10,000–2,000,000 integration steps per task, with at
least as many tasks as requested workers. The standalone helper additionally
validates omega in [.25, 5], zeta in [0, .9] and duration in [1, 20]. Zero damping
is supported as the undamped limit. --limit/--step belong to prime counting and
are rejected in oscillator mode; --steps is rejected in prime mode.

Tests cover the undamped quarter-period known answer, parameter-range corners,
all 64 generated cases at minimum resolution, mechanical-energy decrease for
damped cases, invalid inputs, tampered answers/parameters and non-finite values.
The integration test executes staged simulations through one/two managed workers
and checks metadata and CLI bounds. Numerical correctness is evidence for this
bounded model; it does not validate arbitrary differential equations or solvers.

No performance result follows from implementing the workload. Publish actual raw
trials, baseline comparisons, hardware/Python differences and negative outcomes
after running an experiment. No external-user or onboarding result is implied.
