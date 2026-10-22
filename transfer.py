"""Streaming uploads/downloads, transfer registry, filename and path safety."""
from __future__ import annotations

import asyncio
import errno
import json
import mimetypes
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from urllib.parse import quote, unquote

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

try:
    from python_multipart.multipart import MultipartParser, parse_options_header
except ImportError:  # older python-multipart
    from multipart.multipart import MultipartParser, parse_options_header

from config import HISTORY_FILE, settings
from events import hub
from security import Session, auth

CHUNK = 1024 * 1024
FLUSH_AT = 1024 * 1024
DISK_RESERVE = 64 * 1024 * 1024
PARTIAL_DIR = ".localdrop-partial"

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


# --------------------------------------------------------------------------
# Name / path safety
# --------------------------------------------------------------------------
def sanitize_component(name: str, fallback: str = "file") -> str:
    name = re.sub(r"[\x00-\x1f\x7f<>:\"/\\|?*]", "_", name).strip().strip(".")
    name = name.rstrip(" .")
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:140] + dot + ext[:9]) if dot and len(ext) <= 9 else name[:150]
    return name or fallback


def sanitize_relative(rel: str) -> list[str]:
    """Folder parts for a folder upload (webkitRelativePath minus the file name)."""
    parts = [p for p in re.split(r"[\\/]+", rel or "") if p not in ("", ".", "..")]
    return [sanitize_component(p, "folder") for p in parts[:-1][:12]]


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(name)
    for i in range(1, 100000):
        candidate = directory / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    raise OSError("Could not find a free file name")


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def safe_join(root: Path, rel: str) -> Path:
    """Resolve `rel` inside `root`; refuse traversal, hidden entries and symlink escapes."""
    rel = (rel or "").replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    for p in parts:
        if p == ".." or "\x00" in p or (os.name == "nt" and ":" in p):
            raise HTTPException(400, "Invalid path.")
        if _is_hidden(p):
            raise HTTPException(404, "Not found.")
    root_r = root.resolve()
    resolved = root_r.joinpath(*parts).resolve()
    if resolved != root_r and root_r not in resolved.parents:
        raise HTTPException(403, "Access outside the shared folder is not allowed.")
    return resolved


def list_dir(root: Path, rel: str) -> dict:
    target = safe_join(root, rel)
    if not target.is_dir():
        raise HTTPException(404, "Folder not found.")
    root_r = root.resolve()
    entries = []
    with os.scandir(target) as it:
        for e in it:
            if _is_hidden(e.name):
                continue
            try:
                real = Path(e.path).resolve()
                if root_r not in real.parents:
                    continue                      # symlink pointing outside
                st = e.stat()
                is_dir = e.is_dir()
            except OSError:
                continue
            entries.append({
                "name": e.name,
                "dir": is_dir,
                "size": 0 if is_dir else st.st_size,
                "mtime": st.st_mtime,
                "type": "folder" if is_dir else (mimetypes.guess_type(e.name)[0] or "application/octet-stream"),
            })
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    rel_clean = "/".join(target.relative_to(root_r).parts)
    return {"path": rel_clean, "entries": entries}


# --------------------------------------------------------------------------
# Transfer registry
# --------------------------------------------------------------------------
@dataclass
class Transfer:
    id: str
    direction: str                  # upload | download | share
    name: str
    size: int
    device: str
    session: Optional[str]          # Session.public_id
    started: float = field(default_factory=time.time)
    status: str = "active"          # active | done | failed | cancelled
    bytes: int = 0
    speed: float = 0.0
    finished: Optional[float] = None
    error: Optional[str] = None
    folder: Optional[str] = None
    cancel: bool = False
    _t: float = 0.0
    _b: int = 0
    _pub: float = 0.0

    def public(self) -> dict:
        return {"id": self.id, "direction": self.direction, "name": self.name, "size": self.size,
                "device": self.device, "status": self.status, "bytes": self.bytes,
                "speed": self.speed, "started": self.started, "finished": self.finished,
                "error": self.error, "folder": self.folder}


