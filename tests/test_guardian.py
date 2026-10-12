import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from guardian.config import Config
from guardian.engine import Engine, Conflict
from guardian.grafana import interpolate, rename_label, summarize_frames, Unsupported, GrafanaError
from guardian.model import validate_advice, advise
from guardian.server import Service


class FakeGrafana:
    def __init__(self):
        self.dashboard = {"uid": "checkout", "id": 1, "version": 1, "title": "Checkout",
                          "panels": [{"id": 1, "title": "Queue", "type": "stat", "datasource": {"uid": "prom", "type": "prometheus"},
                                      "targets": [{"refId": "A", "expr": 'queue{app="checkout",environment="prod"}', "instant": True}]}]}
        self.migrated = True
        self.writes = 0
        self.break_after_save = False
        self.fail_save = False

    def dashboards(self):
        return [{"uid": "checkout"}]

    def get(self, uid):
        return {"dashboard": copy.deepcopy(self.dashboard), "meta": {"folderUid": "prod"}}

    def datasource(self, uid):
        return {"uid": uid, "type": "prometheus"}

    def query(self, target, ds, dashboard, context, *args):
        failed = self.migrated and "app=" in target["expr"] or self.break_after_save and self.writes == 1
        return {"state": "empty" if failed else "healthy", "series": 0 if failed else 1,
                "points": 0 if failed else 1, "labels": [] if failed else [{"service": "checkout", "environment": "prod"}],
                "last_timestamp_ms": int(time.time() * 1000), "to_ms": int(time.time() * 1000),
                "query": target["expr"], "ref_id": "A"}

    def screenshot(self, *args):
        return {"available": False, "reason": "Disabled in unit fixture"}

    def prometheus(self, uid, path, args):
        return {"data": {}}

    def panel_url(self, *args):
        return "http://localhost:3000/d/checkout?viewPanel=1"

    def save(self, dashboard, folder, message):
        if self.fail_save:
            raise GrafanaError("Grafana request failed with HTTP 403", 403)
        if dashboard["version"] != self.dashboard["version"]:
            raise GrafanaError("Grafana request failed with HTTP 412", 412)
        self.writes += 1
        self.dashboard = copy.deepcopy(dashboard)
        self.dashboard["version"] += 1
        return {"version": self.dashboard["version"]}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(grafana_url="http://localhost:3000", data_dir=Path(self.temp.name),
                             contracts={"checkout:1": {"expect_data": True, "expected_labels": [{"environment": "prod"}]}},
                             consecutive_failures=1, label_migrations={"app": "service"}, screenshot_mode="disabled")
        self.grafana = FakeGrafana()
        self.engine = Engine(self.config, grafana=self.grafana)

    def tearDown(self):
        self.temp.cleanup()

    def incident(self):
        self.engine.scan()
        return self.engine.store.incidents()[0]

    def test_detection_and_deduplication_without_writes(self):
        item = self.incident()
        self.engine.scan()
        self.assertEqual(len(self.engine.store.incidents()), 1)
        self.assertEqual(item["status"], "awaiting_review")
        self.assertEqual(self.grafana.writes, 0)
        self.assertIn('environment="prod"', item["proposal"]["after"])
        self.assertFalse(item["advisor"]["enabled"])

    def test_consecutive_failures(self):
        self.config.consecutive_failures = 2
        self.engine.scan()
        self.assertEqual(self.engine.store.incidents(), [])
        self.engine.scan()
        self.assertEqual(len(self.engine.store.incidents()), 1)

    def test_empty_without_contract_is_unverified(self):
        self.config.contracts = {}
        self.engine.scan()
        self.assertEqual(self.engine.store.checks()[0]["state"], "unverified")
        self.assertEqual(self.engine.store.incidents(), [])

    def test_expected_empty_is_not_an_incident(self):
        self.config.contracts = {"checkout:1": {"allow_empty": True}}
        self.engine.scan()
        self.assertEqual(self.engine.store.checks()[0]["state"], "expected_empty")
        self.assertEqual(self.engine.store.incidents(), [])

    def test_missing_expected_dimension_blocks_candidate(self):
        self.config.contracts["checkout:1"]["expected_labels"] = [{"environment": "staging"}]
        item = self.incident()
        self.assertIsNone(item["proposal"])

    def test_unsupported_transformations_are_unverified(self):
        self.grafana.dashboard["panels"][0]["transformations"] = [{"id": "filterFieldsByName"}]
        self.engine.scan()
        self.assertEqual(self.engine.store.checks()[0]["state"], "unverified")
        self.assertEqual(self.engine.store.incidents(), [])

    def test_wrong_hash_cannot_write(self):
        item = self.incident()
        with self.assertRaises(Conflict):
            self.engine.approve(item["id"], "wrong", "reviewer")
        self.assertEqual(self.grafana.writes, 0)

    def test_stale_revision_blocks_approval(self):
        item = self.incident()
        self.grafana.dashboard["title"] = "Someone else edited the dashboard"
        self.grafana.dashboard["version"] += 1
        with self.assertRaises(Conflict):
            self.engine.approve(item["id"], item["proposal"]["hash"], "reviewer")
        self.assertEqual(self.grafana.writes, 0)
        self.assertEqual(self.engine.store.get_incident(item["id"])["status"], "stale_approval")

    def test_contract_change_blocks_approval(self):
        item = self.incident()
        self.config.contracts["checkout:1"]["owner"] = "New owner"
        with self.assertRaises(Conflict):
            self.engine.approve(item["id"], item["proposal"]["hash"], "reviewer")
        self.assertEqual(self.grafana.writes, 0)

    def test_approved_repair_verifies_and_saves_rca(self):
        item = self.incident()
        with patch("guardian.engine.time.sleep"):
            result = self.engine.approve(item["id"], item["proposal"]["hash"], "alice", "Checked the environment filter")
        self.assertEqual(self.grafana.writes, 1)
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(self.engine.store.checks()[0]["state"], "healthy")
        stored = self.engine.store.get_incident(item["id"])
        self.assertEqual(stored["approved_by"], "alice")
        self.assertEqual(stored["diagnosis"]["cause_status"], "repair_verified")
        self.assertTrue((self.engine.evidence_dir / (item["id"] + "-rca.md")).is_file())
        kinds = [event["kind"] for event in stored["events"]]
        self.assertIn("approved", kinds)
        self.assertIn("recovery_verified", kinds)
        self.assertIn("rca_saved", kinds)
        with self.assertRaises(Conflict):
            self.engine.approve(item["id"], item["proposal"]["hash"], "alice")

    def test_verification_failure_rolls_back(self):
        item = self.incident()
        self.grafana.break_after_save = True
        with patch("guardian.engine.time.sleep"), self.assertRaises(Conflict):
            self.engine.approve(item["id"], item["proposal"]["hash"], "alice")
        self.assertEqual(self.grafana.writes, 2)
        self.assertEqual(self.grafana.dashboard["panels"][0]["targets"][0]["expr"], item["proposal"]["before"])
        self.assertEqual(self.engine.store.get_incident(item["id"])["status"], "repair_failed")

    def test_read_only_grafana_cannot_apply(self):
        item = self.incident()
        self.grafana.fail_save = True
        with self.assertRaises(GrafanaError):
            self.engine.approve(item["id"], item["proposal"]["hash"], "alice")
        self.assertEqual(self.grafana.writes, 0)

    def test_rejection_leaves_panel_untouched(self):
        item = self.incident()
        self.engine.reject(item["id"], "alice", "Will fix the exporter instead")
        self.engine.scan()
        self.assertEqual(self.grafana.writes, 0)
        self.assertEqual(len(self.engine.store.incidents()), 1)
        with self.assertRaises(Conflict):
            self.engine.approve(item["id"], item["proposal"]["hash"], "alice")

    def test_manual_fix_is_verified_without_agent_write(self):
        item = self.incident()
        self.grafana.dashboard["panels"][0]["targets"][0]["expr"] = item["proposal"]["after"]
        self.grafana.dashboard["version"] += 1
        result = self.engine.verify_manual(item["id"], "human-reviewer")
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["diagnosis"]["cause_status"], "hypothesis")
        self.assertEqual(self.grafana.writes, 0)

    def test_multiple_valid_repairs_need_human_investigation(self):
        self.config.label_migrations = {"app": "service", "environment": "env"}
        # Both transformations would independently return data in this fixture.
        original = self.grafana.query
        def permissive(target, *args):
            if "env=" in target["expr"]:
                revised = dict(target, expr=target["expr"].replace("app=", "service="))
                return original(revised, *args)
            return original(target, *args)
        self.grafana.query = permissive
        item = self.incident()
        self.assertIsNone(item["proposal"])


