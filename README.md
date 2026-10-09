# storage-validator

A single-node CLI that validates a Lustre filesystem: it discovers the
topology (MDTs, OSTs, pools, client mounts), runs health checks, runs basic
per-OST throughput/latency perf checks, and emits a console report plus an
optional JSON report.

Storage backend logic sits behind an abstract `StorageBackend` interface
(`storage_validator.backends.base`), so additional filesystems (e.g. GPFS)
can be added later without touching the CLI, engine, or reporting code.

## Installation

```
pip install -e .[dev]
```

Requires Python >= 3.9, and the `lfs`/`lctl` Lustre client tools available on
`PATH` for the `lustre` backend. Perf checks (both per-OST and per-pool) also
require [`elbencho`](https://github.com/breuner/elbencho) on `PATH`.

## Usage

Run a full validation (discovery + health + perf) against the local Lustre
client, printing a console report:

```
storage-validator
```

Skip the perf checks (discovery + health only):

```
storage-validator --skip-perf
```

Write a JSON report in addition to (or instead of) the console table:

```
storage-validator --json report.json
storage-validator --quiet --json report.json
```

Tune thresholds:

```
storage-validator --warn-pct 75 --fail-pct 90 \
    --warn-mbps 300 --fail-mbps 100 \
    --warn-ms 5 --fail-ms 25
```

Use different perf thresholds per Lustre OST pool (e.g. mixed drive types like
flash vs. archive). Each `--pool-threshold` applies to OSTs in that pool and is
repeatable; OSTs in pools without an override, or not in any pool, use the
`--warn-mbps`/`--fail-mbps`/`--warn-ms`/`--fail-ms` defaults. Both the
per-OST checks and the per-pool checks use
[`elbencho`](https://github.com/breuner/elbencho) (must be installed and on
`PATH`, or pointed to explicitly with `--elbencho-path /path/to/elbencho`)
instead of single-threaded `dd`. In addition to the per-OST checks,
each pool gets a real multi-threaded aggregate test: one scratch file is
single-striped onto each OST in the pool, then elbencho drives all of them
concurrently, measuring true aggregate MB/s and worst-case tail latency across
the pool — not an average of the independent per-OST results:

```
storage-validator \
    --pool-threshold flash:800:400:2:5 \
    --pool-threshold archive:100:20:20:80
```

By default each elbencho invocation uses the host's total CPU thread count
(parsed from `lscpu`) as its worker thread count — for a per-OST check that
means `N` threads driving the one scratch file on that OST, and for a
per-pool check it means `N` threads per OST scratch file (so a pool with 4
OSTs on a 16-thread host runs with 64 total worker threads). Override this
with `--perf-threads` if you want a specific thread count instead (e.g. to
match an expected client concurrency, or to avoid oversubscribing the host):

```
storage-validator --perf-threads 4
```

Point perf checks at a specific client mount (autodetected from `/proc/mounts`
otherwise):

```
storage-validator --mount-path /mnt/scratch --size-mb 512
```

The process exit code reflects the overall report status: `0` = PASS,
`1` = WARN, `2` = FAIL.

## Running tests

```
pytest
```

## Future work

- Multi-client support: aggregate discovery/health/perf results across
  several client nodes instead of a single local node.
- GPFS backend: implement `StorageBackend` for GPFS (`mmlsfs`, `mmdf`,
  `mmgetstate`, etc.) alongside the existing Lustre backend.
- Daemon mode: run validation on a schedule and expose results via an
  API/metrics endpoint instead of a one-shot CLI invocation.
- Richer perf checks: `fio`-based IOPS/latency percentiles, concurrent
  multi-OST throughput aggregation.
