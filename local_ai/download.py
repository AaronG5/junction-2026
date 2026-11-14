"""Resumable downloads via curl (ships with Windows 10+, macOS and most Linux
distros), with a pure-Python fallback.

* Downloads go to "<file>.part" and are renamed only after the size (and,
  for Hugging Face files, the SHA-256) is verified -- an interrupted download
  never looks complete.
* Re-running resumes the .part file (curl -C -), so a dropped Wi-Fi
  connection loses nothing.
* A "<file>.verified" marker records a successful check, so later launches
  skip the network entirely and work offline.
"""
import hashlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import ui

USER_AGENT = "junction-local-ai-launcher/1.0"
MAX_ATTEMPTS = 20


def hf_url(repo, filename):
    return f"https://huggingface.co/{repo}/resolve/main/{filename}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _parse_info(headers):
    size = headers.get("x-linked-size") or headers.get("content-length")
    sha = (headers.get("x-linked-etag") or "").strip('"')
    return (int(size) if size else None), (sha if len(sha) == 64 else None)


def hf_file_info(repo, filename):
    """(size_bytes, sha256) from Hugging Face's redirect headers, or (None, None).

    Uses curl first: it uses the OS certificate store, while some Python
    builds (MSYS2, python.org on macOS) ship without usable CA certificates."""
    url = hf_url(repo, filename)
    if shutil.which("curl"):
        try:
            out = subprocess.run([shutil.which("curl"), "-sI", "--max-time", "20", "-A", USER_AGENT, url],
                                 capture_output=True, text=True, timeout=30).stdout
            headers = {k.strip().lower(): v.strip() for k, _, v in
                       (line.partition(":") for line in out.splitlines() if ":" in line)}
            if headers.get("x-linked-size"):
                return _parse_info(headers)
        except (OSError, subprocess.SubprocessError):
            pass
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(req, timeout=20)
        headers = resp.headers
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            return None, None
        headers = e.headers
    except (urllib.error.URLError, OSError):
        return None, None
    return _parse_info({k.lower(): v for k, v in headers.items()})


def _marker(dest):
    return dest.with_name(dest.name + ".verified")


def is_verified(dest):
    marker = _marker(dest)
    if not (dest.exists() and marker.exists()):
        return False
    try:
        return int(marker.read_text().split()[0]) == dest.stat().st_size
    except (ValueError, IndexError, OSError):
        return False


def sha256_of(path):
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    last = 0.0
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
            done += len(chunk)
            if time.time() - last > 0.5:
                print(f"\r    Verifying file... {done * 100 // max(total, 1)}%", end="", flush=True)
                last = time.time()
    print("\r" + " " * 40 + "\r", end="")
    return h.hexdigest()


def _curl(url, part):
    cmd = [shutil.which("curl"), "-L", "--fail", "--progress-bar",
           "--connect-timeout", "30", "--speed-limit", "1024", "--speed-time", "60",
           "-A", USER_AGENT, "-C", "-", "-o", str(part), url]
    return subprocess.run(cmd).returncode


def _urllib(url, part):
    """Fallback when curl is missing. Supports resume via HTTP Range."""
    start = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               **({"Range": f"bytes={start}-"} if start else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "ab" if start else "wb") as out:
            if start and resp.status != 206:   # server ignored Range -> restart
                out.truncate(0)
            total = int(resp.headers.get("Content-Length") or 0) + (start if resp.status == 206 else 0)
            done = start if resp.status == 206 else 0
            while chunk := resp.read(1024 * 1024):
                out.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r    {done / 1e9:.2f} / {total / 1e9:.2f} GB", end="", flush=True)
        print()
        return 0
    except urllib.error.HTTPError as e:
        return 22 if e.code != 416 else 0
    except (urllib.error.URLError, OSError):
        return 1


def fetch(url, dest, label, expected_size=None, sha256=None):
    """Download url to dest (resumable, verified). Raises ui.StepFailed."""
    dest = Path(dest)
    if is_verified(dest):
        ui.ok(f"{label}: already downloaded")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and expected_size and dest.stat().st_size == expected_size:
        part = dest  # complete but never verified (e.g. placed there by hand)
    else:
        part = dest.with_name(dest.name + ".part")
        size_txt = f" ({expected_size / 1e9:.2f} GB)" if expected_size else ""
        ui.info(f"Downloading {label}{size_txt}")
        downloader = _curl if shutil.which("curl") else _urllib
        for attempt in range(1, MAX_ATTEMPTS + 1):
            code = downloader(url, part)
            have = part.stat().st_size if part.exists() else 0
            if code == 0 or (expected_size and have == expected_size):
                break
            if code == 22 and have == 0:  # HTTP error before any data, e.g. 404
                raise ui.StepFailed(f"Download failed for {label} (server refused: {url})")
            ui.warn(f"Connection interrupted ({have / 1e9:.2f} GB so far). "
                    f"Retrying in 5s (attempt {attempt}/{MAX_ATTEMPTS})...")
            time.sleep(5)
        else:
            raise ui.StepFailed(f"Could not download {label}. Check your internet connection "
                                f"and run the launcher again -- it will continue where it stopped.")

    size = part.stat().st_size
    if expected_size and size != expected_size:
        part.unlink(missing_ok=True)
        raise ui.StepFailed(f"{label} has the wrong size ({size} vs {expected_size} bytes); "
                            f"the partial file was removed. Run the launcher again.")
    if sha256:
        if sha256_of(part) != sha256:
            part.unlink(missing_ok=True)
            raise ui.StepFailed(f"{label} is corrupted (checksum mismatch); it was removed. "
                                f"Run the launcher again to re-download it.")
    if part != dest:
        part.replace(dest)
    _marker(dest).write_text(f"{size} {sha256 or ''}\n")
    ui.ok(f"{label}: downloaded and verified")
    return dest


def fetch_hf(repo, filename, dest_dir, label=None):
    dest = Path(dest_dir) / filename
    if is_verified(dest):
        ui.ok(f"{label or filename}: already downloaded")
        return dest
    size, sha = hf_file_info(repo, filename)
    if size is None:
        ui.warn("Could not reach Hugging Face to check the file; trying the download anyway.")
    return fetch(hf_url(repo, filename), dest, label or filename, size, sha)


if __name__ == "__main__":  # manual test: python -m local_ai.download <url> <dest>
    fetch(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[2]).name)
