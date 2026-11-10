import os
import socket
import threading
import time

import uvicorn
import webview

from app.main import app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    url = "http://127.0.0.1:5173" if os.environ.get("APP_DEV") else f"http://127.0.0.1:{port}"
    webview.create_window("BlahajSphere", url, width=1200, height=800)
    webview.start()
    server.should_exit = True


if __name__ == "__main__":
    main()
