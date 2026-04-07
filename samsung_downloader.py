#!/usr/bin/env python3
import sys, os, socket, time, threading, urllib.request, xml.etree.ElementTree as ET, json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# --- UPnP/DLNA logic ---

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
MIME_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "video/mp4": ".mp4", "video/mpeg": ".mpg", "video/x-msvideo": ".avi",
    "video/quicktime": ".mov", "video/3gpp": ".3gp",
}

_CONFIG_DEFAULTS = {
    "skip_ip_prefixes": ["127.", "172.", "169.254."],
    "ssdp_timeout": 6,
}

def load_config():
    cfg = dict(_CONFIG_DEFAULTS)
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(config_path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return cfg

CONFIG = load_config()

def get_local_ips():
    skip = CONFIG.get("skip_ip_prefixes", _CONFIG_DEFAULTS["skip_ip_prefixes"])
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ":" in ip:
                continue
            if any(ip.startswith(p) for p in skip):
                continue
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips or ["0.0.0.0"]

def ssdp_discover(timeout=None):
    if timeout is None:
        timeout = CONFIG.get("ssdp_timeout", 6)
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 3\r\nST: upnp:rootdevice\r\n\r\n"
    )
    devices = []
    seen = set()
    for local_ip in get_local_ips():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
            sock.settimeout(timeout)
            sock.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                    resp = data.decode("utf-8", errors="replace")
                    location = None
                    for line in resp.splitlines():
                        if line.lower().startswith("location:"):
                            location = line.split(":", 1)[1].strip()
                    if location and location not in seen:
                        seen.add(location)
                        devices.append({"ip": addr[0], "location": location})
                except socket.timeout:
                    break
            sock.close()
        except Exception:
            pass
    return devices

def get_description(location):
    try:
        with urllib.request.urlopen(location, timeout=5) as r:
            root = ET.fromstring(r.read())
        ns = root.tag.split("}")[0] + "}" if "}" in root.tag else ""
        def ft(elem, tag): e = elem.find(f"{ns}{tag}"); return e.text if e is not None else ""
        device = root.find(f"{ns}device")
        if device is None:
            return None
        base = "/".join(location.split("/")[:3])
        services = []
        slist = device.find(f"{ns}serviceList")
        if slist is not None:
            for svc in slist:
                stype = ft(svc, "serviceType")
                curl = ft(svc, "controlURL")
                if curl and not curl.startswith("http"):
                    curl = base + "/" + curl.lstrip("/")
                services.append({"type": stype, "controlURL": curl})
        return {
            "friendlyName": ft(device, "friendlyName"),
            "manufacturer": ft(device, "manufacturer"),
            "modelName": ft(device, "modelName"),
            "services": services,
            "ip": location.split("/")[2].split(":")[0],
        }
    except Exception:
        return None

def find_camera(devices):
    for d in devices:
        desc = get_description(d["location"])
        if not desc:
            continue
        name = desc["friendlyName"].lower()
        mfr = desc["manufacturer"].lower()
        ctrl = next((s["controlURL"] for s in desc["services"] if "ContentDirectory" in s["type"]), None)
        if ctrl and ("camera" in name or "samsung" in mfr):
            return desc, ctrl
    return None, None

def soap_browse(ctrl, obj_id="0", start=0, count=500):
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <ObjectID>{obj_id}</ObjectID>
      <BrowseFlag>BrowseDirectChildren</BrowseFlag>
      <Filter>*</Filter>
      <StartingIndex>{start}</StartingIndex>
      <RequestedCount>{count}</RequestedCount>
      <SortCriteria></SortCriteria>
    </u:Browse>
  </s:Body>
