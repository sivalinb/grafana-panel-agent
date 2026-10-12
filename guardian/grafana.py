import base64
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen


class GrafanaError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class Unsupported(Exception):
    pass


def iter_panels(dashboard):
    def visit(panels):
        for panel in panels:
            if panel.get("type") != "row":
                yield panel
            yield from visit(panel.get("panels", []))
    yield from visit(dashboard.get("panels", []))


def panel_by_id(dashboard, panel_id):
    for panel in iter_panels(dashboard):
        if str(panel.get("id")) == str(panel_id):
            return panel
    raise GrafanaError("Panel no longer exists", 409)


def interpolate(expr, dashboard, context, window, step):
    """Resolve explicit contexts/defaults; fail closed on ambiguous variables."""
    values = dict(context)
    for var in dashboard.get("templating", {}).get("list", []):
        name = var.get("name")
        if name not in values:
            value = var.get("current", {}).get("value")
            if value in (None, ""):
                value = var.get("query") if var.get("type") == "constant" else None
            if value is not None:
                values[name] = value
        if values.get(name) == "$__all" or values.get(name) == ["$__all"]:
            if not var.get("allValue"):
                raise Unsupported("All-variable contexts require an explicit allValue or contract context")
            values[name] = var["allValue"]
    now_ms = int(time.time() * 1000)
    values.update({"__range": str(window) + "s", "__range_s": str(window),
                   "__range_ms": str(window * 1000), "__interval": str(step) + "s",
                   "__interval_ms": str(step * 1000), "__rate_interval": str(max(60, step + 15)) + "s",
                   "__from": str(now_ms - window * 1000), "__to": str(now_ms)})
    def replace(match):
        name, fmt = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), None)
        if name not in values:
            raise Unsupported("Unresolved dashboard variable: " + name)
        value = values[name]
        if isinstance(value, list):
            if len(value) == 1:
                value = value[0]
            elif fmt == "regex":
                return "(" + "|".join(re.escape(str(v)) for v in value) + ")"
            else:
                raise Unsupported("Multi-value variable requires :regex or a single-value context")
        if fmt not in (None, "regex", "raw"):
            raise Unsupported("Unsupported variable format: " + fmt)
        # Escape values for PromQL quoted matchers, except explicit regex patterns.
        return str(value).replace("\\", "\\\\").replace('"', '\\"') if fmt != "regex" else str(value)
    result = re.sub(r"\$\{([A-Za-z_][\w]*)(?::([\w]+))?\}|\$([A-Za-z_][\w]*)", replace, expr)
    if "$" in result:
        raise Unsupported("Unresolved macro or unsupported variable expression")
    return result


def rename_label(expr, old, new):
    """Change matcher keys only, never strings, values, or grouping clauses."""
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", new):
        raise ValueError("Invalid replacement label")
    result, pos, selector, quoted, escaped = [], 0, False, False, False
    while pos < len(expr):
        c = expr[pos]
        if quoted:
            result.append(c)
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                quoted = False
            pos += 1
            continue
        if c == '"':
            quoted = True
        elif c == "{":
            selector = True
        elif c == "}":
            selector = False
        if selector and (c.isalpha() or c == "_"):
            match = re.match(r"[a-zA-Z_][a-zA-Z0-9_]*", expr[pos:])
            name = match.group(0)
            rest = expr[pos + len(name):]
            result.append(new if name == old and re.match(r"\s*(=~|!~|!=|=)", rest) else name)
            pos += len(name)
        else:
            result.append(c)
            pos += 1
    return "".join(result)


