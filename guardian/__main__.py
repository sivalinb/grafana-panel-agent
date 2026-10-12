import argparse
import json
from .config import Config
from .engine import Engine
from .server import Service


def main():
    parser = argparse.ArgumentParser(description="Grafana Panel Agent")
    parser.add_argument("command", choices=["serve", "scan"])
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    engine = Engine(Config.load(args.config))
    if args.command == "serve":
        Service(engine).serve()
    else:
        print(json.dumps(engine.scan(), indent=2))


if __name__ == "__main__":
    main()