</s:Envelope>"""
    req = urllib.request.Request(ctrl, data=body.encode(), headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return None

def parse_didl(didl_xml):
    items = []
    try:
        root = ET.fromstring(didl_xml)
    except ET.ParseError:
        return items
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "item":
            title, url, size, mime = "", "", 0, ""
            for child in elem:
                ct = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ct == "title": title = child.text or ""
                elif ct == "res":
                    url = child.text or ""
                    size = int(child.attrib.get("size", 0))
                    pi = child.attrib.get("protocolInfo", "")
                    mime = pi.split(":")[2] if pi.count(":") >= 2 else ""
            if url:
                items.append({"title": title, "url": url, "size": size, "mime": mime})
        elif tag == "container":
            cid, title = elem.attrib.get("id", ""), ""
            for child in elem:
                ct = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ct == "title": title = child.text or ""
            items.append({"type": "container", "id": cid, "title": title})
    return items

def browse_all(ctrl, obj_id="0"):
    resp = soap_browse(ctrl, obj_id)
    if not resp:
        return []
    result_xml = None
    try:
        envelope = ET.fromstring(resp)
        for elem in envelope.iter():
            if elem.tag.split("}")[-1] == "Result":
                result_xml = elem.text; break
    except ET.ParseError:
        pass
    if not result_xml:
        return []
    files = []
    for item in parse_didl(result_xml):
        if item.get("type") == "container":
            files.extend(browse_all(ctrl, item["id"]))
        else:
            files.append(item)
    return files

def ensure_ext(title, mime, url):
    if "." in title.split("/")[-1]:
        return title
    ext = MIME_EXT.get(mime.lower(), "")
    if not ext:
        u = url.split("?")[0].split("/")[-1]
        if "." in u:
            ext = "." + u.rsplit(".", 1)[-1].lower()
    return title + ext if ext else title


# --- GUI ---

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Samsung camera downloader")
        self.resizable(False, False)
        self.configure(padx=16, pady=12)

        self._camera_ctrl = None
        self._files = []
        self._running = False

        # Kamera sor
        cam_frame = tk.Frame(self)
        cam_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        tk.Label(cam_frame, text="Kamera:", width=8, anchor="w").pack(side="left")
        self.lbl_camera = tk.Label(cam_frame, text="Keresés...", fg="gray", anchor="w", width=40)
        self.lbl_camera.pack(side="left")
        self.btn_search = tk.Button(cam_frame, text="Keresés", command=self._search)
        self.btn_search.pack(side="right")

        # TargetFolder
        dest_frame = tk.Frame(self)
        dest_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        tk.Label(dest_frame, text="Mappa:", width=8, anchor="w").pack(side="left")
        self.dest_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Pictures", "SamsungCamera"))
        tk.Entry(dest_frame, textvariable=self.dest_var, width=38).pack(side="left")
        tk.Button(dest_frame, text="...", command=self._browse_dest).pack(side="left", padx=4)

        # Files 
        self.lbl_files = tk.Label(self, text="", fg="gray")
        self.lbl_files.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 6))

        # Progress bar
        self.progress = ttk.Progressbar(self, length=420, mode="determinate")
        self.progress.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 4))

        # Log
        self.log = tk.Text(self, height=10, width=58, state="disabled", bg="#f5f5f5", font=("Consolas", 9))
        self.log.grid(row=4, column=0, columnspan=3, pady=(0, 8))

        # Gombok
        btn_frame = tk.Frame(self)
        btn_frame.grid(row=5, column=0, columnspan=3)
        self.btn_download = tk.Button(btn_frame, text="All files", width=18,
                                       command=self._download, state="disabled", bg="#0078d4", fg="white",
                                       font=("Segoe UI", 10, "bold"))
        self.btn_download.pack(side="left", padx=4)
        self.btn_new = tk.Button(btn_frame, text="New files only", width=14,
                                  command=lambda: self._download(only_new=True), state="disabled")
        self.btn_new.pack(side="left", padx=4)

        self.after(100, self._search)

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _browse_dest(self):
        d = filedialog.askdirectory(initialdir=self.dest_var.get())
        if d:
            self.dest_var.set(d)

    def _search(self):
        self.btn_search.configure(state="disabled")
        self.lbl_camera.configure(text="Search...", fg="gray")
        self.btn_download.configure(state="disabled")
        self.btn_new.configure(state="disabled")
        self._log("Camera search on network...")
        threading.Thread(target=self._do_search, daemon=True).start()

    def _do_search(self):
        devices = ssdp_discover(timeout=6)
        desc, ctrl = find_camera(devices)
        if desc and ctrl:
            self._camera_ctrl = ctrl
            files = browse_all(ctrl)
            self._files = files
            self.after(0, lambda: self._on_found(desc, files))
        else:
            self.after(0, self._on_not_found)

    def _on_found(self, desc, files):
        name = desc["friendlyName"]
        ip = desc["ip"]
        self.lbl_camera.configure(text=f"{name}  ({ip})", fg="#107c10")
        self.lbl_files.configure(text=f"{len(files)} files on camera", fg="#333")
        self.btn_download.configure(state="normal")
        self.btn_new.configure(state="normal")
        self.btn_search.configure(state="normal")
        self._log(f"Connected: {name}  |  {len(files)} file")

    def _on_not_found(self):
        self.lbl_camera.configure(text="Camera not found", fg="#c42b1c")
        self.lbl_files.configure(text="")
        self.btn_search.configure(state="normal")
        self._log("Camera not found, check the WIFI connection")

    def _download(self, only_new=False):
        if self._running or not self._camera_ctrl:
            return
        dest = self.dest_var.get()
        if not dest:
            messagebox.showwarning("Hiba", "Select target folder!")
            return

        # Frissítjük a fájllistát
        self._running = True
        self.btn_download.configure(state="disabled")
        self.btn_new.configure(state="disabled")
        self.btn_search.configure(state="disabled")
        threading.Thread(target=self._do_download, args=(dest, only_new), daemon=True).start()

    def _do_download(self, dest, only_new):
        os.makedirs(dest, exist_ok=True)
        files = browse_all(self._camera_ctrl)
        if only_new:
            files = [f for f in files if not os.path.exists(
                os.path.join(dest, ensure_ext(f["title"], f.get("mime",""), f.get("url",""))))]

        total = len(files)
        self.after(0, lambda: self._log(f"Download: {total} files -> {dest}"))
        self.after(0, lambda: self.progress.configure(maximum=max(total, 1), value=0))

        ok, skip, err = 0, 0, 0
        for i, f in enumerate(files, 1):
            fname = ensure_ext(f["title"] or f"file_{i}", f.get("mime",""), f.get("url",""))
            fpath = os.path.join(dest, fname)
            if os.path.exists(fpath):
                skip += 1
                self.after(0, lambda fn=fname: self._log(f"  not exits: {fn}"))
            else:
                try:
                    urllib.request.urlretrieve(f["url"], fpath)
                    ok += 1
                    sz = os.path.getsize(fpath) // 1024
                    self.after(0, lambda fn=fname, s=sz: self._log(f"  ✓ {fn}  ({s} KB)"))
                except Exception as e:
                    err += 1
                    self.after(0, lambda fn=fname, ex=e: self._log(f"  ✗ {fn}: {ex}"))
            self.after(0, lambda v=i: self.progress.configure(value=v))

        self.after(0, lambda: self._log(f"\Done! {ok} downloaded, {skip} skipped, {err} error"))
        self.after(0, self._download_done)

    def _download_done(self):
        self._running = False
        self.btn_download.configure(state="normal")
        self.btn_new.configure(state="normal")
        self.btn_search.configure(state="normal")


if __name__ == "__main__":
    app = App()
    app.mainloop()
