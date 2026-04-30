#!/usr/bin/env python3
from pathlib import Path
import argparse
import struct

ROOT = Path(__file__).resolve().parent


def is_pdf_linearized(path: Path) -> bool:
    with path.open('rb') as f:
        head = f.read(2048)
    return b'Linearized' in head


def mp4_moov_before_mdat(path: Path) -> bool | None:
    moov_at = None
    mdat_at = None
    with path.open('rb') as f:
        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            size, box_type = struct.unpack('>I4s', header)
            box_type = box_type.decode('latin1')
            start = f.tell() - 8
            if box_type == 'moov' and moov_at is None:
                moov_at = start
            if box_type == 'mdat' and mdat_at is None:
                mdat_at = start
            if size == 0:
                break
            if size == 1:
                largesize = struct.unpack('>Q', f.read(8))[0]
                payload = largesize - 16
            else:
                payload = size - 8
            if payload < 0:
                return None
            f.seek(payload, 1)
            if moov_at is not None and mdat_at is not None:
                return moov_at < mdat_at
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--fail-on-slow', action='store_true', help='Exit non-zero when slow-layout media is detected')
    args = parser.parse_args()

    print('Media health check')
    print('==================')
    slow_findings = []

    pdfs = sorted((ROOT / 'catalogues').glob('*.pdf'))
    if pdfs:
        print('\nPDFs')
        for pdf in pdfs:
            size_mb = pdf.stat().st_size / (1024 * 1024)
            linearized = is_pdf_linearized(pdf)
            print(f'- {pdf}: {size_mb:.1f} MB | linearized={linearized}')
            if not linearized:
                slow_findings.append(f'non-linearized PDF: {pdf}')

    videos = sorted((ROOT / 'videos').rglob('*.mp4'))
    if videos:
        print('\nMP4 videos')
        for video in videos:
            size_mb = video.stat().st_size / (1024 * 1024)
            fast_start = mp4_moov_before_mdat(video)
            print(f'- {video}: {size_mb:.1f} MB | moov_before_mdat={fast_start}')
            if fast_start is False:
                slow_findings.append(f'moov-after-mdat MP4: {video}')

    if slow_findings:
        print('\nPotentially slow media detected:')
        for item in slow_findings:
            print(f'  - {item}')

    print('\nHints:')
    print('- non-linearized PDFs are often slower to open in-browser over HTTP')
    print('- MP4 files with moov after mdat often buffer longer before playback starts')

    if args.fail_on_slow and slow_findings:
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
