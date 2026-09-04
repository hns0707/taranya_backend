"""
Weighing scale — single module: .env config, REST list, USB serial reader, WebSocket.

Adjust COM port / baud in .env (SCALE_SERIAL_*). TSC tag printing is in tag_print.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import parse_qs

import jwt
from asgiref.sync import async_to_sync
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.auth.admin_jwt import AdminJWTAuthentication
from master.permissions.permission_checker import admin_auth

logger = logging.getLogger(__name__)

try:
    import serial
    from serial import SerialException
except ImportError:  # pragma: no cover
    serial = None
    SerialException = Exception  # type: ignore

# ---------------------------------------------------------------------------
# Config (.env)
# ---------------------------------------------------------------------------


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _machine_from_mapping(raw: dict[str, Any]) -> dict[str, Any]:
    mid = str(raw.get("id") or "default").strip() or "default"
    return {
        "id": mid,
        "enabled": bool(raw.get("enabled", False)),
        "port": str(raw.get("port") or "").strip(),
        "baudrate": int(raw.get("baudrate") or 9600),
        "timeout": float(raw.get("timeout") or 1.0),
        "settle_seconds": float(raw.get("settle_seconds") or 0.5),
    }


def _default_machine() -> dict[str, Any]:
    return {
        "id": (os.getenv("SCALE_MACHINE_ID") or "default").strip() or "default",
        "enabled": _truthy(os.getenv("SCALE_SERIAL_ENABLED")),
        "port": (os.getenv("SCALE_SERIAL_PORT") or "").strip(),
        "baudrate": int(os.getenv("SCALE_SERIAL_BAUDRATE") or "9600"),
        "timeout": float(os.getenv("SCALE_SERIAL_TIMEOUT") or "1"),
        "settle_seconds": float(os.getenv("SCALE_SETTLE_SECONDS") or "0.5"),
    }


def get_scale_machines() -> list[dict[str, Any]]:
    raw_json = (os.getenv("SCALE_MACHINES_JSON") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list) and parsed:
                return [_machine_from_mapping(m) for m in parsed if isinstance(m, dict)]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return [_default_machine()]


def get_scale_machine(machine_id: str) -> dict[str, Any] | None:
    needle = (machine_id or "").strip()
    for m in get_scale_machines():
        if m["id"] == needle:
            return m
    return None


# ---------------------------------------------------------------------------
# USB serial → WebSocket broadcast
# ---------------------------------------------------------------------------

_WEIGHT_RE = re.compile(r"[-+]?\d+\.?\d*")
_hub_lock = threading.Lock()
_hubs: dict[str, "ScaleSerialHub"] = {}


def _parse_weight_line(raw: bytes | str) -> Optional[float]:
    text = (
        raw.decode("utf-8", errors="ignore").strip()
        if isinstance(raw, (bytes, bytearray))
        else str(raw or "").strip()
    )
    if not text:
        return None
    matches = _WEIGHT_RE.findall(text.replace(",", ""))
    if not matches:
        return None
    try:
        value = float(matches[-1])
    except ValueError:
        return None
    if value < 0 or value > 99999:
        return None
    return value


def _format_weight(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


class ScaleSerialHub:
    def __init__(self, machine_id: str):
        self.machine_id = machine_id
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._clients = 0
        self._lock = threading.Lock()
        self._last_emit: Optional[float] = None
        self._stable_since: Optional[float] = None
        self._pending: Optional[float] = None
        self._broadcast: Optional[Callable[[dict], None]] = None

    def set_broadcast(self, fn: Callable[[dict], None]) -> None:
        self._broadcast = fn

    def client_connected(self) -> None:
        with self._lock:
            self._clients += 1
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._run,
                    name=f"scale-serial-{self.machine_id}",
                    daemon=True,
                )
                self._thread.start()

    def client_disconnected(self) -> None:
        with self._lock:
            self._clients = max(0, self._clients - 1)
            if self._clients == 0:
                self._stop.set()

    def _emit_status(self, cfg: dict, *, serial_open: bool) -> None:
        if self._broadcast:
            self._broadcast(
                {
                    "type": "status",
                    "machine_id": self.machine_id,
                    "serial_enabled": bool(cfg.get("enabled")) and serial_open,
                    "port": cfg.get("port") or "",
                }
            )

    def _emit_weight(self, value: float) -> None:
        if self._broadcast:
            self._broadcast(
                {
                    "type": "weight",
                    "machine_id": self.machine_id,
                    "value": value,
                    "formatted": _format_weight(value),
                }
            )

    def _run(self) -> None:
        cfg = get_scale_machine(self.machine_id) or {}
        if not cfg.get("enabled"):
            self._emit_status(cfg, serial_open=False)
            return
        port = (cfg.get("port") or "").strip()
        if not port:
            logger.warning("Scale %s: SCALE_SERIAL_PORT is empty", self.machine_id)
            self._emit_status(cfg, serial_open=False)
            return
        if serial is None:
            logger.error("Scale %s: pip install pyserial", self.machine_id)
            self._emit_status(cfg, serial_open=False)
            return

        settle = float(cfg.get("settle_seconds") or 0.5)
        ser = None
        try:
            ser = serial.Serial(
                port=port,
                baudrate=int(cfg.get("baudrate") or 9600),
                timeout=float(cfg.get("timeout") or 1.0),
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
            self._emit_status(cfg, serial_open=True)
        except SerialException as exc:
            logger.error("Scale %s: cannot open %s — %s", self.machine_id, port, exc)
            self._emit_status(cfg, serial_open=False)
            return

        try:
            while not self._stop.is_set():
                try:
                    line = ser.readline()
                except SerialException as exc:
                    logger.warning("Scale %s: read error — %s", self.machine_id, exc)
                    time.sleep(0.5)
                    continue
                value = _parse_weight_line(line)
                if value is None:
                    continue
                now = time.monotonic()
                if self._pending is None or abs(self._pending - value) > 0.0005:
                    self._pending = value
                    self._stable_since = now
                    continue
                if self._stable_since is not None and (now - self._stable_since) >= settle:
                    if self._last_emit is None or abs(self._last_emit - value) > 0.0005:
                        self._emit_weight(value)
                        self._last_emit = value
                    self._stable_since = now
        finally:
            try:
                if ser and ser.is_open:
                    ser.close()
            except SerialException:
                pass


def _wire_channel_broadcast(hub: ScaleSerialHub) -> None:
    if hub._broadcast is not None:
        return
    layer = get_channel_layer()
    group = f"scale.{hub.machine_id}"

    def broadcast(payload: dict) -> None:
        async_to_sync(layer.group_send)(
            group,
            {"type": "scale.message", "payload": payload},
        )

    hub.set_broadcast(broadcast)


def get_scale_hub(machine_id: str) -> ScaleSerialHub:
    mid = (machine_id or "default").strip() or "default"
    with _hub_lock:
        hub = _hubs.get(mid)
        if hub is None:
            hub = ScaleSerialHub(mid)
            _hubs[mid] = hub
        _wire_channel_broadcast(hub)
        return hub


# ---------------------------------------------------------------------------
# WebSocket consumer
# ---------------------------------------------------------------------------


def _token_from_scope(scope) -> str | None:
    qs = parse_qs((scope.get("query_string") or b"").decode("utf-8"))
    parts = qs.get("token") or []
    if parts and parts[0].strip():
        return parts[0].strip()
    for header_name, header_value in scope.get("headers") or []:
        if header_name.lower() == b"authorization":
            text = header_value.decode("utf-8", errors="ignore").strip()
            if text.lower().startswith("bearer "):
                return text[7:].strip()
    return None


class ScaleConsumer(AsyncWebsocketConsumer):
    """ws://host/ws/scale/<machine_id>/?token=<admin_jwt>"""

    async def connect(self):
        self.machine_id = (self.scope["url_route"]["kwargs"].get("machine_id") or "default").strip()
        self.group_name = f"scale.{self.machine_id}"

        token = _token_from_scope(self.scope)
        if not token:
            await self.close(code=4401)
            return
        try:
            AdminJWTAuthentication.validate_admin_token(token)
        except (jwt.InvalidTokenError, jwt.ExpiredSignatureError, Exception):
            await self.close(code=4401)
            return

        cfg = get_scale_machine(self.machine_id)
        if cfg is None:
            await self.close(code=4404)
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        get_scale_hub(self.machine_id).client_connected()
        await self.send(
            text_data=json.dumps(
                {
                    "type": "status",
                    "machine_id": self.machine_id,
                    "serial_enabled": bool(cfg.get("enabled") and (cfg.get("port") or "").strip()),
                    "port": cfg.get("port") or "",
                }
            )
        )

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        if hasattr(self, "machine_id"):
            get_scale_hub(self.machine_id).client_disconnected()

    async def scale_message(self, event):
        await self.send(text_data=json.dumps(event["payload"]))

    async def receive(self, text_data=None, bytes_data=None):
        pass


# ---------------------------------------------------------------------------
# REST (barcode screen)
# ---------------------------------------------------------------------------


@api_view(["GET"])
@admin_auth()
def scale_machines_list(request):
    """GET /master/scale/machines/"""
    return Response({"machines": get_scale_machines()})
