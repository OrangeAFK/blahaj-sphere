import os
import socket
import sys
import threading
import time

import uvicorn

from app.main import app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    app.state.grading_retry = True  # retry the grading queue on start and periodically (SPEC §3.1)
    browser = "--browser" in sys.argv[1:]  # serve on a fixed port and use a normal browser tab instead of the pywebview window
    port = 8765 if browser else _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    url = "http://127.0.0.1:5173" if os.environ.get("APP_DEV") else f"http://127.0.0.1:{port}"
    if browser:
        print(f"BlahajSphere running at {url} (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        return
    import webview

    webview.create_window("BlahajSphere", url, width=1200, height=800)
    webview.start()
    server.should_exit = True


if __name__ == "__main__":
    main()
