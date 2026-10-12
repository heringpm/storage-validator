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
with Direct I/O (`--direct`). In addition to the per-OST checks,
each pool gets a real multi-threaded aggregate test: one scratch file is
single-striped onto each OST in the pool, then elbencho drives all of them
concurrently, measuring true aggregate MB/s and worst-case tail latency across
the pool — not an average of the independent per-OST results:

```
storage-validator \
    --pool-threshold flash:800:400:2:5 \
    --pool-threshold archive:100:20:20:80
```

Every check (per-OST and per-pool) allocates one dedicated scratch file per
worker thread — never a single file shared by all threads — and runs one
`elbencho` write pass followed by one read pass against that same set of
files, each pass deriving both its throughput ("MiB/s [last]") and its
latency ("IO lat us [max]") from its own output. The write pass populates the
files; the read pass reuses that data instead of writing it again, cutting
total runtime roughly in half compared to writing separately for each
direction. The write pass uses elbencho's `--sync` flag so each file is
fsynced to the OST backend before the pass exits, ensuring the write is
actually durable before the read pass starts — otherwise the two phases'
backend I/O can visibly overlap even though they run strictly sequentially
on the client. Scratch files are only removed once both passes have finished. By
default each elbencho invocation uses the host's total CPU thread count
(parsed from `lscpu`) as its worker thread/file count. This total is never
multiplied by the number of OSTs in a pool — a per-pool run still uses
exactly that many threads/files in total, cycled evenly across the pool's
OSTs, so a run on a single system never exceeds the host's thread count.
Override this with `--perf-threads` if you want a specific thread count
instead:

```
storage-validator --perf-threads 4
```

Tune the dataset size, block size, and per-pass runtime used by every
read/write test (defaults: 10g, 1m blocks, 60s):

```
storage-validator --size 2g --block-size 1m --timelimit 120
```

Point perf checks at a specific client mount (autodetected from `/proc/mounts`
otherwise):

```
storage-validator --mount-path /mnt/scratch --size 2g
```

Run elbencho in distributed mode across multiple client hosts (threads apply
per host, so throughput scales with the number of hosts), and drop OSS-side
page cache between each check's write and read passes so read results reflect
real disk I/O instead of server-side RAM:

```
storage-validator --hosts client1,client2 --oss-hosts oss1,oss2
```

Override the stripe count and/or stripe size used for perf scratch
directories (defaults: `-c 1`, i.e. each scratch file single-striped onto
one OST, and the filesystem's own default stripe size):

```
storage-validator --stripe-count 4 --stripe-size 4m
```

Save the raw elbencho CSV output and `.log` files (command line plus
stdout/stderr) for every check's write and read passes into a results
directory:

```
storage-validator --results-dir ./results
```

Run only the perf checks, skipping health checks and the topology/health
table:

```
storage-validator --perf-only
```

Print every `lfs`/`lctl`/`elbencho` command that would be run instead of
running it:

```
storage-validator --dry-run
```

The process exit code reflects the overall report status: `0` = PASS,
`1` = WARN, `2` = FAIL.

## Flags

| Flag | Default | Description |
| --- | --- | --- |
| `--backend TEXT` | `lustre` | Storage backend to validate. |
| `--mount-path TEXT` | autodetected | Client mount point to use for perf checks. |
| `--size TEXT` | `10g` | Size of each read/write elbencho test's dataset (e.g. `1g`, `500m`, `10g`). |
| `--block-size TEXT` | `1m` | elbencho I/O block size (e.g. `1m`, `4k`). |
| `--timelimit INTEGER` | `60` | elbencho test runtime (s) per read/write run. |
| `--timeout FLOAT` | `90.0` | Timeout (s) for each perf/health subprocess call. |
| `--warn-pct INTEGER` | `80` | OST/MDT usage % that triggers a WARN. |
| `--fail-pct INTEGER` | `95` | OST/MDT usage % that triggers a FAIL. |
| `--warn-mbps FLOAT` | `200.0` | Throughput (MB/s) below which to WARN. |
| `--fail-mbps FLOAT` | `50.0` | Throughput (MB/s) below which to FAIL. |
| `--warn-ms FLOAT` | `10.0` | Write latency (ms) above which to WARN. |
| `--fail-ms FLOAT` | `50.0` | Write latency (ms) above which to FAIL. |
| `--skip-perf` | off | Skip throughput/latency perf checks. |
| `--perf-only` | off | Run only the perf checks: skip health checks and the topology/health table output, printing just the perf table. |
| `--dry-run` | off | Print every `lfs`/`lctl`/`elbencho` command that would be run instead of running it. No real discovery/health/perf data is produced. |
| `--pool-threshold NAME:WARN_MBPS:FAIL_MBPS:WARN_MS:FAIL_MS` | none | Per-pool perf thresholds, overriding `--warn-mbps`/`--fail-mbps`/`--warn-ms`/`--fail-ms` for OSTs in that Lustre OST pool. Repeatable. |
| `--elbencho-path TEXT` | `elbencho` | Path to the elbencho binary used for perf checks, if not on `PATH`. |
| `--perf-threads INTEGER` | detected CPU thread count | Total elbencho worker thread count for a single perf test run (per-OST or per-pool). |
| `--ost NAME` | none | Restrict per-OST perf checks to this OST name (e.g. `scratch-OST0000`). Repeatable. Per-pool checks are unaffected. |
| `--pool NAME` | none | Restrict perf checks (both per-OST and per-pool) to this OST pool name. Repeatable. |
| `--hosts HOST1,HOST2,...` | none | Comma-separated list of hosts (reachable via passwordless SSH) to run elbencho on in distributed mode. Threads apply per host, so throughput scales with the number of hosts. Include this client's own hostname to have it participate too. |
| `--hosts-file FILE` | none | Path to a file with one hostname per line, added to `--hosts`. |
| `--oss-hosts HOST1,HOST2,...` | none | Comma-separated list of OSS/server hosts (reachable via passwordless SSH) whose page cache is dropped between each check's write and read passes, so read results measure real disk I/O instead of data served back out of server-side RAM (ZFS ARC / page cache). |
| `--oss-hosts-file FILE` | none | Path to a file with one OSS hostname per line, added to `--oss-hosts`. |
| `--stripe-count INTEGER` | `1` | `lfs setstripe -c` override for perf scratch directories (both per-OST and per-pool). Default: each scratch file single-striped onto one OST. |
| `--stripe-size SIZE` | filesystem default | `lfs setstripe -S` override for perf scratch directories (e.g. `1m`, `4m`). |
| `--results-dir DIRECTORY` | none | Save raw elbencho CSV output and `.log` files (command line + stdout/stderr) for every check's write and read passes into this directory. |
| `--json FILE` | none | Write the JSON report to this path. |
| `--quiet` | off | Suppress the console table output. |
| `--help` | — | Show the help message and exit. |

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