class QueryTests(unittest.TestCase):
    def test_zero_is_data_and_null_is_empty(self):
        frame = {"schema": {"fields": [{"type": "time"}, {"type": "number"}]}, "data": {"values": [[1000], [0]]}}
        self.assertEqual(summarize_frames({"frames": [frame]})["state"], "healthy")
        frame["data"]["values"][1] = [None]
        self.assertEqual(summarize_frames({"frames": [frame]})["state"], "empty")

    def test_nan_is_empty(self):
        frame = {"schema": {"fields": [{"type": "number"}]}, "data": {"values": [[float("nan")]]}}
        self.assertEqual(summarize_frames({"frames": [frame]})["state"], "empty")

    def test_label_rename_preserves_strings_and_grouping(self):
        query = 'sum by (app) (metric{app="checkout",note="x,app=y",environment="prod"})'
        result = rename_label(query, "app", "service")
        self.assertEqual(result, 'sum by (app) (metric{service="checkout",note="x,app=y",environment="prod"})')

    def test_variables_and_ambiguous_context(self):
        self.assertEqual(interpolate('m{service="$service"}', {}, {"service": "checkout"}, 300, 5), 'm{service="checkout"}')
        with self.assertRaises(Unsupported):
            interpolate('m{service="$service"}', {}, {}, 300, 5)
        with self.assertRaises(Unsupported):
            interpolate('m{service="$service"}', {}, {"service": ["one", "two"]}, 300, 5)

    def test_model_cannot_cite_invented_evidence(self):
        advice = {"explanation": "A label changed", "confidence": "likely", "evidence_ids": ["invented"], "suggested_query": None, "prevention": []}
        with self.assertRaises(ValueError):
            validate_advice(advice, {"e1"})

    def test_model_request_is_structured_and_advisory(self):
        config = Config(grafana_url="http://localhost", data_dir=Path("."), model="test-model", model_api_key="test-key")
        advice = {"explanation": "Possibly a label migration", "confidence": "likely", "evidence_ids": ["e1"], "suggested_query": None, "prevention": ["Test the panel after deployment"]}
        result = {"status": "completed", "output": [{"content": [{"type": "output_text", "text": json.dumps(advice)}]}]}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return json.dumps(result).encode()
        with patch("guardian.model.urlopen", return_value=Response()) as call:
            output = advise(config, {"items": [{"id": "e1", "data": "empty"}]})
        payload = json.loads(call.call_args[0][0].data)
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertNotIn("tools", payload)
        self.assertEqual(output["advice"], advice)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = Config(grafana_url="http://localhost:3000", data_dir=Path(self.temp.name), review_token="test-review-token-is-long-enough")
        self.service = Service(Engine(config, grafana=FakeGrafana()))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.service.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.pool.shutdown()
        self.temp.cleanup()

    def test_evidence_requires_auth(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base + "/api/state")
        self.assertEqual(error.exception.code, 401)

    def test_cross_origin_approval_is_rejected(self):
        request = Request(self.base + "/api/scan", data=b"{}", headers={"Content-Type": "application/json", "Origin": "http://evil.example", "Authorization": "Bearer test-review-token-is-long-enough"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)

    def test_demo_login_is_unavailable_in_production(self):
        request = Request(self.base + "/api/session", data=b'{"demo":true}', headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
