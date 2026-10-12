import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.request import urlopen, Request

from .grafana import Grafana, GrafanaError, Unsupported, iter_panels, panel_by_id, interpolate, rename_label
from .model import advise
from .store import Store, digest, now


class Conflict(Exception):
    pass


class Engine:
    def __init__(self, config, grafana=None, store=None):
        self.config = config
        self.grafana = grafana or Grafana(config)
        self.store = store or Store(config.data_dir / "guardian.sqlite3")
        self.lock = threading.RLock()
        self.busy = None
        self.last_error = None
        self.evidence_dir = config.data_dir / "evidence"
        self.evidence_dir.mkdir(exist_ok=True)

    def datasource(self, panel, target, dashboard, context):
        ds = target.get("datasource", panel.get("datasource"))
        if isinstance(ds, str):
            raise Unsupported("Legacy data-source names require conversion to UID references")
        uid = (ds or {}).get("uid")
        if not uid:
            raise Unsupported("No explicit data-source UID")
        uid = interpolate(uid, dashboard, context, self.config.window_seconds, self.config.step_seconds)
        if uid in ("__expr__", "-- Mixed --"):
            raise Unsupported("Expressions or unresolved mixed data source")
        return self.grafana.datasource(uid)

    def evaluate(self, dashboard, panel, context, contract):
        if panel.get("libraryPanel"):
            raise Unsupported("Library panels must be checked and repaired through their owning resource")
        if panel.get("transformations"):
            raise Unsupported("Client-side transformations require a browser-level contract in a future adapter")
        if panel.get("repeat") or panel.get("timeShift") or panel.get("timeFrom"):
            raise Unsupported("Repeated panels and panel time overrides are not supported in v0.1")
        targets = [t for t in panel.get("targets", []) if not t.get("hide")]
        if not targets:
            raise Unsupported("Panel has no visible telemetry query")
        summaries = []
        for target in targets:
            ds = self.datasource(panel, target, dashboard, context)
            summary = self.grafana.query(target, ds, dashboard, context)
            summary["datasource_uid"] = ds["uid"]
            if summary["state"] == "healthy":
                for required in contract.get("expected_labels", []):
                    if not any(all(labels.get(k) == v for k, v in required.items()) for labels in summary["labels"]):
                        summary.update(state="missing_series", error="An expected label set is missing")
                if summary["series"] < contract.get("min_series", 1):
                    summary.update(state="missing_series", error="Series count is below the panel contract")
                max_age = contract.get("max_age_seconds", 120)
                if summary.get("last_timestamp_ms") and summary["to_ms"] - summary["last_timestamp_ms"] > max_age * 1000:
                    summary.update(state="stale", error="No recent query results within the freshness contract")
                # Optional raw selector checks sample age, not the query evaluation timestamp.
                if contract.get("freshness_selector"):
                    selector = interpolate(contract["freshness_selector"], dashboard, context, self.config.window_seconds, self.config.step_seconds)
                    age = self.grafana.prometheus(ds["uid"], "/api/v1/query", {"query": "time() - timestamp(" + selector + ")"})
                    samples = age.get("data", {}).get("result", [])
                    if not samples or any(float(s["value"][1]) > max_age for s in samples):
                        summary.update(state="stale", error="Source samples exceed the freshness contract")
            if summary["state"] == "empty" and contract.get("allow_empty"):
                summary["state"] = "expected_empty"
            summaries.append(summary)
        priority = ["error", "missing_series", "stale", "empty", "healthy", "expected_empty"]
        state = next(s for s in priority if any(q["state"] == s for q in summaries))
        return {"state": state, "queries": summaries}

    def scan(self):
        with self.lock:
            self.busy = "scan"
            counts = {"dashboards": 0, "panels": 0, "healthy": 0, "expected_empty": 0, "unverified": 0, "failing": 0, "new_incidents": 0}
            try:
                for entry in self.grafana.dashboards():
                    snapshot = self.grafana.get(entry["uid"])
                    dashboard = snapshot["dashboard"]
                    uid = dashboard["uid"]
                    counts["dashboards"] += 1
                    contexts = self.config.contexts.get(uid, [{}])
                    for context in contexts:
                        for panel in iter_panels(dashboard):
                            contract = self.config.contract(uid, panel["id"])
                            key = digest({"uid": uid, "panel": panel["id"], "context": context})
                            previous = self.store.check(key)
                            check = {"key": key, "uid": uid, "dashboard": dashboard["title"], "panel_id": panel["id"],
                                     "panel": panel.get("title", "Untitled panel"), "context": context, "at": now(), "owner": contract.get("owner", "Unassigned")}
                            counts["panels"] += 1
                            try:
                                result = self.evaluate(dashboard, panel, context, contract)
                                check.update(result)
                                healthy = result["state"] in ("healthy", "expected_empty")
                                expected = contract.get("expect_data", False) or previous.get("ever_healthy", False)
                                check["ever_healthy"] = previous.get("ever_healthy", False) or result["state"] == "healthy"
                                check["failure_count"] = 0 if healthy else previous.get("failure_count", 0) + 1
                                if result["state"] == "empty" and not expected:
                                    check.update(state="unverified", reason="No explicit data contract or healthy baseline")
                                    counts["unverified"] += 1
                                elif healthy:
                                    counts[result["state"]] += 1
                                    active = self.store.active(key)
                                    if active and active["status"] not in ("applying", "verifying"):
                                        self.resolve(active, "external", "Recovery observed by scheduled check", dashboard, panel, result)
                                else:
                                    counts["failing"] += 1
                                    if check["failure_count"] >= self.config.consecutive_failures and not self.store.active(key):
                                        incident = self.investigate(snapshot, panel, context, contract, result, key)
                                        self.store.put_incident(incident)
                                        counts["new_incidents"] += 1
                            except Unsupported as exc:
                                check.update(state="unverified", reason=str(exc), failure_count=0,
                                             ever_healthy=previous.get("ever_healthy", False))
                                counts["unverified"] += 1
                            except GrafanaError as exc:
                                check.update(state="error", reason=str(exc), failure_count=previous.get("failure_count", 0) + 1,
                                             ever_healthy=previous.get("ever_healthy", False))
                                counts["failing"] += 1
                                if check["failure_count"] >= self.config.consecutive_failures and not self.store.active(key):
                                    result = {"state": "error", "queries": [], "error": str(exc)}
                                    self.store.put_incident(self.investigate(snapshot, panel, context, contract, result, key))
                                    counts["new_incidents"] += 1
                            self.store.put_check(key, check)
                self.last_error = None
                self.store.run(counts)
                return counts
            except Exception as exc:
                self.last_error = str(exc) if isinstance(exc, GrafanaError) else type(exc).__name__
                self.store.run(dict(counts, error=self.last_error))
                raise
            finally:
                self.busy = None

    def changes(self):
        if not self.config.changes_url:
            return []
        try:
            with urlopen(self.config.changes_url, timeout=5) as response:
                items = json.loads(response.read(64000))
            return items[-10:] if isinstance(items, list) else []
        except Exception:
            return []

    def investigate(self, snapshot, panel, context, contract, result, fingerprint):
        dashboard = snapshot["dashboard"]
        incident_id = uuid.uuid4().hex[:12]
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - self.config.window_seconds * 1000
        item = {"id": incident_id, "fingerprint": fingerprint, "status": "awaiting_review", "created_at": now(),
                "dashboard_uid": dashboard["uid"], "dashboard_title": dashboard["title"], "panel_id": panel["id"],
                "panel_title": panel.get("title", "Untitled panel"), "context": context, "contract": contract,
                "owner": contract.get("owner", "Unassigned"), "observed": result, "dashboard_version": dashboard.get("version", 0),
                "dashboard_hash": digest(dashboard), "before_dashboard": dashboard, "folder_uid": snapshot.get("meta", {}).get("folderUid", ""),
                "panel_url": self.grafana.panel_url(dashboard["uid"], panel["id"], context, start_ms, end_ms),
                "evidence": [{"id": "e1", "title": "Original query evaluation", "data": result}], "proposal": None}
        self.store.event(incident_id, "detected", {"state": result["state"], "panel_id": panel["id"]})
        file = self.evidence_dir / (incident_id + "-before.png")
        item["screenshot_before"] = self.grafana.screenshot(dashboard["uid"], panel["id"], context, start_ms, end_ms, file)
        self.store.event(incident_id, "evidence_captured", {"screenshot": item["screenshot_before"]["available"]})
        changes = self.changes()
        if changes:
            item["evidence"].append({"id": "e2", "title": "Recent changes (context, not proof by itself)", "data": changes})
        candidates = []
        inspected_sources = set()
        for target_index, target in enumerate(panel.get("targets", [])):
            if target.get("hide"):
                continue
            try:
                ds = self.datasource(panel, target, dashboard, context)
                if ds["uid"] not in inspected_sources:
                    inspected_sources.add(ds["uid"])
                    metrics = re.findall(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\{", target.get("expr", ""))
                    for metric in metrics[:2]:
                        try:
                            metadata = self.grafana.prometheus(ds["uid"], "/api/v1/metadata", {"metric": metric})
                            item["evidence"].append({"id": "e" + str(len(item["evidence"]) + 1), "title": "Metric type and help text",
                                                     "data": {"metric": metric, "metadata": metadata.get("data", {}).get(metric, [])[:5]}})
                        except (GrafanaError, AttributeError):
                            pass
                    if contract.get("scrape_selector"):
                        try:
                            selector = interpolate(contract["scrape_selector"], dashboard, context, self.config.window_seconds, self.config.step_seconds)
                            targets = self.grafana.prometheus(ds["uid"], "/api/v1/query", {"query": selector})
                            item["evidence"].append({"id": "e" + str(len(item["evidence"]) + 1), "title": "Live scrape target health",
                                                     "data": targets.get("data", {}).get("result", [])[:20]})
                        except (GrafanaError, AttributeError):
                            pass
                # Metadata provides context; a live candidate query supplies the repair test.
                for old, new in self.config.label_migrations.items():
                    changed = rename_label(target.get("expr", ""), old, new)
                    if changed == target.get("expr", ""):
                        continue
                    candidate_panel = copy.deepcopy(panel)
                    candidate_panel["targets"][target_index]["expr"] = changed
                    validation = self.evaluate(dashboard, candidate_panel, context, contract)
                    item["evidence"].append({"id": "e" + str(len(item["evidence"]) + 1),
                                             "title": "Live candidate test: " + old + " → " + new, "data": validation})
                    if validation["state"] == "healthy":
                        candidates.append({"target_index": target_index, "ref_id": target.get("refId", "A"),
                                           "before": target["expr"], "after": changed, "label_before": old, "label_after": new,
                                           "validation": validation})
            except (Unsupported, GrafanaError):
                continue
        if len(candidates) == 1:
            candidate = candidates[0]
            proposal = {"dashboard_uid": dashboard["uid"], "panel_id": panel["id"], "dashboard_version": dashboard.get("version", 0),
                        "dashboard_hash": digest(dashboard), "context": context, "contract_hash": digest(contract), **candidate}
            proposal["hash"] = digest(proposal)
            item["proposal"] = proposal
            item["diagnosis"] = {
                "cause": "The panel uses the obsolete '" + candidate["label_before"] + "' label matcher; live telemetry responds to '" + candidate["label_after"] + "'.",
                "confidence": "likely", "cause_status": "hypothesis", "category": "label_schema_drift",
                "reason": "Exactly one configured semantic label migration restores all visible panel queries and passes the panel contract.",
                "prevention": ["Test this panel query after exporter or collector changes.",
                               "Keep label migration policies aligned with the instrumentation contract.",
                               "Track source-sample freshness and expected service/environment dimensions."]}
        else:
            item["diagnosis"] = {"cause": "The available evidence does not establish a unique executable repair.",
                                 "confidence": "uncertain", "cause_status": "unknown", "category": result["state"],
                                 "reason": "Check the exporter, ingestion path, data-source permissions, query syntax and dashboard variables.",
                                 "prevention": ["Add an explicit panel contract and deployment/change context."]}
        item["advisor"] = advise(self.config, {"items": item["evidence"], "rule_diagnosis": item["diagnosis"]},
                                  file if item["screenshot_before"]["available"] else None)
        self.store.event(incident_id, "diagnosed", {"category": item["diagnosis"]["category"], "executable_patch": bool(item["proposal"]),
                                                    "model_used": bool(item["advisor"].get("advice"))})
        self.store.event(incident_id, "review_requested", {"proposal_hash": item["proposal"]["hash"] if item["proposal"] else None})
        return item

    def approve(self, incident_id, proposal_hash, actor, note=""):
        with self.lock:
            item = self.store.get_incident(incident_id)
            if item["status"] != "awaiting_review" or not item.get("proposal"):
                raise Conflict("This incident has no pending executable proposal")
            proposal = item["proposal"]
            if proposal_hash != proposal["hash"] or digest({k: v for k, v in proposal.items() if k != "hash"}) != proposal_hash:
                raise Conflict("Approval does not match the immutable proposal")
            snapshot = self.grafana.get(item["dashboard_uid"])
            dashboard = snapshot["dashboard"]
            if digest(dashboard) != proposal["dashboard_hash"] or digest(self.config.contract(item["dashboard_uid"], item["panel_id"])) != proposal["contract_hash"]:
                item["status"] = "stale_approval"
                self.store.put_incident(item)
                self.store.event(item["id"], "approval_blocked", {"reason": "Dashboard or contract changed since diagnosis"})
                raise Conflict("Dashboard or contract changed; close this proposal and scan again")
            panel = panel_by_id(dashboard, item["panel_id"])
            if panel["targets"][proposal["target_index"]].get("expr") != proposal["before"]:
                raise Conflict("The target query has changed")
            patched = copy.deepcopy(dashboard)
            patched_panel = panel_by_id(patched, item["panel_id"])
            patched_panel["targets"][proposal["target_index"]]["expr"] = proposal["after"]
            if self.evaluate(patched, patched_panel, item["context"], item["contract"])["state"] != "healthy":
                raise Conflict("Candidate no longer passes the live panel contract")
            item.update(status="applying", approved_by=actor, approved_at=now(), approval_note=note[:2000])
            self.store.event(item["id"], "approved", {"actor": actor, "hash": proposal_hash, "dashboard_version": dashboard.get("version"), "note": note[:2000]})
            self.store.put_incident(item)
            try:
                saved = self.grafana.save(patched, item["folder_uid"], "Panel Agent: approved repair " + item["id"])
                self.store.event(item["id"], "patch_applied", {"version": saved.get("version"), "ref_id": proposal["ref_id"]})
                item.update(status="verifying", applied_version=saved.get("version"))
                self.store.put_incident(item)
                applied_snapshot = self.grafana.get(item["dashboard_uid"])["dashboard"]
                applied_hash = digest(applied_snapshot)
                validation = None
                for _ in range(3):
                    live = self.grafana.get(item["dashboard_uid"])["dashboard"]
                    if digest(live) != applied_hash:
                        raise Conflict("Dashboard changed during verification; manual review required")
                    live_panel = panel_by_id(live, item["panel_id"])
                    validation = self.evaluate(live, live_panel, item["context"], item["contract"])
                    if validation["state"] != "healthy":
                        break
                    time.sleep(1)
                if validation and validation["state"] == "healthy":
                    return self.resolve(item, actor, "Approved agent repair verified on three fresh evaluations", live, live_panel, validation)
                # Roll back only if no intervening dashboard edits occurred.
                current = self.grafana.get(item["dashboard_uid"])["dashboard"]
                if digest(current) == applied_hash:
                    restore = copy.deepcopy(item["before_dashboard"])
                    restore["version"] = current["version"]
                    self.grafana.save(restore, item["folder_uid"], "Panel Agent: rollback failed repair " + item["id"])
                    self.store.event(item["id"], "rolled_back", {"reason": "Verification failed"})
                raise Conflict("Recovery verification failed; inspect the incident timeline")
            except Exception as exc:
                item.update(status="repair_failed", repair_error=str(exc) if isinstance(exc, (Conflict, GrafanaError)) else type(exc).__name__)
                self.store.put_incident(item)
                self.store.event(item["id"], "repair_failed", {"error": item["repair_error"]})
                raise

    def resolve(self, item, actor, reason, dashboard, panel, validation):
        if validation["state"] not in ("healthy", "expected_empty"):
            raise Conflict("Panel still fails its contract")
        end_ms = int(time.time() * 1000)
        item["screenshot_after"] = self.grafana.screenshot(item["dashboard_uid"], item["panel_id"], item["context"],
                end_ms - self.config.window_seconds * 1000, end_ms, self.evidence_dir / (item["id"] + "-after.png"))
        item.update(status="resolved", resolved_at=now(), resolved_by=actor, resolution=reason,
                    verification=validation, recovered_dashboard_version=dashboard.get("version"))
        check = self.store.check(item["fingerprint"])
        if check:
            check.update(validation, at=now(), failure_count=0, ever_healthy=True)
            self.store.put_check(item["fingerprint"], check)
        if actor != "external" and item.get("approved_by"):
            item["diagnosis"]["cause_status"] = "repair_verified"
        self.store.event(item["id"], "recovery_verified", {"actor": actor, "reason": reason, "state": validation["state"]})
        self.store.put_incident(item)
        report = self.report(item["id"])
        (self.evidence_dir / (item["id"] + "-rca.md")).write_text(report)
        self.store.event(item["id"], "rca_saved", {"file": item["id"] + "-rca.md"})
        return item

    def verify_manual(self, incident_id, actor, note=""):
        with self.lock:
            item = self.store.get_incident(incident_id)
            if item["status"] == "resolved":
                raise Conflict("Incident is already resolved")
            dashboard = self.grafana.get(item["dashboard_uid"])["dashboard"]
            panel = panel_by_id(dashboard, item["panel_id"])
            validation = self.evaluate(dashboard, panel, item["context"], item["contract"])
            return self.resolve(item, actor, "Human/external repair verified. " + note[:2000], dashboard, panel, validation)

    def reject(self, incident_id, actor, note=""):
        with self.lock:
            item = self.store.get_incident(incident_id)
            if item["status"] != "awaiting_review":
                raise Conflict("Only a pending proposal can be rejected")
            item.update(status="rejected", rejected_by=actor, rejection_note=note[:2000])
            self.store.put_incident(item)
            self.store.event(incident_id, "rejected", {"actor": actor, "note": note[:2000]})
            return item

    def close(self, incident_id, actor):
        with self.lock:
            item = self.store.get_incident(incident_id)
            if item["status"] not in ("stale_approval", "repair_failed", "rejected"):
                raise Conflict("Close is only available for stale, rejected or failed proposals")
            item["status"] = "closed"
            self.store.put_incident(item)
            self.store.event(incident_id, "closed_for_rescan", {"actor": actor})
            return item

    def demo_control(self, action):
        if not self.config.demo or not self.config.exporter_url:
            raise Conflict("Simulation controls are disabled outside the synthetic demo")
        if action not in ("migrate", "reset"):
            raise Conflict("Unknown simulation action")
        with self.lock:
            req = Request(self.config.exporter_url.rstrip("/") + "/control/" + action, data=b"{}", headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=5) as response:
                result = json.loads(response.read())
            if action == "reset":
                uid = "guardian-checkout"
                live = self.grafana.get(uid)
                original = json.loads((Path(__file__).resolve().parent.parent / "demo" / "dashboard.json").read_text())
                original["id"] = live["dashboard"].get("id")
                original["version"] = live["dashboard"]["version"]
                self.grafana.save(original, live.get("meta", {}).get("folderUid", ""), "Reset synthetic demo")
                for item in self.store.incidents():
                    if item["dashboard_uid"] == uid and item["status"] != "resolved":
                        item["status"] = "closed"
                        self.store.put_incident(item)
                        self.store.event(item["id"], "demo_reset")
            time.sleep(3)
            return dict(result, scan=self.scan())

    def report(self, incident_id):
        item = self.store.get_incident(incident_id)
        diagnosis = item["diagnosis"]
        lines = ["# Monitoring failure RCA: " + item["panel_title"], "",
                 "- Incident: `" + item["id"] + "`", "- Dashboard: " + item["dashboard_title"],
                 "- Panel ID: " + str(item["panel_id"]), "- Status: " + item["status"],
                 "- Detected: " + item["created_at"], "- Resolved: " + item.get("resolved_at", "Pending"),
                 "- Reviewer: " + item.get("approved_by", item.get("resolved_by", "Pending")),
                 "- Context: `" + json.dumps(item["context"], sort_keys=True) + "`", "",
                 "## Findings", "", diagnosis["cause"], "", "Cause status: **" + diagnosis["cause_status"] + "**.",
                 "A successful repair confirms recovery; it does not by itself prove every contributing cause.", "", "## Repair", ""]
        if item.get("proposal"):
            lines += ["```diff", "- " + item["proposal"]["before"], "+ " + item["proposal"]["after"], "```", "",
                      "Proposal SHA-256: `" + item["proposal"]["hash"] + "`", ""]
        else:
            lines += ["No executable patch was established.", ""]
        lines += ["## Evidence", ""]
        for evidence in item["evidence"]:
            lines += ["### " + evidence["id"] + ": " + evidence["title"], "", "```json", json.dumps(evidence["data"], indent=2), "```", ""]
        lines += ["## Verification", "", "```json", json.dumps(item.get("verification", {}), indent=2), "```", "", "## Timeline", ""]
        for event in item.get("events", []):
            lines.append("- " + event["at"] + " — " + event["kind"] + ": " + json.dumps(event["body"]))
        lines += ["", "## Prevention actions", ""] + ["- " + text for text in diagnosis["prevention"]]
        lines += ["", "Suggested regression check: rerun the repaired query with this context after instrumentation deployments.",
                  "The prevention actions are recommendations; no application instrumentation was changed.", ""]
        return "\n".join(lines)

    def snapshot(self):
        incidents = self.store.incidents()
        public = [{k: v for k, v in item.items() if k not in ("before_dashboard", "dashboard_hash")} for item in incidents]
        return {"checks": self.store.checks(), "incidents": public, "last_run": self.store.last_run(), "busy": self.busy,
                "last_error": self.last_error, "demo": self.config.demo, "scan_interval_seconds": self.config.scan_interval_seconds,
                "model_enabled": bool(self.config.model and self.config.model_api_key), "grafana_url": self.config.grafana_url}
