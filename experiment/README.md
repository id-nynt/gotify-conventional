# Gotify experiment operations

Run these files in WSL/Linux. `operations.py` performs exactly one requested
operation; it never chooses retries, restart or rollback. The two repositories
must retain byte-identical copies of the shared operations, Compose and browser
probe. Controller-specific files are separate.

Each call requires `--approach conventional|bdi` and a unique `--trial`.
Deployment calls also require `--environment staging|production` and
`--release v1|v2`. Run `python3 experiment/operations.py --help` for the CLI.

| Operation | Effect |
|---|---|
| prepare | Reserve the laptop for one trial; create isolated credentials and data directories from an immutable image manifest |
| reset | Deploy v1 into the new trial's empty environment; never delete an earlier trial's database |
| probe | Verify container/image/trial, health, version, exact message, baseline sentinel and dashboard/reload |
| observe | One fresh identity, health, version, message and persistence sample; no retry or recovery |
| deploy | Deploy the selected immutable image after the environment's baseline has passed a full probe |
| diagnose | Report exact container/execution identity, running state and read-only SQLite integrity |
| restart | Restart the identified container once; the controller must request verification afterwards |
| rollback | Restore the verified v1 image and unchanged Compose configuration, retaining the same data directory |
| evidence | Inventory the public evidence files |
| finish | Record an explicit outcome and release the trial reservation; retain containers, data and evidence |

The runtime root defaults to `$HOME/gotify-study-runtime`. Private passwords and
tokens are under `trials/<approach>/<trial>/`, outside the Git checkout. Publish
only its `evidence/` subdirectory. A persistent trial reservation prevents
cross-repository interference; a Linux file lock serializes individual operations.
An interrupted controller leaves the reservation in place for reconciliation.

Ports are conventional 8101/8100 and BDI 8201/8200 (staging/production).
No monitoring/helper listener is needed: the collector uses Docker inspect and
loopback Gotify HTTP endpoints. It records UTC timestamps, HTTP status/duration,
actual and expected image identities, content checks and observation results in
JSON receipts. Observation calls sample on demand; the controller chooses the
interval and limits. These are probe results, not production workload metrics.
Each probe includes request count/failures, error fraction and p95 request time.
Both controllers use full `probe` observations, including the dashboard. The
deployment execution ID ties observations and repair receipts to one deployment.

`boundary.py` optionally waits at production v2, after deployment identity is
verified and before the first controller observation. An external harness must
register the exact trial before deployment and acknowledge it within 60 seconds.
The boundary does not know the scenario or choose recovery. Unarmed deployments
continue immediately. Healthy and stopped-candidate pilots use the same boundary.

The local rehearsal uses the same Gotify source for both releases, distinguished
by `experiment-v1` and `experiment-v2` metadata. It tests deployment/recovery
mechanics, not database schema migration. It is not a measured experiment.
