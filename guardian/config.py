import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class Config:
    grafana_url: str
    data_dir: Path
    grafana_token: str = ""
    grafana_user: str = ""
    grafana_password: str = ""
    grafana_public_url: str = ""
    tags: list = field(default_factory=lambda: ["production"])
    contracts: dict = field(default_factory=dict)
    contexts: dict = field(default_factory=dict)
    label_migrations: dict = field(default_factory=dict)
    scan_interval_seconds: int = 60
    consecutive_failures: int = 2
    window_seconds: int = 300
    step_seconds: int = 5
    timeout_seconds: int = 20
    demo: bool = False
    exporter_url: str = ""
    changes_url: str = ""
    screenshot_mode: str = "playwright"
    review_token: str = ""
    reviewer_name: str = "reviewer"
    model: str = ""
    model_api_key: str = ""
    model_api_url: str = "https://api.openai.com/v1/responses"
    model_include_screenshot: bool = False
    host: str = "127.0.0.1"
    port: int = 8787

    @classmethod
    def load(cls, path):
        raw = json.loads(Path(path).read_text())
        allowed = set(cls.__dataclass_fields__)
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError("Unknown config fields: " + ", ".join(sorted(unknown)))
        raw["data_dir"] = Path(os.environ.get("GUARDIAN_DATA_DIR", raw.get("data_dir", "data"))).resolve()
        for name in ("grafana_url", "grafana_public_url", "grafana_token", "grafana_user", "grafana_password", "review_token", "reviewer_name", "model", "model_api_url"):
            key = "GUARDIAN_" + name.upper()
            if key in os.environ:
                raw[name] = os.environ[key]
        raw["model_api_key"] = os.environ.get("OPENAI_API_KEY", "")
        config = cls(**raw)
        if urlparse(config.grafana_url).scheme not in ("http", "https"):
            raise ValueError("grafana_url must be an HTTP(S) URL")
        if not config.demo and len(config.review_token) < 24:
            raise ValueError("Set GUARDIAN_REVIEW_TOKEN to at least 24 characters")
        if config.demo and urlparse(config.grafana_url).hostname not in ("127.0.0.1", "localhost", "grafana"):
            raise ValueError("Demo controls can only target the local demo Grafana")
        if config.consecutive_failures < 1 or config.scan_interval_seconds < 2:
            raise ValueError("Invalid scan interval or failure threshold")
        if config.screenshot_mode not in ("playwright", "renderer", "disabled"):
            raise ValueError("Unknown screenshot mode")
        config.data_dir.mkdir(parents=True, exist_ok=True)
        return config

    def contract(self, uid, panel_id):
        return self.contracts.get(uid + ":" + str(panel_id), {})
