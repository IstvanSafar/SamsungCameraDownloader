#!/usr/bin/env python3
"""
Samsung MobileLink replacement for Windows — CLI tool

Usage:
  1. Connect to the camera's WiFi (SSID: SEC_DSC_XXXXXXXX or join same network)
  2. Run: python samsung_link.py discover
  3. Run: python samsung_link.py browse
  4. Run: python samsung_link.py download --dest C:/Photos
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import socket
import time
import urllib.request
import xml.etree.ElementTree as ET
import os
import argparse

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_TIMEOUT = 5

SSDP_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
    "MAN: \"ssdp:discover\"\r\n"
    "MX: 3\r\n"
    "ST: upnp:rootdevice\r\n"
    "\r\n"
)

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


def get_local_ips():
    """Return local IPv4 addresses, skipping loopback and virtual adapters (172.x)."""
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
    return ips or ["0.0.0.0"]


def ssdp_discover(timeout=SSDP_TIMEOUT):
    """Send SSDP M-SEARCH on all local interfaces and collect responses."""
    local_ips = get_local_ips()
    print(f"[SSDP] Local interfaces: {local_ips}")

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
                        print(f"    Found: {addr[0]}  {server}")
                        devices.append({"ip": addr[0], "location": location, "server": server})
                except socket.timeout:
                    break
            sock.close()
        except Exception as e:
            print(f"  [warn] {local_ip}: {e}")

    return devices


def get_device_description(location):
    """Fetch and parse a UPnP device description XML."""
    try:
        with urllib.request.urlopen(location, timeout=5) as r:
            xml_data = r.read()
        root = ET.fromstring(xml_data)
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""

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
        if service_list is not None:
            for svc in service_list:
                svc_type = findtext(svc, "serviceType")
                control_url = findtext(svc, "controlURL")
                if control_url:
                    base = "/".join(location.split("/")[:3])
                    if not control_url.startswith("http"):
                        control_url = base + "/" + control_url.lstrip("/")
                info["services"].append({
                    "type": svc_type,
                    "controlURL": control_url,
                })
        return info
    except Exception as e:
        print(f"  [error] Failed to fetch device description: {e}")
        return None


def find_content_directory(device_info):
    """Return the ContentDirectory service control URL, or None."""
    for svc in device_info.get("services", []):
        if "ContentDirectory" in svc.get("type", ""):
            return svc["controlURL"]
    return None


def soap_browse(control_url, object_id="0", starting_index=0, requested_count=200):
    """Send a UPnP ContentDirectory Browse SOAP request."""
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
        print(f"  [error] SOAP Browse: {e}")
        return None


def parse_didl(didl_xml):
    """Parse DIDL-Lite XML and return a list of file/container dicts."""
    items = []
    try:
        root = ET.fromstring(didl_xml)
    except ET.ParseError as e:
        print(f"  [warn] DIDL parse error: {e}")
        return items

    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "item":
            title, url, size, mime = "", "", 0, ""
            for child in elem:
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag == "title":
                    title = child.text or ""
                elif ctag == "res":
                    url = child.text or ""
                    size = int(child.attrib.get("size", 0))
                    pi = child.attrib.get("protocolInfo", "")
                    mime = pi.split(":")[2] if pi.count(":") >= 2 else ""
            if url:
                items.append({"title": title, "url": url, "size": size, "mime": mime})
        elif tag == "container":
            cid = elem.attrib.get("id", "")
            title = ""
            for child in elem:
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag == "title":
                    title = child.text or ""
            items.append({"title": title, "id": cid, "type": "container"})
    return items


def browse_all(control_url, object_id="0", depth=0):
    """Recursively browse the camera's content directory."""
    indent_str = "  " * depth
    print(f"{indent_str}[Browse] ObjectID={object_id}")

    resp = soap_browse(control_url, object_id)
    if not resp:
        return []

    result_match = None
    try:
        envelope = ET.fromstring(resp)
        for elem in envelope.iter():
            if elem.tag.split("}")[-1] == "Result":
                result_match = elem.text
                break
    except ET.ParseError:
        pass

    if not result_match:
        print(f"{indent_str}  [warn] Empty result")
        return []

    all_files = []
    for item in parse_didl(result_match):
        if item.get("type") == "container":
            print(f"{indent_str}  [Folder] {item['title']}  (id={item.get('id','')})")
            all_files.extend(browse_all(control_url, item["id"], depth + 1))
        else:
            size_kb = item["size"] // 1024 if item["size"] else 0
            print(f"{indent_str}  [File] {item['title']}  ({size_kb} KB)  {item['mime']}")
            all_files.append(item)
    return all_files


def ensure_extension(title, mime, url):
    """Add a file extension based on MIME type if the title has none."""
    if "." in title.split("/")[-1]:
        return title
    ext = MIME_EXT.get(mime.lower(), "")
    if not ext and "." in url.split("?")[0].split("/")[-1]:
        ext = "." + url.split("?")[0].split("/")[-1].rsplit(".", 1)[-1].lower()
    return title + ext if ext else title


