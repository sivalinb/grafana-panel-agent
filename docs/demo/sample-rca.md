# Monitoring failure RCA: Checkout queue depth

- Incident: `03e665d0b151`
- Dashboard: Checkout · Production
- Panel ID: 1
- Status: resolved
- Detected: 2026-10-12T02:13:20.164+00:00
- Resolved: 2026-10-12T02:13:32.951+00:00
- Reviewer: demo-reviewer
- Context: `{"environment": "prod", "service": "checkout"}`

## Findings

The panel uses the obsolete 'app' label matcher; live telemetry responds to 'service'.

Cause status: **repair_verified**.
A successful repair confirms recovery; it does not by itself prove every contributing cause.

## Repair

```diff
- guardian_checkout_queue_depth{app="$service",environment="$environment"}
+ guardian_checkout_queue_depth{service="$service",environment="$environment"}
```

Proposal SHA-256: `5e349154f634b12a42fbc3fa7efac87b49fc444be81af4be73f8019d7751dcbb`

## Evidence

### e1: Original query evaluation

```json
{
  "state": "empty",
  "queries": [
    {
      "state": "empty",
      "series": 0,
      "points": 0,
      "labels": [],
      "last_timestamp_ms": null,
      "query": "guardian_checkout_queue_depth{app=\"checkout\",environment=\"prod\"}",
      "ref_id": "A",
      "from_ms": 1791770900158,
      "to_ms": 1791771200158,
      "datasource_uid": "guardian-prometheus"
    }
  ]
}
```

### e2: Recent changes (context, not proof by itself)

```json
[
  {
    "at": "2026-10-12T02:11:47.655728+00:00",
    "service": "checkout",
    "environment": "prod",
    "deployment": "checkout-exporter-v1",
    "change": "Restored app label for synthetic demo"
  },
  {
    "at": "2026-10-12T02:11:53.156487+00:00",
    "service": "checkout",
    "environment": "prod",
    "deployment": "checkout-exporter-v2",
    "change": "Queue-depth label renamed from app to service"
  },
  {
    "at": "2026-10-12T02:13:11.606221+00:00",
    "service": "checkout",
    "environment": "prod",
    "deployment": "checkout-exporter-v1",
    "change": "Restored app label for synthetic demo"
  },
  {
    "at": "2026-10-12T02:13:17.121644+00:00",
    "service": "checkout",
    "environment": "prod",
    "deployment": "checkout-exporter-v2",
    "change": "Queue-depth label renamed from app to service"
  }
]
```

### e3: Metric type and help text

```json
{
  "metric": "guardian_checkout_queue_depth",
  "metadata": [
    {
      "type": "gauge",
      "unit": "",
      "help": "Pending synthetic checkout jobs."
    }
  ]
}
```

### e4: Live scrape target health

```json
[
  {
    "metric": {
      "__name__": "up",
      "instance": "127.0.0.1:9109",
      "job": "checkout"
    },
    "value": [
      1791771202.854,
      "1"
    ]
  }
]
```

### e5: Live candidate test: app → service

```json
{
  "state": "healthy",
  "queries": [
    {
      "state": "healthy",
      "series": 1,
      "points": 1,
      "labels": [
        {
          "__name__": "guardian_checkout_queue_depth",
          "environment": "prod",
          "instance": "127.0.0.1:9109",
          "job": "checkout",
          "service": "checkout"
        }
      ],
      "last_timestamp_ms": 1791771202859,
      "query": "guardian_checkout_queue_depth{service=\"checkout\",environment=\"prod\"}",
      "ref_id": "A",
      "from_ms": 1791770902859,
      "to_ms": 1791771202859,
      "datasource_uid": "guardian-prometheus"
    }
  ]
}
```

## Verification

```json
{
  "state": "healthy",
  "queries": [
    {
      "state": "healthy",
      "series": 1,
      "points": 1,
      "labels": [
        {
          "__name__": "guardian_checkout_queue_depth",
          "environment": "prod",
          "instance": "127.0.0.1:9109",
          "job": "checkout",
          "service": "checkout"
        }
      ],
      "last_timestamp_ms": 1791771209287,
      "query": "guardian_checkout_queue_depth{service=\"checkout\",environment=\"prod\"}",
      "ref_id": "A",
      "from_ms": 1791770909287,
      "to_ms": 1791771209287,
      "datasource_uid": "guardian-prometheus"
    }
  ]
}
```

## Timeline

- 2026-10-12T02:13:20.164+00:00 — detected: {"state": "empty", "panel_id": 1}
- 2026-10-12T02:13:22.836+00:00 — evidence_captured: {"screenshot": true}
- 2026-10-12T02:13:22.863+00:00 — diagnosed: {"category": "label_schema_drift", "executable_patch": true, "model_used": false}
- 2026-10-12T02:13:22.868+00:00 — review_requested: {"proposal_hash": "5e349154f634b12a42fbc3fa7efac87b49fc444be81af4be73f8019d7751dcbb"}
- 2026-10-12T02:13:27.188+00:00 — approved: {"actor": "demo-reviewer", "hash": "5e349154f634b12a42fbc3fa7efac87b49fc444be81af4be73f8019d7751dcbb", "dashboard_version": 7, "note": "Synthetic demo review: validated checkout service, production environment and live candidate results."}
- 2026-10-12T02:13:27.214+00:00 — patch_applied: {"version": 8, "ref_id": "A"}
- 2026-10-12T02:13:32.959+00:00 — recovery_verified: {"actor": "demo-reviewer", "reason": "Approved agent repair verified on three fresh evaluations", "state": "healthy"}
- 2026-10-12T02:13:32.966+00:00 — rca_saved: {"file": "03e665d0b151-rca.md"}

## Prevention actions

- Test this panel query after exporter or collector changes.
- Keep label migration policies aligned with the instrumentation contract.
- Track source-sample freshness and expected service/environment dimensions.

Suggested regression check: rerun the repaired query with this context after instrumentation deployments.
The prevention actions are recommendations; no application instrumentation was changed.
