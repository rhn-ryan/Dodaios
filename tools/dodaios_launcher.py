#!/usr/bin/env python3
"""Dodaios Launcher — a small desktop app that starts and stops the server.

Why this exists: ``dodaios serve`` is a foreground process. Stopping it means
Ctrl+C in the right terminal, and a launcher that simply spawns a child dies
with that child. This app owns the lifecycle instead:

    python3 tools/dodaios_launcher.py            # the window
    python3 tools/dodaios_launcher.py --status   # JSON, no GUI (scripting/health)

It runs on the *system* Python (GTK4/PyGObject) and never imports ``dodaios``,
so a broken virtualenv still leaves you a window that can report the breakage.
The server itself is always started from the project's interpreter.

Resilience rules it follows (each one is a failure seen in the wild):

* **Detached child.** ``start_new_session=True`` + stdin closed + stdout to a
  log file, so the server survives the launcher closing and never blocks on a
  terminal that went away.
* **Truth is the health endpoint, not a pid file.** ``/api/health`` decides
  "running". A stale state file, a recycled pid, or a server left behind by an
  earlier terminal launch all resolve to the real state.
* **Never kill a stranger.** Before signalling, the pid is checked against
  ``/proc/<pid>/cmdline`` for ``dodaios``, and the launcher refuses to signal
  its own process group.
* **Port conflicts are explained, not retried.** Something else on the admin
  port gets a clear message plus the next free port, not a boot loop.
* **Crash recovery is bounded.** Optional auto-restart with exponential
  backoff and a hard attempt cap, so a server that cannot start does not spin.
* **Logs are captured.** Every launch appends to one log file; the window
  shows the tail, so a silent failure is impossible.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(
    os.environ.get("DODAIOS_HOME") or Path(__file__).resolve().parents[1]
).resolve()

APP_ID = "com.dodaios.Launcher"
APP_NAME = "Dodaios Launcher"
DEFAULT_PORT = 47821

# Longest a cold boot may take before the launcher calls it stuck. A first run
# on a slow disk loads ~50 registries and a 54-skill scan; measured cold boots
# are 10-25 s, so 180 s only fires on a genuine hang.
BOOT_TIMEOUT_S = 180.0
STOP_TIMEOUT_S = 25.0
HEALTH_TIMEOUT_S = 1.5

# Where the launcher keeps its own bookkeeping. Never inside the repo: the
# checkout is moved/updated, the launcher state should not travel with it.
_STATE_HOME = Path(
    os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
)


def state_dir() -> Path:
    """Per-user launcher state (log + json). Created on demand."""
    path = _STATE_HOME / "dodaios-launcher"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    return state_dir() / "server.log"


def state_path() -> Path:
    return state_dir() / "launcher.json"


def frontend_built() -> bool:
    """True when a built web UI is on disk (the server serves it at /)."""
    candidates = (
        PROJECT_ROOT / "dodaios" / "ui" / "web" / "dist" / "index.html",
        PROJECT_ROOT / "dodaios" / "ui" / "web" / "frontend" / "dist" / "index.html",
    )
    return any(p.is_file() for p in candidates)


# ---------------------------------------------------------------------------
# Controller — all lifecycle logic, no GUI imports
# ---------------------------------------------------------------------------


@dataclass
class Status:
    """What the launcher believes, and why. ``state`` drives the UI."""

    state: str  # running | starting | unhealthy | stopped | port-taken | failed | no-runtime
    port: int
    pid: int | None = None
    version: str | None = None
    instance: str | None = None
    managed: bool = False  # True when this launcher started the process
    detail: str = ""
    log: str = field(default_factory=lambda: str(log_path()))

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class Controller:
    """Start/stop/observe one Dodaios server."""

    def __init__(self, port: int | None = None) -> None:
        self._explicit_port = port
        self.port = port or self._config_port()
        self._proc: subprocess.Popen | None = None

    # -- discovery ---------------------------------------------------------

    def _config_port(self) -> int:
        """Admin port: env override, then the project's TOML, then the default.

        Mirrors ``dodaios.core.config``: ``JARVIS__UI__ADMIN_API_PORT`` wins,
        and the config lives in the project root (``dodaios.toml``, with the
        legacy ``jarvis.toml`` preferred while it still exists).
        """
        env = os.environ.get("JARVIS__UI__ADMIN_API_PORT")
        if env:
            try:
                return int(env)
            except ValueError:
                pass
        import tomllib

        for name in ("jarvis.toml", "dodaios.toml"):
            candidate = PROJECT_ROOT / name
            if not candidate.is_file():
                continue
            try:
                data = tomllib.loads(candidate.read_text(encoding="utf-8"))
                value = data.get("ui", {}).get("admin_api_port")
                if isinstance(value, int) and 0 < value < 65536:
                    return value
            except Exception:  # noqa: BLE001 — a bad TOML must not block the UI
                continue
        return DEFAULT_PORT

    @staticmethod
    def _script_usable(script: Path) -> bool:
        """A console script is only usable when its shebang interpreter exists.

        An editable install bakes an absolute interpreter path into every
        generated script. Moving or renaming the checkout leaves the script in
        place but pointing at a path that is gone, and ``Popen`` then fails with
        a bare ENOENT that looks like the script is missing (live 2026-11-20:
        the checkout moved to another partition). The venv's interpreter is the
        reliable entry point, so the shebang has to be checked, not assumed.
        """
        if not (script.is_file() and os.access(script, os.X_OK)):
            return False
        try:
            first = script.read_bytes().split(b"\n", 1)[0]
        except OSError:
            return False
        if not first.startswith(b"#!"):
            return True  # binary or wrapper; let exec decide
        interpreter = first[2:].strip().decode("utf-8", "replace").split(" ")[0]
        return bool(interpreter) and os.path.exists(interpreter)

    @property
    def can_override_port(self) -> bool:
        """True when the runtime can be told which port to bind.

        ``dodaios serve`` hardcodes ``--headless`` and takes no flags, so a port
        choice there would come from the TOML only. The launcher's own module
        accepts ``--port``, which is what the "use another port" path relies on.
        """
        return (PROJECT_ROOT / ".venv" / "bin" / "python").is_file()

    def runtime(self, port: int | None = None) -> tuple[list[str], str | None]:
        """The command that starts the server, plus a reason when it is missing.

        ``dodaios serve`` is exactly ``launcher.main(["--headless"])``, so the
        module entry point is not a different program — it is the only spelling
        that accepts ``--port``, and going through the venv interpreter means a
        stale console-script shebang (an editable install bakes an absolute
        path in) can never break a launch.
        """
        bind_port = port or self.port
        venv_bin = PROJECT_ROOT / ".venv" / "bin"
        python = venv_bin / "python"
        if python.is_file():
            return (
                [
                    str(python),
                    "-m",
                    "dodaios.ui.web.launcher",
                    "--headless",
                    "--port",
                    str(bind_port),
                ],
                None,
            )
        script = venv_bin / "dodaios"
        if self._script_usable(script):
            return [str(script), "serve"], None
        found = shutil.which("dodaios")
        if found and self._script_usable(Path(found)):
            return [found, "serve"], None
        for fallback in (Path(sys.executable), Path("/usr/bin/python3")):
            if fallback.is_file():
                return [str(fallback), "-m", "dodaios", "serve"], None
        return (
            [],
            "No dodaios runtime found: expected .venv/bin/python in the project "
            "or a dodaios command on PATH.",
        )

    # -- state file --------------------------------------------------------

    def read_state(self) -> dict:
        try:
            data = json.loads(state_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 — absent/corrupt state is simply "unknown"
            return {}

    def _write_state(self, **updates: object) -> None:
        data = self.read_state()
        data.update(updates)
        tmp = state_path().with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(state_path())
        except Exception:  # noqa: BLE001 — bookkeeping must never crash a launch
            pass

    # -- probes ------------------------------------------------------------

    def health(self, timeout: float = HEALTH_TIMEOUT_S) -> tuple[bool, dict]:
        """``/api/health`` is the only authority on "the server is up".

        The proxy handler is deliberately empty: an ``http_proxy`` inherited
        from the shell would otherwise send a loopback request to the proxy and
        report a healthy local server as down.
        """
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(
                f"http://127.0.0.1:{self.port}/api/health", timeout=timeout
            ) as response:
                if response.status != 200:
                    return False, {}
                return True, json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False, {}

    @staticmethod
    def port_in_use(port: int) -> bool:
        """Is something *listening* there.

        Not "can I bind": a socket in TIME_WAIT refuses a plain bind but cannot
        accept connections, and no server (all of which set SO_REUSEADDR)
        would be blocked by it. Counting TIME_WAIT as busy made the launcher
        report "port in use" for a second after every clean stop.
        """
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            pass
        # Bound but not accepting yet (still booting, backlog full).
        return bool(Controller._listening_inodes(port))

    def next_free_port(self, start: int | None = None, limit: int = 25) -> int:
        base = start or self.port
        for offset in range(1, limit + 1):
            candidate = base + offset
            if candidate > 65535:
                break
            if not self.port_in_use(candidate):
                return candidate
        return base + 1

    @staticmethod
    def pid_alive(pid: int) -> bool:
        """Is the pid a *live* process (a zombie is not).

        ``kill(pid, 0)`` succeeds for a zombie, so a stopped server whose
        parent has not reaped it looked "still alive" and the launcher reported
        a failed stop even though the port was already free.
        """
        if pid <= 0:
            return False
        stat = Path(f"/proc/{pid}/stat")
        if stat.parent.parent.is_dir():  # Linux: read the process state
            try:
                fields = stat.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[-1]
                if fields.split()[0] == "Z":
                    return False
            except (OSError, IndexError):
                return False
        try:
            os.kill(pid, 0)
        except OSError as exc:
            return exc.errno == errno.EPERM
        return True

    @staticmethod
    def pid_is_dodaios(pid: int) -> bool:
        """Guard against signalling a recycled pid or an unrelated process."""
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            # No /proc (macOS/Windows): fall back to the state file's claim.
            return pid > 0
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        return "dodaios" in cmdline or "jarvis" in cmdline

    @staticmethod
    def _listening_inodes(port: int) -> set[str]:
        """Socket inodes listening on ``port``, from /proc/net/tcp{,6}."""
        inodes: set[str] = set()
        wanted = f"{port:04X}"
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                lines = Path(table).read_text(encoding="utf-8").splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) < 10:
                    continue
                local, state, inode = fields[1], fields[3], fields[9]
                if state != "0A":  # 0A == TCP_LISTEN
                    continue
                if local.rsplit(":", 1)[-1].upper() == wanted:
                    inodes.add(inode)
        return inodes

    def pid_on_port(self, port: int | None = None) -> int | None:
        """Whoever is listening on the port right now, or ``None``.

        This is what makes "Stop" work for a server started from a terminal —
        the launcher has no pid of its own to trust in that case.
        """
        inodes = self._listening_inodes(port or self.port)
        if not inodes:
            return None
        targets = {f"socket:[{inode}]" for inode in inodes}
        try:
            pids = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]
        except OSError:
            return None
        for pid in pids:
            fd_dir = Path(f"/proc/{pid}/fd")
            try:
                for fd in fd_dir.iterdir():
                    try:
                        if os.readlink(fd) in targets:
                            return pid
                    except OSError:
                        continue
            except OSError:
                continue
        return None

    # -- status ------------------------------------------------------------

    def status(self, health_timeout: float = HEALTH_TIMEOUT_S) -> Status:
        """Resolve reality. Order matters: health, then listener, then state.

        ``health_timeout`` is short on the 1 s UI timer so a hung server cannot
        stall the window, and generous for one-shot checks.
        """
        ok, data = self.health(timeout=health_timeout)
        state_data = self.read_state()

        if ok:
            pid = self.pid_on_port()
            managed = bool(
                state_data.get("pid")
                and pid
                and int(state_data["pid"]) == int(pid)
                and state_data.get("port") == self.port
            )
            return Status(
                state="running",
                port=self.port,
                pid=pid,
                version=str(data.get("version") or "") or None,
                instance=str(data.get("instance") or "") or None,
                managed=managed,
                detail=(
                    "Started outside the launcher — Stop will still shut it down."
                    if not managed
                    else "Running."
                ),
            )

        pid = self.pid_on_port()
        if pid is not None:
            if self.pid_is_dodaios(pid):
                # Bound but not answering: either mid-boot or hung. The UI
                # times this and only complains after BOOT_TIMEOUT_S.
                return Status(
                    state="starting",
                    port=self.port,
                    pid=pid,
                    detail="The port is bound but /api/health has not answered yet.",
                )
            return Status(
                state="port-taken",
                port=self.port,
                pid=pid,
                detail=(
                    f"Port {self.port} is held by a process that is not Dodaios "
                    f"(pid {pid}). Pick another port to start."
                ),
            )

        if self.port_in_use(self.port):
            return Status(
                state="port-taken",
                port=self.port,
                detail=f"Port {self.port} is in use by another service.",
            )

        stale_pid = state_data.get("pid")
        if stale_pid and state_data.get("port") == self.port and self.pid_alive(int(stale_pid)):
            return Status(
                state="failed",
                port=self.port,
                pid=int(stale_pid),
                detail="The recorded process is alive but is not serving on this port.",
            )

        return Status(
            state="stopped",
            port=self.port,
            detail=(
                "Ready to start."
                if frontend_built()
                else "Ready to start — but the web UI is not built yet."
            ),
        )

    # -- lifecycle ---------------------------------------------------------

    def remember_port(self, port: int) -> None:
        """Move this controller to another port and persist the choice."""
        self.port = port
        self._write_state(port=port)

    def _rotate_log(self, keep_bytes: int = 8 * 1024 * 1024) -> None:
        path = log_path()
        try:
            if path.is_file() and path.stat().st_size > keep_bytes:
                backup = path.with_suffix(".1.log")
                backup.unlink(missing_ok=True)
                path.replace(backup)
        except OSError:
            pass

    def start(self) -> tuple[bool, str]:
        """Spawn the server detached. Returns success and a human reason."""
        cmd, missing = self.runtime(self.port)
        if missing:
            return False, missing

        current = self.status()
        if current.state == "running":
            return True, f"Already running on {current.url}"
        if current.state == "port-taken":
            suggestion = self.next_free_port() if self.can_override_port else None
            if suggestion:
                return False, f"{current.detail} A free port would be {suggestion}."
            return False, current.detail
        if current.state in ("starting", "unhealthy"):
            return False, "A Dodaios process is already holding the port."

        self._rotate_log()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("DODAIOS_INSTANCE", None)

        try:
            handle = open(log_path(), "ab", buffering=0)  # noqa: SIM115 — child owns it
        except OSError as exc:
            return False, f"Cannot open the log file {log_path()}: {exc}"

        handle.write(
            f"\n===== {APP_NAME}: starting at {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(port {self.port}) =====\n".encode()
        )
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # survive this launcher exiting
                close_fds=True,
            )
        except OSError as exc:
            handle.write(f"spawn failed: {exc}\n".encode())
            handle.close()
            return False, f"Could not start {cmd[0]}: {exc}"
        finally:
            handle.close()

        pid = self._proc.pid
        self._write_state(pid=pid, port=self.port, started_at=time.time(), url=f"http://127.0.0.1:{self.port}")
        return True, f"Starting (pid {pid})…"

    def wait_ready(
        self, timeout: float = BOOT_TIMEOUT_S, on_tick=None
    ) -> tuple[bool, str]:
        """Block until the server answers, dies, or times out (call off the UI thread)."""
        if self._proc is None and not self.health(timeout=0.8)[0]:
            return False, "No server process was started."
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                code = self._proc.returncode
                tail = self.tail(12)
                reason = f"The server exited with code {code}."
                if tail.strip():
                    reason += f"\n\n{tail.strip()}"
                self._write_state(last_exit={"code": code, "at": time.time()})
                return False, reason
            ok, _ = self.health(timeout=0.8)
            if ok:
                self._write_state(ready_at=time.time())
                return True, f"Running at http://127.0.0.1:{self.port}"
            if on_tick is not None:
                on_tick()
            time.sleep(0.35)
        return False, (
            f"The server did not answer /api/health within {int(timeout)}s. "
            "See the log for where it stopped."
        )

    def _signal(self, pid: int, sig: int, group: bool) -> bool:
        try:
            if group:
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)
            return True
        except ProcessLookupError:
            return True
        except OSError:
            return False

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> tuple[bool, str]:
        """Graceful TERM to the process group, then KILL. Idempotent."""
        state_data = self.read_state()
        pid = None
        for candidate in (state_data.get("pid"), self.pid_on_port()):
            if candidate and self.pid_alive(int(candidate)):
                pid = int(candidate)
                break
        if pid is None:
            self._write_state(pid=None)
            return True, "Already stopped."

        if pid == os.getpid():
            return False, "Refusing to stop the launcher itself."

        if not self.pid_is_dodaios(pid):
            return False, (
                f"Pid {pid} does not look like Dodaios; not signalling it. "
                "Stop that process from its owner."
            )

        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None
        same_group = pgid is not None and pgid == os.getpgrp()
        use_group = pgid is not None and pgid == pid and not same_group

        if not self._signal(pid, signal.SIGTERM, use_group):
            return False, f"Could not signal pid {pid}."

        def _gone() -> bool:
            if self._proc is not None and self._proc.pid == pid:
                with contextlib.suppress(Exception):
                    self._proc.wait(timeout=0.1)  # reap; that is what makes it exit
            return not self.pid_alive(pid) and not self.health(timeout=0.5)[0]

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _gone():
                self._write_state(pid=None, stopped_at=time.time())
                return True, "Stopped."
            time.sleep(0.2)

        self._signal(pid, getattr(signal, "SIGKILL", signal.SIGTERM), use_group)
        for _ in range(25):
            if _gone():
                self._write_state(pid=None, stopped_at=time.time())
                return True, "Stopped (had to force-kill)."
            time.sleep(0.2)

        # A process this launcher did not parent cannot be reaped here; what
        # matters for the user is whether the port came free.
        if not self.port_in_use(self.port):
            self._write_state(pid=None, stopped_at=time.time())
            return True, "Stopped."
        return False, f"Pid {pid} is still alive after SIGKILL."

    # -- log ---------------------------------------------------------------

    def tail(self, lines: int = 200, max_bytes: int = 256 * 1024) -> str:
        path = log_path()
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max_bytes))
                chunk = handle.read().decode("utf-8", "replace")
        except OSError:
            return ""
        parts = chunk.splitlines()
        return "\n".join(parts[-lines:])

    def log_signature(self) -> tuple[int, int]:
        try:
            stat = log_path().stat()
            return stat.st_size, int(stat.st_mtime)
        except OSError:
            return 0, 0

    # -- misc --------------------------------------------------------------

    def open_url(self) -> bool:
        url = f"http://127.0.0.1:{self.port}"
        try:
            if sys.platform == "win32":
                os.startfile(url)  # type: ignore[attr-defined]
            else:
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen(
                    [opener, url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            return True
        except OSError:
            return False

    def open_path(self, path: Path) -> bool:
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen(
                    [opener, str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Desktop-entry install (so the app is clickable, not just runnable)
# ---------------------------------------------------------------------------


def install_desktop_entry() -> tuple[bool, Path]:
    data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    apps = data_home / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    target = apps / "dodaios-launcher.desktop"

    icon = PROJECT_ROOT / "dodaios" / "assets" / "icons" / "dodaios.png"
    if not icon.is_file():
        icon = PROJECT_ROOT / "assets" / "icons" / "dodaios-1024.png"

    python = Path("/usr/bin/python3")
    interpreter = python if python.is_file() else Path(sys.executable)

    entry = "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            f"Name={APP_NAME}",
            "Comment=Start and stop the Dodaios server",
            f'Exec="{interpreter}" "{Path(__file__).resolve()}"',
            f"Icon={icon}" if icon.is_file() else "Icon=utilities-terminal",
            "Terminal=false",
            "Categories=Development;",
            "Keywords=dodaios;jarvis;server;",
            "StartupNotify=true",
            "",
        ]
    )
    try:
        target.write_text(entry, encoding="utf-8")
        target.chmod(0o755)
    except OSError:
        return False, target

    updater = shutil.which("update-desktop-database")
    if updater:
        subprocess.run(
            [updater, str(apps)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    return True, target


# ---------------------------------------------------------------------------
# GTK UI
# ---------------------------------------------------------------------------


CSS = """
.window { background-color: @window_bg_color; }
.hero { padding: 18px 20px; }
.hero-title { font-size: 1.5em; font-weight: 800; }
.dim { opacity: 0.68; }
.mono { font-family: monospace; font-size: 0.9em; }
.status-dot { border-radius: 7px; min-width: 14px; min-height: 14px; background-color: #9a9996; }
.status-dot.running { background-color: #2ec27e; }
.status-dot.starting { background-color: #f5c211; }
.status-dot.failed, .status-dot.port-taken { background-color: #e01b24; }
.card { padding: 16px; border-radius: 12px; background-color: alpha(currentColor, 0.05); }
.url-label { font-family: monospace; font-size: 1.05em; font-weight: 600; }
.action-row { padding: 4px; }
logview { padding: 8px; }
/* Some themes measure the scrollbar slider at -2 px and log an allocation
   warning on first map; explicit floors keep it quiet. */
scrollbar slider { min-width: 8px; min-height: 32px; }
"""


def _load_gtk():
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk

    return Gtk, Gdk, Gio, GLib


class LauncherWindow:
    """The window. Owns threading + timers; Controller owns all the logic."""

    def __init__(self, app, controller: Controller) -> None:
        (
            self._Gtk,
            self._Gdk,
            self._Gio,
            self._GLib,
        ) = _load_gtk()
        Gtk = self._Gtk

        self.app = app
        self.controller = controller
        self.busy = False
        self.user_stop = False
        self.ready_since: float | None = None
        self.start_deadline: float | None = None
        self.restart_attempts = 0
        self.last_state: str | None = None
        self._starting_since: float | None = None
        self._log_sig: tuple[int, int] | None = None
        self._busy_detail = ""

        self.window = Gtk.ApplicationWindow(application=app)
        self.window.set_default_size(780, 660)
        self.window.set_title(APP_NAME)
        self.window.add_css_class("window")
        self.window.connect("close-request", self._on_close_request)

        self._build_ui()
        self._install_css()
        self._refresh()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        Gtk = self._Gtk

        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        logo = self._logo_widget(28)
        if logo is not None:
            title_box.append(logo)
        title_box.append(Gtk.Label(label=APP_NAME))
        header.set_title_widget(title_box)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_button.set_tooltip_text("More actions")
        popover = Gtk.Popover()
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        menu_box.set_margin_top(6)
        menu_box.set_margin_bottom(6)
        menu_box.set_margin_start(6)
        menu_box.set_margin_end(6)
        for label, callback in (
            ("Open log file", self._on_open_log_file),
            ("Open log folder", self._on_open_log_folder),
            ("Add to application menu", self._on_install_desktop_entry),
        ):
            button = Gtk.Button(label=label)
            button.add_css_class("flat")
            button.connect("clicked", lambda _b, cb=callback: (popover.popdown(), cb()))
            menu_box.append(button)
        popover.set_child(menu_box)
        menu_button.set_popover(popover)
        header.pack_end(menu_button)
        self.window.set_titlebar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for side in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{side}")(18)
        self.window.set_child(root)

        # Status card
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.dot = Gtk.Box()
        self.dot.add_css_class("status-dot")
        self.dot.set_valign(Gtk.Align.CENTER)
        status_row.append(self.dot)
        self.state_label = Gtk.Label(label="Checking…")
        self.state_label.add_css_class("hero-title")
        self.state_label.set_xalign(0)
        status_row.append(self.state_label)
        card.append(status_row)

        self.detail_label = Gtk.Label(label="")
        self.detail_label.set_xalign(0)
        self.detail_label.set_wrap(True)
        self.detail_label.add_css_class("dim")
        card.append(self.detail_label)

        url_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.url_label = Gtk.Label(label="")
        self.url_label.add_css_class("url-label")
        self.url_label.set_xalign(0)
        self.url_label.set_selectable(True)
        url_row.append(self.url_label)
        self.meta_label = Gtk.Label(label="")
        self.meta_label.add_css_class("dim")
        self.meta_label.set_xalign(1)
        self.meta_label.set_hexpand(True)
        url_row.append(self.meta_label)
        card.append(url_row)

        self.progress = Gtk.ProgressBar()
        self.progress.set_visible(False)
        card.append(self.progress)
        root.append(card)

        # Actions
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        actions.add_css_class("action-row")
        self.start_button = Gtk.Button(label="Start")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self._on_start)
        self.stop_button = Gtk.Button(label="Stop")
        self.stop_button.add_css_class("destructive-action")
        self.stop_button.connect("clicked", self._on_stop)
        self.restart_button = Gtk.Button(label="Restart")
        self.restart_button.connect("clicked", self._on_restart)
        self.open_button = Gtk.Button(label="Open in Browser")
        self.open_button.connect("clicked", self._on_open_browser)
        for button in (
            self.start_button,
            self.stop_button,
            self.restart_button,
            self.open_button,
        ):
            actions.append(button)
        root.append(actions)

        # Options. Check buttons rather than switches: the GTK theme on some
        # desktops measures a squeezed Gtk.Switch slider at -2 px and logs an
        # allocation warning on every launch.
        options = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22)
        self.auto_restart_switch = Gtk.CheckButton(label="Restart if it crashes")
        self.auto_restart_switch.set_active(True)
        self.open_browser_switch = Gtk.CheckButton(label="Open browser when ready")
        self.open_browser_switch.set_active(True)
        options.append(self.auto_restart_switch)
        options.append(self.open_browser_switch)
        root.append(options)

        # Log
        self.expander = Gtk.Expander(label="Server log")
        self.expander.set_expanded(True)
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        log_box.set_margin_top(8)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(230)
        self.log_buffer = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buffer)
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class("mono")
        scroller.set_child(self.log_view)
        log_box.append(scroller)

        log_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        copy_button = Gtk.Button(label="Copy log")
        copy_button.connect("clicked", self._on_copy_log)
        reveal_button = Gtk.Button(label="Open log file")
        reveal_button.connect("clicked", self._on_open_log_file)
        clear_button = Gtk.Button(label="Clear log")
        clear_button.connect("clicked", self._on_clear_log)
        log_actions.append(copy_button)
        log_actions.append(reveal_button)
        log_actions.append(clear_button)
        log_box.append(log_actions)
        self.expander.set_child(log_box)
        root.append(self.expander)

        # Footer note about the build, if relevant
        self.notice_label = Gtk.Label(label="")
        self.notice_label.set_xalign(0)
        self.notice_label.set_wrap(True)
        self.notice_label.add_css_class("dim")
        if not frontend_built():
            self.notice_label.set_text(
                "The web UI has not been built in this checkout yet. The server "
                "will start, but the browser page needs `npm run build` in "
                "dodaios/ui/web/frontend (the launcher still reports health)."
            )
        root.append(self.notice_label)

    def _logo_widget(self, size: int):
        Gtk = self._Gtk
        icon = PROJECT_ROOT / "dodaios" / "assets" / "icons" / "dodaios.png"
        if not icon.is_file():
            icon = PROJECT_ROOT / "assets" / "icons" / "dodaios-1024.png"
        if not icon.is_file():
            return None
        try:
            texture = self._Gdk.Texture.new_from_filename(str(icon))
            image = Gtk.Image.new_from_paintable(texture)
        except Exception:  # noqa: BLE001 — no icon is cosmetic, never fatal
            try:
                image = Gtk.Image.new_from_file(str(icon))
            except Exception:  # noqa: BLE001
                return None
        image.set_pixel_size(size)
        return image

    def _install_css(self) -> None:
        Gtk = self._Gtk
        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(CSS)
        except AttributeError:  # GTK < 4.12
            provider.load_from_data(CSS.encode())
        display = self._Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    # -- notifications -----------------------------------------------------

    def _notify(self, summary: str, body: str) -> None:
        try:
            import gi

            gi.require_version("Notify", "0.7")
            from gi.repository import Notify

            if not Notify.is_initted():
                Notify.init(APP_NAME)
            notification = Notify.Notification.new(summary, body, None)
            notification.show()
        except Exception:  # noqa: BLE001 — desktop without a notification daemon
            pass

    # -- rendering ---------------------------------------------------------

    def _set_state_style(self, state: str) -> None:
        for cls in ("running", "starting", "failed", "port-taken"):
            self.dot.remove_css_class(cls)
        if state in ("running", "starting", "port-taken"):
            self.dot.add_css_class(state)
        elif state in ("failed", "no-runtime"):
            self.dot.add_css_class("failed")

    def _refresh(self) -> None:
        """One status poll + repaint. Called on a 1 s timer."""
        status = self.controller.status(health_timeout=0.7)
        self.current = status

        labels = {
            "running": "Running",
            "starting": "Starting…",
            "unhealthy": "Not responding",
            "stopped": "Stopped",
            "port-taken": "Port in use",
            "failed": "Stopped (error)",
            "no-runtime": "Runtime missing",
        }
        if self.busy and self._busy_detail:
            self.state_label.set_text(self._busy_detail)
        else:
            self.state_label.set_text(labels.get(status.state, status.state))
        self._set_state_style(status.state)

        # A bound port that never answers is a hang, not a slow boot. Without
        # this the window sits on "Starting…" forever and the only clue is the
        # log.
        detail = status.detail
        if status.state == "starting":
            if self._starting_since is None:
                self._starting_since = time.monotonic()
            hung_for = time.monotonic() - self._starting_since
            if hung_for > BOOT_TIMEOUT_S and not self.busy:
                self.state_label.set_text("Not responding")
                self._set_state_style("failed")
                detail = (
                    f"Port {status.port} has been held for {int(hung_for)}s without "
                    "answering /api/health. Press Restart; the log shows where it "
                    "stopped."
                )
        else:
            self._starting_since = None

        if not self.busy:
            self.detail_label.set_text(detail)
        self.url_label.set_text(status.url if status.state in ("running", "starting") else "")

        meta_bits = []
        if status.version:
            meta_bits.append(f"v{status.version}")
        if status.instance:
            meta_bits.append(status.instance)
        if status.pid:
            meta_bits.append(f"pid {status.pid}")
        self.meta_label.set_text("  ·  ".join(meta_bits))

        running = status.state == "running"
        self.start_button.set_sensitive(not running and not self.busy)
        self.stop_button.set_sensitive(
            status.state in ("running", "starting", "unhealthy") and not self.busy
        )
        self.restart_button.set_sensitive(not self.busy)
        self.open_button.set_sensitive(running)

        if status.state == "starting" or self.busy:
            self.progress.set_visible(True)
            self.progress.pulse()
        else:
            self.progress.set_visible(False)

        self._refresh_log()

        if status.state == "running" and self.ready_since is None:
            self.ready_since = time.monotonic()
            self.restart_attempts = 0
            self.start_deadline = None
        elif status.state != "running":
            self.ready_since = None

        self._maybe_auto_restart(status)
        self.last_state = status.state

    def _refresh_log(self) -> None:
        signature = self.controller.log_signature()
        if signature == self._log_sig:
            return
        self._log_sig = signature
        text = self.controller.tail(200) or "(no log output yet)"
        self.log_buffer.set_text(text)
        # Keep the newest lines in view.
        end = self.log_buffer.get_end_iter()
        self.log_view.scroll_to_iter(end, 0.0, False, 0.0, 1.0)

    # -- actions -----------------------------------------------------------

    def _run_in_thread(self, work, done) -> None:
        import threading

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 — surface, never swallow
                result = (False, f"Unexpected error: {exc}")
            self._GLib.idle_add(lambda: (done(result), False)[1])

        threading.Thread(target=runner, daemon=True).start()

    def _begin_busy(self, label: str) -> None:
        self.busy = True
        self._busy_detail = label
        self.state_label.set_text(label)
        self.progress.set_visible(True)
        self.start_button.set_sensitive(False)
        self.stop_button.set_sensitive(False)
        self.restart_button.set_sensitive(False)

    def _end_busy(self) -> None:
        self.busy = False
        self._busy_detail = ""
        self._refresh()

    def _on_start(self, _button=None) -> None:
        if self.busy:
            return
        status = self.controller.status()
        if status.state == "port-taken":
            self._offer_alternate_port(status)
            return

        self.user_stop = False
        self._begin_busy("Starting…")
        self.start_deadline = time.monotonic()

        def work():
            ok, message = self.controller.start()
            if not ok:
                return False, message
            return self.controller.wait_ready(on_tick=self._on_boot_tick)

        def done(result):
            ok, message = result
            self._end_busy()
            if ok:
                self._notify("Dodaios is ready", self.controller.status().url)
                if self.open_browser_switch.get_active():
                    self.controller.open_url()
                self.detail_label.set_text(message)
            else:
                self.detail_label.set_text(message)
                self._notify("Dodaios failed to start", message.split("\n")[0])
                self._show_error("Dodaios did not start", message)

        self._run_in_thread(work, done)

    def _on_boot_tick(self) -> None:
        if self.start_deadline is None:
            return
        elapsed = time.monotonic() - self.start_deadline
        self._GLib.idle_add(
            lambda: (
                self.state_label.set_text(f"Starting… ({int(elapsed)}s)"),
                False,
            )[1]
        )

    def _offer_alternate_port(self, status: Status) -> None:
        Gtk = self._Gtk
        if not self.controller.can_override_port:
            self._show_error(
                f"Port {status.port} is unavailable",
                status.detail
                + "\n\nThis runtime starts without a port flag, so free the port "
                "and press Start again.",
            )
            return
        suggestion = self.controller.next_free_port()
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Port {status.port} is unavailable")
        dialog.set_detail(
            f"{status.detail}\n\nStart on port {suggestion} instead? The setting in "
            "your config stays untouched — this only applies to this run."
        )
        dialog.set_buttons(["Use another port", "Cancel"])
        dialog.set_cancel_button(1)
        dialog.set_default_button(0)

        def on_choice(source, result):
            try:
                choice = source.choose_finish(result)
            except Exception:  # noqa: BLE001 — dialog dismissed
                return
            if choice == 0:
                self.controller.remember_port(suggestion)
                self._refresh()
                self._on_start()

        dialog.choose(self.window, None, on_choice)

    def _on_stop(self, _button=None) -> None:
        if self.busy:
            return
        self.user_stop = True
        self._begin_busy("Stopping…")

        def done(result):
            ok, message = result
            self._end_busy()
            self.detail_label.set_text(message)
            if not ok:
                self._show_error("Could not stop Dodaios", message)

        self._run_in_thread(self.controller.stop, done)

    def _on_restart(self, _button=None) -> None:
        if self.busy:
            return
        self.user_stop = False
        self._begin_busy("Restarting…")

        def work():
            ok, message = self.controller.stop()
            if not ok:
                return False, message
            started, start_message = self.controller.start()
            if not started:
                return False, start_message
            return self.controller.wait_ready(on_tick=self._on_boot_tick)

        def done(result):
            ok, message = result
            self._end_busy()
            if ok:
                self._notify("Dodaios restarted", self.controller.status().url)
                if self.open_browser_switch.get_active():
                    self.controller.open_url()
            else:
                self.detail_label.set_text(message)
                self._show_error("Restart failed", message)

        self._run_in_thread(work, done)

    def _on_open_browser(self, _button=None) -> None:
        url = f"http://127.0.0.1:{self.controller.port}"
        try:
            # Gio (not the deprecated Gtk.show_uri): works under a portal and
            # falls back to the xdg handler without a DBus round trip.
            self._Gio.AppInfo.launch_default_for_uri(url, None)
        except Exception:  # noqa: BLE001 — no portal/DBus: fall back to xdg-open
            self.controller.open_url()

    def _on_copy_log(self, _button=None) -> None:
        clipboard = self._Gdk.Display.get_default().get_clipboard()
        clipboard.set(self.controller.tail(200))

    def _on_clear_log(self, _button=None) -> None:
        try:
            log_path().write_text("", encoding="utf-8")
        except OSError:
            return
        self._log_sig = None
        self._refresh_log()

    def _on_open_log_file(self) -> None:
        log_path().touch(exist_ok=True)
        self.controller.open_path(log_path())

    def _on_open_log_folder(self) -> None:
        self.controller.open_path(state_dir())

    def _on_install_desktop_entry(self) -> None:
        ok, target = install_desktop_entry()
        if ok:
            self._notify("Added to application menu", str(target))
            self.detail_label.set_text(
                f"App-menu entry installed: {target} — search for "
                f"“{APP_NAME}” in your application launcher."
            )
        else:
            self._show_error(
                "Could not install the menu entry", f"Writing {target} failed."
            )

    def _show_error(self, message: str, detail: str) -> None:
        Gtk = self._Gtk
        dialog = Gtk.AlertDialog()
        dialog.set_message(message)
        dialog.set_detail(detail[:4000])
        dialog.set_buttons(["Close"])
        dialog.show(self.window)

    # -- crash recovery ----------------------------------------------------

    def _maybe_auto_restart(self, status: Status) -> None:
        if self.busy:
            return
        crashed = (
            self.last_state in ("running", "starting")
            and status.state in ("stopped", "failed")
            and not self.user_stop
        )
        if not crashed:
            return
        if not self.auto_restart_switch.get_active():
            self.detail_label.set_text(
                "The server stopped unexpectedly. Auto-restart is off."
            )
            return
        if self.restart_attempts >= 5:
            self.detail_label.set_text(
                "The server stopped repeatedly — auto-restart gave up after 5 "
                "attempts. Check the log, then press Start."
            )
            return

        self.restart_attempts += 1
        delay = min(60, 3 * (2 ** (self.restart_attempts - 1)))
        self.detail_label.set_text(
            f"The server stopped unexpectedly. Restarting in {delay}s "
            f"(attempt {self.restart_attempts}/5)…"
        )
        self._GLib.timeout_add_seconds(delay, self._auto_restart_now)

    def _auto_restart_now(self) -> bool:
        if not self.busy and self.controller.status().state != "running":
            self._on_start()
        return False  # one-shot

    # -- window close ------------------------------------------------------

    def _on_close_request(self, _window) -> bool:
        status = self.controller.status()
        if self.busy or status.state not in ("running", "starting"):
            return False  # default: quit

        Gtk = self._Gtk
        dialog = Gtk.AlertDialog()
        dialog.set_message("Dodaios is running")
        dialog.set_detail(
            "The server can keep running in the background after this window "
            "closes, so your chats stay available in the browser."
        )
        dialog.set_buttons(["Keep it running", "Stop and quit", "Cancel"])
        dialog.set_cancel_button(2)
        dialog.set_default_button(0)

        def on_choice(source, result):
            try:
                choice = source.choose_finish(result)
            except Exception:  # noqa: BLE001 — dismissed == cancel
                return
            if choice == 0:
                self.app.quit()
            elif choice == 1:
                self._stop_then_quit()

        dialog.choose(self.window, None, on_choice)
        return True

    def _stop_then_quit(self) -> None:
        def work():
            return self.controller.stop()

        def done(_result):
            self.app.quit()

        self._run_in_thread(work, done)


def run_gui(controller: Controller, argv: list[str]) -> int:
    Gtk, _Gdk, _Gio, GLib = _load_gtk()

    class LauncherApp(Gtk.Application):
        def __init__(self) -> None:
            super().__init__(
                application_id=APP_ID,
                flags=_Gio.ApplicationFlags.DEFAULT_FLAGS,
                # Never exit just because the window closed: the tick timer
                # decides, and a server outliving the window is normal here.
                inactivity_timeout=0,
            )
            self.window: LauncherWindow | None = None

        def do_activate(self) -> None:
            if self.window is None:
                self.window = LauncherWindow(self, controller)
                self.window.window.present()
                GLib.timeout_add(1000, self._tick)
            else:
                self.window.window.present()

        def _tick(self) -> bool:
            if self.window is None:
                return False
            try:
                self.window._refresh()
            except Exception:  # noqa: BLE001 — a failed poll must not kill the loop
                pass
            return True

    return LauncherApp().run(argv)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dodaios_launcher",
        description="Start and stop the Dodaios server from a desktop window.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the server state as JSON and exit (no window).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Admin API port to manage (default: config value, else {DEFAULT_PORT}).",
    )
    args = parser.parse_args(argv)

    controller = Controller(port=args.port)
    if args.status:
        print(json.dumps(asdict(controller.status()), indent=2))
        return 0

    return run_gui(controller, [])


if __name__ == "__main__":
    raise SystemExit(main())