def summarize_frames(result):
    if result.get("error") or result.get("status", 200) >= 400:
        return {"state": "error", "error": str(result.get("error", "Query failed"))[:1500],
                "series": 0, "points": 0, "labels": [], "last_timestamp_ms": None}
    series, points, last, labels = 0, 0, None, []
    for frame in result.get("frames", []):
        fields = frame.get("schema", {}).get("fields", [])
        values = frame.get("data", {}).get("values", [])
        times = []
        for field, data in zip(fields, values):
            if field.get("type") == "time":
                times = data
        for field, data in zip(fields, values):
            if field.get("type") != "number":
                continue
            valid = [(i, v) for i, v in enumerate(data) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
            if valid:
                series += 1
                points += len(valid)
                labels.append(field.get("labels", {}))
                for i, _ in valid:
                    if i < len(times) and isinstance(times[i], (int, float)):
                        last = max(last or 0, times[i])
    return {"state": "healthy" if points else "empty", "series": series, "points": points,
            "labels": labels[:50], "last_timestamp_ms": last}


class Grafana:
    def __init__(self, config):
        self.config = config
        self.base = config.grafana_url.rstrip("/")

    def headers(self):
        if self.config.grafana_token:
            return {"Authorization": "Bearer " + self.config.grafana_token}
        if self.config.grafana_user:
            token = base64.b64encode((self.config.grafana_user + ":" + self.config.grafana_password).encode()).decode()
            return {"Authorization": "Basic " + token}
        return {}

    def request(self, path, body=None, method=None, binary=False):
        headers = self.headers()
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                      headers=headers, method=method)
        try:
            with urlopen(req, timeout=self.config.timeout_seconds) as response:
                data = response.read(16 * 1024 * 1024)
                return data if binary else json.loads(data)
        except HTTPError as exc:
            if path == "/api/ds/query":
                try:
                    result = json.loads(exc.read(256 * 1024))
                    if isinstance(result.get("results"), dict):
                        return result
                except (ValueError, AttributeError):
                    pass
            # Never log credentials or arbitrary upstream response bodies.
            raise GrafanaError("Grafana request failed with HTTP " + str(exc.code), exc.code) from None
        except (URLError, TimeoutError, ValueError) as exc:
            raise GrafanaError("Grafana request failed: " + type(exc).__name__) from None

    def dashboards(self):
        all_items, page = [], 1
        while True:
            args = [("type", "dash-db"), ("limit", 100), ("page", page)] + [("tag", t) for t in self.config.tags]
            items = self.request("/api/search?" + urlencode(args))
            all_items.extend(items)
            if len(items) < 100:
                return all_items
            page += 1

    def get(self, uid):
        return self.request("/api/dashboards/uid/" + quote(uid, safe=""))

    def save(self, dashboard, folder_uid, message):
        return self.request("/api/dashboards/db", {"dashboard": dashboard, "folderUid": folder_uid or "",
                            "message": message, "overwrite": False})

    def datasource(self, uid):
        return self.request("/api/datasources/uid/" + quote(uid, safe=""))

    def query(self, target, ds, dashboard, context, start_ms=None, end_ms=None):
        if ds.get("type") != "prometheus":
            raise Unsupported("Only Prometheus-backed queries are supported in v0.1")
        expr = interpolate(target.get("expr", ""), dashboard, context,
                           self.config.window_seconds, self.config.step_seconds)
        if not expr:
            raise Unsupported("Query has no PromQL expression")
        end_ms = end_ms or int(time.time() * 1000)
        start_ms = start_ms or end_ms - self.config.window_seconds * 1000
        query = dict(target, datasource={"uid": ds["uid"], "type": "prometheus"}, expr=expr,
                     refId=target.get("refId", "A"), intervalMs=self.config.step_seconds * 1000,
                     maxDataPoints=1000)
        response = self.request("/api/ds/query", {"from": str(start_ms), "to": str(end_ms), "queries": [query]})
        summary = summarize_frames(response.get("results", {}).get(query["refId"], {"error": "Missing query result"}))
        summary.update({"query": expr, "ref_id": query["refId"], "from_ms": start_ms, "to_ms": end_ms})
        return summary

    def prometheus(self, uid, path, args=None):
        return self.request("/api/datasources/proxy/uid/" + quote(uid, safe="") + path +
                            ("?" + urlencode(args, doseq=True) if args else ""))

    def panel_url(self, uid, panel_id, context, start_ms, end_ms, solo=False):
        args = {"orgId": 1, "from": start_ms, "to": end_ms, "theme": "dark"}
        args["panelId" if solo else "viewPanel"] = panel_id
        args.update({"var-" + k: v for k, v in context.items()})
        base = self.base if solo else (self.config.grafana_public_url or self.base).rstrip("/")
        return base + ("/d-solo/" if solo else "/d/") + quote(uid, safe="") + "/guardian?" + urlencode(args, doseq=True)

    def screenshot(self, uid, panel_id, context, start_ms, end_ms, output):
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        if self.config.screenshot_mode == "disabled":
            return {"available": False, "reason": "Screenshot capture disabled in configuration"}
        try:
            if self.config.screenshot_mode == "renderer":
                url = self.panel_url(uid, panel_id, context, start_ms, end_ms, solo=True)
                path = url[len(self.base):].replace("/d-solo/", "/render/d-solo/", 1)
                data = self.request(path + "&width=1100&height=420", binary=True)
                if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("Renderer did not return a PNG")
                output.write_bytes(data)
            else:
                helper = Path(__file__).resolve().parent.parent / "scripts" / "capture.mjs"
                payload = {"url": self.panel_url(uid, panel_id, context, start_ms, end_ms, solo=True),
                           "output": str(output), "headers": self.headers()}
                subprocess.run([os.environ.get("NODE_BIN", "node"), str(helper)], input=json.dumps(payload),
                               text=True, capture_output=True, check=True, timeout=50)
            return {"available": True, "path": output.name}
        except (subprocess.SubprocessError, OSError, GrafanaError, ValueError) as exc:
            return {"available": False, "reason": "Capture failed: " + type(exc).__name__}
