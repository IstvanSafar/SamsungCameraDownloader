#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Samsung MobileLink replacement for Windows

Hasznalat:
  1. Csatlakozz a kamera WiFi-jehez (pl. "SEC_DSC_XXXXXXXX" SSID)
  2. Futtasd: python samsung_link.py discover
  3. Futtasd: python samsung_link.py browse
  4. Futtasd: python samsung_link.py download --dest C:/Kepek
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import socket
import time
import urllib.request
import xml.etree.ElementTree as ET
import sys
import os
import argparse
from textwrap import indent

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_TIMEOUT = 5  # másodperc

SSDP_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
    "MAN: \"ssdp:discover\"\r\n"
    "MX: 3\r\n"
    "ST: upnp:rootdevice\r\n"
    "\r\n"
)

NS = {
    "upnp": "urn:schemas-upnp-org:metadata-1-5",
    "dc": "http://purl.org/dc/elements/1.1/",
    "didl": "urn:schemas-upnp-org:metadata-1-5",
}


def get_local_ips():
    """Az osszes helyi IP cim lekerese (Hyper-V/loopback kihagyasaval)."""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ip.startswith("127.") or ip.startswith("172.") or ":" in ip:
                continue
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    # Fallback
    if not ips:
        ips = ["0.0.0.0"]
    return ips


def ssdp_discover(timeout=SSDP_TIMEOUT):
    """SSDP M-SEARCH kuldese minden lokalis interfeszen."""
    local_ips = get_local_ips()
    print(f"[SSDP] Helyi interfeszek: {local_ips}")

    devices = []
    seen_locations = set()

    for local_ip in local_ips:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
            sock.settimeout(timeout)

            print(f"  M-SEARCH -> {SSDP_ADDR}:{SSDP_PORT} ({local_ip})")
            sock.sendto(SSDP_MSEARCH.encode(), (SSDP_ADDR, SSDP_PORT))

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                    resp = data.decode("utf-8", errors="replace")
                    location = None
                    server = ""
                    for line in resp.splitlines():
                        ll = line.lower()
                        if ll.startswith("location:"):
                            location = line.split(":", 1)[1].strip()
                        elif ll.startswith("server:"):
                            server = line.split(":", 1)[1].strip()
                    if location and location not in seen_locations:
                        seen_locations.add(location)
                        print(f"    Talalt: {addr[0]}  {server}")
                        devices.append({"ip": addr[0], "location": location, "server": server})
                except socket.timeout:
                    break
            sock.close()
        except Exception as e:
            print(f"  [warn] {local_ip}: {e}")

    return devices


def get_device_description(location):
    """Letölti és parse-olja az UPnP device description XML-t."""
    try:
        with urllib.request.urlopen(location, timeout=5) as r:
            xml_data = r.read()
        root = ET.fromstring(xml_data)
        # Namespace eltávolítása az egyszerűség kedvéért
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        def find(elem, tag):
            return elem.find(f"{ns}{tag}")

        def findtext(elem, tag, default=""):
            e = find(elem, tag)
            return e.text if e is not None else default

        device = find(root, "device")
        if device is None:
            return None

        info = {
            "friendlyName": findtext(device, "friendlyName"),
            "manufacturer": findtext(device, "manufacturer"),
            "modelName": findtext(device, "modelName"),
            "modelNumber": findtext(device, "modelNumber"),
            "UDN": findtext(device, "UDN"),
            "services": [],
        }

        service_list = find(device, "serviceList")
        if service_list:
            for svc in service_list:
                svc_type = findtext(svc, "serviceType")
                control_url = findtext(svc, "controlURL")
                if control_url:
                    # Relatív URL -> abszolút
                    base = "/".join(location.split("/")[:3])
                    if not control_url.startswith("http"):
                        control_url = base + "/" + control_url.lstrip("/")
                info["services"].append({
                    "type": svc_type,
                    "controlURL": control_url,
                })
        return info
    except Exception as e:
        print(f"  [hiba] Leírás letöltése sikertelen: {e}")
        return None


def find_content_directory(device_info):
    """Megkeresi a ContentDirectory service control URL-t."""
    for svc in device_info.get("services", []):
        if "ContentDirectory" in svc.get("type", ""):
            return svc["controlURL"]
    return None


def soap_browse(control_url, object_id="0", starting_index=0, requested_count=200):
    """UPnP ContentDirectory Browse SOAP kérés."""
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <ObjectID>{object_id}</ObjectID>
      <BrowseFlag>BrowseDirectChildren</BrowseFlag>
      <Filter>*</Filter>
      <StartingIndex>{starting_index}</StartingIndex>
      <RequestedCount>{requested_count}</RequestedCount>
      <SortCriteria></SortCriteria>
    </u:Browse>
  </s:Body>
