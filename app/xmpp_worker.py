from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any
import xml.etree.ElementTree as ET

import requests
from requests.exceptions import RequestException
from urllib3.exceptions import InsecureRequestWarning
from urllib3 import disable_warnings
from slixmpp import ClientXMPP
from slixmpp.exceptions import IqError, IqTimeout


def emit(event_type: str, **payload: Any) -> None:
    line = {"type": event_type, **payload}
    print(json.dumps(line), flush=True)


UPLOAD_NS = "urn:xmpp:http:upload:0"


def insecure_ssl_context() -> ssl.SSLContext:
    # For onion self-signed cert deployments. Replace with cert pinning in production.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class RegisterClient(ClientXMPP):
    def __init__(self, jid: str, password: str, insecure_tls: bool = True) -> None:
        super().__init__(jid, password)
        self.success = False
        self.error_message = ""
        if insecure_tls:
            self.ssl_context = insecure_ssl_context()

        self.register_plugin("xep_0030")
        self.register_plugin("xep_0004")
        self.register_plugin("xep_0066")
        self.register_plugin("xep_0077")
        self["xep_0077"].force_registration = True

        self.add_event_handler("register", self._on_register)
        self.add_event_handler("failed_auth", self._on_failed_auth)

    async def _on_register(self, _event: Any) -> None:
        iq = self.Iq()
        iq["type"] = "set"
        iq["register"]["username"] = self.boundjid.user
        iq["register"]["password"] = self.password

        try:
            await iq.send()
            self.success = True
            emit("register_success", jid=self.boundjid.bare)
            self.disconnect(wait=0.1)
        except IqError as exc:
            self.error_message = str(exc)
            emit("register_error", message=f"IQ error during registration: {exc}")
            self.disconnect(wait=0.1)
        except IqTimeout:
            self.error_message = "Timeout waiting for registration response"
            emit("register_error", message=self.error_message)
            self.disconnect(wait=0.1)

    def _on_failed_auth(self, _event: Any) -> None:
        self.error_message = "Authentication failed while attempting registration"
        emit("register_error", message=self.error_message)
        self.disconnect(wait=0.1)


def run_register(args: argparse.Namespace) -> int:
    jid = f"{args.username}@{args.domain}"
    xmpp = RegisterClient(jid, args.password, insecure_tls=args.insecure_tls)
    try:
        connect_task = xmpp.connect(args.host, args.port)
        xmpp.loop.run_until_complete(connect_task)
        xmpp.loop.call_later(args.timeout, lambda: xmpp.disconnect(wait=0.1))
        xmpp.loop.run_until_complete(xmpp.disconnected)
    except Exception as exc:
        emit("register_error", message=f"Connection failed: {exc}")
        return 1
    return 0 if xmpp.success else 1


