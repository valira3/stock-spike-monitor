# Remote-control system benchmark

## Host
- host:     `Vals-MacBook-Pro.local`
- platform: `macOS-26.5-arm64-arm-64bit`
- kernel:   `25.5.0`
- python:   `3.9.6`
- CPU:      `unknown`
- cores:    logical=18  physical=0
- RAM:      total=0.0 B  available=0.0 B

## Results

| metric                                  | value         | unit |
|-----------------------------------------|---------------|------|
| cpu.sha256_singlethread                 |         3,232 | MB/s |
| cpu.sha256_18thread                     |        35,831 | MB/s |
| memory.bytearray_alloc                  |        21,970 | MB/s |
| memory.bytearray_copy_to_bytes          |        15,255 | MB/s |
| disk.seq_write                          |        16,463 | MB/s |
| disk.seq_read                           |        29,757 | MB/s |
| disk.random_4k_read                     |       949,912 | IOPS |
| disk.metadata_create_stat_unlink        |        17,138 | ops/s |
| python.cold_startup                     |          10.1 | ms/spawn |
| subprocess.true                         |         1.535 | ms/spawn |
| json.parse                              |         303.5 | MB/s |
| threading.pool_8_noop                   |       172,134 | tasks/s |
| net.api.anthropic.com                   |       TIMEOUT | ms (median, HEAD) |
| net.api.github.com                      |         151.0 | ms (median, HEAD) |
| net.raw.githubusercontent.com           |         251.4 | ms (median, HEAD) |
| net.pypi.org                            |          92.5 | ms (median, HEAD) |

## Aggregate score (higher = better, sum of throughput + inverse-latency)
`1,262,767`

> Save the full table above. To compare to another machine, run this same script there and diff the rows. The aggregate score is a quick single-number sanity check; the per-row numbers are the real comparison.
