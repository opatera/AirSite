#!/usr/bin/env python3

import argparse
import json
import mimetypes
import os
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parent
INDEX_FILE = REPO_ROOT / "index.html"
SHARE_ROOT = Path("/media/airowen/Storage/share").resolve()


def clean_relative_path(raw_path: str) -> str:
    parts = [part for part in raw_path.replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("Path traversal is not allowed.")
    return "/".join(parts)


def resolve_share_path(raw_path: str) -> tuple[Path, str]:
    relative = clean_relative_path(raw_path)
    destination = (SHARE_ROOT / relative).resolve()

    if os.path.commonpath([SHARE_ROOT, destination]) != str(SHARE_ROOT):
        raise ValueError("Destination is outside the shared drive.")

    return destination, relative


def human_sort_key(entry: os.DirEntry) -> tuple[int, str]:
    return (0 if entry.is_dir() else 1, entry.name.lower())


class AirSiteHandler(BaseHTTPRequestHandler):
    server_version = "AirSite/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/":
            return self.serve_index()
        if parsed.path == "/api/list":
            return self.handle_list(parsed.query)
        if parsed.path == "/api/download":
            return self.handle_download(parsed.query)

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/upload":
            return self.handle_upload(parsed.query)
        if parsed.path == "/api/folder":
            return self.handle_create_folder(parsed.query)

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def serve_index(self) -> None:
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

    def handle_create_folder(self, query: str) -> None:
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

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve AirSite with AirServer upload support.")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind to.")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on.")
    args = parser.parse_args()

    if not SHARE_ROOT.exists():
        raise SystemExit(f"Share root does not exist: {SHARE_ROOT}")

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
