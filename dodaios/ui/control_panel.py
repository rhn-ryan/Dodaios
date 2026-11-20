# Control panel for Dodaios with instance management

import json
import sys
import urllib.request
import urllib.error
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, List, Dict, Any


# Instance management - KNOWN_INSTANCE_NAMES should match instance.py
DEFAULT_INSTANCE_NAME = "default"
DEV_INSTANCE_NAME = "dev"
KNOWN_INSTANCE_NAMES = (DEFAULT_INSTANCE_NAME, DEV_INSTANCE_NAME, "original", "fresh")

_NAME_RE = r"^[a-z][a-z0-9-]{0,23}$"


def _instance_from_args(args) -> str | None:
    """Extract instance name from control panel args, if provided."""
    return getattr(args, "instance", None)


def _instance_url_base() -> str:
    """Return the app URL, respecting JARVIS_INSTANCE if set.

    When an instance is active, resolve its data directory and port.
    Uses port offset for instances: original/fresh use base port,
    dev instance uses base port + 100.
    """
    from dodaios.core.instance import resolve_instance, DEFAULT_INSTANCE_NAME

    # When an instance is active, resolve its data directory and port
    inst = resolve_instance()
    if inst and inst != DEFAULT_INSTANCE_NAME:
        # Use the instance-specific port if available
        # original/fresh use base ports, dev uses offset of 100
        from dodaios.ui.web.launcher import _fast_admin_port, _app_port
        inst_port = _fast_admin_port()
        return f"http://127.0.0.1:{inst_port}"
    return f"http://127.0.0.1:{_app_port()}"


def _app_port() -> int:
    """Return the default admin port (47821 for original/fresh, 47921 for dev)."""
    from dodaios.ui.web.launcher import _fast_admin_port
    return _fast_admin_port()


