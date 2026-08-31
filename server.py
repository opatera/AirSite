#!/usr/bin/env python3

import argparse
import json
import mimetypes
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from email.parser import BytesParser
from email.policy import default
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parent
INDEX_FILE = REPO_ROOT / "index.html"
SHARE_ROOT = Path("/media/airowen/Storage/share").resolve()
MUSIC_ROOT = (SHARE_ROOT / "Music").resolve()
GBA_ROOT = (SHARE_ROOT / "ROMs" / "GBA").resolve()
GBA_SAVE_ROOT = (GBA_ROOT / ".airsite-saves").resolve()
BOOT_TIME = time.time() - float(Path("/proc/uptime").read_text().split()[0])
SYSTEM_HISTORY_MAX = 60
SYSTEM_HISTORY_INTERVAL = 60
SYSTEM_HISTORY = deque(maxlen=SYSTEM_HISTORY_MAX)
SYSTEM_HISTORY_LOCK = threading.Lock()


def clean_relative_path(raw_path: str) -> str:
    """Normalize a user-provided relative path and reject traversal segments.

    Args:
        raw_path: Path text that may include repeated separators or '.' segments.

    Returns:
        A cleaned relative path that stays within the intended root.
    """
    parts = [part for part in raw_path.replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("Path traversal is not allowed.")
    return "/".join(parts)


def resolve_share_path(raw_path: str) -> tuple[Path, str]:
    """Resolve a relative path inside the shared drive.

    Args:
        raw_path: Relative path requested by the client.

    Returns:
        A tuple of the resolved filesystem path and the normalized relative path.
    """
    relative = clean_relative_path(raw_path)
    destination = (SHARE_ROOT / relative).resolve()

    if os.path.commonpath([SHARE_ROOT, destination]) != str(SHARE_ROOT):
        raise ValueError("Destination is outside the shared drive.")

    return destination, relative


def resolve_rooted_path(root: Path, raw_path: str) -> tuple[Path, str]:
    """Resolve a relative path inside an arbitrary allowed root directory.

    Args:
        root: Base directory the result must stay inside.
        raw_path: Relative path requested by the client.

    Returns:
        A tuple of the resolved filesystem path and the normalized relative path.
    """
    relative = clean_relative_path(raw_path)
    destination = (root / relative).resolve()

    if os.path.commonpath([root, destination]) != str(root):
        raise ValueError("Destination is outside the allowed directory.")

    return destination, relative


def human_sort_key(entry: os.DirEntry) -> tuple[int, str]:
    """Sort directories before files and compare names case-insensitively.

    Args:
        entry: Directory entry to rank.

    Returns:
        A tuple suitable for use as a stable sort key.
    """
    return (0 if entry.is_dir() else 1, entry.name.lower())


def is_audio_file(path: Path) -> bool:
    """Check whether a file path points to a supported audio format."""
    return path.suffix.lower() in {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".wav"}


def is_art_file(path: Path) -> bool:
    """Check whether a file path points to a supported album-art image."""
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}


def is_gba_rom(path: Path) -> bool:
    """Check whether a file path points to a Game Boy Advance ROM."""
    return path.suffix.lower() == ".gba"


def gba_save_relative_path(rom_relative: str) -> str:
    """Map a ROM path to the matching relative save-file path.

    Args:
        rom_relative: Relative path to a ROM within the GBA library.

    Returns:
        The relative path for the `.sav` file that belongs to the ROM.
    """
    rom_path = Path(rom_relative)
    return str(rom_path.with_suffix(".sav")).replace(os.sep, "/")


def gba_save_path_for_rom(rom_relative: str) -> tuple[Path, str]:
    """Resolve the filesystem save path for a given ROM.

    Args:
        rom_relative: Relative path to a ROM within the GBA library.

    Returns:
        A tuple of the resolved save-file path and its normalized relative path.
    """
    save_relative = gba_save_relative_path(rom_relative)
    target_file, _ = resolve_rooted_path(GBA_SAVE_ROOT, save_relative)
    return target_file, save_relative


def find_album_art(directory: Path) -> Path | None:
    """Find the best candidate album-art file in a music directory.

    Args:
        directory: Folder to inspect for image files.

    Returns:
        The preferred artwork path, or `None` when no image is available.
    """
    try:
        candidates = sorted(
            (
                entry for entry in directory.iterdir()
                if entry.is_file() and is_art_file(entry)
            ),
            key=lambda entry: (0 if entry.stem.lower() in {"cover", "folder", "front"} else 1, entry.name.lower()),
        )
    except (FileNotFoundError, PermissionError):
        return None

    return candidates[0] if candidates else None


def read_cpu_times() -> tuple[int, int]:
    """Read cumulative CPU totals from `/proc/stat`.

    Returns:
        A tuple containing total ticks and idle ticks.
    """
    values = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    cpu_values = [int(value) for value in values[:8]]
    idle = cpu_values[3] + cpu_values[4]
    total = sum(cpu_values)
    return total, idle


