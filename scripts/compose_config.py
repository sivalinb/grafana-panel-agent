"""Create a synthetic-only config inside the container's writable state volume."""
import json
from pathlib import Path
config = json.loads(Path("demo/config.json").read_text())
config.update(grafana_url="http://grafana:3000", data_dir="/app/data", host="0.0.0.0",
              grafana_public_url="http://127.0.0.1:3000", exporter_url="http://exporter:9109", changes_url="http://exporter:9109/changes")
Path("/app/data/config.json").write_text(json.dumps(config, indent=2))
