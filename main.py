"""LocalDrop - send files between your phone and PC over the local network.

Run:  python main.py [--port 8765] [--no-browser] [--background]
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import segno
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import network
import startup
from config import APP_NAME, BASE_DIR, DATA_DIR, LOG_FILE, VERSION, SettingsError, settings
from events import hub
from security import (COOKIE, SESSION_TTL, SecurityMiddleware, auth, is_local, require_admin,
                      require_device, ws_admin_ok)
from transfer import (PARTIAL_DIR, clean_partials, list_dir, manager, receive_upload, safe_join,
                      stream_download)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --------------------------------------------------------------------------
# Sharing controller: the LAN listener exists only while sharing is on.
# --------------------------------------------------------------------------
class ShareController:
    def __init__(self) -> None:
        self.server: Optional[uvicorn.Server] = None
        self.task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()

    async def start(self, ip: str) -> None:
        async with self.lock:
            if self.server:
                await self._stop()
            if ip not in network.lan_addresses():
                raise HTTPException(400, "That address is not a private network address of this PC.")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if os.name != "nt":
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((ip, auth.port))
                sock.listen(100)
            except OSError as exc:
                sock.close()
                raise HTTPException(409, f"Could not listen on {ip}:{auth.port} ({exc.strerror or exc}). "
                                         "Is another program using that port?")
            cfg = uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=1,
                                 ws_max_size=1 << 20, lifespan="off")
            server = uvicorn.Server(cfg)
            server.install_signal_handlers = lambda: None     # type: ignore[assignment]
            auth.new_pairing()
            auth.reset()
            auth.lan_ip, auth.sharing = ip, True
            self.server = server
            self.task = asyncio.create_task(server.serve(sockets=[sock]))
            for _ in range(50):
                if server.started or self.task.done():
                    break
                await asyncio.sleep(0.1)
            if not server.started:
                await self._stop()
                raise HTTPException(500, "The network listener failed to start.")
        hub.publish({"type": "state"})

    async def stop(self) -> None:
        async with self.lock:
            await self._stop()
        hub.publish({"type": "state"})

    async def _stop(self) -> None:
        auth.sharing, auth.lan_ip = False, None        # middleware now answers 503 immediately
        manager.cancel_all()
        hub.close_devices()
        auth.reset()
        if self.server:
            self.server.should_exit = True
            self.server.force_exit = True
            try:
                await asyncio.wait_for(self.task, 5)
            except Exception:
                self.task.cancel()
        self.server = self.task = None


share = ShareController()


@asynccontextmanager
async def lifespan(_: FastAPI):
    for d in (settings.upload_dir, settings.shared_dir):
        clean_partials(Path(d))
    auth.on_rotate = lambda: hub.publish({"type": "state"}, admin_only=True)
    yield
    await share.stop()


app = FastAPI(title=APP_NAME, version=VERSION, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(SettingsError)
async def _settings_error(_: Request, exc: SettingsError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if is_local(request):
        return templates.TemplateResponse(request, "dashboard.html", {"admin_token": auth.admin_token})
    paired = auth.session_for(request.cookies.get(COOKIE)) is not None
    return templates.TemplateResponse(request, "device.html", {"paired": paired})


def _set_cookie(resp: Response, session) -> None:
    resp.set_cookie(COOKIE, session.id, httponly=True, samesite="strict", max_age=SESSION_TTL, path="/")


def _announce_session() -> None:
    hub.publish({"type": "sessions", "sessions": [s.public() for s in auth.sessions.values()]}, admin_only=True)


@app.get("/pair")
async def pair_link(request: Request, t: str = ""):
    """Target of the QR code: exchanges the pairing token for a session cookie."""
    if is_local(request):
        return RedirectResponse("/", 303)
    try:
        session = auth.pair(t, request.client.host, request.headers.get("user-agent", ""))
    except HTTPException:
        return RedirectResponse("/?pair=locked", 303)
    if not session:
        return RedirectResponse("/?pair=failed", 303)
    resp = RedirectResponse("/", 303)
    _set_cookie(resp, session)
    _announce_session()
    return resp


class PairReq(BaseModel):
    code: str


@app.post("/api/pair")
async def pair_pin(body: PairReq, request: Request):
    session = auth.pair(body.code, request.client.host, request.headers.get("user-agent", ""))
    if not session:
        raise HTTPException(401, "That code is not right. Check the PIN on the PC screen.")
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, session)
    _announce_session()
    return resp


# --------------------------------------------------------------------------
# Phone / device API (requires a paired session)
# --------------------------------------------------------------------------
@app.get("/api/me")
async def me(session=Depends(require_device)):
    return {"device": session.device, "max_file_mb": settings.max_file_mb,
            "max_concurrent": settings.max_concurrent, "version": VERSION}


@app.post("/api/logout")
async def logout(session=Depends(require_device)):
    auth.revoke(session.public_id)
    _announce_session()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/api/files")
async def files(path: str = "", session=Depends(require_device)):
    return await asyncio.to_thread(list_dir, Path(settings.shared_dir), path)


@app.get("/api/download")
async def download(request: Request, path: str, session=Depends(require_device)):
    return await stream_download(request, path, session)


@app.get("/api/transfers")
async def my_transfers(session=Depends(require_device)):
    return {"transfers": manager.snapshot(session.public_id)}


@app.post("/api/upload")
async def upload(request: Request, session=Depends(require_device)):
    return await receive_upload(request, Path(settings.upload_dir), direction="upload", session=session)


@app.websocket("/ws")
async def ws_device(ws: WebSocket):
    session = auth.session_for(ws.cookies.get(COOKIE))
    if not session:
        await ws.close(code=1008)
        return
    await hub.serve(ws, "device", session.public_id)


# --------------------------------------------------------------------------
# PC dashboard API (loopback only + admin token)
# --------------------------------------------------------------------------
admin = [Depends(require_admin)]


def _free_space(p: str) -> int:
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 0


def admin_state() -> dict:
    ips = network.lan_addresses()
    ip = auth.lan_ip or (settings.preferred_ip if settings.preferred_ip in ips else (ips[0] if ips else None))
    return {
        "app": APP_NAME, "version": VERSION, "platform": sys.platform,
        "sharing": auth.sharing, "ips": ips, "ip": ip, "port": auth.port,
        "url": network.connection_url(auth.lan_ip, auth.port) if auth.sharing else None,
        "pin": auth.pin if auth.sharing else None,
        "sessions": [s.public() for s in auth.sessions.values()],
        "transfers": manager.snapshot(),
        "settings": {"upload_dir": settings.upload_dir, "shared_dir": settings.shared_dir,
                     "max_file_mb": settings.max_file_mb, "max_concurrent": settings.max_concurrent,
                     "port": settings.port},
        "free_bytes": _free_space(settings.upload_dir),
        "startup": {"supported": startup.supported(), "enabled": startup.supported() and startup.is_enabled()},
    }


@app.get("/api/admin/state", dependencies=admin)
async def a_state():
    return await asyncio.to_thread(admin_state)


class ShareReq(BaseModel):
    enabled: bool
    ip: Optional[str] = None


@app.post("/api/admin/share", dependencies=admin)
async def a_share(body: ShareReq):
    if body.enabled:
        ips = network.lan_addresses()
        ip = body.ip or settings.preferred_ip or (ips[0] if ips else None)
        if not ip:
            raise HTTPException(409, "No local network address found. Connect this PC to Wi-Fi or Ethernet.")
        await share.start(ip)
        if body.ip and body.ip != settings.preferred_ip:
            settings.preferred_ip = body.ip
            settings.save()
    else:
        await share.stop()
    return admin_state()


@app.post("/api/admin/regenerate", dependencies=admin)
async def a_regen():
    auth.new_pairing()
    auth.reset()
    hub.close_devices()
    hub.publish({"type": "state"})
    return admin_state()


@app.get("/api/admin/qr.svg", dependencies=admin)
async def a_qr():
    if not auth.sharing:
        raise HTTPException(409, "Sharing is stopped.")
    url = f"{network.connection_url(auth.lan_ip, auth.port)}/pair?t={auth.token}"
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=8, border=2, xmldecl=False, svgns=True, nl=False,
                                    dark="#10222b", light="#ffffff")
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.post("/api/admin/settings", dependencies=admin)
async def a_settings(body: dict):
    await asyncio.to_thread(settings.update, body)
    settings.save()
    hub.publish({"type": "state"})
    return admin_state()


class IdReq(BaseModel):
    id: str


@app.post("/api/admin/cancel", dependencies=admin)
async def a_cancel(body: IdReq):
    return {"ok": manager.cancel(body.id)}


@app.post("/api/admin/revoke", dependencies=admin)
async def a_revoke(body: IdReq):
    s = auth.revoke(body.id)
    if s:
        manager.cancel_all(body.id)
        hub.close_devices(body.id)
    _announce_session()
    return {"ok": bool(s)}


@app.post("/api/admin/clear-history", dependencies=admin)
async def a_clear():
    manager.clear_finished()
    return {"ok": True}


class StartupReq(BaseModel):
    enabled: bool


@app.post("/api/admin/startup", dependencies=admin)
async def a_startup(body: StartupReq):
    try:
        await asyncio.to_thread(startup.set_enabled, body.enabled)
    except OSError as exc:
        raise HTTPException(400, str(exc))
    return admin_state()


class FolderReq(BaseModel):
    which: str


@app.post("/api/admin/open-folder", dependencies=admin)
async def a_open(body: FolderReq):
    path = settings.upload_dir if body.which == "uploads" else settings.shared_dir
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)                                    # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as exc:
        raise HTTPException(500, f"Could not open the folder: {exc.strerror or exc}")
    return {"ok": True}


@app.post("/api/admin/shared-upload", dependencies=admin)
async def a_shared_upload(request: Request):
    return await receive_upload(request, Path(settings.shared_dir), direction="share", session=None)


@app.get("/api/admin/shared", dependencies=admin)
async def a_shared():
    return await asyncio.to_thread(list_dir, Path(settings.shared_dir), "")


class PathReq(BaseModel):
    path: str


@app.post("/api/admin/shared/delete", dependencies=admin)
async def a_shared_delete(body: PathReq):
    target = safe_join(Path(settings.shared_dir), body.path)
    if target == Path(settings.shared_dir).resolve():
        raise HTTPException(400, "Cannot delete the shared folder itself.")
    try:
        if target.is_dir():
            target.rmdir()          # only empty folders
        else:
            target.unlink()
    except OSError as exc:
        raise HTTPException(400, f"Could not delete: {exc.strerror or exc}")
    return {"ok": True}


@app.websocket("/ws/admin")
async def ws_admin(ws: WebSocket):
    if not ws_admin_ok(ws):
        await ws.close(code=1008)
        return
    await hub.serve(ws, "admin")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def _setup_logging_for_background() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:      # pythonw.exe has no console
        f = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = f


async def _serve(port: int, open_browser: bool) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(50)
    except OSError as exc:
        print(f"\nCould not start LocalDrop on port {port}: {exc.strerror or exc}.\n"
              f"Another copy may already be running. Try:  python main.py --port {port + 1}\n")
        sys.exit(1)
    cfg = uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=2, ws_max_size=1 << 20)
    server = uvicorn.Server(cfg)
    url = f"http://127.0.0.1:{port}"
    print(f"\n  {APP_NAME} {VERSION} is running.\n  Dashboard: {url}\n  Press Ctrl+C to quit.\n")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    await server.serve(sockets=[sock])


def main() -> None:
    ap = argparse.ArgumentParser(description="LocalDrop - local network file transfer")
    ap.add_argument("--port", type=int, default=None, help=f"port (default {settings.port}, from config)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the dashboard automatically")
    ap.add_argument("--background", action="store_true", help="started by the system at login (implies --no-browser)")
    args = ap.parse_args()
    if args.background:
        _setup_logging_for_background()
    auth.port = args.port or settings.port
    try:
        asyncio.run(_serve(auth.port, not (args.no_browser or args.background)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
