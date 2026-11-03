#!/bin/sh
# Run inside forge-dev. Isolated output avoids concurrent writes to demo binaries.
set -u
cd /workspace
previous=
trap 'exit 0' INT TERM
while :; do
    current=$(
        { sha256sum CMakeLists.txt
          find src include tests scripts -type f ! -path '*/__pycache__/*' -print0 |
              sort -z | xargs -0 sha256sum
        } | sha256sum
    )
    if [ "$current" != "$previous" ]; then
        previous=$current
        printf '\n[forge-watch] source change; building and testing\n'
        if cmake -S . -B build/watch -G Ninja -DCMAKE_BUILD_TYPE=Debug &&
           cmake --build build/watch --parallel 2 &&
           ctest --test-dir build/watch --output-on-failure; then
            printf '[forge-watch] PASS; waiting for changes\n'
        else
            printf '[forge-watch] FAIL; waiting for changes\n'
        fi
    fi
    sleep 2 &
    wait $! || exit 0
done
