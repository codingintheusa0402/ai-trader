"""AI Trader: engine + API server (+ an optional desktop window).

    python -m app.main               # engine + a desktop window (needs pywebview)
    AITRADER_HEADLESS=1 python -m app.main   # engine only; open the printed URL in a browser

Port: AITRADER_PORT (default 8764). Only 127.0.0.1 is served, plus the optional phone address (AITRADER_MOBILE_HOST).
"""
import os
import socket
import subprocess
import threading
import time

import uvicorn


def free_port(pref=None):
    pref = int(pref or os.environ.get("AITRADER_PORT") or 8764)
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # same as uvicorn; keeps the port (and saved UI prefs) stable
    try:
        s.bind(("127.0.0.1", pref)); return pref
    except OSError:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]
    finally:
        s.close()


def main():
    import fcntl
    from trader import config as C
    (C.STATE_DIR / "app").mkdir(parents=True, exist_ok=True)
    lockf = open(C.STATE_DIR / "app" / ".lock", "w")
    portf = C.STATE_DIR / "app" / "port"
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # one engine per state folder: a second launch just points at the running one
        print(f"Already running: http://127.0.0.1:{portf.read_text().strip()}")
        if os.environ.get("AITRADER_HEADLESS"):
            return
        import webview
        webview.create_window("AI Trader", f"http://127.0.0.1:{portf.read_text().strip()}", width=1480, height=940)
        webview.start(); os._exit(0)
    from app.server import app, start_engine
    port = free_port()
    portf.write_text(str(port))
    start_engine()
    threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning"),
                     daemon=True).start()
    from app.server import tailscale_ip, MOBILE_PORT, engine as _eng

    def mobile_listener():   # phone access, only when AITRADER_MOBILE_HOST is set (private VPN address, token required)
        while True:
            ip = tailscale_ip()
            if ip:
                try:
                    uvicorn.run(app, host=ip, port=MOBILE_PORT, log_level="warning")
                except BaseException:
                    pass
            time.sleep(30)
    threading.Thread(target=mobile_listener, daemon=True).start()

    def watchdog():   # if the trading loop stalls, exit (a service manager such as launchd/systemd restarts it)
        time.sleep(120)
        while True:
            if time.time() - getattr(_eng, "heartbeat", time.time()) > 120:
                os._exit(3)
            time.sleep(15)
    threading.Thread(target=watchdog, daemon=True).start()
    try:   # macOS: keep the computer awake while the engine runs
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    except FileNotFoundError:
        pass
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close(); break
        except OSError:
            time.sleep(0.1)
    url = f"http://127.0.0.1:{port}"
    if os.environ.get("AITRADER_HEADLESS"):
        print(f"AI Trader is running: {url}", flush=True); threading.Event().wait(); return
    try:
        import webview
    except ImportError:
        print(f"AI Trader is running: {url}  (pip install pywebview for a desktop window)", flush=True)
        threading.Event().wait(); return
    webview.create_window("AI Trader", url, width=1480, height=940, min_size=(1100, 720), background_color="#0B0D12")
    webview.start()


if __name__ == "__main__":
    main()
