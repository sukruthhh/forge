"""Summarize one JSON input using only the Python standard library."""
import json
import math
from pathlib import Path
import platform
import socket
import statistics
import sys

document = json.loads(Path(sys.argv[1]).read_text())
values = document["values"]
if not isinstance(values, list) or not values or any(
    type(value) not in (int, float) or not math.isfinite(value) for value in values
):
    raise ValueError("values must be a nonempty list of finite numbers")
print(json.dumps({
    "input": Path(sys.argv[1]).name,
    "worker": socket.gethostname(),
    "architecture": platform.machine(),
    "count": len(values),
    "mean": statistics.fmean(values),
    "min": min(values),
    "max": max(values),
}, sort_keys=True))
