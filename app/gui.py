from __future__ import annotations

import json
import subprocess
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Optional

from .tor_manager import TorManager, TorRuntime


class SessionBridge:
    """Bidirectional JSON-over-stdio bridge to the XMPP worker."""

    def __init__(self, on_event: Callable[[dict[str, Any]], None]) -> None:
        self.on_event = on_event
        self.proc: Optional[subprocess.Popen[str]] = None
        self._reader_thread: Optional[threading.Thread] = None

    def start(
        self,
        workdir: Path,
        env: dict[str, str],
        host: str,
        port: int,
        domain: str,
        username: str,
        password: str,
    ) -> None:
        if self.proc and self.proc.poll() is None:
            raise RuntimeError("Session already running")

        cmd = [
            "torsocks",
            "python3",
            "-m",
            "app.xmpp_worker",
            "session",
            "--host",
            host,
            "--port",
            str(port),
            "--domain",
            domain,
            "--username",
            username,
            "--password",
            password,
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self._reader_thread.start()

    def _read_output(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        for line in self.proc.stdout:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    self.on_event(payload)
                else:
                    self.on_event({"type": "status", "message": raw})
            except json.JSONDecodeError:
                self.on_event({"type": "status", "message": raw})

    def send(self, payload: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin or self.proc.poll() is not None:
            raise RuntimeError("Session is not running")
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def stop(self) -> None:
        if not self.proc:
            return
        if self.proc.poll() is None:
            try:
                self.send({"action": "shutdown"})
            except Exception:
                pass
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
        self.proc = None


class MainWindow:
    def __init__(self, root: tk.Tk, project_root: Path) -> None:
        self.root = root
        self.project_root = project_root
        self.app_data_dir = Path.home() / ".prosody-onion-chat"
        self.tor = TorManager(self.app_data_dir)
        self.tor_runtime: Optional[TorRuntime] = None
        self.tor_starting = False
        self.session = SessionBridge(self._on_worker_event)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._log("App started")
        self._set_status("Initializing Tor...")
        threading.Thread(target=self._start_tor, daemon=True).start()

    def _build_ui(self) -> None:
        self.root.title("Prosody Onion Chat")
        self.root.geometry("980x620")

        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Domain").grid(row=0, column=0, sticky=tk.W)
        self.domain_var = tk.StringVar(value="24nzlv4njhn5bpxuqjnn7jqvjfvpmbc6oijq72z3p4kzwd7n6gpn4eid.onion")
        ttk.Entry(top, textvariable=self.domain_var, width=64).grid(row=0, column=1, sticky=tk.W, padx=6)

        ttk.Label(top, text="Host").grid(row=0, column=2, sticky=tk.W)
        self.host_var = tk.StringVar(value=self.domain_var.get())
        ttk.Entry(top, textvariable=self.host_var, width=64).grid(row=0, column=3, sticky=tk.W, padx=6)

        ttk.Label(top, text="Port").grid(row=0, column=4, sticky=tk.W)
        self.port_var = tk.StringVar(value="5222")
        ttk.Entry(top, textvariable=self.port_var, width=8).grid(row=0, column=5, sticky=tk.W)

        ttk.Label(top, text="Username").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        self.username_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.username_var, width=24).grid(row=1, column=1, sticky=tk.W, padx=6, pady=(8, 0))

        ttk.Label(top, text="Password").grid(row=1, column=2, sticky=tk.W, pady=(8, 0))
        self.password_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.password_var, width=24, show="*").grid(row=1, column=3, sticky=tk.W, padx=6, pady=(8, 0))

        ttk.Label(top, text="Peer JID").grid(row=1, column=4, sticky=tk.W, pady=(8, 0))
        self.peer_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.peer_var, width=35).grid(row=1, column=5, sticky=tk.W, pady=(8, 0))

        buttons = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        buttons.pack(fill=tk.X)

        ttk.Button(buttons, text="Start Tor", command=lambda: threading.Thread(target=self._start_tor, daemon=True).start()).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Register", command=lambda: threading.Thread(target=self._register_user, daemon=True).start()).pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="Connect", command=self._connect_session).pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="Disconnect", command=self._disconnect_session).pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="Send File", command=self._send_file).pack(side=tk.LEFT, padx=6)

        msg_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        msg_frame.pack(fill=tk.X)

        self.message_var = tk.StringVar()
        ttk.Entry(msg_frame, textvariable=self.message_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(msg_frame, text="Send Message", command=self._send_message).pack(side=tk.LEFT, padx=(8, 0))

        log_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log = tk.Text(log_frame, height=28, state=tk.DISABLED)
        self.log.pack(fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar(value="Idle")
        status = ttk.Label(self.root, textvariable=self.status_var, anchor=tk.W, padding=8)
        status.pack(fill=tk.X)

    def _set_status(self, msg: str) -> None:
        self.root.after(0, lambda: self.status_var.set(msg))

    def _log(self, msg: str) -> None:
        def append() -> None:
            self.log.configure(state=tk.NORMAL)
            self.log.insert(tk.END, msg + "\n")
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)

        self.root.after(0, append)

    def _start_tor(self) -> None:
        if self.tor_runtime is not None:
            self._log(f"Tor already running on {self.tor_runtime.socks_host}:{self.tor_runtime.socks_port}")
            self._set_status("Tor running")
            return
        if self.tor_starting:
            self._log("Tor startup already in progress")
            self._set_status("Tor starting...")
            return

        self.tor_starting = True
        try:
            runtime = self.tor.start(log=self._log)
            self.tor_runtime = runtime
            self._set_status(f"Tor running on {runtime.socks_host}:{runtime.socks_port}")
        except Exception as exc:
            self._set_status("Tor startup failed")
            self._log(f"Tor startup failed: {exc}")
        finally:
            self.tor_starting = False

    def _run_register_command(self) -> tuple[int, str]:
        if self.tor_runtime is None:
            raise RuntimeError("Tor is not running")
        host = self.host_var.get().strip()
        port = int(self.port_var.get().strip())
        domain = self.domain_var.get().strip()
        username = self.username_var.get().strip()
        password = self.password_var.get()

        cmd = [
            "torsocks",
            "python3",
            "-m",
            "app.xmpp_worker",
            "register",
            "--host",
            host,
            "--port",
            str(port),
            "--domain",
            domain,
            "--username",
            username,
            "--password",
            password,
        ]
        result = subprocess.run(
            cmd,
            cwd=str(self.project_root),
            env=self.tor.worker_env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        return result.returncode, output.strip()

    def _register_user(self) -> None:
        try:
            if self.tor_runtime is None:
                if self.tor_starting:
                    self._set_status("Tor starting...")
                    self._log("Registration postponed: Tor sidecar is still starting")
                    return
                threading.Thread(target=self._start_tor, daemon=True).start()
                self._set_status("Tor starting...")
                self._log("Registration postponed: started Tor sidecar, retry Register shortly")
                return
            self._set_status("Registering user...")
            rc, output = self._run_register_command()
            if output:
                for line in output.splitlines():
                    self._log(line)
            if rc == 0:
                self._set_status("Registration successful")
                self._log("Registration succeeded")
            else:
                self._set_status("Registration failed")
                self._log("Registration failed")
        except Exception as exc:
            self._set_status("Registration failed")
            self._log(f"Registration error: {exc}")

    def _connect_session(self) -> None:
        if self.tor_runtime is None:
            if self.tor_starting:
                messagebox.showinfo("Tor starting", "Tor sidecar is still starting. Please retry in a few seconds.")
                return
            threading.Thread(target=self._start_tor, daemon=True).start()
            messagebox.showinfo("Starting Tor", "Tor sidecar was not running. Startup has been triggered; retry Connect shortly.")
            return

        host = self.host_var.get().strip()
        port = int(self.port_var.get().strip())
        domain = self.domain_var.get().strip()
        username = self.username_var.get().strip()
        password = self.password_var.get()

        if not all([host, port, domain, username, password]):
            messagebox.showerror("Missing fields", "Host/domain/username/password are required")
            return

        try:
            self.session.start(
                workdir=self.project_root,
                env=self.tor.worker_env(),
                host=host,
                port=port,
                domain=domain,
                username=username,
                password=password,
            )
            self._set_status("Connecting XMPP session...")
        except Exception as exc:
            self._set_status("Connect failed")
            self._log(f"Session start failed: {exc}")

    def _disconnect_session(self) -> None:
        self.session.stop()
        self._set_status("Disconnected")
        self._log("Session disconnected")

    def _send_message(self) -> None:
        text = self.message_var.get().strip()
        peer = self.peer_var.get().strip()
        if not text or not peer:
            messagebox.showerror("Missing fields", "Peer JID and message are required")
            return
        try:
            self.session.send({"action": "send_message", "to": peer, "body": text})
            self.message_var.set("")
        except Exception as exc:
            self._log(f"Send failed: {exc}")

    def _send_file(self) -> None:
        peer = self.peer_var.get().strip()
        if not peer:
            messagebox.showerror("Missing peer", "Peer JID is required")
            return
        path = filedialog.askopenfilename(title="Choose file")
        if not path:
            return
        try:
            self.session.send({"action": "send_file", "to": peer, "path": path})
        except Exception as exc:
            self._log(f"File send failed: {exc}")

    def _on_worker_event(self, payload: dict[str, Any]) -> None:
        typ = payload.get("type", "status")
        if typ == "connected":
            self._set_status("Connected")
            self._log(f"Connected as {payload.get('jid', '')}")
            return
        if typ == "disconnected":
            self._set_status("Disconnected")
            self._log("Worker disconnected")
            return
        if typ == "message":
            self._log(f"< {payload.get('from_jid')}: {payload.get('body')}")
            return
        if typ == "sent":
            self._log(f"> to {payload.get('to_jid')}: {payload.get('body')}")
            return
        if typ == "file_sent":
            self._log(f"> file to {payload.get('to_jid')}: {payload.get('url')}")
            return
        if typ in {"error", "auth_error", "register_error"}:
            self._set_status("Error")
            self._log(f"ERROR: {payload.get('message', payload)}")
            return
        if typ == "register_success":
            self._set_status("Registration successful")
            self._log(f"Registered: {payload.get('jid')}")
            return
        self._log(payload.get("message", str(payload)))

    def _on_close(self) -> None:
        self.session.stop()
        self.tor.stop(log=self._log)
        self.root.destroy()
