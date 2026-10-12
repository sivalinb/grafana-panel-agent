"""Synthetic checkout exporter with an explicit, reproducible schema migration."""
import argparse
import json
import math
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Exporter:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.time()
        self.migrated = False
        self.changes = []

    def control(self, action):
        with self.lock:
            self.migrated = action == "migrate"
            event = {"at": datetime.now(timezone.utc).isoformat(), "service": "checkout", "environment": "prod",
                     "deployment": "checkout-exporter-v2" if self.migrated else "checkout-exporter-v1",
                     "change": "Queue-depth label renamed from app to service" if self.migrated else "Restored app label for synthetic demo"}
            self.changes.append(event)
            return event

    def metrics(self):
        with self.lock:
            age = time.time() - self.started
            label = "service" if self.migrated else "app"
            return "\n".join([
                "# HELP guardian_checkout_queue_depth Pending synthetic checkout jobs.",
                "# TYPE guardian_checkout_queue_depth gauge",
                'guardian_checkout_queue_depth{' + label + '="checkout",environment="prod"} ' + str(round(42 + 4 * math.sin(age / 10), 1)),
                "# HELP guardian_http_requests_total Synthetic checkout requests.",
                "# TYPE guardian_http_requests_total counter",
                'guardian_http_requests_total{service="checkout",environment="prod"} ' + str(int(age * 24)),
                "# HELP guardian_checkout_latency_seconds Synthetic checkout latency.",
                "# TYPE guardian_checkout_latency_seconds gauge",
                'guardian_checkout_latency_seconds{service="checkout",environment="prod"} ' + str(round(.084 + .007 * math.sin(age / 13), 4)),
                ""
            ])


def serve(host="127.0.0.1", port=9109):
    exporter = Exporter()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, body, content_type="application/json"):
            data = json.dumps(body).encode() if content_type == "application/json" else body.encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/metrics":
                return self.respond(exporter.metrics(), "text/plain; version=0.0.4")
            if self.path == "/changes":
                return self.respond(exporter.changes)
            if self.path == "/healthz":
                return self.respond({"status": "ok", "migrated": exporter.migrated})
            self.send_error(404)

        def do_POST(self):
            if self.path not in ("/control/migrate", "/control/reset"):
                return self.send_error(404)
            self.respond(exporter.control(self.path.rsplit("/", 1)[-1]))
    print("Synthetic exporter on " + str(port), flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9109)
    args = parser.parse_args()
    serve(args.host, args.port)
