#!/usr/bin/env python3
"""
Build SKU thumbnails from worker/images.csv and save them into:
  for_price/200/<sku>.webp
  for_price/300/<sku>.webp

- Keeps aspect ratio (no distortion)
- Resizes down so the longest side is <= target size
- Tries to keep files small (quality loop), logs warnings if it can't hit target
- Skips unchanged SKUs using worker/state.json (url hash)

CSV expected: 2 columns: sku,url
Delimiter can be comma, semicolon, or tab.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import requests
from PIL import Image, ImageOps


# -----------------------------
# Poka-yoke: safe printing
# -----------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 2) -> None:
    log(f"[FATAL] {msg}")
    sys.exit(code)


# -----------------------------
# Config
# -----------------------------
SKU_RE = re.compile(r"^\d+$")  # digits only


@dataclass
class Row:
    sku: str
    url: str


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def detect_csv_dialect(sample: str) -> csv.Dialect:
    # Robust: try sniff, fallback to common delimiters.
    sniffer = csv.Sniffer()
    try:
        return sniffer.sniff(sample, delimiters=[",", ";", "\t"])
    except Exception:
        class Fallback(csv.Dialect):
            delimiter = ","
            quotechar = '"'
            doublequote = True
            skipinitialspace = True
            lineterminator = "\n"
            quoting = csv.QUOTE_MINIMAL
        return Fallback()


def read_rows(csv_path: Path) -> List[Row]:
    if not csv_path.exists():
        die(f"CSV not found: {csv_path}")

    raw = csv_path.read_bytes()
    # Try UTF-8-sig for BOM safety
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")

    sample = text[:4096]
    dialect = detect_csv_dialect(sample)

    rows: List[Row] = []
    reader = csv.reader(io.StringIO(text), dialect=dialect)
    for i, parts in enumerate(reader, start=1):
        if not parts:
            continue

        # Support accidental extra columns: take first two non-empty tokens
        parts = [p.strip() for p in parts]
        parts = [p for p in parts if p != ""]
        if len(parts) < 2:
            log(f"[SKIP] Line {i}: not enough columns")
            continue

        sku, url = parts[0], parts[1]
        if sku.lower() == "sku" and url.lower() in ("url", "link"):
            # header row
            continue

        sku = sku.strip()
        url = url.strip()

        if not SKU_RE.match(sku):
            log(f"[SKIP] Line {i}: invalid SKU '{sku}' (digits only expected)")
            continue

        if not (url.startswith("http://") or url.startswith("https://")):
            log(f"[SKIP] Line {i}: invalid URL '{url}'")
            continue

        rows.append(Row(sku=sku, url=url))

    # Dedupe by SKU (last one wins) to avoid fighting yourself
    dedup: Dict[str, Row] = {}
    for r in rows:
        dedup[r.sku] = r

    return list(dedup.values())


def load_state(state_path: Path) -> Dict[str, Dict]:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            log(f"[WARN] State file is unreadable, starting fresh: {state_path}")
            return {}
    return {}


def save_state(state_path: Path, state: Dict[str, Dict]) -> None:
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def requests_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "images_gallery-thumb-worker/1.0 (+GitHub Actions)"
    })
    return s


def download_image_bytes(sess: requests.Session, url: str, retries: int = 3) -> bytes:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = sess.get(url, timeout=30)
            r.raise_for_status()
            return r.content
        except Exception as e:
            last_err = e
            log(f"[WARN] Download failed (attempt {attempt}/{retries}): {url} -> {e}")
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"Download failed after {retries} retries: {url} ({last_err})")


def make_thumb(img: Image.Image, max_side: int) -> Image.Image:
    # Fix orientation from EXIF (poka-yoke for phone images)
    img = ImageOps.exif_transpose(img)

    # Convert to RGB for consistent encoding
    if img.mode not in ("RGB",):
        img = img.convert("RGB")

    # Resize down keeping aspect ratio (no distortion)
    thumb = img.copy()
    thumb.thumbnail((max_side, max_side), resample=Image.Resampling.LANCZOS)
    return thumb


def save_webp_with_target_size(
    img: Image.Image,
    out_path: Path,
    target_min_kb: int = 1,
    target_max_kb: int = 5,
) -> Tuple[int, int]:
    """
    Save as WEBP, trying to land within [target_min_kb, target_max_kb].
    Returns (final_bytes, final_quality).
    """
    ensure_dir(out_path.parent)

    # Quality loop: start decent, reduce if too big
    qualities = [80, 70, 60, 50, 45, 40, 35, 30]
    best_bytes = None
    best_q = None

    for q in qualities:
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=q, method=6)  # method=6 = better compression
        data = buf.getvalue()
        size_kb = len(data) / 1024.0

        best_bytes = data
        best_q = q

        if target_min_kb <= size_kb <= target_max_kb:
            break
        if size_kb <= target_max_kb:
            # Small enough; stop early
            break

    # Atomic write
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_bytes(best_bytes)
    tmp.replace(out_path)

    return len(best_bytes), int(best_q)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="worker/images.csv", help="Path to images.csv")
    parser.add_argument("--out", default="for_price", help="Output root folder")
    parser.add_argument("--sizes", nargs="+", type=int, default=[200, 300], help="Thumbnail max side sizes")
    parser.add_argument("--state", default="worker/state.json", help="State JSON path")
    parser.add_argument("--force", action="store_true", help="Rebuild all thumbnails")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    out_root = Path(args.out)
    state_path = Path(args.state)
    sizes = sorted(set(args.sizes))

    if not sizes:
        die("No sizes provided")

    rows = read_rows(csv_path)
    if not rows:
        die("No valid rows found in CSV")

    state = load_state(state_path)
    sess = requests_session()

    processed = 0
    skipped = 0
    failed = 0

    log(f"[OK] Loaded {len(rows)} unique SKUs from {csv_path}")
    log(f"[OK] Target sizes: {sizes}")
    log(f"[OK] Output root: {out_root}")
    log("----")

    for r in rows:
        url_hash = sha256_hex(r.url)
        st = state.get(r.sku, {})
        unchanged = (st.get("url_hash") == url_hash)

        # Build expected output paths
        out_paths = [out_root / str(s) / f"{r.sku}.webp" for s in sizes]
        have_all = all(p.exists() for p in out_paths)

        if not args.force and unchanged and have_all:
            skipped += 1
            continue

        try:
            blob = download_image_bytes(sess, r.url)
            img = Image.open(io.BytesIO(blob))
        except Exception as e:
            failed += 1
            log(f"[FAIL] {r.sku}: cannot load image from URL -> {e}")
            continue

        for s, out_path in zip(sizes, out_paths):
            try:
                thumb = make_thumb(img, max_side=s)
                final_bytes, q = save_webp_with_target_size(thumb, out_path)
                kb = final_bytes / 1024.0
                if kb > 5.0:
                    log(f"[WARN] {r.sku} size {s}: {kb:.1f} KB (>{5} KB) even at quality={q}")
                else:
                    log(f"[OK] {r.sku} size {s}: {kb:.1f} KB (quality={q}) -> {out_path.as_posix()}")
            except Exception as e:
                failed += 1
                log(f"[FAIL] {r.sku} size {s}: cannot write thumb -> {e}")

        state[r.sku] = {
            "url": r.url,
            "url_hash": url_hash,
            "sizes": sizes,
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        processed += 1

    save_state(state_path, state)

    log("----")
    log(f"[DONE] processed={processed} skipped={skipped} failed={failed}")
    if failed == 0:
        log("[OK] Completed successfully ✅")
    else:
        # Fail the action so you notice broken URLs
        die(f"Completed with failures: {failed}", code=1)


if __name__ == "__main__":
    main()
