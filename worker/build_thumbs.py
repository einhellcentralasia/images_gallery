#!/usr/bin/env python3
"""
Build SKU thumbnails from worker/images.csv and save them into:
  for_price/200/<sku>.webp
  for_price/300/<sku>.webp

- Keeps aspect ratio (no distortion)
- Resizes down so the longest side is <= target size
- Tries to keep files small (quality loop), logs warnings if it can't hit target
- Skips unchanged SKUs using worker/state.json (url hash)
- DOES NOT FAIL the whole run if some URLs are broken; writes worker/failures.csv

CSV expected: 2 columns: sku,url
Delimiter can be comma, semicolon, or tab.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from PIL import Image, ImageOps


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 2) -> None:
    log(f"[FATAL] {msg}")
    sys.exit(code)


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

        parts = [p.strip() for p in parts]
        parts = [p for p in parts if p != ""]
        if len(parts) < 2:
            log(f"[SKIP] Line {i}: not enough columns")
            continue

        sku, url = parts[0], parts[1]
        if sku.lower() == "sku" and url.lower() in ("url", "link"):
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

    # last SKU wins
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


def write_failures_csv(fail_path: Path, failures: List[Tuple[str, str, str]]) -> None:
    ensure_dir(fail_path.parent)
    with fail_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sku", "url", "error"])
        for sku, url, err in failures:
            w.writerow([sku, url, err])


def requests_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "images_gallery-thumb-worker/1.1 (+GitHub Actions)"})
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
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    thumb = img.copy()
    thumb.thumbnail((max_side, max_side), resample=Image.Resampling.LANCZOS)
    return thumb


def save_webp_with_target_size(
    img: Image.Image,
    out_path: Path,
    target_min_kb: int = 1,
    target_max_kb: int = 5,
) -> Tuple[int, int]:
    ensure_dir(out_path.parent)

    qualities = [80, 70, 60, 50, 45, 40, 35, 30]
    best_bytes = None
    best_q = None

    for q in qualities:
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=q, method=6)
        data = buf.getvalue()
        size_kb = len(data) / 1024.0

        best_bytes = data
        best_q = q

        if target_min_kb <= size_kb <= target_max_kb:
            break
        if size_kb <= target_max_kb:
            break

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_bytes(best_bytes)
    tmp.replace(out_path)

    return len(best_bytes), int(best_q)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="worker/images.csv")
    parser.add_argument("--out", default="for_price")
    parser.add_argument("--sizes", nargs="+", type=int, default=[200, 300])
    parser.add_argument("--state", default="worker/state.json")
    parser.add_argument("--failures", default="worker/failures.csv")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    out_root = Path(args.out)
    state_path = Path(args.state)
    failures_path = Path(args.failures)
    sizes = sorted(set(args.sizes))

    rows = read_rows(csv_path)
    if not rows:
        die("No valid rows found in CSV")

    state = load_state(state_path)
    sess = requests_session()

    processed = 0
    skipped = 0
    failed = 0
    failures: List[Tuple[str, str, str]] = []

    log(f"[OK] Loaded {len(rows)} unique SKUs from {csv_path}")
    log(f"[OK] Target sizes: {sizes}")
    log(f"[OK] Output root: {out_root}")
    log("----")

    for r in rows:
        url_hash = sha256_hex(r.url)
        st = state.get(r.sku, {})
        unchanged = (st.get("url_hash") == url_hash)

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
            err = str(e)
            failures.append((r.sku, r.url, err))
            log(f"[FAIL] {r.sku}: cannot load image from URL -> {e}")
            # IMPORTANT: do NOT update state so it retries next run if you fix the URL
            continue

        for s in sizes:
            out_path = out_root / str(s) / f"{r.sku}.webp"
            try:
                thumb = make_thumb(img, max_side=s)
                final_bytes, q = save_webp_with_target_size(thumb, out_path)
                kb = final_bytes / 1024.0
                if kb > 5.0:
                    log(f"[WARN] {r.sku} size {s}: {kb:.1f} KB (>5 KB) even at quality={q}")
                else:
                    log(f"[OK] {r.sku} size {s}: {kb:.1f} KB (quality={q}) -> {out_path.as_posix()}")
            except Exception as e:
                failed += 1
                failures.append((r.sku, r.url, f"write size {s}: {e}"))
                log(f"[FAIL] {r.sku} size {s}: cannot write thumb -> {e}")

        # update state only if we got through processing this SKU (download succeeded)
        state[r.sku] = {
            "url": r.url,
            "url_hash": url_hash,
            "sizes": sizes,
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        processed += 1

    save_state(state_path, state)
    write_failures_csv(failures_path, failures)

    log("----")
    log(f"[DONE] processed={processed} skipped={skipped} failed={failed}")
    log(f"[OK] failures report: {failures_path.as_posix()}")
    log("[OK] Completed successfully ✅ (even if some links are broken)")

    # Always exit 0: you explicitly want "skip + log"
    sys.exit(0)


if __name__ == "__main__":
    main()