class TransferManager:
    def __init__(self) -> None:
        self.items: dict[str, Transfer] = {}
        self.uploads_in_flight = 0
        self._load()

    def _load(self) -> None:
        try:
            for d in json.loads(HISTORY_FILE.read_text(encoding="utf-8")):
                t = Transfer(id=d["id"], direction=d["direction"], name=d["name"], size=d["size"],
                             device=d["device"], session=None, started=d["started"], status=d["status"],
                             bytes=d["bytes"], finished=d.get("finished"), error=d.get("error"),
                             folder=d.get("folder"))
                if t.status == "active":
                    t.status, t.error = "failed", "LocalDrop was closed during this transfer."
                self.items[t.id] = t
        except (OSError, ValueError, KeyError):
            pass

    def _save(self) -> None:
        done = sorted((t for t in self.items.values() if t.status != "active"), key=lambda t: t.started)[-300:]
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = HISTORY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps([t.public() for t in done]), encoding="utf-8")
            os.replace(tmp, HISTORY_FILE)
        except OSError:
            pass

    def _emit(self, t: Transfer) -> None:
        hub.publish({"type": "transfer", "transfer": t.public()}, session=t.session)

    def create(self, **kw) -> Transfer:
        t = Transfer(id=uuid.uuid4().hex[:12], **kw)
        t._t = time.monotonic()
        self.items[t.id] = t
        self._emit(t)
        return t

    def progress(self, t: Transfer, nbytes: int) -> None:
        t.bytes = nbytes
        now = time.monotonic()
        if now - t._t >= 0.5:
            inst = (nbytes - t._b) / (now - t._t)
            t.speed = inst if t.speed == 0 else 0.6 * t.speed + 0.4 * inst
            t._t, t._b = now, nbytes
        if now - t._pub >= 0.25:
            t._pub = now
            self._emit(t)

    def finish(self, t: Transfer, status: str, error: Optional[str] = None, folder: Optional[str] = None) -> None:
        t.status, t.error, t.finished, t.speed = status, error, time.time(), 0.0
        if folder:
            t.folder = folder
        self._emit(t)
        self._save()

    def cancel(self, tid: str) -> bool:
        t = self.items.get(tid)
        if t and t.status == "active":
            t.cancel = True
            return True
        return False

    def cancel_all(self, session: Optional[str] = None) -> None:
        for t in self.items.values():
            if t.status == "active" and (session is None or t.session == session):
                t.cancel = True

    def clear_finished(self) -> None:
        self.items = {k: v for k, v in self.items.items() if v.status == "active"}
        self._save()
        hub.publish({"type": "history_cleared"})

    def snapshot(self, session: Optional[str] = None) -> list[dict]:
        rows = [t for t in self.items.values() if session is None or t.session == session]
        rows.sort(key=lambda t: t.started, reverse=True)
        return [t.public() for t in rows[:200]]


manager = TransferManager()


def clean_partials(root: Path) -> None:
    shutil.rmtree(root / PARTIAL_DIR, ignore_errors=True)


# --------------------------------------------------------------------------
# Upload (streaming multipart straight to disk)
# --------------------------------------------------------------------------
class UploadError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _fail(status: int, message: str, **kw) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status, **kw)


