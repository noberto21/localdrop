"""End-to-end checks of the security and transfer rules.  Run:  python selftest.py

Starts a throw-away LocalDrop (own temp folders, port 18765) and drives it over HTTP.
Needs `httpx` (pip install httpx).  It never touches your real uploads/ or shared/ folders.
"""
import os, subprocess, sys, tempfile, time
from pathlib import Path

import httpx

HOME = Path(tempfile.mkdtemp(prefix="localdrop-test-"))
PORT = 18765
env = dict(os.environ, LOCALDROP_HOME=str(HOME), LOCALDROP_TEST_LOOPBACK="1", PYTHONUNBUFFERED="1")
proc = subprocess.Popen([sys.executable, str(Path(__file__).parent / "main.py"), "--port", str(PORT), "--no-browser"],
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
results = []


def check(name, cond):
    results.append(cond)
    print(("PASS  " if cond else "FAIL  ") + name)


try:
    local = f"http://127.0.0.1:{PORT}"
    for _ in range(50):
        try:
            httpx.get(local, timeout=1); break
        except Exception:
            time.sleep(0.2)
    page = httpx.get(local).text
    token = page.split('name="ld-admin-token" content="')[1].split('"')[0]
    A = {"X-LocalDrop": "1", "X-Admin-Token": token}
    lan = "127.0.0.2"                       # stands in for the LAN address (non-loopback listener)
    r = httpx.post(f"{local}/api/admin/share", json={"enabled": True, "ip": lan}, headers=A)
    check("start sharing", r.status_code == 200 and r.json()["sharing"])
    st = r.json(); pin = st["pin"]
    base = f"http://{lan}:{PORT}"
    H = {"X-LocalDrop": "1"}

    check("admin API refuses requests without token", httpx.get(f"{local}/api/admin/state").status_code == 403)
    check("admin API unreachable from the LAN listener", httpx.get(f"{base}/api/admin/state", headers=A).status_code == 403)
    check("unpaired upload rejected", httpx.post(f"{base}/api/upload", headers=H, files={"file": ("a.txt", b"x")}).status_code == 401)
    check("unpaired file list rejected", httpx.get(f"{base}/api/files").status_code == 401)
    check("unpaired download rejected", httpx.get(f"{base}/api/download?path=x").status_code == 401)
    check("DNS-rebinding Host header rejected", httpx.get(base, headers={"Host": "evil.example.com"}).status_code == 421)
    check("cross-origin POST rejected", httpx.post(f"{base}/api/pair", json={"code": pin}, headers={**H, "Origin": "http://evil.example.com"}).status_code == 403)
    check("POST without X-LocalDrop rejected", httpx.post(f"{base}/api/pair", json={"code": pin}).status_code == 403)

    c = httpx.Client(base_url=base, headers=H)
    check("wrong PIN rejected", c.post("/api/pair", json={"code": "000000" if pin != "000000" else "111111"}).status_code == 401)
    check("correct PIN pairs", c.post("/api/pair", json={"code": pin}).status_code == 200)
    check("session works", c.get("/api/me").status_code == 200)

    data = os.urandom(5 * 1024 * 1024 + 123)
    r = c.post("/api/upload", files={"file": ("café photo.jpg", data)}, headers={"X-File-Size": str(len(data))})
    up = Path(st["settings"]["upload_dir"])
    check("upload succeeds", r.status_code == 200 and r.json()["ok"])
    check("uploaded file is byte-identical (incl. unicode name)", (up / "café photo.jpg").read_bytes() == data)
    r = c.post("/api/upload", files={"file": ("café photo.jpg", b"second")})
    check("duplicate gets a new name, original kept", r.json()["name"] == "café photo (1).jpg" and (up / "café photo.jpg").exists())
    r = c.post("/api/upload", files={"file": ("../../evil.txt", b"x")})
    check("traversal in filename neutralised", r.status_code == 200 and not (up.parent / "evil.txt").exists() and (up / ".._.._evil.txt").exists() or (up / "evil.txt").exists() or any("evil" in p.name for p in up.iterdir()))
    r = c.post("/api/upload", files={"file": ("CON.txt", b"x")})
    check("reserved Windows name neutralised", r.status_code == 200 and r.json()["name"].startswith("_"))
    r = c.post("/api/upload", files={"file": ("sub.bin", b"hello")}, headers={"X-Relative-Path": "Trip/Day1/sub.bin"})
    check("folder upload keeps structure", (up / "Trip" / "Day1" / "sub.bin").read_bytes() == b"hello")
    check("no partial files left behind", not list((up / ".localdrop-partial").glob("*")))

    shared = Path(st["settings"]["shared_dir"]); (shared / "docs").mkdir(exist_ok=True)
    (shared / "docs" / "note.txt").write_text("hi"); (shared / "big.bin").write_bytes(data)
    secret = HOME / "secret.txt"; secret.write_text("TOP SECRET")
    lst = c.get("/api/files").json()
    check("list shared folder", {e["name"] for e in lst["entries"]} >= {"docs", "big.bin"})
    check("download matches", c.get("/api/download", params={"path": "big.bin"}).content == data)
    r = c.get("/api/download", params={"path": "big.bin"}, headers={"Range": "bytes=10-19"})
    check("range request", r.status_code == 206 and r.content == data[10:20])
    for bad in ["../secret.txt", "..%2Fsecret.txt", "docs/../../secret.txt", "/etc/passwd", "..\\secret.txt", ".localdrop-partial"]:
        r = c.get("/api/download?path=" + bad if "%" in bad else "/api/download", params=None if "%" in bad else {"path": bad})
        check(f"traversal blocked: {bad}", r.status_code in (400, 403, 404) and b"TOP SECRET" not in r.content)
    check("traversal blocked in listing", c.get("/api/files", params={"path": "../"}).status_code in (400, 403))
    if hasattr(os, "symlink"):
        os.symlink(secret, shared / "link.txt")
        check("symlink escape blocked", c.get("/api/download", params={"path": "link.txt"}).status_code == 403
              and "link.txt" not in {e["name"] for e in c.get("/api/files").json()["entries"]})

    r = httpx.post(f"{local}/api/admin/settings", json={"max_file_mb": 1}, headers=A)
    r = c.post("/api/upload", files={"file": ("toobig.bin", data)}, headers={"X-File-Size": str(len(data))})
    check("size limit enforced", r.status_code == 413 and not (up / "toobig.bin").exists())
    httpx.post(f"{local}/api/admin/settings", json={"max_file_mb": 10240}, headers=A)
    check("settings reject filesystem root", httpx.post(f"{local}/api/admin/settings", json={"shared_dir": "/"}, headers=A).status_code == 400)

    # interrupted upload: send half a body then drop the connection
    import socket
    s = socket.create_connection((lan, PORT)); cookie = c.cookies.get("ld_session")
    body = b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"half.bin\"\r\nContent-Type: application/octet-stream\r\n\r\n" + os.urandom(300000)
    s.sendall((f"POST /api/upload HTTP/1.1\r\nHost: {lan}:{PORT}\r\nX-LocalDrop: 1\r\nCookie: ld_session={cookie}\r\n"
               f"Content-Type: multipart/form-data; boundary=B\r\nContent-Length: {len(body)+500000}\r\n\r\n").encode() + body)
    time.sleep(0.5); s.close(); time.sleep(1)
    check("interrupted upload leaves no file", not (up / "half.bin").exists() and not list((up / ".localdrop-partial").glob("*")))
    tr = httpx.get(f"{local}/api/admin/state", headers=A).json()["transfers"]
    check("interrupted upload recorded as cancelled/failed", any(t["name"] == "half.bin" and t["status"] in ("cancelled", "failed") for t in tr))
    check("completed files preserved", (up / "café photo.jpg").exists())

    for _ in range(14):
        c2 = httpx.Client(base_url=base, headers=H)
        c2.post("/api/pair", json={"code": "999999"})
    check("brute-force lockout / rotation", httpx.Client(base_url=base, headers=H).post("/api/pair", json={"code": pin}).status_code in (401, 429))

    httpx.post(f"{local}/api/admin/share", json={"enabled": False}, headers=A)
    time.sleep(0.5)
    try:
        r = httpx.get(base, timeout=2); closed = r.status_code == 503
    except httpx.HTTPError:
        closed = True
    check("stopping the server closes network access", closed)
    check("dashboard still works after stop", httpx.get(f"{local}/api/admin/state", headers=A).json()["sharing"] is False)
finally:
    proc.terminate()
    try: proc.wait(5)
    except Exception: proc.kill()
    err = proc.stderr.read().decode(errors="replace")
    if "Traceback" in err: print("\nSERVER ERRORS:\n" + err[-2500:])

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
