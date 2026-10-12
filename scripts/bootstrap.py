import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guardian.config import Config
from guardian.grafana import Grafana, GrafanaError


def bootstrap(config_path, prometheus_url="http://127.0.0.1:9090"):
    config = Config.load(config_path)
    client = Grafana(config)
    try:
        client.datasource("guardian-prometheus")
    except GrafanaError as exc:
        if exc.status != 404:
            raise
        client.request("/api/datasources", {"name": "Guardian Prometheus", "uid": "guardian-prometheus", "type": "prometheus",
                       "url": prometheus_url, "access": "proxy", "isDefault": True, "jsonData": {"httpMethod": "POST", "timeInterval": "2s"}})
    try:
        client.get("guardian-checkout")
    except GrafanaError as exc:
        if exc.status != 404:
            raise
        dashboard = json.loads((Path(__file__).resolve().parent.parent / "demo" / "dashboard.json").read_text())
        client.save(dashboard, "", "Install synthetic Panel Agent demo")
    print("Demo dashboard ready: " + client.base + "/d/guardian-checkout", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="demo/config.json")
    parser.add_argument("--prometheus-url", default=os.environ.get("DEMO_PROMETHEUS_URL", "http://127.0.0.1:9090"))
    args = parser.parse_args()
    bootstrap(args.config, args.prometheus_url)
