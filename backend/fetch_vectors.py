"""Download precomputed post vectors (EmbeddingGemma-300M Q8_0, 768-d, document prompt) for the data in data/parsed/:

  uv run python backend/fetch_vectors.py        then   uv run python backend/import_data.py
import_data.py stores them instead of embedding every post on this computer.
"""
import hashlib
import sys
import urllib.request
from pathlib import Path

URL = "https://github.com/AaronG5/junction-2026/releases/download/post-vectors-768-v1/posts_768.npz"
SHA256 = "9100dd1381f651d5c156dda6e5411da061236fe4d657476c1cee783517280a34"
DEST = Path(__file__).resolve().parents[1] / "data" / "vectors" / "posts_768.npz"


def main() -> int:
    if DEST.is_file() and "--force" not in sys.argv:
        print(f"Already there: {DEST} (use --force to download again)")
        return 0
    DEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEST.with_suffix(".part")
    print(f"Downloading {URL} ...")
    h = hashlib.sha256()
    with urllib.request.urlopen(URL) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
            h.update(chunk)
    if h.hexdigest() != SHA256:
        tmp.unlink()
        print("The download is damaged (checksum mismatch). Try again.")
        return 1
    tmp.replace(DEST)
    print(f"Done: {DEST}. Now run: uv run python backend/import_data.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
