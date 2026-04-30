#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import requests


GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class SyncError(RuntimeError):
    pass


@dataclass
class SharePointTarget:
    source_url: str
    site_id: str
    drive_id: str
    folder_path: str


def env_first(names: Iterable[str]) -> Tuple[str, str]:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return name, value
    raise SyncError(f"Missing required secret. Tried: {', '.join(names)}")


def get_access_token() -> str:
    _, tenant_id = env_first(["TENANT_ID"])
    _, client_id = env_first(["CLIENT_ID"])
    _, client_secret = env_first(["CLIENT_SECRET"])

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    response = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=60,
    )
    if response.status_code >= 400:
        raise SyncError(
            f"Token request failed ({response.status_code}): {response.text[:600]}"
        )
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise SyncError("Token response did not contain access_token")
    return token


def graph_request(
    token: str,
    method: str,
    endpoint_or_url: str,
    *,
    stream: bool = False,
    **kwargs,
) -> requests.Response:
    url = endpoint_or_url
    if not endpoint_or_url.startswith("http"):
        url = f"{GRAPH_BASE}{endpoint_or_url}"

    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {token}"
    response = requests.request(
        method,
        url,
        headers=headers,
        timeout=120,
        stream=stream,
        **kwargs,
    )
    if response.status_code >= 400:
        raise SyncError(f"Graph call failed ({response.status_code}) {url}: {response.text[:800]}")
    return response


def ensure_url(raw: str) -> str:
    cleaned = raw.strip()
    if not cleaned:
        raise SyncError("SharePoint folder link is empty")
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    parsed = urlparse(cleaned)
    if not parsed.netloc:
        raise SyncError(f"Invalid SharePoint link: {raw}")
    path = unquote(parsed.path or "")
    if not path.startswith("/"):
        path = f"/{path}"
    return parsed._replace(path=path).geturl()


def first_site_anchor(segments: List[str]) -> int:
    for index, part in enumerate(segments):
        if part in {"sites", "teams"}:
            return index
    raise SyncError(
        "SharePoint link must include /sites/<siteName>/... or /teams/<teamName>/..."
    )