async def receive_upload(request: Request, dest_root: Path, *, direction: str,
                         session: Optional[Session]) -> JSONResponse:
    device = session.device if session else "This PC"
    sess_id = session.public_id if session else None
    max_bytes = settings.max_bytes

    ctype = request.headers.get("content-type", "")
    main, opts = parse_options_header(ctype)
    if main.lower() != b"multipart/form-data" or b"boundary" not in opts:
        return _fail(400, "Expected a multipart/form-data upload.")

    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    try:
        declared = int(request.headers.get("x-file-size", "") or content_length)
    except ValueError:
        declared = content_length
    early_name = sanitize_component(unquote(request.headers.get("x-file-name", "")) or "file")

    def rejected(status: int, msg: str) -> JSONResponse:
        t = manager.create(direction=direction, name=early_name, size=declared, device=device, session=sess_id)
        manager.finish(t, "failed", msg)
        return _fail(status, msg)

    if not auth.sharing and session is not None:
        return _fail(503, "Sharing is stopped on the PC.")
    if manager.uploads_in_flight >= settings.max_concurrent:
        return _fail(429, "The PC is busy with other transfers; retrying shortly.", headers={"Retry-After": "2"})
    if max_bytes and max(declared, content_length) > max_bytes:
        return rejected(413, f"File is larger than the {settings.max_file_mb} MB limit set on the PC.")
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(dest_root).free
    except OSError as exc:
        return rejected(500, f"Destination folder is not available: {exc.strerror or exc}")
    if free < max(declared, content_length) + DISK_RESERVE:
        return rejected(507, "Not enough free disk space on the PC.")

    st = SimpleNamespace(field=b"", value=b"", headers={}, in_file=False, files=0, pending_open=False,
                         part_done=False, error=None, buf=[], buflen=0, f=None, part_path=None,
                         name="file", t=None, written=0)

    def on_part_begin():
        st.headers, st.in_file = {}, False

    def on_header_field(data, s, e):
        st.field += data[s:e]

    def on_header_value(data, s, e):
        st.value += data[s:e]

    def on_header_end():
        st.headers[st.field.lower()] = st.value
        st.field = st.value = b""

    def on_headers_finished():
        cd = st.headers.get(b"content-disposition", b"")
        _, o = parse_options_header(cd)
        fname = o.get(b"filename")
        if fname is None:
            return
        st.files += 1
        if st.files > 1:
            st.error = UploadError(400, "Send one file per request.")
            return
        st.name = sanitize_component(fname.decode("utf-8", "replace"))
        st.in_file = st.pending_open = True

    def on_part_data(data, s, e):
        if st.in_file:
            st.buf.append(data[s:e])
            st.buflen += e - s

    def on_part_end():
        if st.in_file:
            st.part_done, st.in_file = True, False

    parser = MultipartParser(opts[b"boundary"], {
        "on_part_begin": on_part_begin, "on_header_field": on_header_field,
        "on_header_value": on_header_value, "on_header_end": on_header_end,
        "on_headers_finished": on_headers_finished, "on_part_data": on_part_data,
        "on_part_end": on_part_end,
    })

    # Optional folder structure + original modification time from the phone.
    subdirs = sanitize_relative(unquote(request.headers.get("x-relative-path", "")))
    try:
        mtime_ms = int(request.headers.get("x-last-modified", "0"))
        mtime = mtime_ms / 1000 if 631152000 < mtime_ms / 1000 < time.time() + 86400 else None
    except ValueError:
        mtime = None

    async def flush():
        if not st.buf:
            return
        data = b"".join(st.buf)
        st.buf.clear()
        st.buflen = 0
        await run_in_threadpool(st.f.write, data)
        st.written += len(data)
        if max_bytes and st.written > max_bytes:
            raise UploadError(413, f"File is larger than the {settings.max_file_mb} MB limit set on the PC.")
        manager.progress(st.t, st.written)

    manager.uploads_in_flight += 1
    try:
        received = 0
        async for chunk in request.stream():
            if session is not None and not auth.sharing:
                raise UploadError(503, "Sharing was stopped on the PC.")
            if st.t and st.t.cancel:
                raise UploadError(499, "Cancelled on the PC.")
            received += len(chunk)
            if max_bytes and received > max_bytes + 1024 * 1024:
                raise UploadError(413, f"File is larger than the {settings.max_file_mb} MB limit set on the PC.")
            parser.write(chunk)
            if st.error:
                raise st.error
            if st.pending_open:
                st.pending_open = False
                part_dir = dest_root / PARTIAL_DIR
                part_dir.mkdir(exist_ok=True)
                st.part_path = part_dir / (uuid.uuid4().hex + ".part")
                st.f = open(st.part_path, "wb")
                st.t = manager.create(direction=direction, name=st.name, size=declared or content_length,
                                      device=device, session=sess_id)
            if st.f and (st.buflen >= FLUSH_AT or st.part_done):
                await flush()
        parser.finalize()
        if not st.f or not st.part_done:
            raise UploadError(400, "The upload was incomplete.")
        await flush()
        await run_in_threadpool(st.f.close)
        st.f = None

        # Final placement: no await between choosing the name and renaming.
        target_dir = dest_root.joinpath(*subdirs)
        target_dir.mkdir(parents=True, exist_ok=True)
        real = target_dir.resolve()
        if real != dest_root.resolve() and dest_root.resolve() not in real.parents:
            raise UploadError(400, "Invalid folder path.")
        final = unique_path(real, st.name)
        os.replace(st.part_path, final)
        st.part_path = None
        try:
            os.chmod(final, 0o644)            # never executable
            if mtime:
                os.utime(final, (mtime, mtime))
        except OSError:
            pass
        manager.finish(st.t, "done", folder=str(real))
        return JSONResponse({"ok": True, "id": st.t.id, "name": final.name, "size": st.written,
                             "folder": str(real), "renamed": final.name != st.name})
    except ClientDisconnect:
        err = UploadError(499, "Transfer cancelled.")
        status, msg = "cancelled", err.message
    except UploadError as exc:
        status, msg, err = ("cancelled" if exc.status == 499 else "failed"), exc.message, exc
    except OSError as exc:
        msg = "Not enough disk space on the PC." if exc.errno == errno.ENOSPC else f"Could not write the file: {exc.strerror or exc}"
        status, err = "failed", UploadError(507 if exc.errno == errno.ENOSPC else 500, msg)
    except Exception as exc:                    # malformed multipart etc.
        status, msg, err = "failed", "The upload could not be processed.", UploadError(400, "The upload could not be processed.")
    finally:
        manager.uploads_in_flight -= 1
        if st.f:
            try:
                st.f.close()
            except OSError:
                pass
        if st.part_path:
            try:
                os.remove(st.part_path)
            except OSError:
                pass
    if st.t and st.t.status == "active":
        manager.finish(st.t, status, msg)
    elif not st.t:
        t = manager.create(direction=direction, name=early_name, size=declared, device=device, session=sess_id)
        manager.finish(t, status, msg)
    return _fail(err.status if err.status != 499 else 400, msg)


