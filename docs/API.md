# HTTP API

The UI uses these same routes. All evidence and state routes require a reviewer session or `Authorization: Bearer <GUARDIAN_REVIEW_TOKEN>`. Use `Content-Type: application/json` for mutations. Browsers must send requests from the same origin.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/healthz` | Agent process/scheduler liveness |
| GET | `/api/public-config` | Synthetic demo flag only |
| POST | `/api/session` | Exchange `{"token":"..."}` for a reviewer session |
| GET | `/api/state` | Checks, incidents, run state and recent jobs |
| POST | `/api/scan` | Queue a dashboard scan |
| GET | `/api/incidents/<id>` | Evidence, proposal and event timeline |
| POST | `/api/incidents/<id>/approve` | Queue an exact approved repair: `{"proposal_hash":"...","note":"..."}` |
| POST | `/api/incidents/<id>/reject` | Record rejection without modifying Grafana |
| POST | `/api/incidents/<id>/verify` | Check a human/external repair and record recovery |
| POST | `/api/incidents/<id>/close` | Close a rejected, stale or failed proposal so a new scan can reinvestigate |
| GET | `/api/incidents/<id>/rca` | Markdown RCA, including pending findings when unresolved |
| GET | `/api/evidence/<id>-before.png` | Original Grafana screenshot |
| GET | `/api/evidence/<id>-after.png` | Recovered Grafana screenshot |
| POST | `/api/demo/migrate` | Synthetic-only exporter label migration and scan |
| POST | `/api/demo/reset` | Restore the synthetic exporter/dashboard and scan |

Queued operations return HTTP 202 and a job ID. Read `/api/state` until that job is `completed` or `failed`; an accepted request alone does not mean a repair succeeded. Conflicting active jobs return 409. Authentication failures return 401. Cross-origin mutations return 403.

Production does not allow the demo session exchange `{"demo":true}` or simulation controls.

## Incident states

`awaiting_review → applying → verifying → resolved` is the successful agent repair path. Other states include `rejected`, `stale_approval`, `repair_failed` and `closed`. Rejected incidents suppress duplicate investigations while the same failure remains active. A healthy later scan can resolve an incident without applying an agent patch.

The proposal hash includes the exact before/after expression, target/refId, context, dashboard version/snapshot, contract hash and live candidate results. Approval fails if the dashboard or contract changed. A Grafana save conflict or missing write permission never bypasses approval/version checks.
