"""Download the narrative bank used by veritas/narrative.py (browser-extension narrative check) into data/narrative_bank.

  uv run python backend/fetch_narrative_bank.py          # about 200 MB, once per server
The bank: narrative claims (EUvsDisinfo, Overload, Antibot4Navalny; RT/RIA as state-media echo) and the team's
Telegram source posts, embedded with the same EmbeddingGemma-300M the backend runs (768-d, document prompt).
Restart the backend afterwards (the bank is loaded on the first /api/ext request).
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

URL = "https://github.com/AaronG5/junction-2026/releases/download/narrative-bank-768-v1/narrative_bank_768.tar.gz"
SHA256 = "0cd410f2ac2200102dddb463850cbeccdfc6504ad76632500ebd07bd05dfc126"
DEST = Path(__file__).resolve().parents[1] / "data" / "narrative_bank"


def main() -> int:
    if (DEST / "manifest.json").is_file() and "--force" not in sys.argv:
        print(f"Already there: {DEST} (use --force to download again)")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "bank.tar.gz"
        print(f"Downloading {URL} ...")
        h = hashlib.sha256()
        with urllib.request.urlopen(URL) as r, open(archive, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                h.update(chunk)
        if h.hexdigest() != SHA256:
            print("The download is damaged (checksum mismatch). Try again.")
            return 1
        staging = Path(tmp) / "bank"
        with tarfile.open(archive) as t:
            t.extractall(staging, filter="data")
        if DEST.exists():
            shutil.rmtree(DEST)
        DEST.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), DEST)
    print(f"Done: {DEST}. Restart the backend to load it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