# _App class manages the application process state
class _App:
    _instance: str | None = None
    _proc: Any | None = None
    _started_at: float | None = None

    @classmethod
    def start(cls, instance: str | None = None) -> Dict[str, Any]:
        """Start the dodaios application.

        If ``instance`` is provided, it sets ``JARVIS_INSTANCE`` in the
        environment so that a separate data directory and configuration is used.

        Supported instance names: ``default``, ``dev``, ``original``, ``fresh``.

        Returns a dict with status information.
        """
        import subprocess  # noqa: F401 local import to avoid top-level pollution

        # Clean up any existing process
        if cls._proc is not None:
            try:
                cls._proc.terminate()
                cls._proc.wait(timeout=5)
            except Exception:
                cls._proc.kill()
            cls._proc = None
            cls._started_at = None

        instance = _instance_from_args(instance) or instance or DEFAULT_INSTANCE_NAME

        # Validate instance name
        if instance not in KNOWN_INSTANCE_NAMES:
            return {
                "error": f"Unknown instance '{instance}'. Known instances: {', '.join(KNOWN_INSTANCE_NAMES)}"
            }

        cls._instance = instance
        env = dict(__import__("os").environ)
        env["JARVIS_INSTANCE"] = instance

        # Build launcher command with --instance flag if specified (and not default)
        launcher_cmd = [sys.executable, "-m", "dodaios.ui.web.launcher"]
        if instance not in (DEFAULT_INSTANCE_NAME,):
            launcher_cmd.extend(["--instance", instance])

        try:
            cls._proc = subprocess.Popen(
                launcher_cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            cls._started_at = __import__("time").time()
            return {"ok": True, "pid": cls._proc.pid}
        except Exception as exc:
            cls._proc = None
            cls._started_at = None
            return {"error": str(exc)}

    @classmethod
    def stop(cls) -> Dict[str, Any]:
        """Stop the running dodaios application."""
        import subprocess  # noqa: F401 local import

        if cls._proc is not None:
            try:
                cls._proc.terminate()
                cls._proc.wait(timeout=5)
            except Exception:
                try:
                    cls._proc.kill()
                except Exception:
                    pass
            cls._proc = None
            cls._started_at = None
            return {"ok": True, "was_running": True}
        return {"ok": True, "was_running": False}

    @classmethod
    def status(cls) -> Dict[str, Any]:
        """Return the current status of the dodaios application."""
        import urllib.request
        import json

        if cls._proc is None or cls._proc.poll() is not None:
            cls._proc = None
            cls._started_at = None
            return {
                "running": False,
                "pid": None,
                "uptime_s": 0,
                "app_url": _instance_url_base(),
                "backend_ready": False,
                "error": "",
            }

        try:
            # Use instance-specific URL when instance is set
            inst = cls._instance or "default"
            url_base = _instance_url_base()
            r = urllib.request.urlopen(f"{url_base}/api/status", timeout=2)
            payload = json.loads(r.read().decode("utf-8"))
            # Ensure app_url is instance-aware
            if "app_url" not in payload or not payload["app_url"].startswith("http://127.0.0.1"):
                payload["app_url"] = url_base
            return payload
        except Exception as exc:
            return {
                "running": False,
                "pid": cls._proc.pid if cls._proc else None,
                "uptime_s": 0,
                "app_url": _instance_url_base(),
                "backend_ready": False,
                "error": str(exc),
            }


# HTTP handler for the control panel
class _Handler(BaseHTTPRequestHandler):
    """Serves the panel page and its JSON endpoints."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # a control panel that writes an access log is noise

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        # A page that navigated away or a window that closed mid-request is
        # normal traffic, not a panel failure: writing to it raises.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        if self.path in ("/", "/index.html"):
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._json(APP.status())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        if self.path == "/api/start":
            self._json(APP.start())
        elif self.path == "/api/stop":
            self._json(APP.stop())
        elif self.path == "/api/set-instance":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            try:
                payload = json.loads(body.decode("utf-8"))
                instance = payload.get("instance", "default")
                result = APP.start(instance=instance)
                self._json(result)
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
        else:
            self._json({"error": "not found"}, 404)


# HTML page with instance selector
_Page = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>Dodaios control</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         font: 15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif;
         background: #14161a; color: #e8eaed; }
  main { width: 340px; padding: 28px; border-radius: 16px; background: #1c1f25;
         box-shadow: 0 12px 40px rgba(0,0,0,.45); }
  h1 { margin: 0 0 4px; font-size: 17px; font-weight: 600; }
  .state { display: flex; align-items: center; gap: 8px; margin: 16px 0 20px;
           font-size: 14px; color: #b6bcc6; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #6b7280; }
  .dot.on { background: #34d399; box-shadow: 0 0 0 4px rgba(52,211,153,.15); }
  button { width: 100%; padding: 11px; margin-top: 10px; border: 0; border-radius: 10px;
           font: inherit; font-weight: 600; cursor: pointer; color: #0b0d10; background: #e8eaed; }
  button.stop { background: #f87171; }
  button:disabled { opacity: .4; cursor: default; }
  .row { display: flex; gap: 10px; }
  a { display: block; margin-top: 14px; color: #93c5fd; font-size: 13px;
      text-decoration: none; }
  small { display: block; margin-top: 12px; color: #8b93a1; font-size: 12px;
          word-break: break-word; }
</style>
<main>
  <h1>Dodaios</h1>
  <div class="state">
    <span class="dot" id="dot"></span>
    <span id="label">Loading…</span>
  </div>
  <div class="row">
    <select id="instance-selector" onchange="setInstance(this.value)">
      <option value="original">Original Instance</option>
      <option value="fresh">Fresh Instance</option>
      <option value="default">Default Instance</option>
      <option value="dev">Dev Instance</option>
    </select>
  </div>
  <div class="row">
    <button id="start">Start</button>
    <button id="stop" class="stop">Stop</button>
  </div>
  <a id="open" href="#" target="_blank" rel="noreferrer">Open the app</a>
  <small id="meta"></small>
</main>
<script>
const $ = (id) => document.getElementById(id);
async function refresh() {
  const r = await fetch("/api/status").then((x) => x.json());
  $("dot").className = "dot" + (r.running ? " on" : "");
  $("label").textContent = r.running
    ? (r.backend_ready ? "Running" : "Starting…")
    : "Stopped";
  $("start").disabled = r.running;
  $("stop").disabled = !r.running;
  const bits = [];
  if (r.pid) bits.push("pid " + r.pid);
  if (r.running && r.uptime_s) bits.push("up " + r.uptime_s + "s");
  if (r.error) bits.push(r.error);
  $("meta").textContent = bits.join(" · ");
  $("open").href = r.app_url;
}
async function setInstance(name) {
  $("label").textContent = "Setting instance…";
  await fetch("/api/set-instance", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instance: name })
  });
  refresh();
}
async function act(what) {
  $("start").disabled = $("stop").disabled = true;
  $("label").textContent = what === "start" ? "Starting…" : "Stopping…";
  await fetch("/api/" + what, { method: "POST" });
  refresh();
}
$("start").onclick = () => act("start");
$("stop").onclick = () => act("stop");
refresh();
setInterval(refresh, 2000);
</script>
</html>"""


# Module-level APP instance
APP = _App()


def run_panel(port: int = 47991) -> None:
    """Run the control panel HTTP server."""
    server = HTTPServer(("127.0.0.1", port), _Handler)
    print(f"Dodaios control panel: http://127.0.0.1:{port}/")
    print("Ctrl+C to close the panel (the app keeps running).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