# --------------------------------------------------------------------------
# Download (streaming, with HTTP Range so Android can resume)
# --------------------------------------------------------------------------
def _parse_range(header: str, size: int) -> Optional[tuple[int, int]]:
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    else:
        start, end = max(0, size - int(m.group(2))), size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        raise HTTPException(416, "Range not satisfiable.", headers={"Content-Range": f"bytes */{size}"})
    return start, end


async def stream_download(request: Request, rel: str, session: Session) -> StreamingResponse:
    target = safe_join(Path(settings.shared_dir), rel)
    if not target.is_file():
        raise HTTPException(404, "File not found.")
    size = target.stat().st_size
    rng = _parse_range(request.headers.get("range", ""), size)
    start, end = rng if rng else (0, size - 1)
    length = end - start + 1 if size else 0

    t = manager.create(direction="download", name=target.name, size=size, device=session.device,
                       session=session.public_id) if start == 0 else None

    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", target.name) or "file"
    headers = {
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(target.name)}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    status = 200
    if rng:
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    async def body():
        f = None
        sent = 0
        completed = False
        reason = "Connection lost before the download finished."
        try:
            f = await run_in_threadpool(open, target, "rb")
            f.seek(start)
            remaining = length
            while remaining > 0:
                if not auth.sharing:
                    reason = "Sharing was stopped on the PC."
                    return
                if t and t.cancel:
                    reason = "Cancelled on the PC."
                    return
                chunk = await run_in_threadpool(f.read, min(CHUNK, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                sent += len(chunk)
                yield chunk
                if t:
                    manager.progress(t, sent)
            completed = True
        finally:
            if f:
                f.close()
            if t and t.status == "active":
                manager.finish(t, "done" if completed else "cancelled", None if completed else reason)

    return StreamingResponse(body(), status_code=status, headers=headers, media_type="application/octet-stream")