</s:Envelope>"""

    req = urllib.request.Request(
        control_url,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [hiba] SOAP Browse: {e}")
        return None


def parse_didl(didl_xml):
    """DIDL-Lite XML parse-olása -> fájl lista."""
    items = []
    try:
        root = ET.fromstring(didl_xml)
    except ET.ParseError as e:
        print(f"  [warn] DIDL parse hiba: {e}")
        return items

    ns = {
        "d": "urn:schemas-upnp-org:metadata-1-5",
        "dc": "http://purl.org/dc/elements/1.1/",
        "upnp": "urn:schemas-upnp-org:metadata-1-5",
    }

    # Namespace-független keresés
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "item":
            title = ""
            url = ""
            size = 0
            mime = ""
            for child in elem:
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag == "title":
                    title = child.text or ""
                elif ctag == "res":
                    url = child.text or ""
                    size = int(child.attrib.get("size", 0))
                    mime = child.attrib.get("protocolInfo", "").split(":")[2] if ":" in child.attrib.get("protocolInfo", "") else ""
            if url:
                items.append({"title": title, "url": url, "size": size, "mime": mime})
        elif tag == "container":
            title = ""
            cid = elem.attrib.get("id", "")
            for child in elem:
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag == "title":
                    title = child.text or ""
            items.append({"title": title, "id": cid, "type": "container"})
    return items


def browse_all(control_url, object_id="0", depth=0):
    """Rekurzívan bejárja a kamera fájlrendszerét."""
    indent_str = "  " * depth
    print(f"{indent_str}[Browse] ObjectID={object_id}")

    resp = soap_browse(control_url, object_id)
    if not resp:
        return []

    # Kiveszi a Result XML-t
    result_match = None
    try:
        envelope = ET.fromstring(resp)
        for elem in envelope.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Result":
                result_match = elem.text
                break
    except ET.ParseError:
        pass

    if not result_match:
        print(f"{indent_str}  [warn] Ures eredmeny")
        return []

    items = parse_didl(result_match)
    all_files = []
    for item in items:
        if item.get("type") == "container":
            print(f"{indent_str}  [Mappa] {item['title']}  (id={item.get('id','')})")
            sub = browse_all(control_url, item["id"], depth + 1)
            all_files.extend(sub)
        else:
            size_kb = item["size"] // 1024 if item["size"] else 0
            print(f"{indent_str}  [Fajl] {item['title']}  ({size_kb} KB)  {item['mime']}")
            all_files.append(item)
    return all_files


MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/mpeg": ".mpg",
    "video/x-msvideo": ".avi",
    "video/quicktime": ".mov",
    "video/3gpp": ".3gp",
}


def ensure_extension(title, mime, url):
    """Kiterjesztes hozzaadasa ha nincs."""
    # Ha mar van kiterjesztes, nem csinalunk semmit
    if "." in title.split("/")[-1]:
        return title
    # MIME alapjan
    ext = MIME_EXT.get(mime.lower(), "")
    # URL-bol is megprobaljuk
    if not ext and "." in url.split("?")[0].split("/")[-1]:
        ext = "." + url.split("?")[0].split("/")[-1].rsplit(".", 1)[-1].lower()
    return title + ext if ext else title


def download_files(files, dest_dir):
    """Letölti a fájlokat a megadott mappába."""
    os.makedirs(dest_dir, exist_ok=True)
    total = len(files)
    for i, f in enumerate(files, 1):
        raw_title = f["title"] or f"file_{i}"
        fname = ensure_extension(raw_title, f.get("mime", ""), f.get("url", ""))
        fpath = os.path.join(dest_dir, fname)
        if os.path.exists(fpath):
            print(f"  [{i}/{total}] Mar letezik: {fname}")
            continue
        print(f"  [{i}/{total}] Letoltes: {fname} ...")
        try:
            urllib.request.urlretrieve(f["url"], fpath)
            size_kb = os.path.getsize(fpath) // 1024
            print(f"    OK -> {fpath}  ({size_kb} KB)")
        except Exception as e:
            print(f"    [hiba] {e}")


def cmd_discover(args):
    devices = ssdp_discover()
    if not devices:
        print("\nNem talalt eszkozokat.")
        print("Ellenorizd, hogy:")
        print("  1. A kamera WiFi hotspotjara vagy csatlakozva")
        print("  2. A kamera 'Remote Shooting' vagy 'MobileLink' modban van")
        return

    print(f"\n{len(devices)} eszkoz talalt:")
    for d in devices:
        desc = get_device_description(d["location"])
        if desc:
            print(f"\n  Nev: {desc['friendlyName']}")
            print(f"  Gyarto: {desc['manufacturer']}")
            print(f"  Model: {desc['modelName']} {desc['modelNumber']}")
            print(f"  IP: {d['ip']}")
            print(f"  Services:")
            for svc in desc["services"]:
                print(f"    - {svc['type']}")
                print(f"      {svc['controlURL']}")


def find_camera_device(devices):
    """Megkeresi a Samsung kamerát az eszközök között."""
    for d in devices:
        desc = get_device_description(d["location"])
        if not desc:
            continue
        name = desc.get("friendlyName", "").lower()
        manufacturer = desc.get("manufacturer", "").lower()
        # Samsung kamera jellemzoi: "[Camera]" a nevben, vagy Samsung + MediaServer
        if "camera" in name or ("samsung" in manufacturer and find_content_directory(desc)):
            return desc, d
    return None, None


def cmd_browse(args):
    devices = ssdp_discover(timeout=6)
    if not devices:
        print("Nem talalt eszkozokat.")
        print("Tipp: py samsung_link.py manual 192.168.0.225")
        return

    desc, device = find_camera_device(devices)
    if not desc:
        print("Nem talalt Samsung kamera eszkozot a halozaton.")
        print("Talalt eszkozok:")
        for d in devices:
            print(f"  {d['ip']} - {d.get('server','?')}")
        return

    print(f"Kapcsolodva: {desc['friendlyName']} ({desc['modelName']}) @ {device['ip']}")
    ctrl_url = find_content_directory(desc)
    if not ctrl_url:
        print("ContentDirectory service nem talalhato!")
        return

    print(f"ContentDirectory URL: {ctrl_url}\n")
    files = browse_all(ctrl_url, "0")
    print(f"\nOsszesen {len(files)} fajl talalt.")
    return files, ctrl_url, desc


def cmd_download(args):
    result = cmd_browse(args)
    if not result:
        return
    files, ctrl_url, desc = result
    if not files:
        print("Nincs letoltendo fajl.")
        return

    dest = args.dest or os.path.join(os.path.expanduser("~"), "Pictures", "SamsungCamera")
    print(f"\nLetoltes ide: {dest}")
    download_files(files, dest)
    print("\nKesz!")


def cmd_manual(args):
    """Kezi mod: megadott IP-re csatlakozik."""
    ip = args.ip
    port = args.port or 52235
    # Probaljuk a tipikus Samsung camera description URL-t
    candidates = [
        f"http://{ip}:{port}/description.xml",
        f"http://{ip}:{port}/rootDesc.xml",
        f"http://{ip}/description.xml",
        f"http://{ip}/rootDesc.xml",
        f"http://{ip}:49152/description.xml",
        f"http://{ip}:49153/description.xml",
        f"http://{ip}:8080/description.xml",
    ]

    desc = None
    for url in candidates:
        print(f"Probalok: {url}")
        desc = get_device_description(url)
        if desc:
            print(f"  Siker! {desc['friendlyName']}")
            break

    if not desc:
        print("Nem sikerult elerni a kamera leirasat.")
        print("Csatlakozz a kamera WiFi-jere, majd probalj SSDP discovery-t:")
        print("  python samsung_link.py discover")
        return

    ctrl_url = find_content_directory(desc)
    if not ctrl_url:
        print("ContentDirectory nem talalhato.")
        return

    files = browse_all(ctrl_url, "0")
    print(f"\nOsszesen {len(files)} fajl.")

    if args.dest and files:
        download_files(files, args.dest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Samsung kamera MobileLink kliens Windowsra"
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("discover", help="SSDP felfedezes - eszkozok listazasa")
    sub.add_parser("browse", help="Fajlok bongeszetse a kameran")

    dl = sub.add_parser("download", help="Osszes fajl letoltese")
    dl.add_argument("--dest", default=None, help="Celmappa (alapert.: ~/Pictures/SamsungCamera)")

    manual = sub.add_parser("manual", help="Kezi IP cim megadasa")
    manual.add_argument("ip", help="Kamera IP-je (pl. 192.168.100.1)")
    manual.add_argument("--port", type=int, default=None, help="Port (alapert.: automatikus)")
    manual.add_argument("--dest", default=None, help="Letoltesi mappa")

    args = parser.parse_args()

    if args.cmd == "discover":
        cmd_discover(args)
    elif args.cmd == "browse":
        cmd_browse(args)
    elif args.cmd == "download":
        cmd_download(args)
    elif args.cmd == "manual":
        cmd_manual(args)
    else:
        parser.print_help()
        print("\nGyors start:")
        print("  1. Csatlakozz a kamera WiFi-jere (SSID: SEC_DSC_XXXXXXXX)")
        print("  2. py samsung_link.py discover")
        print("  3. py samsung_link.py browse")
        print("  4. py samsung_link.py download --dest C:/Kepek")
