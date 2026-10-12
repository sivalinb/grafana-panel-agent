"""Run the real stack natively on macOS/Linux, with verified official binaries."""
import argparse
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
GRAFANA_VERSION = "12.3.6"
PROMETHEUS_VERSION = "3.15.0"


def download(url, output):
    print("Downloading " + url.rsplit("/", 1)[-1], flush=True)
    with urlopen(url, timeout=180) as source, open(output, "wb") as destination:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            destination.write(chunk)


def unpack(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive) as tar:
        for item in tar.getmembers():
            path = (root / item.name).resolve()
            if root not in path.parents and path != root:
                raise ValueError("Archive member escapes destination")
            if item.issym() or item.islnk():
                link = (path.parent / item.linkname).resolve()
                if root not in link.parents and link != root:
                    raise ValueError("Unsafe archive link")
        tar.extractall(root)
    return next(p for p in destination.iterdir() if p.is_dir())


def native_binaries(runtime):
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64" if machine in ("x86_64", "amd64") else None
    if system not in ("darwin", "linux") or not arch:
        raise SystemExit("Use Docker Compose on this platform")
    bins = runtime / "bin"
    bins.mkdir(parents=True, exist_ok=True)
    grafana_dirs = list((bins / "grafana").glob("*/bin/grafana"))
    prometheus_dirs = list((bins / "prometheus").glob("*/prometheus"))
    if not grafana_dirs:
        grafana_name = "grafana-" + GRAFANA_VERSION + "." + system + "-" + arch + ".tar.gz"
        url = "https://dl.grafana.com/oss/release/" + grafana_name
        archive = bins / grafana_name
        download(url, archive)
        with urlopen(url + ".sha256", timeout=30) as response:
            expected = response.read().decode().split()[0]
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise ValueError("Grafana checksum mismatch")
        home = unpack(archive, bins / "grafana")
    else:
        home = grafana_dirs[0].parent.parent
    if not prometheus_dirs:
        name = "prometheus-" + PROMETHEUS_VERSION + "." + system + "-" + arch + ".tar.gz"
        url = "https://github.com/prometheus/prometheus/releases/download/v" + PROMETHEUS_VERSION + "/"
        archive = bins / name
        download(url + name, archive)
        with urlopen(url + "sha256sums.txt", timeout=30) as response:
            checksums = response.read().decode().splitlines()
        expected = next(line.split()[0] for line in checksums if line.split()[-1] == name)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise ValueError("Prometheus checksum mismatch")
        prometheus = unpack(archive, bins / "prometheus") / "prometheus"
    else:
        prometheus = prometheus_dirs[0]
    return home, prometheus


def wait_for(url, processes, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(p.poll() is not None for p in processes):
            raise RuntimeError("A demo service exited; inspect .runtime/logs")
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(.5)
    raise RuntimeError("Service did not become ready: " + url)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--grafana-home", type=Path)
    parser.add_argument("--prometheus-bin", type=Path)
    parser.add_argument("--runtime", type=Path, default=ROOT / ".runtime")
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    grafana_home, prometheus = (args.grafana_home.resolve(), args.prometheus_bin.resolve()) if args.grafana_home and args.prometheus_bin else native_binaries(runtime)
    logs = runtime / "logs"
    logs.mkdir(exist_ok=True)
    for name in ("grafana/data", "grafana/plugins", "grafana/provisioning", "prometheus", "guardian"):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    grafana_config = runtime / "grafana.ini"
    grafana_config.write_text("\n".join([
        "[server]", "http_addr = 127.0.0.1", "http_port = 3000", "domain = localhost",
        "[paths]", "data = " + str(runtime / "grafana/data"), "logs = " + str(logs),
        "plugins = " + str(runtime / "grafana/plugins"), "provisioning = " + str(runtime / "grafana/provisioning"),
        "[security]", "admin_user = admin", "admin_password = local-demo-only", "disable_initial_admin_creation = false",
        "[auth.anonymous]", "enabled = true", "org_role = Viewer",
        "[auth.basic]", "enabled = true", "[analytics]", "reporting_enabled = false", "check_for_updates = false",
        "[log]", "mode = console", "level = warn", "[plugins]", "preinstall_disabled = true", ""
    ]))
    prometheus_config = runtime / "prometheus.yml"
    prometheus_config.write_text("global:\n  scrape_interval: 2s\n  evaluation_interval: 2s\nscrape_configs:\n  - job_name: checkout\n    static_configs:\n      - targets: ['127.0.0.1:9109']\n")
    config = json.loads((ROOT / "demo/config.json").read_text())
    config["data_dir"] = str(runtime / "guardian")
    config_path = runtime / "config.json"
    config_path.write_text(json.dumps(config, indent=2))
    processes, handles = [], []
    def launch(name, command):
        handle = open(logs / (name + ".log"), "w")
        handles.append(handle)
        process = subprocess.Popen(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
        processes.append(process)
        return process
    def stop(signum=None, frame=None):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    try:
        launch("exporter", [sys.executable, "demo/exporter.py"])
        launch("prometheus", [str(prometheus), "--config.file=" + str(prometheus_config), "--storage.tsdb.path=" + str(runtime / "prometheus"), "--web.listen-address=127.0.0.1:9090"])
        launch("grafana", [str(grafana_home / "bin/grafana"), "server", "--homepath=" + str(grafana_home), "--config=" + str(grafana_config)])
        wait_for("http://127.0.0.1:3000/api/health", processes)
        wait_for("http://127.0.0.1:9090/-/ready", processes)
        subprocess.run([sys.executable, "scripts/bootstrap.py", "--config", str(config_path)], cwd=ROOT, check=True)
        time.sleep(8)
        launch("guardian", [sys.executable, "-m", "guardian", "serve", "--config", str(config_path)])
        wait_for("http://127.0.0.1:8787/healthz", processes)
        print("\nDemo ready", flush=True)
        print("Review workspace: http://127.0.0.1:8787", flush=True)
        print("Grafana dashboard: http://127.0.0.1:3000/d/guardian-checkout", flush=True)
        print("Click 'Simulate label migration', then review and approve the exact patch.", flush=True)
        print("All telemetry is synthetic. Ctrl+C stops the four services.", flush=True)
        while all(p.poll() is None for p in processes):
            time.sleep(1)
        raise RuntimeError("A service exited; inspect " + str(logs))
    except KeyboardInterrupt:
        print("Stopping demo services", flush=True)
    finally:
        for process in reversed(processes):
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    main()
