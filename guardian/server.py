import hmac
import json
import mimetypes
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .engine import Conflict
from .grafana import GrafanaError, Unsupported


class Service:
    def __init__(self, engine):
        self.engine = engine
        self.sessions = {}
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="guardian")
        self.stopping = threading.Event()
        self.static = Path(__file__).resolve().parent.parent / "static"

    def submit(self, name, fn):
        with self.jobs_lock:
            if any(j["status"] == "running" for j in self.jobs.values()):
                raise Conflict("Another operation is running; wait for it to finish")
            job_id = secrets.token_hex(6)
            self.jobs[job_id] = {"id": job_id, "name": name, "status": "running", "started_at": time.time()}
            if len(self.jobs) > 100:
                self.jobs.pop(next(iter(self.jobs)))
        def run():
            try:
                fn()
                result = {"status": "completed"}
            except Exception as exc:
                result = {"status": "failed", "error": str(exc) if isinstance(exc, (Conflict, GrafanaError, Unsupported)) else type(exc).__name__}
            with self.jobs_lock:
                self.jobs[job_id].update(result, finished_at=time.time())
        self.pool.submit(run)
        return self.jobs[job_id].copy()

    def schedule(self):
        while not self.stopping.is_set():
            try:
                self.submit("Scheduled scan", self.engine.scan)
            except Conflict:
                pass
            self.stopping.wait(self.engine.config.scan_interval_seconds)

    def handler(self):
        service = self
        class Handler(BaseHTTPRequestHandler):
            server_version = "GrafanaPanelAgent/0.1"

            def log_message(self, fmt, *args):
                # Keep tokens, request bodies, and upstream payloads out of logs.
                print("http", self.command, urlparse(self.path).path, args[1] if len(args) > 1 else "", flush=True)

            def respond(self, value, status=200, content_type="application/json", cookie=None):
                data = json.dumps(value).encode() if content_type == "application/json" else value
                if isinstance(data, str):
                    data = data.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(data)

            def actor(self):
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                config = service.engine.config
                if token and config.review_token and hmac.compare_digest(token, config.review_token):
                    return config.reviewer_name
                try:
                    cookies = SimpleCookie(self.headers.get("Cookie", ""))
                    session = cookies.get("guardian_session")
                    value = service.sessions.get(session.value) if session else None
                    if value and value["expires"] > time.time():
                        return value["actor"]
                except Exception:
                    pass
                return None

            def json_body(self):
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    raise ValueError("Use application/json")
                size = int(self.headers.get("Content-Length", "0"))
                if size > 65536 or size < 0:
                    raise ValueError("Request is too large")
                body = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("JSON body must be an object")
                return body

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/healthz":
                    return self.respond({"status": "ok", "scheduler": not service.stopping.is_set()})
                if path == "/api/public-config":
                    return self.respond({"demo": service.engine.config.demo})
                if path.startswith("/api/") and not self.actor():
                    return self.respond({"error": "Sign in to view monitoring evidence"}, 401)
                if path == "/api/state":
                    state = service.engine.snapshot()
                    with service.jobs_lock:
                        state["jobs"] = list(service.jobs.values())[-10:]
                    return self.respond(state)
                match = re.fullmatch(r"/api/incidents/([a-f0-9]{12})(/rca)?", path)
                if match:
                    try:
                        if match[2]:
                            return self.respond(service.engine.report(match[1]), content_type="text/markdown; charset=utf-8")
                        item = service.engine.store.get_incident(match[1])
                        item.pop("before_dashboard", None)
                        return self.respond(item)
                    except KeyError:
                        return self.respond({"error": "Incident not found"}, 404)
                evidence = re.fullmatch(r"/api/evidence/([a-f0-9]{12}-(?:before|after)\.png)", path)
                if evidence:
                    file = service.engine.evidence_dir / evidence[1]
                    return self.respond(file.read_bytes(), content_type="image/png") if file.is_file() else self.respond({"error": "Evidence not available"}, 404)
                if path.startswith("/api/"):
                    return self.respond({"error": "Not found"}, 404)
                files = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
                if path not in files:
                    return self.respond({"error": "Not found"}, 404)
                file = service.static / files[path]
                return self.respond(file.read_bytes(), content_type=mimetypes.guess_type(str(file))[0] or "text/plain")

            def do_POST(self):
                path = urlparse(self.path).path
                origin = self.headers.get("Origin")
                if origin and urlparse(origin).netloc != self.headers.get("Host"):
                    return self.respond({"error": "Cross-origin mutation rejected"}, 403)
                try:
                    body = self.json_body()
                    if path == "/api/session":
                        config = service.engine.config
                        if config.demo and body.get("demo") is True:
                            host = self.headers.get("Host", "").split(":")[0]
                            if host not in ("localhost", "127.0.0.1", "guardian"):
                                return self.respond({"error": "Demo sessions are local only"}, 403)
                            actor = "demo-reviewer"
                        elif config.review_token and hmac.compare_digest(str(body.get("token", "")), config.review_token):
                            actor = config.reviewer_name
                        else:
                            return self.respond({"error": "Invalid review token"}, 401)
                        key = secrets.token_urlsafe(32)
                        service.sessions[key] = {"actor": actor, "expires": time.time() + 8 * 3600}
                        cookie = "guardian_session=" + key + "; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"
                        if origin and urlparse(origin).scheme == "https":
                            cookie += "; Secure"
                        return self.respond({"actor": actor}, cookie=cookie)
                    actor = self.actor()
                    if not actor:
                        return self.respond({"error": "Authentication required"}, 401)
                    if path == "/api/scan":
                        return self.respond(service.submit("Dashboard scan", service.engine.scan), 202)
                    if path == "/api/demo/migrate" or path == "/api/demo/reset":
                        if not service.engine.config.demo:
                            raise Conflict("Simulation controls are disabled")
                        action = path.rsplit("/", 1)[-1]
                        return self.respond(service.submit("Demo " + action, lambda: service.engine.demo_control(action)), 202)
                    match = re.fullmatch(r"/api/incidents/([a-f0-9]{12})/(approve|verify|reject|close)", path)
                    if match:
                        incident_id, action = match.groups()
                        service.engine.store.get_incident(incident_id)
                        note = str(body.get("note", ""))[:2000]
                        if action == "approve":
                            if not isinstance(body.get("proposal_hash"), str):
                                raise ValueError("Approval requires a proposal hash")
                            operation = lambda: service.engine.approve(incident_id, body["proposal_hash"], actor, note)
                        elif action == "verify":
                            operation = lambda: service.engine.verify_manual(incident_id, actor, note)
                        elif action == "reject":
                            operation = lambda: service.engine.reject(incident_id, actor, note)
                        else:
                            operation = lambda: service.engine.close(incident_id, actor)
                        return self.respond(service.submit(action.capitalize() + " " + incident_id, operation), 202)
                    return self.respond({"error": "Not found"}, 404)
                except (Conflict, GrafanaError, Unsupported) as exc:
                    return self.respond({"error": str(exc)}, 409)
                except KeyError:
                    return self.respond({"error": "Incident not found"}, 404)
                except (ValueError, TypeError):
                    return self.respond({"error": "Invalid request body"}, 400)

            def do_OPTIONS(self):
                self.respond({"error": "Cross-origin access is disabled"}, 403)
        return Handler

    def serve(self):
        server = ThreadingHTTPServer((self.engine.config.host, self.engine.config.port), self.handler())
        thread = threading.Thread(target=self.schedule, daemon=True, name="scheduler")
        thread.start()
        print("Grafana Panel Agent: http://" + self.engine.config.host + ":" + str(self.engine.config.port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stopping.set()
            server.server_close()
            self.pool.shutdown(wait=True)
