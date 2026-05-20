# Remote-control system benchmark

## Host
- host:     `ValsSpectre`
- platform: `Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.39`
- kernel:   `6.6.87.2-microsoft-standard-WSL2`
- python:   `3.12.3`
- CPU:      `11th Gen Intel(R) Core(TM) i7-1195G7 @ 2.90GHz`
- cores:    logical=8  physical=4
- RAM:      total=7.6 GB  available=6.0 GB

## Results

| metric                                  | value         | unit |
|-----------------------------------------|---------------|------|
| cpu.sha256_singlethread                 |         1,424 | MB/s |
| cpu.sha256_8thread                      |         4,372 | MB/s |
| memory.bytearray_alloc                  |         1,931 | MB/s |
| memory.bytearray_copy_to_bytes          |         1,569 | MB/s |
| disk.seq_write                          |         1,364 | MB/s |
| disk.seq_read                           |         9,708 | MB/s |
| disk.random_4k_read                     |       554,403 | IOPS |
| disk.metadata_create_stat_unlink        |        25,314 | ops/s |
| python.cold_startup                     |         7.926 | ms/spawn |
| subprocess.true                         |         0.504 | ms/spawn |
| json.parse                              |         115.0 | MB/s |
| threading.pool_8_noop                   |       107,853 | tasks/s |
| net.api.anthropic.com                   |       TIMEOUT | ms (median, HEAD) |
| net.api.github.com                      |         150.7 | ms (median, HEAD) |
| net.raw.githubusercontent.com           |         253.9 | ms (median, HEAD) |
| net.pypi.org                            |          99.3 | ms (median, HEAD) |

## Aggregate score (higher = better, sum of throughput + inverse-latency)
`710,185`

> Save the full table above. To compare to another machine, run this same script there and diff the rows. The aggregate score is a quick single-number sanity check; the per-row numbers are the real comparison.
