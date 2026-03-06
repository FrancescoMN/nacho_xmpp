from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import socket
import subprocess
import threading
import time
from typing import Callable, Optional

from stem.process import launch_tor_with_config


LogFn = Callable[[str], None]


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class TorRuntime:
    socks_host: str
    socks_port: int
    control_port: int
    torsocks_conf: Path


class TorManager:
    """Starts a private Tor process and writes a dedicated torsocks config."""

    def __init__(self, base_dir: Path, tor_cmd: str = "tor") -> None:
        self.base_dir = base_dir
        self.tor_cmd = tor_cmd
        self._process: Optional[subprocess.Popen[str]] = None
        self.runtime: Optional[TorRuntime] = None

    def start(self, log: Optional[LogFn] = None, timeout: Optional[int] = 120) -> TorRuntime:
        if self.runtime is not None:
            return self.runtime

        def emit(msg: str) -> None:
            if log:
                log(msg)

        self.base_dir.mkdir(parents=True, exist_ok=True)
        run_id = f"{os.getpid()}-{int(time.time() * 1000)}"
        run_dir = self.base_dir / "runs" / run_id
        data_dir = run_dir / "tor-data"
        data_dir.mkdir(parents=True, exist_ok=True)

        socks_port = _pick_free_port()
        control_port = _pick_free_port()

        emit(f"Starting Tor sidecar on 127.0.0.1:{socks_port}")

        def init_msg_handler(line: str) -> None:
            emit(f"[tor] {line.rstrip()}")

        effective_timeout = timeout
        if timeout is not None and threading.current_thread() is not threading.main_thread():
            emit("Tor launch called from background thread; disabling timeout for compatibility")
            effective_timeout = None

        proc = launch_tor_with_config(
            tor_cmd=self.tor_cmd,
            config={
                "DataDirectory": str(data_dir),
                "SocksPort": f"127.0.0.1:{socks_port}",
                "ControlPort": f"127.0.0.1:{control_port}",
                "CookieAuthentication": "1",
                "ClientOnly": "1",
                "AvoidDiskWrites": "1",
                "Log": "NOTICE stdout",
            },
            completion_percent=0,
            init_msg_handler=init_msg_handler,
            timeout=effective_timeout,
            take_ownership=True,
        )

        torsocks_conf = run_dir / "torsocks.conf"
        torsocks_conf.write_text(
            "\n".join(
                [
                    f"TorAddress 127.0.0.1",
                    f"TorPort {socks_port}",
                    "OnionAddrRange 127.42.42.0/24",
                    "AllowOutboundLocalhost 1",
                    "IsolatePID 1",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        self._process = proc
        self.runtime = TorRuntime(
            socks_host="127.0.0.1",
            socks_port=socks_port,
            control_port=control_port,
            torsocks_conf=torsocks_conf,
        )
        emit("Tor sidecar bootstrapped successfully")
        return self.runtime

    def stop(self, log: Optional[LogFn] = None) -> None:
        if self._process is None:
            return
        proc = self._process
        self._process = None
        self.runtime = None

        if proc.poll() is not None:
            return

        if log:
            log("Stopping Tor sidecar")
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def worker_env(self) -> dict[str, str]:
        if self.runtime is None:
            raise RuntimeError("Tor is not running")
        env = dict(os.environ)
        env["TORSOCKS_CONF_FILE"] = str(self.runtime.torsocks_conf)
        return env
