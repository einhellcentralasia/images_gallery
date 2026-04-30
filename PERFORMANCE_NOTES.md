# Media performance notes

Observed behavior: links under `catalogues/*.pdf` and `videos/**/*.mp4` can feel slow after a SharePoint sync.

## Likely causes

1. **Large binary payloads**: browsers must fetch multi‑MB assets.
2. **Non-linearized PDFs**: PDF first-page display can be delayed when Fast Web View is missing.
3. **MP4 metadata placement**: when the `moov` atom is written *after* `mdat`, many browsers cannot start playback quickly.
4. **Cache invalidation after sync**: replacing files at the same path can force full re-downloads.

## Quick checks

Run:

```bash
python scripts_check_media_health.py
```

This reports:
- PDF size and `linearized=True/False`
- MP4 size and `moov_before_mdat=True/False`

## What changed in this repo

Current MP4 layout check shows `videos/hero_videos/3.mp4` through `6.mp4` have `moov_before_mdat=False`, which is a common reason for slow stream start on static hosting.

## Recommendations

- Export PDFs as **Fast Web View / Linearized** before committing.
- Re-mux MP4 files with **faststart** so `moov` is at the beginning (example with ffmpeg: `-movflags +faststart`).
- Keep JPEG poster/preview links for immediate visual loading.
- For large videos, generate lower-bitrate web variants.