def sample_cpu_usage(interval: float = 0.12) -> float:
    """Measure CPU usage across a short sampling interval.

    Args:
        interval: Number of seconds to wait between CPU snapshots.

    Returns:
        The measured CPU usage percentage from 0 to 100.
    """
    total_a, idle_a = read_cpu_times()
    time.sleep(interval)
    total_b, idle_b = read_cpu_times()
    total_delta = total_b - total_a
    idle_delta = idle_b - idle_a
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100))


def read_cpu_usage_from_last_sample(previous: tuple[int, int], current: tuple[int, int]) -> float:
    """Compute CPU usage between two cumulative CPU snapshots.

    Args:
        previous: Earlier `(total, idle)` CPU tick counts.
        current: Later `(total, idle)` CPU tick counts.

    Returns:
        The CPU usage percentage represented by the interval.
    """
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100))


def read_meminfo() -> dict[str, int]:
    """Parse `/proc/meminfo` into a byte-based dictionary.

    Returns:
        A mapping of memory metric names to byte counts.
    """
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        info[key] = int(value.strip().split()[0]) * 1024
    return info


def disk_snapshot(path: str, label: str) -> dict:
    """Collect disk-usage details for a filesystem path.

    Args:
        path: Filesystem path to inspect.
        label: Human-friendly label for the UI.

    Returns:
        A JSON-ready dictionary with total, used, free, and percentage values.
    """
    usage = shutil.disk_usage(path)
    percent = (usage.used / usage.total * 100) if usage.total else 0
    return {
        "label": label,
        "path": path,
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percentUsed": percent,
    }


