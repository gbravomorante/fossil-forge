import argparse
import json
import socket
import threading
import webbrowser
from urllib.request import urlopen

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Fossil Forge local workbench")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    try:
        with urlopen(url + "/api/health", timeout=1) as response:
            health = json.load(response)
        if health.get("application") == "Fossil Forge":
            print(f"Fossil Forge is already running at {url}.")
            if not args.no_browser:
                webbrowser.open(url)
            return
    except (OSError, ValueError):
        pass
    if not args.no_browser:

        def open_when_ready():
            import time

            for _ in range(80):
                try:
                    with socket.create_connection(("127.0.0.1", args.port), timeout=0.5):
                        webbrowser.open(f"http://127.0.0.1:{args.port}")
                        return
                except OSError:
                    time.sleep(0.25)

        threading.Thread(target=open_when_ready, daemon=True).start()
    uvicorn.run("fossilforge.server:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
