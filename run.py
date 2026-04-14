"""WOMS Launcher — starts the server and opens the browser in app mode."""

import os
import sys
import socket
import subprocess
import threading
import time


def find_free_port(start=8000):
    """Find the first available port starting from `start`."""
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def wait_for_server(port, timeout=30):
    """Block until the server is accepting connections or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def open_app_window(url):
    """Open the URL in a minimal browser window (Chrome/Edge --app mode).

    Falls back to the default browser if no Chromium-based browser is found.
    """
    if sys.platform == "darwin":
        for app_name in ("Google Chrome", "Microsoft Edge", "Brave Browser"):
            app_path = f"/Applications/{app_name}.app"
            if os.path.exists(app_path):
                subprocess.Popen(
                    ["open", "-na", app_name, "--args", f"--app={url}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
    elif sys.platform == "win32":
        # Common install locations on Windows
        candidates = []
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env, "")
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
                candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
                candidates.append(os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"))
        for exe in candidates:
            if os.path.isfile(exe):
                subprocess.Popen(
                    [exe, f"--app={url}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return

    # Fallback: default browser (will have full chrome/address bar)
    import webbrowser
    webbrowser.open(url)


def main():
    port = find_free_port()
    url = f"http://127.0.0.1:{port}"

    print()
    print("  =============================================")
    print("   WOMS  -  Work Order Management System")
    print("  =============================================")
    print()
    print(f"  Server:  {url}")
    print("  Press Ctrl+C to stop.")
    print()

    import uvicorn

    config = uvicorn.Config("app:app", host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    def _open_when_ready():
        if wait_for_server(port):
            print(f"  Ready! Opening browser...")
            print()
            open_app_window(url)

    threading.Thread(target=_open_when_ready, daemon=True).start()

    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        print()
        print("  WOMS stopped. You can close this window.")


if __name__ == "__main__":
    main()