def download_files(files, dest_dir):
    """Download a list of files to a local directory."""
    os.makedirs(dest_dir, exist_ok=True)
    total = len(files)
    for i, f in enumerate(files, 1):
        raw_title = f["title"] or f"file_{i}"
        fname = ensure_extension(raw_title, f.get("mime", ""), f.get("url", ""))
        fpath = os.path.join(dest_dir, fname)
        if os.path.exists(fpath):
            print(f"  [{i}/{total}] Already exists: {fname}")
            continue
        print(f"  [{i}/{total}] Downloading: {fname} ...")
        try:
            urllib.request.urlretrieve(f["url"], fpath)
            size_kb = os.path.getsize(fpath) // 1024
            print(f"    OK -> {fpath}  ({size_kb} KB)")
        except Exception as e:
            print(f"    [error] {e}")


def find_camera_device(devices):
    """Find the Samsung camera among discovered UPnP devices."""
    for d in devices:
        desc = get_device_description(d["location"])
        if not desc:
            continue
        name = desc.get("friendlyName", "").lower()
        manufacturer = desc.get("manufacturer", "").lower()
        if "camera" in name or ("samsung" in manufacturer and find_content_directory(desc)):
            return desc, d
    return None, None


def cmd_discover(args):
    devices = ssdp_discover()
    if not devices:
        print("\nNo devices found.")
        print("Make sure:")
        print("  1. The camera WiFi is on and connected to the same network")
        print("  2. The camera is in AutoShare or MobileLink mode")
        return

    print(f"\n{len(devices)} device(s) found:")
    for d in devices:
        desc = get_device_description(d["location"])
        if desc:
            print(f"\n  Name:         {desc['friendlyName']}")
            print(f"  Manufacturer: {desc['manufacturer']}")
            print(f"  Model:        {desc['modelName']} {desc['modelNumber']}")
            print(f"  IP:           {d['ip']}")
            print(f"  Services:")
            for svc in desc["services"]:
                print(f"    - {svc['type']}")
                print(f"      {svc['controlURL']}")


def cmd_browse(args):
    devices = ssdp_discover(timeout=6)
    if not devices:
        print("No devices found.")
        print("Tip: try  python samsung_link.py manual <camera-ip>")
        return None

    desc, device = find_camera_device(devices)
    if not desc:
        print("No Samsung camera found on the network.")
        print("Devices found:")
        for d in devices:
            print(f"  {d['ip']} - {d.get('server', '?')}")
        return None

    print(f"Connected: {desc['friendlyName']} ({desc['modelName']}) @ {device['ip']}")
    ctrl_url = find_content_directory(desc)
    if not ctrl_url:
        print("ContentDirectory service not found.")
        return None

    print(f"ContentDirectory URL: {ctrl_url}\n")
    files = browse_all(ctrl_url, "0")
    print(f"\nTotal: {len(files)} file(s) found.")
    return files, ctrl_url, desc


def cmd_download(args):
    result = cmd_browse(args)
    if not result:
        return
    files, ctrl_url, desc = result
    if not files:
        print("No files to download.")
        return

    dest = args.dest or os.path.join(os.path.expanduser("~"), "Pictures", "SamsungCamera")
    print(f"\nDownloading to: {dest}")
    download_files(files, dest)
    print("\nDone!")


def cmd_manual(args):
    """Connect manually by IP address."""
    ip = args.ip
    candidates = [
        f"http://{ip}:7676/smp_6_",
        f"http://{ip}/description.xml",
        f"http://{ip}/rootDesc.xml",
        f"http://{ip}:49152/description.xml",
        f"http://{ip}:49153/description.xml",
        f"http://{ip}:8080/description.xml",
    ]
    if args.port:
        candidates.insert(0, f"http://{ip}:{args.port}/description.xml")

    desc = None
    for url in candidates:
        print(f"Trying: {url}")
        desc = get_device_description(url)
        if desc:
            print(f"  Success! {desc['friendlyName']}")
            break

    if not desc:
        print("Could not reach the camera description.")
        print("Make sure you are connected to the camera's WiFi, then try:")
        print("  python samsung_link.py discover")
        return

    ctrl_url = find_content_directory(desc)
    if not ctrl_url:
        print("ContentDirectory service not found.")
        return

    files = browse_all(ctrl_url, "0")
    print(f"\nTotal: {len(files)} file(s).")

    if args.dest and files:
        download_files(files, args.dest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Samsung WiFi camera downloader — CLI"
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("discover", help="SSDP discovery — list all UPnP devices on the network")
    sub.add_parser("browse",   help="Browse files on the camera without downloading")

    dl = sub.add_parser("download", help="Download all files from the camera")
    dl.add_argument("--dest", default=None, help="Destination folder (default: ~/Pictures/SamsungCamera)")

    manual = sub.add_parser("manual", help="Connect by IP address (if auto-discovery fails)")
    manual.add_argument("ip",          help="Camera IP address (e.g. 192.168.0.225)")
    manual.add_argument("--port",      type=int, default=None, help="Port (default: auto-detect)")
    manual.add_argument("--dest",      default=None, help="Destination folder")

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
        print("\nQuick start:")
        print("  1. Connect your PC to the same WiFi as the camera")
        print("  2. python samsung_link.py discover")
        print("  3. python samsung_link.py browse")
        print("  4. python samsung_link.py download --dest C:/Photos")
