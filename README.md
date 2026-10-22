# LocalDrop

Send photos, videos, documents, APKs and ZIPs between your Android phone and your PC over your own Wi-Fi.
Files go directly from device to device. No account, no cloud, no internet needed after the one-time install.

## Install (Windows)

1. Install **Python 3.9 or newer** from <https://www.python.org/downloads/> (tick **Add python.exe to PATH**).
2. Unzip this folder anywhere (for example `C:\LocalDrop`).
3. Double-click **`run.bat`**.
   The first run creates a private `.venv` and installs the packages from `requirements.txt` (internet needed once).
   Your browser then opens the dashboard at `http://127.0.0.1:8765`.

Command-line equivalent:

```bat
cd C:\LocalDrop
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

Linux/macOS: `./run.sh`. Options: `--port 9000`, `--no-browser`. Update packages later with `run.bat --update`.

## Connect your phone

1. Connect the phone and PC to the **same Wi-Fi**.
2. On the PC dashboard click **Start sharing** and confirm the warning.
3. On the phone, scan the **QR code** with the camera (or open the address shown and type the 6-digit **PIN**).
4. Tap **Select Files** (or **Select Folder** where the browser supports it). Progress, speed and time left are shown live.
   Files are saved to the **uploads** folder (shown after each transfer; change it in Settings).
5. To get files *from* the PC: drop them onto the dashboard's "Share files with your phone" area (or put them in the shared
   folder), then open **PC Files** on the phone and tap **Download**.
6. Click **Stop sharing** when finished. Network access ends immediately.

Keep the phone's screen on and the browser in the foreground during large transfers.

## Windows Firewall

Windows asks the first time LocalDrop starts sharing. Tick **Private networks** and click *Allow access*.
If you missed it, or the phone still cannot connect:

* Settings > Network & Internet > Wi-Fi > your network > set **Network profile = Private**.
* Allow the port from an **Administrator** PowerShell/Command Prompt:
  ```bat
  netsh advfirewall firewall add rule name="LocalDrop" dir=in action=allow protocol=TCP localport=8765 profile=private
  ```
  Remove it later with `netsh advfirewall firewall delete rule name="LocalDrop"`.
* Or: Windows Security > Firewall & network protection > *Allow an app through firewall* > allow `python.exe` (from `.venv\Scripts`) on Private.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Phone page never loads | Same Wi-Fi? Guest networks and routers with **client/AP isolation** block device-to-device traffic. Try another network or your phone's/PC's hotspot. |
| Works on the PC browser but not on the phone | Firewall (above). |
| "No local network address found" | Connect the PC to Wi-Fi/Ethernet. With several adapters (VPN, Hyper-V, VirtualBox) pick the right address in the dropdown next to *Start sharing*. |
| Wi-Fi has no internet and phone can't connect | Android may send traffic over mobile data. Turn mobile data off, or disable "switch to mobile data automatically". |
| VPN running | Disconnect VPNs on both devices; they often capture local traffic. |
| "Could not listen on port" | Another program (or a second LocalDrop) uses the port. Start with `--port 8766`. |
| QR code says it is no longer valid | The code was regenerated or sharing was restarted. Scan the current QR. |
| Too many wrong codes | Wait the stated time. After repeated failures the PIN/QR is replaced automatically. |
| Transfer failed | Retry from the phone (it still has the file). Check free space and the size limit in Settings. |

## Security model

* Server listens on the LAN **only while sharing is on**, bound to the single address you chose, never the public internet.
  The dashboard itself is only reachable from the PC (`127.0.0.1`) and needs a per-launch admin token.
* Phones must pair with the one-run QR token or 6-digit PIN; pairing creates a random, expiring, HttpOnly SameSite=Strict session cookie.
  Wrong codes are rate-limited per device and the PIN/QR is rotated after repeated failures. Paired devices can be disconnected from the dashboard.
* All state-changing requests need a custom header and a matching `Origin`, WebSockets check `Origin`, and `Host` must be an IP address:
  other websites cannot drive the service (CSRF / DNS rebinding).
* Downloads are limited to the shared folder: `..`, absolute paths, hidden entries and symlinks leading outside are refused.
  The app never lists or serves anything else on the PC.
* Uploaded names are sanitised (path parts, reserved Windows names, control characters); duplicates become `name (1).ext`;
  files are stored non-executable and are never opened or run. Downloads are sent as attachments.
* Uploads stream to a hidden temp file and are moved into place only when complete, so interrupted transfers leave no partial files
  and earlier completed files are untouched. A size limit and a free-disk check apply.
* **Traffic is plain HTTP on your LAN** (no TLS), so use only networks you trust. Anyone on the same network could observe transfers.

## Settings and data

Settings are stored in `data/config.json`; transfer history in `data/history.json` (no database). Incoming files default to `uploads/`,
the shared folder to `shared/`. LocalDrop refuses a drive root, your whole user profile (as shared folder) or its own program folder.
**Start LocalDrop when I sign in** (Settings) adds a per-user startup entry (`HKCU\...\Run` on Windows); sharing still has to be started manually.

## Project layout

`main.py` app, routes, sharing on/off · `transfer.py` streaming upload/download, path and filename safety · `security.py` pairing, sessions,
request protection · `network.py` LAN address detection · `config.py` settings · `events.py` WebSocket hub · `startup.py` autostart ·
`templates/`, `static/` UI · `selftest.py` automated security/transfer checks.

## Self-test

```bat
.venv\Scripts\pip install httpx
.venv\Scripts\python selftest.py
```
Runs a throw-away copy (its own folders, port 18765) and verifies pairing, unauthorised access, traversal/symlink blocking, duplicates,
size limits, interrupted uploads, range downloads and that stopping closes access.