class SessionClient(ClientXMPP):
    def __init__(self, jid: str, password: str, domain: str, insecure_tls: bool = True) -> None:
        super().__init__(jid, password)
        self.domain = domain
        self.command_queue: Queue[dict[str, Any]] = Queue()
        self.stop_requested = False
        self.verify_https = not insecure_tls

        if insecure_tls:
            self.ssl_context = insecure_ssl_context()
            disable_warnings(InsecureRequestWarning)

        self.register_plugin("xep_0030")
        self.register_plugin("xep_0199")

        self.add_event_handler("session_start", self._on_session_start)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("failed_auth", self._on_failed_auth)
        self.add_event_handler("disconnected", self._on_disconnected)

    async def _on_session_start(self, _event: Any) -> None:
        self.send_presence()
        emit("connected", jid=self.boundjid.bare)
        asyncio.create_task(self._command_loop())

    def _on_message(self, msg: Any) -> None:
        if msg["type"] not in ("chat", "normal"):
            return
        body = str(msg["body"])
        emit("message", from_jid=str(msg["from"]), body=body)

    def _on_failed_auth(self, _event: Any) -> None:
        emit("auth_error", message="Authentication failed")
        self.disconnect(wait=0.1)

    def _on_disconnected(self, _event: Any) -> None:
        emit("disconnected")

    async def _command_loop(self) -> None:
        while not self.stop_requested:
            command = await asyncio.to_thread(self.command_queue.get)
            action = command.get("action")

            if action == "send_message":
                to_jid = str(command.get("to", "")).strip()
                body = str(command.get("body", ""))
                if not to_jid or not body:
                    emit("error", message="Missing peer JID or message text")
                    continue
                self.send_message(mto=to_jid, mbody=body, mtype="chat")
                emit("sent", to_jid=to_jid, body=body)
                continue

            if action == "send_file":
                to_jid = str(command.get("to", "")).strip()
                path = Path(str(command.get("path", "")))
                if not to_jid or not path.exists() or not path.is_file():
                    emit("error", message="Invalid file or peer JID")
                    continue
                await self._send_file(to_jid, path)
                continue

            if action == "shutdown":
                self.stop_requested = True
                self.disconnect(wait=0.1)
                return

            emit("error", message=f"Unknown action: {action}")

    async def _request_upload_slot(self, path: Path) -> tuple[str, str, dict[str, str]]:
        iq = self.Iq()
        iq["type"] = "get"
        iq["to"] = f"upload.{self.domain}"

        request = ET.Element(f"{{{UPLOAD_NS}}}request")
        request.set("filename", path.name)
        request.set("size", str(path.stat().st_size))
        request.set("content-type", "application/octet-stream")
        iq.append(request)

        result = await iq.send(timeout=45)
        slot = result.xml.find(f"{{{UPLOAD_NS}}}slot")
        if slot is None:
            raise ValueError("Upload service returned no <slot/> element")

        put = slot.find(f"{{{UPLOAD_NS}}}put")
        get = slot.find(f"{{{UPLOAD_NS}}}get")
        if put is None or get is None:
            raise ValueError("Upload service response missing PUT/GET URLs")

        put_url = str(put.get("url") or "")
        get_url = str(get.get("url") or "")
        if not put_url or not get_url:
            raise ValueError("Upload service response contains empty URLs")

        extra_headers: dict[str, str] = {}
        for header in put.findall(f"{{{UPLOAD_NS}}}header"):
            name = header.get("name")
            if not name:
                continue
            extra_headers[name] = header.text or ""

        return put_url, get_url, extra_headers

    async def _send_file(self, to_jid: str, path: Path) -> None:
        emit("status", message=f"Uploading file: {path.name}")
        try:
            put_url, get_url, extra_headers = await self._request_upload_slot(path)
            headers = {
                "Content-Length": str(path.stat().st_size),
                "Content-Type": "application/octet-stream",
                **extra_headers,
            }

            with path.open("rb") as file_handle:
                response = requests.put(
                    put_url,
                    data=file_handle,
                    headers=headers,
                    verify=self.verify_https,
                    timeout=90,
                )
            response.raise_for_status()

            self.send_message(mto=to_jid, mbody=get_url, mtype="chat")
            emit("file_sent", to_jid=to_jid, url=get_url, name=path.name)
        except (IqError, IqTimeout) as exc:
            emit("error", message=f"HTTP Upload slot request failed: {exc}")
        except RequestException as exc:
            emit("error", message=f"HTTP upload failed: {exc}")
        except Exception as exc:
            emit("error", message=f"File send failed: {exc}")


def _stdin_reader(client: SessionClient) -> None:
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        try:
            cmd = json.loads(raw)
            if not isinstance(cmd, dict):
                raise ValueError("Command must be a JSON object")
        except Exception as exc:
            emit("error", message=f"Invalid command payload: {exc}")
            continue
        client.command_queue.put(cmd)


def run_session(args: argparse.Namespace) -> int:
    jid = f"{args.username}@{args.domain}"
    xmpp = SessionClient(jid, args.password, args.domain, insecure_tls=args.insecure_tls)
    reader_thread = Thread(target=_stdin_reader, args=(xmpp,), daemon=True)
    reader_thread.start()

    try:
        connect_task = xmpp.connect(args.host, args.port)
        xmpp.loop.run_until_complete(connect_task)
        xmpp.loop.run_until_complete(xmpp.disconnected)
    except Exception as exc:
        emit("error", message=f"Session connect failed: {exc}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="XMPP worker process")
    sub = parser.add_subparsers(dest="mode", required=True)

    register = sub.add_parser("register")
    register.add_argument("--host", required=True)
    register.add_argument("--port", required=True, type=int)
    register.add_argument("--domain", required=True)
    register.add_argument("--username", required=True)
    register.add_argument("--password", required=True)
    register.add_argument("--timeout", type=int, default=25)
    register.add_argument("--insecure-tls", dest="insecure_tls", action="store_true", default=True)
    register.add_argument("--strict-tls", dest="insecure_tls", action="store_false")

    session = sub.add_parser("session")
    session.add_argument("--host", required=True)
    session.add_argument("--port", required=True, type=int)
    session.add_argument("--domain", required=True)
    session.add_argument("--username", required=True)
    session.add_argument("--password", required=True)
    session.add_argument("--insecure-tls", dest="insecure_tls", action="store_true", default=True)
    session.add_argument("--strict-tls", dest="insecure_tls", action="store_false")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.mode == "register":
        return run_register(args)
    if args.mode == "session":
        return run_session(args)
    emit("error", message="Unknown mode")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