def service_state(name: str) -> dict:
    """Read the current `systemd` activity state for a service.

    Args:
        name: Service unit name to inspect.

    Returns:
        A dictionary containing the service name and the detected state.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.SubprocessError, PermissionError):
        return {"name": name, "state": "unknown"}

    state = (result.stdout or result.stderr).strip() or "unknown"
    return {"name": name, "state": state}


def read_text_file(path: Path, default: str = "") -> str:
    """Read and trim a text file, falling back when it is unavailable.

    Args:
        path: File to read.
        default: Value to return when the file cannot be read.

    Returns:
        The stripped file contents or the provided default value.
    """
    try:
        return path.read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return default


def decode_route_hex(value: str) -> str:
    """Convert a little-endian hex route address into dotted IPv4 text.

    Args:
        value: Hex string from `/proc/net/route`.

    Returns:
        A dotted IPv4 address string.
    """
    raw = bytes.fromhex(value)
    return socket.inet_ntoa(raw)


def dns_servers() -> list[str]:
    """Read configured DNS server addresses from `/etc/resolv.conf`.

    Returns:
        A list of DNS server IP strings, or an empty list on failure.
    """
    servers = []
    try:
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            line = line.strip()
            if line.startswith("nameserver "):
                servers.append(line.split()[1])
    except (FileNotFoundError, PermissionError, OSError):
        return []
    return servers


def default_gateway() -> str | None:
    """Determine the default IPv4 gateway from the kernel route table.

    Returns:
        The gateway IP string when present, otherwise `None`.
    """
    try:
        lines = Path("/proc/net/route").read_text().splitlines()[1:]
    except (FileNotFoundError, PermissionError, OSError):
        return None

    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "00000000":
            return decode_route_hex(fields[2])
    return None


def addresses_by_interface() -> dict[str, list[str]]:
    """Read IPv4 addresses grouped by interface from the `ip` command.

    Returns:
        A mapping of interface names to their IPv4 addresses.
    """
    try:
        result = subprocess.run(
            ["ip", "-brief", "address"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.SubprocessError, PermissionError):
        return {}

    if result.returncode != 0:
        return {}

    addresses = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[0]
        ips = [part for part in parts[2:] if "/" in part and ":" not in part]
        addresses[name] = [ip.split("/", 1)[0] for ip in ips]
    return addresses


def network_interfaces() -> list[dict]:
    """Build UI-friendly network interface details from `/sys` and `ip`.

    Returns:
        A list of dictionaries describing interface state, addresses, and counters.
    """
    base = Path("/sys/class/net")
    ip_map = addresses_by_interface()
    interfaces = []

    for path in sorted(base.iterdir(), key=lambda item: item.name):
        iface = path.name
        stats = path / "statistics"
        interfaces.append(
            {
                "name": iface,
                "state": read_text_file(path / "operstate", "unknown"),
                "mac": read_text_file(path / "address", "unknown"),
                "mtu": int(read_text_file(path / "mtu", "0") or "0"),
                "rxBytes": int(read_text_file(stats / "rx_bytes", "0") or "0"),
                "txBytes": int(read_text_file(stats / "tx_bytes", "0") or "0"),
                "addresses": ip_map.get(iface, []),
                "speedMbps": int(read_text_file(path / "speed", "0") or "0"),
            }
        )

    return interfaces


def arp_neighbors() -> list[dict]:
    """Parse the ARP table into a list of nearby IPv4 neighbors.

    Returns:
        A list of dictionaries containing neighbor IP, MAC, interface, and state.
    """
    try:
        lines = Path("/proc/net/arp").read_text().splitlines()[1:]
    except (FileNotFoundError, PermissionError, OSError):
        return []

    neighbors = []
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            continue
        ip_addr, _hw_type, flags, mac, _mask, device = fields[:6]
        if mac == "00:00:00:00:00:00":
            continue
        neighbors.append(
            {
                "ip": ip_addr,
                "mac": mac,
                "device": device,
                "state": "reachable" if flags != "0x0" else "incomplete",
            }
        )
    return neighbors


def network_snapshot() -> dict:
    """Assemble the current network status payload for the frontend.

    Returns:
        A JSON-ready dictionary with host identity, interfaces, DNS, and neighbors.
    """
    gateway = default_gateway()
    interfaces = network_interfaces()
    primary_ip = None
    for iface in interfaces:
        if iface["state"] == "up" and iface["addresses"]:
            primary_ip = iface["addresses"][0]
            break

    return {
        "hostname": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        "primaryIp": primary_ip,
        "gateway": gateway,
        "dnsServers": dns_servers(),
        "interfaces": interfaces,
        "neighbors": arp_neighbors(),
        "timestamp": int(time.time()),
    }


def system_snapshot() -> dict:
    """Assemble the current system-health payload for the frontend.

    Returns:
        A JSON-ready dictionary with CPU, memory, storage, service, and history data.
    """
    meminfo = read_meminfo()
    mem_total = meminfo.get("MemTotal", 0)
    mem_available = meminfo.get("MemAvailable", 0)
    mem_used = max(0, mem_total - mem_available)
    swap_total = meminfo.get("SwapTotal", 0)
    swap_free = meminfo.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    load_averages = os.getloadavg()
    cpu_usage = sample_cpu_usage()
    current_timestamp = int(time.time())

    with SYSTEM_HISTORY_LOCK:
        history = list(SYSTEM_HISTORY)

    current_history_point = {
        "timestamp": current_timestamp,
        "cpuPercent": cpu_usage,
        "memoryPercent": (mem_used / mem_total * 100) if mem_total else 0,
        "swapPercent": (swap_used / swap_total * 100) if swap_total else 0,
    }
    if not history or history[-1]["timestamp"] != current_timestamp:
        history.append(current_history_point)
    history = history[-SYSTEM_HISTORY_MAX:]

    return {
        "hostname": socket.gethostname(),
        "kernel": platform.release(),
        "platform": platform.platform(),
        "cpu": {
            "usagePercent": cpu_usage,
            "cores": os.cpu_count() or 1,
            "loadAverage": {
                "one": load_averages[0],
                "five": load_averages[1],
                "fifteen": load_averages[2],
            },
        },
        "memory": {
            "total": mem_total,
            "used": mem_used,
            "available": mem_available,
            "percentUsed": (mem_used / mem_total * 100) if mem_total else 0,
        },
        "swap": {
            "total": swap_total,
            "used": swap_used,
            "free": swap_free,
            "percentUsed": (swap_used / swap_total * 100) if swap_total else 0,
        },
        "uptimeSeconds": max(0, int(time.time() - BOOT_TIME)),
        "storage": [
            disk_snapshot("/", "System"),
            disk_snapshot(str(SHARE_ROOT), "Share Drive"),
        ],
        "services": [
            service_state("airsite"),
            service_state("nginx"),
        ],
        "history": history,
        "timestamp": current_timestamp,
    }


def history_sample(cpu_usage: float | None = None, timestamp: int | None = None) -> dict:
    """Capture a single historical system-usage sample.

    Args:
        cpu_usage: Optional precomputed CPU percentage to reuse.
        timestamp: Optional Unix timestamp to associate with the sample.

    Returns:
        A dictionary containing CPU, memory, and swap percentages for one point in time.
    """
    meminfo = read_meminfo()
    mem_total = meminfo.get("MemTotal", 0)
    mem_available = meminfo.get("MemAvailable", 0)
    mem_used = max(0, mem_total - mem_available)
    swap_total = meminfo.get("SwapTotal", 0)
    swap_free = meminfo.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)

    return {
        "timestamp": timestamp if timestamp is not None else int(time.time()),
        "cpuPercent": sample_cpu_usage() if cpu_usage is None else cpu_usage,
        "memoryPercent": (mem_used / mem_total * 100) if mem_total else 0,
        "swapPercent": (swap_used / swap_total * 100) if swap_total else 0,
    }


def start_system_history_sampler() -> None:
    """Start a background thread that keeps the in-memory history buffer fresh."""
    def sampler() -> None:
        """Collect periodic history samples and append them to the shared deque."""
        previous_cpu_times = read_cpu_times()
        with SYSTEM_HISTORY_LOCK:
            if not SYSTEM_HISTORY:
                SYSTEM_HISTORY.append(history_sample())

        while True:
            time.sleep(SYSTEM_HISTORY_INTERVAL)
            current_cpu_times = read_cpu_times()
            cpu_usage = read_cpu_usage_from_last_sample(previous_cpu_times, current_cpu_times)
            previous_cpu_times = current_cpu_times
            sample = history_sample(cpu_usage=cpu_usage)
            with SYSTEM_HISTORY_LOCK:
                SYSTEM_HISTORY.append(sample)

    thread = threading.Thread(target=sampler, name="system-history-sampler", daemon=True)
    thread.start()


class AirSiteHandler(BaseHTTPRequestHandler):
    server_version = "AirSite/1.0"

    def do_GET(self) -> None:
        """Route incoming GET requests to the matching AirSite page or API handler."""
        parsed = urlparse(self.path)

        if parsed.path == "/":
            return self.serve_index()
        if parsed.path == "/api/list":
            return self.handle_list(parsed.query)
        if parsed.path == "/api/download":
            return self.handle_download(parsed.query)
        if parsed.path == "/api/media/list":
            return self.handle_media_list(parsed.query)
        if parsed.path == "/api/media/art":
            return self.handle_media_art(parsed.query)
        if parsed.path == "/api/media/stream":
            return self.handle_media_stream(parsed.query)
        if parsed.path == "/api/gba/list":
            return self.handle_gba_list(parsed.query)
        if parsed.path == "/api/gba/rom":
            return self.handle_gba_rom(parsed.query)
        if parsed.path == "/api/gba/save":
            return self.handle_gba_save_get(parsed.query)
        if parsed.path == "/gba-player":
            return self.handle_gba_player(parsed.query)
        if parsed.path == "/api/system":
            return self.handle_system()
        if parsed.path == "/api/network":
            return self.handle_network()

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:
        """Route incoming POST requests to the matching upload or mutation handler."""
        parsed = urlparse(self.path)

        if parsed.path == "/api/upload":
            return self.handle_upload(parsed.query)
        if parsed.path == "/api/folder":
            return self.handle_create_folder(parsed.query)
        if parsed.path == "/api/gba/save":
            return self.handle_gba_save_post(parsed.query)

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def serve_index(self) -> None:
        """Send the main AirSite HTML document to the client."""
        try:
            body = INDEX_FILE.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND, "index.html not found")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_list(self, query: str) -> None:
        """List folders and files within the shared drive.

        Args:
            query: Raw query string containing an optional relative `path`.
        """
        params = parse_qs(query)
        requested_path = params.get("path", [""])[0]

        try:
            target_dir, relative = resolve_share_path(requested_path)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        if not target_dir.exists():
            return self.send_json({"error": "Folder does not exist."}, status=HTTPStatus.NOT_FOUND)
        if not target_dir.is_dir():
            return self.send_json({"error": "Target is not a folder."}, status=HTTPStatus.BAD_REQUEST)

        try:
            entries = []
            for entry in sorted(os.scandir(target_dir), key=human_sort_key):
                item_relative = f"{relative}/{entry.name}" if relative else entry.name
                size = None if entry.is_dir() else entry.stat().st_size
                entries.append(
                    {
                        "name": entry.name,
                        "kind": "directory" if entry.is_dir() else "file",
                        "size": size,
                        "path": item_relative,
                    }
                )
        except PermissionError:
            return self.send_json({"error": "Permission denied while reading the folder."}, status=HTTPStatus.FORBIDDEN)

        return self.send_json({"currentPath": relative, "entries": entries})

    def handle_upload(self, query: str) -> None:
        """Accept multipart file uploads into a shared-drive folder.

        Args:
            query: Raw query string containing an optional destination `path`.
        """
        params = parse_qs(query)
        requested_path = params.get("path", [""])[0]

        try:
            target_dir, _relative = resolve_share_path(requested_path)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        if not target_dir.exists():
            return self.send_json({"error": "Folder does not exist."}, status=HTTPStatus.NOT_FOUND)
        if not target_dir.is_dir():
            return self.send_json({"error": "Upload destination must be a folder."}, status=HTTPStatus.BAD_REQUEST)

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send_json({"error": "Invalid Content-Length header."}, status=HTTPStatus.BAD_REQUEST)

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return self.send_json(
                {"error": "Uploads must use multipart/form-data."},
                status=HTTPStatus.BAD_REQUEST,
            )

        body = self.rfile.read(length)
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
        )

        uploaded = []

        for part in message.iter_parts():
            if part.get_param("name", header="content-disposition") != "files":
                continue

            safe_name = os.path.basename(part.get_filename() or "")
            if not safe_name:
                continue

            destination = target_dir / safe_name
            payload = part.get_payload(decode=True) or b""

            try:
                with destination.open("wb") as output:
                    output.write(payload)
            except PermissionError:
                return self.send_json(
                    {"error": f"Permission denied while writing {safe_name}."},
                    status=HTTPStatus.FORBIDDEN,
                )

            uploaded.append(safe_name)

        if not uploaded:
            return self.send_json({"error": "No files were received."}, status=HTTPStatus.BAD_REQUEST)

        return self.send_json({"message": f"Uploaded {len(uploaded)} file(s): {', '.join(uploaded)}"})

    def handle_download(self, query: str) -> None:
        """Stream a shared-drive file as a download response.

        Args:
            query: Raw query string containing the file `path` to download.
        """
        params = parse_qs(query)
        requested_path = params.get("path", [""])[0]

        try:
            target_file, _relative = resolve_share_path(requested_path)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        if not target_file.exists():
            return self.send_json({"error": "File does not exist."}, status=HTTPStatus.NOT_FOUND)
        if not target_file.is_file():
            return self.send_json({"error": "Download target must be a file."}, status=HTTPStatus.BAD_REQUEST)

        content_type, _encoding = mimetypes.guess_type(target_file.name)
        content_type = content_type or "application/octet-stream"

        try:
            file_size = target_file.stat().st_size
            with target_file.open("rb") as source:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(file_size))
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename*=UTF-8''{quote(target_file.name)}",
                )
                self.end_headers()

                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except PermissionError:
            return self.send_json({"error": "Permission denied while reading the file."}, status=HTTPStatus.FORBIDDEN)

    def handle_media_list(self, query: str) -> None:
        """List music folders and playable tracks within the music root.

        Args:
            query: Raw query string containing an optional relative `path`.
        """
        params = parse_qs(query)
        requested_path = params.get("path", [""])[0]

        try:
            target_dir, relative = resolve_rooted_path(MUSIC_ROOT, requested_path)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        if not target_dir.exists():
            return self.send_json({"error": "Folder does not exist."}, status=HTTPStatus.NOT_FOUND)
        if not target_dir.is_dir():
            return self.send_json({"error": "Target is not a folder."}, status=HTTPStatus.BAD_REQUEST)

        try:
            entries = []
            for entry in sorted(os.scandir(target_dir), key=human_sort_key):
                entry_path = Path(entry.path)
                if not entry.is_dir() and not is_audio_file(entry_path):
                    continue

                item_relative = f"{relative}/{entry.name}" if relative else entry.name
                art_path = find_album_art(entry_path if entry.is_dir() else entry_path.parent)
                art_relative = None
                if art_path is not None:
                    art_relative = str(art_path.relative_to(MUSIC_ROOT)).replace(os.sep, "/")
                entries.append(
                    {
                        "name": entry.name,
                        "kind": "directory" if entry.is_dir() else "track",
                        "size": None if entry.is_dir() else entry.stat().st_size,
                        "path": item_relative,
                        "format": entry_path.suffix.lower().lstrip(".") if not entry.is_dir() else None,
                        "artPath": art_relative,
                    }
                )
        except PermissionError:
            return self.send_json({"error": "Permission denied while reading the music folder."}, status=HTTPStatus.FORBIDDEN)

        return self.send_json({"currentPath": relative, "entries": entries, "rootLabel": "/share/Music"})

    def handle_media_art(self, query: str) -> None:
        """Return an album-art image from the music library.

        Args:
            query: Raw query string containing the artwork `path`.
        """
        params = parse_qs(query)
        requested_path = params.get("path", [""])[0]

        try:
            target_file, _relative = resolve_rooted_path(MUSIC_ROOT, requested_path)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        if not target_file.exists():
            return self.send_json({"error": "Artwork does not exist."}, status=HTTPStatus.NOT_FOUND)
        if not target_file.is_file() or not is_art_file(target_file):
            return self.send_json({"error": "Artwork target must be an image file."}, status=HTTPStatus.BAD_REQUEST)

        content_type, _encoding = mimetypes.guess_type(target_file.name)
        content_type = content_type or "image/jpeg"

        try:
            body = target_file.read_bytes()
        except PermissionError:
            return self.send_json({"error": "Permission denied while reading the artwork."}, status=HTTPStatus.FORBIDDEN)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_media_stream(self, query: str) -> None:
        """Stream an audio file, including HTTP range support for seeking.

        Args:
            query: Raw query string containing the track `path`.
        """
        params = parse_qs(query)
        requested_path = params.get("path", [""])[0]

        try:
            target_file, _relative = resolve_rooted_path(MUSIC_ROOT, requested_path)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        if not target_file.exists():
            return self.send_json({"error": "Track does not exist."}, status=HTTPStatus.NOT_FOUND)
        if not target_file.is_file() or not is_audio_file(target_file):
            return self.send_json({"error": "Stream target must be an audio file."}, status=HTTPStatus.BAD_REQUEST)

        content_type, _encoding = mimetypes.guess_type(target_file.name)
        if target_file.suffix.lower() == ".flac":
            content_type = "audio/flac"
        content_type = content_type or "application/octet-stream"

        try:
            file_size = target_file.stat().st_size
            range_header = self.headers.get("Range")
            start = 0
            end = file_size - 1
            status = HTTPStatus.OK

            if range_header and range_header.startswith("bytes="):
                byte_range = range_header.split("=", 1)[1]
                start_text, _, end_text = byte_range.partition("-")
                if start_text:
                    start = int(start_text)
                if end_text:
                    end = int(end_text)
                end = min(end, file_size - 1)
                if start > end or start < 0:
                    return self.send_json({"error": "Invalid byte range."}, status=HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                status = HTTPStatus.PARTIAL_CONTENT

            chunk_length = end - start + 1
            with target_file.open("rb") as source:
                source.seek(start)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(chunk_length))
                if status == HTTPStatus.PARTIAL_CONTENT:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.end_headers()

                remaining = chunk_length
                while remaining > 0:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except PermissionError:
            return self.send_json({"error": "Permission denied while reading the track."}, status=HTTPStatus.FORBIDDEN)

    def handle_gba_list(self, query: str) -> None:
        """List GBA folders and ROM files, including save-file status.

        Args:
            query: Raw query string containing an optional relative `path`.
        """
        params = parse_qs(query)
        requested_path = params.get("path", [""])[0]

        try:
            target_dir, relative = resolve_rooted_path(GBA_ROOT, requested_path)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        if not target_dir.exists():
            return self.send_json({"error": "Folder does not exist."}, status=HTTPStatus.NOT_FOUND)
        if not target_dir.is_dir():
            return self.send_json({"error": "Target is not a folder."}, status=HTTPStatus.BAD_REQUEST)

        try:
            entries = []
            for entry in sorted(os.scandir(target_dir), key=human_sort_key):
                entry_path = Path(entry.path)
                if entry.name.startswith("."):
                    continue
                if not entry.is_dir() and not is_gba_rom(entry_path):
                    continue

                item_relative = f"{relative}/{entry.name}" if relative else entry.name
                save_relative = None
                save_exists = False
                if entry.is_file():
                    _save_path, save_relative = gba_save_path_for_rom(item_relative)
                    save_exists = _save_path.exists()

                entries.append(
                    {
                        "name": entry.name,
                        "kind": "directory" if entry.is_dir() else "rom",
                        "size": None if entry.is_dir() else entry.stat().st_size,
                        "path": item_relative,
                        "savePath": save_relative,
                        "saveExists": save_exists,
                    }
                )
        except PermissionError:
            return self.send_json({"error": "Permission denied while reading the ROM folder."}, status=HTTPStatus.FORBIDDEN)

        return self.send_json(
            {
                "currentPath": relative,
                "entries": entries,
                "rootLabel": "/share/ROMs/GBA",
                "saveRootLabel": "/share/ROMs/GBA/.airsite-saves",
            }
        )

    def handle_gba_rom(self, query: str) -> None:
        """Stream a GBA ROM file to the browser-based emulator.

        Args:
            query: Raw query string containing the ROM `path`.
        """
        params = parse_qs(query)
        requested_path = params.get("path", [""])[0]

        try:
            target_file, _relative = resolve_rooted_path(GBA_ROOT, requested_path)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        if not target_file.exists():
            return self.send_json({"error": "ROM does not exist."}, status=HTTPStatus.NOT_FOUND)
        if not target_file.is_file() or not is_gba_rom(target_file):
            return self.send_json({"error": "ROM target must be a .gba file."}, status=HTTPStatus.BAD_REQUEST)

        try:
            file_size = target_file.stat().st_size
            with target_file.open("rb") as source:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(file_size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(target_file.name)}")
                self.end_headers()

                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except PermissionError:
            return self.send_json({"error": "Permission denied while reading the ROM."}, status=HTTPStatus.FORBIDDEN)

    def handle_gba_save_get(self, query: str) -> None:
        """Return an existing server-side save file for a ROM if one exists.

        Args:
            query: Raw query string containing the ROM `path`.
        """
        params = parse_qs(query)
        requested_path = params.get("path", [""])[0]

        try:
            _rom_file, rom_relative = resolve_rooted_path(GBA_ROOT, requested_path)
            save_path, save_relative = gba_save_path_for_rom(rom_relative)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        if save_path.exists():
            try:
                body = save_path.read_bytes()
                modified = int(save_path.stat().st_mtime)
            except PermissionError:
                return self.send_json({"error": "Permission denied while reading the save file."}, status=HTTPStatus.FORBIDDEN)

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-AirSite-Save-Path", save_relative)
            self.send_header("X-AirSite-Save-Modified", str(modified))
            self.end_headers()
            self.wfile.write(body)
            return

        return self.send_json(
            {
                "exists": False,
                "savePath": save_relative,
                "message": "No server-side save exists for this ROM yet.",
            },
            status=HTTPStatus.NOT_FOUND,
        )

    def handle_gba_save_post(self, query: str) -> None:
        """Persist an uploaded emulator save file on the server.

        Args:
            query: Raw query string containing the ROM `path`.
        """
        params = parse_qs(query)
        requested_path = params.get("path", [""])[0]

        try:
            _rom_file, rom_relative = resolve_rooted_path(GBA_ROOT, requested_path)
            save_path, save_relative = gba_save_path_for_rom(rom_relative)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send_json({"error": "Invalid Content-Length header."}, status=HTTPStatus.BAD_REQUEST)

        payload = self.rfile.read(length)
        if not payload:
            return self.send_json({"error": "Save payload is empty."}, status=HTTPStatus.BAD_REQUEST)

        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with save_path.open("wb") as output:
                output.write(payload)
        except PermissionError:
            return self.send_json({"error": "Permission denied while writing the save file."}, status=HTTPStatus.FORBIDDEN)

        return self.send_json(
            {
                "message": "Save synced to server.",
                "savePath": save_relative,
                "bytes": len(payload),
            },
            status=HTTPStatus.CREATED,
        )

    def handle_gba_player(self, query: str) -> None:
        """Render a standalone emulator page for a selected GBA ROM.

        Args:
            query: Raw query string containing the ROM `path` and optional `volume`.
        """
        params = parse_qs(query)
        requested_path = params.get("path", [""])[0]
        volume_text = params.get("volume", ["0.65"])[0]

        try:
            volume = max(0.0, min(1.0, float(volume_text)))
        except ValueError:
            volume = 0.65

        try:
            target_file, rom_relative = resolve_rooted_path(GBA_ROOT, requested_path)
            save_path, save_relative = gba_save_path_for_rom(rom_relative)
        except ValueError as exc:
            body = f"<h1>Unable to load GBA player</h1><p>{exc}</p>".encode("utf-8")
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not target_file.exists() or not target_file.is_file() or not is_gba_rom(target_file):
            body = b"<h1>Unable to load GBA player</h1><p>ROM not found.</p>"
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        rom_name = target_file.stem
        rom_name_html = escape(rom_name)
        rom_relative_html = escape(rom_relative)
        save_relative_html = escape(save_relative)
        rom_url = f"/api/gba/rom?path={quote(rom_relative)}"
        save_url = f"/api/gba/save?path={quote(rom_relative)}"
        external_files: dict[str, str] = {}
        if save_path.exists():
            external_files[f"/userdata/saves/{rom_name}.sav"] = save_url
            external_files[f"/userdata/saves/{rom_name}.srm"] = save_url

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{rom_name_html} - AirSite GBA</title>
  <style>
    html, body {{
      margin: 0;
      min-height: 100%;
      background: #050b15;
      color: #eff6ff;
      font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    }}

    .frame {{
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: 14px;
      padding: 14px;
      background:
        radial-gradient(circle at top left, rgba(56, 189, 248, 0.18), transparent 26%),
        linear-gradient(180deg, #07111f, #0a1526);
    }}

    .hero {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 16px;
      border: 1px solid rgba(125, 211, 252, 0.14);
      border-radius: 18px;
      background: rgba(8, 16, 31, 0.78);
    }}

    .title {{
      font-size: 1rem;
      font-weight: 700;
    }}

    .meta {{
      font-size: 0.86rem;
      color: #9fb4d3;
    }}

    .pill {{
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(56, 189, 248, 0.1);
      color: #d8f5ff;
      font-size: 0.82rem;
      border: 1px solid rgba(125, 211, 252, 0.16);
    }}

    .game-shell {{
      min-height: 0;
      display: flex;
      align-items: center;
      justify-content: center;
    }}

    #game {{
      width: min(100%, 1100px);
      height: min(72vh, 760px);
      border-radius: 22px;
      overflow: hidden;
      border: 1px solid rgba(125, 211, 252, 0.16);
      background: rgba(4, 10, 20, 0.94);
      box-shadow: 0 24px 80px rgba(2, 6, 23, 0.42);
    }}

    .status {{
      padding: 12px 14px;
      border-radius: 16px;
      border: 1px solid rgba(125, 211, 252, 0.12);
      background: rgba(8, 16, 31, 0.74);
      color: #9fb4d3;
      font-size: 0.88rem;
    }}
  </style>
</head>
<body>
  <div class="frame">
    <div class="hero">
      <div>
        <div class="title">{rom_name_html}</div>
        <div class="meta">ROM: /share/ROMs/GBA/{rom_relative_html}</div>
      </div>
      <div class="pill">Server save: /share/ROMs/GBA/.airsite-saves/{save_relative_html}</div>
    </div>
    <div class="game-shell">
      <div id="game"></div>
    </div>
    <div class="status" id="syncStatus">Launching emulator...</div>
  </div>

  <script>
    const syncStatus = document.getElementById("syncStatus");

    // Updates the embedded emulator status line with the latest sync message.
    // Input: `message` is the text shown below the player. Output: none.
    function setSyncStatus(message) {{
      syncStatus.textContent = message;
    }}

    EJS_player = "#game";
    EJS_core = "gba";
    EJS_gameName = {json.dumps(rom_name)};
    EJS_gameUrl = {json.dumps(rom_url)};
    EJS_pathtodata = "https://cdn.emulatorjs.org/stable/data/";
    EJS_startOnLoaded = true;
    EJS_color = "#38bdf8";
    EJS_volume = {json.dumps(volume)};
    EJS_fixedSaveInterval = 7000;
    EJS_backgroundColor = "#07111f";
    EJS_externalFiles = {json.dumps(external_files)};
    EJS_Buttons = {{
      fullscreen: true,
      volume: true,
      saveSavFiles: true,
      loadSavFiles: true
    }};
    // Uploads the latest emulator save buffer back to the server.
    // Input: `event.save` contains the raw save bytes. Output: none.
    EJS_onSaveUpdate = async function(event) {{
      try {{
        const response = await fetch({json.dumps(save_url)}, {{
          method: "POST",
          headers: {{
            "Content-Type": "application/octet-stream"
          }},
          body: event.save
        }});

        if (!response.ok) {{
          throw new Error("Server rejected the save sync.");
        }}

        setSyncStatus("Save synced to /share/ROMs/GBA/.airsite-saves/{save_relative_html}");
      }} catch (error) {{
        setSyncStatus(error.message || "Save sync failed.");
      }}
    }};
    // Announces that the emulator has started running the game.
    // Input: none. Output: none.
    EJS_onGameStart = function() {{
      setSyncStatus("Game running. In-game saves sync back to the server automatically.");
    }};
    // Announces that the emulator loader finished bootstrapping the ROM.
    // Input: none. Output: none.
    EJS_ready = function() {{
      setSyncStatus("Emulator ready. Launching {rom_name}...");
    }};
  </script>
  <script src="https://cdn.emulatorjs.org/stable/data/loader.js"></script>
</body>
</html>
"""
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_create_folder(self, query: str) -> None:
        """Create a new folder beneath the current shared-drive directory.

        Args:
            query: Raw query string containing an optional parent `path`.
        """
        params = parse_qs(query)
        requested_path = params.get("path", [""])[0]

        try:
            target_dir, relative = resolve_share_path(requested_path)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        if not target_dir.exists():
            return self.send_json({"error": "Parent folder does not exist."}, status=HTTPStatus.NOT_FOUND)
        if not target_dir.is_dir():
            return self.send_json({"error": "Parent path must be a folder."}, status=HTTPStatus.BAD_REQUEST)

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send_json({"error": "Invalid Content-Length header."}, status=HTTPStatus.BAD_REQUEST)

        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "Request body must be valid JSON."}, status=HTTPStatus.BAD_REQUEST)

        raw_name = str(payload.get("name", "")).strip()
        if not raw_name:
            return self.send_json({"error": "Folder name is required."}, status=HTTPStatus.BAD_REQUEST)
        if "/" in raw_name or "\\" in raw_name:
            return self.send_json({"error": "Folder name cannot include slashes."}, status=HTTPStatus.BAD_REQUEST)

        try:
            folder_name = clean_relative_path(raw_name)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        if not folder_name:
            return self.send_json({"error": "Folder name is required."}, status=HTTPStatus.BAD_REQUEST)

        destination = (target_dir / folder_name).resolve()
        if os.path.commonpath([SHARE_ROOT, destination]) != str(SHARE_ROOT):
            return self.send_json({"error": "Destination is outside the shared drive."}, status=HTTPStatus.BAD_REQUEST)
        if destination.exists():
            return self.send_json({"error": "A file or folder with that name already exists."}, status=HTTPStatus.CONFLICT)

        try:
            destination.mkdir()
        except PermissionError:
            return self.send_json({"error": "Permission denied while creating the folder."}, status=HTTPStatus.FORBIDDEN)

        created_path = f"{relative}/{folder_name}" if relative else folder_name
        return self.send_json({"message": f"Created folder: {folder_name}", "path": created_path}, status=HTTPStatus.CREATED)

    def handle_system(self) -> None:
        """Return the latest system metrics as JSON."""
        try:
            payload = system_snapshot()
        except (FileNotFoundError, OSError, ValueError) as exc:
            return self.send_json({"error": f"Unable to read system metrics: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        return self.send_json(payload)

    def handle_network(self) -> None:
        """Return the latest network snapshot as JSON."""
        try:
            payload = network_snapshot()
        except (FileNotFoundError, OSError, ValueError) as exc:
            return self.send_json({"error": f"Unable to read network details: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        return self.send_json(payload)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        """Serialize a dictionary as JSON and send it with the given HTTP status.

        Args:
            payload: JSON-serializable response body.
            status: HTTP status code to send with the response.
        """
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    """Parse CLI options, validate paths, and start the threaded HTTP server."""
    parser = argparse.ArgumentParser(description="Serve AirSite with AirServer upload support.")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind to.")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on.")
    args = parser.parse_args()

    if not SHARE_ROOT.exists():
        raise SystemExit(f"Share root does not exist: {SHARE_ROOT}")

    start_system_history_sampler()
    server = ThreadingHTTPServer((args.host, args.port), AirSiteHandler)
    print(f"Serving AirSite on http://{args.host}:{args.port}")
    print(f"Shared root: {SHARE_ROOT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
