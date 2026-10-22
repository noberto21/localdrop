"""Pairing, sessions and request protection.

Design
------
* The PC dashboard is served only to loopback clients that reach 127.0.0.1 and it
  carries a per-launch admin token that every admin call must echo back. Other
  websites cannot read that token (same-origin policy) and DNS-rebinding is blocked
  by the Host-header check below.
* Phones pair with either the one-time-per-run token inside the QR code or a
  6-digit PIN. Pairing yields a random session cookie (HttpOnly, SameSite=Strict).
* Every state-changing request must carry a custom header (forces a CORS preflight
  that we never approve) and, when the browser sends an Origin, it must match Host.
* When sharing is stopped, the LAN listener is closed and anything still arriving
  is answered with 503.
"""
from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, WebSocket
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse

from network import is_ip_literal

COOKIE = "ld_session"
SESSION_TTL = 24 * 3600
LOCAL_HOSTS = {"127.0.0.1"}


@dataclass
class Session:
    id: str            # secret (cookie value) - never sent to any page
    public_id: str     # safe identifier used in events and the dashboard
    device: str
    ip: str
    created: float
    last_seen: float

    def public(self) -> dict:
        return {"id": self.public_id, "device": self.device, "ip": self.ip,
                "created": self.created, "last_seen": self.last_seen}


class AuthManager:
    def __init__(self) -> None:
        self.admin_token = secrets.token_urlsafe(32)
        self.sessions: dict[str, Session] = {}
        self.sharing = False
        self.lan_ip: Optional[str] = None
        self.port = 8765
        self._fail_ip: dict[str, tuple[int, float]] = {}
        self._fail_total = 0
        self.on_rotate: Optional[Callable[[], None]] = None
        self.new_pairing()

    # -- pairing -----------------------------------------------------------
    def new_pairing(self) -> None:
        self.token = secrets.token_urlsafe(24)
        self.pin = f"{secrets.randbelow(10**6):06d}"
        self._fail_total = 0
        self._fail_ip.clear()

    def pair(self, code: str, ip: str, user_agent: str) -> Optional[Session]:
        """Validate a QR token or PIN. Returns a new Session, or None if wrong.

        Raises HTTPException(429) when the client is locked out.
        """
        now = time.time()
        count, until = self._fail_ip.get(ip, (0, 0.0))
        if until > now:
            raise HTTPException(429, f"Too many wrong codes. Try again in {int(until - now) + 1} seconds.")
        code = (code or "").strip().replace(" ", "")
        ok_token = hmac.compare_digest(code.encode(), self.token.encode())
        ok_pin = hmac.compare_digest(code.encode(), self.pin.encode())
        if self.sharing and (ok_token or ok_pin):
            self._fail_ip.pop(ip, None)
            return self._create_session(ip, user_agent)
        count += 1
        lock = min(300.0, 10.0 * 2 ** (count - 3)) if count >= 3 else 0.0
        self._fail_ip[ip] = (count, now + lock if lock else 0.0)
        self._fail_total += 1
        if self._fail_total >= 12:        # distributed guessing: invalidate the PIN/QR
            self.new_pairing()
            if self.on_rotate:
                self.on_rotate()
        return None

    def _create_session(self, ip: str, ua: str) -> Session:
        now = time.time()
        s = Session(secrets.token_urlsafe(32), secrets.token_hex(4), device_name(ua, ip), ip, now, now)
        self.sessions[s.id] = s
        return s

    def session_for(self, cookie: Optional[str]) -> Optional[Session]:
        if not cookie or not self.sharing:
            return None
        s = self.sessions.get(cookie)
        if not s:
            return None
        now = time.time()
        if now - s.created > SESSION_TTL:
            self.sessions.pop(cookie, None)
            return None
        s.last_seen = now
        return s

    def revoke(self, public_id: str) -> Optional[Session]:
        for sid, s in list(self.sessions.items()):
            if s.public_id == public_id:
                return self.sessions.pop(sid)
        return None

    def reset(self) -> None:
        self.sessions.clear()


auth = AuthManager()


def device_name(ua: str, ip: str) -> str:
    u = (ua or "").lower()
    if "android" in u:
        kind = "Android phone" if "mobile" in u else "Android tablet"
    elif "iphone" in u:
        kind = "iPhone"
    elif "ipad" in u:
        kind = "iPad"
    elif "windows" in u:
        kind = "Windows PC"
    elif "mac os" in u or "macintosh" in u:
        kind = "Mac"
    elif "linux" in u:
        kind = "Linux device"
    else:
        kind = "Device"
    return f"{kind} ({ip})"


# -- request helpers / dependencies -----------------------------------------
def is_local(conn) -> bool:
    return bool(conn.scope.get("localdrop", {}).get("is_local"))


def require_admin(request: Request) -> None:
    if not is_local(request):
        raise HTTPException(403, "Only available on the PC running LocalDrop.")
    tok = request.headers.get("x-admin-token", "")
    if not hmac.compare_digest(tok.encode(), auth.admin_token.encode()):
        raise HTTPException(403, "Invalid admin token. Reload the dashboard.")


def require_device(request: Request) -> Session:
    s = auth.session_for(request.cookies.get(COOKIE))
    if not s:
        raise HTTPException(401, "Not paired. Scan the QR code or enter the PIN.")
    return s


def ws_admin_ok(ws: WebSocket) -> bool:
    tok = ws.query_params.get("token", "")
    return is_local(ws) and hmac.compare_digest(tok.encode(), auth.admin_token.encode())


# -- ASGI middleware ---------------------------------------------------------
_SAFE = {"GET", "HEAD", "OPTIONS"}
_CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'")


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        server_host = (scope.get("server") or ("", 0))[0]
        client_host = (scope.get("client") or ("", 0))[0]
        is_loc = server_host in LOCAL_HOSTS and client_host in ("127.0.0.1", "::1")
        scope["localdrop"] = {"is_local": is_loc}

        async def reject(status: int, message: str):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": message}, status_code=status)(scope, receive, send)

        # 1. Host header must be an IP literal or localhost (blocks DNS rebinding).
        host = headers.get("host", "")
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if not (is_ip_literal(hostname) or hostname.lower() == "localhost"):
            return await reject(421, "Open LocalDrop using the IP address shown on the PC.")

        # 2. LAN listener is only usable while sharing is on.
        if not is_loc and not auth.sharing:
            return await reject(503, "Sharing is stopped on the PC.")

        # 3. Cross-site request protection.
        origin = headers.get("origin")
        if origin and origin != "null":
            if urlsplit(origin).netloc != host:
                return await reject(403, "Cross-origin request blocked.")
        elif origin == "null":
            return await reject(403, "Cross-origin request blocked.")
        if scope["type"] == "http" and scope["method"] not in _SAFE:
            if headers.get("x-localdrop") != "1":
                return await reject(403, "Missing X-LocalDrop header.")

        if scope["type"] == "websocket":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h["X-Content-Type-Options"] = "nosniff"
                h["Referrer-Policy"] = "no-referrer"
                h["X-Frame-Options"] = "DENY"
                h["Content-Security-Policy"] = _CSP
                if not scope["path"].startswith("/static/"):
                    h["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_wrapper)