def resolve_site_and_library(
    token: str,
    source_url: str,
) -> SharePointTarget:
    parsed = urlparse(source_url)
    host = parsed.netloc
    path = unquote(parsed.path or "")
    segments = [seg for seg in path.strip("/").split("/") if seg]
    anchor = first_site_anchor(segments)

    site_id: Optional[str] = None
    doc_path: Optional[str] = None
    matched_site_path: Optional[str] = None

    for end in range(len(segments), anchor + 1, -1):
        site_path = "/" + "/".join(segments[anchor:end])
        remainder = segments[end:]
        if not remainder:
            continue
        site_encoded = quote(site_path, safe="/")
        endpoint = f"/sites/{host}:{site_encoded}?$select=id"
        response = requests.get(
            f"{GRAPH_BASE}{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if response.status_code == 200:
            site_id = response.json()["id"]
            doc_path = "/".join(remainder)
            matched_site_path = site_path
            break

    if not site_id or not doc_path or not matched_site_path:
        raise SyncError(f"Could not resolve SharePoint site from URL: {source_url}")

    drives: List[Dict[str, str]] = []
    next_url = f"{GRAPH_BASE}/sites/{site_id}/drives?$select=id,name,webUrl"
    while next_url:
        response = graph_request(token, "GET", next_url)
        payload = response.json()
        drives.extend(payload.get("value", []))
        next_url = payload.get("@odata.nextLink")

    doc_path_norm = doc_path.strip("/")
    doc_path_norm_lower = doc_path_norm.lower()
    site_path_norm = matched_site_path.strip("/").lower()

    selected_drive_id: Optional[str] = None
    selected_drive_prefix: Optional[str] = None

    for drive in drives:
        drive_web_url = drive.get("webUrl", "")
        drive_web_path = unquote(urlparse(drive_web_url).path or "").strip("/")
        drive_parts = drive_web_path.split("/")
        if site_path_norm and "/".join(part.lower() for part in drive_parts[: len(site_path_norm.split("/"))]) != site_path_norm:
            continue

        rel_parts = drive_parts[len(site_path_norm.split("/")) :]
        if not rel_parts:
            continue
        drive_prefix = "/".join(rel_parts)
        prefix_lower = drive_prefix.lower()

        if doc_path_norm_lower == prefix_lower:
            selected_drive_id = drive["id"]
            selected_drive_prefix = drive_prefix
            break
        if doc_path_norm_lower.startswith(prefix_lower + "/"):
            selected_drive_id = drive["id"]
            selected_drive_prefix = drive_prefix
            break

    if not selected_drive_id:
        for drive in drives:
            drive_name = (drive.get("name") or "").strip("/")
            if not drive_name:
                continue
            name_lower = drive_name.lower()
            if doc_path_norm_lower == name_lower or doc_path_norm_lower.startswith(name_lower + "/"):
                selected_drive_id = drive["id"]
                selected_drive_prefix = drive_name
                break

    if not selected_drive_id or not selected_drive_prefix:
        available = ", ".join(d.get("name", "<unknown>") for d in drives)
        raise SyncError(
            f"Could not map document library for URL: {source_url}. "
            f"Available libraries: {available}"
        )

    folder_path = doc_path_norm[len(selected_drive_prefix) :].strip("/")
    return SharePointTarget(
        source_url=source_url,
        site_id=site_id,
        drive_id=selected_drive_id,
        folder_path=folder_path,
    )


def list_children_page(token: str, drive_id: str, folder_path: str, next_url: Optional[str]) -> Dict:
    if next_url:
        response = graph_request(token, "GET", next_url)
        return response.json()

    if folder_path:
        encoded = quote(folder_path, safe="/")
        endpoint = (
            f"/drives/{drive_id}/root:/{encoded}:/children"
            "?$select=id,name,file,folder&$top=200"
        )
    else:
        endpoint = f"/drives/{drive_id}/root/children?$select=id,name,file,folder&$top=200"

    response = graph_request(token, "GET", endpoint)
    return response.json()


def list_source_files(token: str, drive_id: str, root_folder: str) -> Dict[str, str]:
    files: Dict[str, str] = {}
    queue: List[Tuple[str, str]] = [("", root_folder.strip("/"))]

    while queue:
        rel_prefix, folder_path = queue.pop(0)
        next_url: Optional[str] = None
        while True:
            payload = list_children_page(token, drive_id, folder_path, next_url)
            for item in payload.get("value", []):
                name = item.get("name")
                if not name:
                    continue
                item_rel = f"{rel_prefix}/{name}".strip("/")
                if item.get("folder") is not None:
                    nested_folder = f"{folder_path}/{name}".strip("/")
                    queue.append((item_rel, nested_folder))
                elif item.get("file") is not None:
                    files[item_rel] = item["id"]
            next_url = payload.get("@odata.nextLink")
            if not next_url:
                break

    return files


def download_file(token: str, drive_id: str, item_id: str, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    response = graph_request(
        token,
        "GET",
        f"/drives/{drive_id}/items/{item_id}/content",
        stream=True,
    )
    with target_path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)


def cleanup_empty_dirs(path: Path) -> None:
    for current_root, dirs, files in os.walk(path, topdown=False):
        if files:
            continue
        if dirs:
            continue
        root_path = Path(current_root)
        if root_path == path:
            continue
        root_path.rmdir()


def find_destination_dirs(repo_root: Path, folder_name: str) -> List[Path]:
    matches: List[Path] = []
    for current_root, dirs, _ in os.walk(repo_root):
        root_path = Path(current_root)
        dirs[:] = [d for d in dirs if d != ".git"]
        if root_path.name == folder_name:
            matches.append(root_path)
    return matches


def load_mappings(repo_root: Path) -> List[Tuple[str, str, Path]]:
    addresses_dir = repo_root / "addresses"
    if not addresses_dir.is_dir():
        raise SyncError(f"Missing addresses directory: {addresses_dir}")

    mappings: List[Tuple[str, str, Path]] = []
    for txt_path in sorted(addresses_dir.glob("*.txt")):
        destination_name = txt_path.stem.strip()
        source_link = txt_path.read_text(encoding="utf-8").strip()
        if not destination_name:
            raise SyncError(f"Invalid mapping file name: {txt_path.name}")
        if not source_link:
            raise SyncError(f"Mapping file has empty source link: {txt_path.name}")

        matches = find_destination_dirs(repo_root, destination_name)
        if len(matches) == 0:
            raise SyncError(
                f"Destination folder '{destination_name}' from {txt_path.name} not found in repo."
            )
        if len(matches) > 1:
            examples = ", ".join(str(m.relative_to(repo_root)) for m in matches[:5])
            raise SyncError(
                f"Destination folder '{destination_name}' is ambiguous ({len(matches)} matches): {examples}"
            )

        mappings.append((destination_name, source_link, matches[0]))
    return mappings


def sync_one_mapping(
    token: str,
    destination_name: str,
    source_link: str,
    destination_dir: Path,
) -> Tuple[int, int, int]:
    if not destination_dir.is_dir():
        raise SyncError(
            f"Destination folder for '{destination_name}' must exist and will not be created: "
            f"{destination_dir}"
        )

    normalized_url = ensure_url(source_link)
    sp_target = resolve_site_and_library(token, normalized_url)
    source_files = list_source_files(token, sp_target.drive_id, sp_target.folder_path)
    source_rel_paths = set(source_files.keys())

    local_files: Dict[str, Path] = {}
    for path in destination_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(destination_dir).as_posix()
        local_files[rel] = path

    updated = 0
    created = 0
    deleted = 0

    for rel_path, item_id in sorted(source_files.items()):
        target_path = destination_dir / Path(rel_path)
        before_exists = target_path.exists()
        download_file(token, sp_target.drive_id, item_id, target_path)
        if before_exists:
            updated += 1
        else:
            created += 1

    for rel_path, local_path in sorted(local_files.items()):
        if rel_path not in source_rel_paths:
            local_path.unlink()
            deleted += 1

    cleanup_empty_dirs(destination_dir)
    print(
        f"[sync] {destination_name}: source={len(source_files)} created={created} "
        f"updated={updated} deleted={deleted}"
    )
    return len(source_files), created, updated + deleted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sync media from SharePoint folders to repository folders using "
            "addresses/*.txt mapping files."
        )
    )
    parser.add_argument(
        "--repo-root",
        default=Path(__file__).resolve().parents[1],
        type=Path,
        help="Repository root path (defaults to project root).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    token = get_access_token()

    mappings = load_mappings(repo_root)
    if not mappings:
        raise SyncError("No mapping files found under addresses/*.txt")

    print(f"[sync] mappings found: {len(mappings)}")
    totals = {"source_files": 0, "created": 0, "changed_or_deleted": 0}
    for destination_name, source_link, destination_dir in mappings:
        source_count, created, changed_or_deleted = sync_one_mapping(
            token,
            destination_name,
            source_link,
            destination_dir,
        )
        totals["source_files"] += source_count
        totals["created"] += created
        totals["changed_or_deleted"] += changed_or_deleted

    print(
        "[sync] done: "
        f"source_files={totals['source_files']} "
        f"created={totals['created']} "
        f"changed_or_deleted={totals['changed_or_deleted']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as error:
        print(f"[sync] ERROR: {error}")
        raise SystemExit(1)
