"""Install llama.cpp (llama-server) and llama-swap.

First choice: a copy already on the machine (LLAMA_SERVER_PATH /
LLAMA_SWAP_PATH in .env, PATH, a llama.cpp source build in the usual places,
or a winget install), if it is new enough and can use the GPU.

Otherwise: download the official prebuilt release that matches the hardware
backend -- no compiler, CMake, CUDA toolkit or Go needed, which matters for
end users. `--build-from-source` clones and compiles llama.cpp instead
(needs git + CMake + a C++ compiler, plus the CUDA toolkit / Vulkan SDK for
GPU builds). llama-swap is always the prebuilt binary (building it needs Go).

Versions are pinned so every install is reproducible; bump them here.
Release files are fetched from github.com/.../releases/download, which is
NOT the rate-limited GitHub REST API (60 requests/hour per IP, which a
shared venue or office network exhausts quickly).
"""
import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

from . import paths, ui
from .download import fetch
from .state import read_env

LLAMA_CPP_TAG = "b12377"
LLAMA_SWAP_VERSION = "272"

# Oldest versions accepted when reusing a copy already on the machine.
# llama.cpp b10103 is confirmed to support Gemma 4 + --fit; older builds may
# not know the Gemma 4 architecture. llama-swap v243 is confirmed to work
# with the generated config.
MIN_LLAMA_BUILD = 10103
MIN_LLAMA_SWAP_VERSION = 243

LLAMA_CPP_RELEASES = "https://github.com/ggml-org/llama.cpp/releases/download"
LLAMA_SWAP_RELEASES = "https://github.com/mostlygeek/llama-swap/releases/download"

EXE = ".exe" if os.name == "nt" else ""


# ---- archives ---------------------------------------------------------------

def _extract(archive, target):
    target.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(target)
    else:
        with tarfile.open(archive) as t:
            if hasattr(tarfile, "data_filter"):
                t.extractall(target, filter="data")
            else:
                t.extractall(target)  # noqa: S202 -- trusted, pinned GitHub release


def _find_file(root, name):
    for p in root.rglob(name):
        if p.is_file():
            return p
    return None


def _copy_tree(src, dst):
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)


# ---- llama.cpp prebuilt -----------------------------------------------------

def _cuda_flavor(hw):
    """Pick the CUDA build the installed driver can run."""
    cuda = hw.best_gpu.cuda_version or (0, 0)
    if hw.os == "windows":
        return "13.5" if cuda >= (13, 5) else "12.4"
    return "13.5" if cuda >= (13, 5) else "12.8"


def _llama_assets(hw, backend):
    """List of release file names for this platform + backend (main + extras)."""
    t, arch = LLAMA_CPP_TAG, hw.arch
    if hw.os == "windows":
        if backend == "cuda":
            v = _cuda_flavor(hw)
            return [f"llama-{t}-bin-win-cuda-{v}-{arch}.zip", f"cudart-llama-bin-win-cuda-{v}-{arch}.zip"]
        if backend == "vulkan":
            return [f"llama-{t}-bin-win-vulkan-{arch}.zip"]
        return [f"llama-{t}-bin-win-cpu-{arch}.zip"]
    if hw.os == "linux":
        if backend == "cuda":
            v = _cuda_flavor(hw)
            return [f"llama-{t}-bin-ubuntu-cuda-{v}-{arch}.tar.gz",
                    f"cudart-llama-{t}-bin-ubuntu-cuda-{v}-{arch}.tar.gz"]
        if backend == "vulkan":
            return [f"llama-{t}-bin-ubuntu-vulkan-{arch}.tar.gz"]
        return [f"llama-{t}-bin-ubuntu-{arch}.tar.gz"]
    if hw.os == "macos":
        return [f"llama-{t}-bin-macos-{arch}.tar.gz"]  # Metal is built in
    raise ui.StepFailed(f"Unsupported operating system: {hw.os}")


def lib_env(bin_dir):
    """Extra environment so llama-server finds the shared libraries next to it."""
    if os.name == "nt":
        return {}
    var = "DYLD_LIBRARY_PATH" if os.uname().sysname == "Darwin" else "LD_LIBRARY_PATH"
    return {var: str(bin_dir)}


def _run_server(server, *args, timeout=60):
    env = {**os.environ, **lib_env(server.parent)}
    try:
        r = subprocess.run([str(server), *args], capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def server_runs(server):
    """Can llama-server start at all on this computer? Returns (ok, detail).
    When a model 'exits prematurely' under llama-swap this tells a broken
    binary (CPU without the instructions it was built for, missing shared
    library, too old a glibc) apart from a problem with the model itself."""
    code, out = _run_server(server, "--version", timeout=30)
    out = " ".join(out.split())  # one line, however it was printed
    if code == 0 and out:
        return True, out[:200]
    if code < 0:  # ended by a signal: -4 is SIGILL, an instruction this CPU lacks
        why = {-4: "SIGILL: it uses CPU instructions this computer does not have",
               -11: "SIGSEGV: it crashed"}.get(code, f"signal {-code}")
        return False, f"{why}. Run the launcher with --build-from-source to compile a build for this CPU."
    return False, out[-400:] or f"exit code {code}"


def gpu_visible(server, backend):
    """True if llama-server can actually use the GPU (driver/runtime OK)."""
    if backend in ("cpu", "metal"):  # Metal devices are listed as "MTL0", always present
        return True
    code, out = _run_server(server, "--list-devices")
    if code != 0:
        return False
    keyword = {"cuda": "CUDA", "vulkan": "Vulkan", "metal": "Metal"}.get(backend, "")
    return keyword.lower() in out.lower()


def _install_prebuilt(hw, backend):
    dest = paths.BIN_DIR / f"llama.cpp-{LLAMA_CPP_TAG}-{backend}"
    server = dest / f"llama-server{EXE}"
    if server.exists() and (dest / ".installed").exists():
        return server
    assets = _llama_assets(hw, backend)
    ui.info(f"Downloading llama.cpp {LLAMA_CPP_TAG} ({backend} build)")
    tmp = paths.BIN_DIR / f"_extract-{backend}"
    shutil.rmtree(tmp, ignore_errors=True)
    for i, asset in enumerate(assets):
        archive = fetch(f"{LLAMA_CPP_RELEASES}/{LLAMA_CPP_TAG}/{asset}",
                        paths.DOWNLOADS_DIR / asset, asset)
        out = tmp / str(i)
        _extract(archive, out)
        if i == 0:
            found = _find_file(out, f"llama-server{EXE}")
            if not found:
                raise ui.StepFailed(f"llama-server not found inside {asset}")
            _copy_tree(found.parent, dest)
        else:  # CUDA runtime libraries: flatten next to llama-server
            for f in out.rglob("*"):
                if f.is_file() or f.is_symlink():
                    target = dest / f.name
                    if not target.exists():
                        shutil.copy2(f, target, follow_symlinks=False)
    shutil.rmtree(tmp, ignore_errors=True)
    if os.name != "nt":
        for f in dest.iterdir():
            if f.is_file() and f.name.startswith("llama-"):
                f.chmod(f.stat().st_mode | 0o111)
    (dest / ".installed").write_text(LLAMA_CPP_TAG)
    _remove_archives(assets)
    return server


def _remove_archives(names):
    """Free disk space once an archive has been extracted and installed."""
    for name in names:
        for p in (paths.DOWNLOADS_DIR / name, paths.DOWNLOADS_DIR / f"{name}.verified"):
            p.unlink(missing_ok=True)


# ---- llama.cpp from source ----------------------------------------------------

def _build_from_source(hw, backend):
    for tool in ("git", "cmake"):
        if not shutil.which(tool):
            raise ui.StepFailed(f"--build-from-source needs '{tool}' installed and on PATH.")
    src = paths.BIN_DIR / f"llama.cpp-src-{LLAMA_CPP_TAG}"
    dest = paths.BIN_DIR / f"llama.cpp-{LLAMA_CPP_TAG}-{backend}-src"
    server = dest / f"llama-server{EXE}"
    if server.exists():
        return server
    if not src.exists():
        ui.info(f"Cloning llama.cpp {LLAMA_CPP_TAG}")
        subprocess.run(["git", "clone", "--depth", "1", "--branch", LLAMA_CPP_TAG,
                        "https://github.com/ggml-org/llama.cpp", str(src)], check=True)
    flags = {"cuda": ["-DGGML_CUDA=ON"], "vulkan": ["-DGGML_VULKAN=ON"]}.get(backend, [])
    build = src / f"build-{backend}"
    ui.info(f"Compiling llama.cpp ({backend}); this can take 10-30 minutes")
    try:
        subprocess.run(["cmake", "-S", str(src), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
                        "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", *flags], check=True)
        subprocess.run(["cmake", "--build", str(build), "--config", "Release", "-j",
                        str(os.cpu_count() or 4), "--target", "llama-server"], check=True)
    except subprocess.CalledProcessError:
        raise ui.StepFailed("Compiling llama.cpp failed (see output above). Re-run without "
                            "--build-from-source to use the prebuilt release instead.")
    built = _find_file(build, f"llama-server{EXE}")
    if not built:
        raise ui.StepFailed("Build finished but llama-server was not found.")
    _copy_tree(built.parent, dest)
    return server


def _build_number(server):
    """llama-server --version prints e.g. 'version: 10103 (c588c4f47)'."""
    _, out = _run_server(server, "--version", timeout=30)
    m = re.search(r"version:\s*(\d+)", out)
    return int(m.group(1)) if m else None


def _existing_llama_servers():
    """llama-server binaries already on this machine, most explicit first."""
    env_path = read_env().get("LLAMA_SERVER_PATH") or os.environ.get("LLAMA_SERVER_PATH")
    candidates = [Path(env_path)] if env_path else []
    if shutil.which("llama-server"):
        candidates.append(Path(shutil.which("llama-server")))
    home = Path.home()
    for src in (home / "Documents" / "GitHub" / "llama.cpp", home / "llama.cpp",
                home / "src" / "llama.cpp", home / "code" / "llama.cpp", home / "git" / "llama.cpp"):
        for sub in ("build/bin/Release", "build/bin"):
            candidates.append(src / sub / f"llama-server{EXE}")
    seen, result = set(), []
    for c in candidates:
        if c.is_file() and c.resolve() not in seen:
            seen.add(c.resolve())
            result.append(c)
    return result


def find_existing_llama_server(backend):
    """Reuse a llama.cpp the user already built/installed, if it is new enough
    and can use the GPU. Returns (server, backend) or None."""
    for server in _existing_llama_servers():
        build = _build_number(server)
        if build is None or build < MIN_LLAMA_BUILD:
            ui.info(f"Found llama.cpp at {server} (build {build}), but it is older than "
                    f"build {MIN_LLAMA_BUILD}; not using it.")
            continue
        if gpu_visible(server, backend):
            return server, backend
        if backend != "cpu" and gpu_visible(server, "cpu"):
            ui.info(f"Found llama.cpp at {server}, but it was built without {backend} support.")
    return None


def install_llama_cpp(hw, build_from_source=False, bundled_only=False):
    """Use an existing llama.cpp if there is a suitable one; otherwise install
    the pinned release, falling back to slower backends if the GPU build
    can't see the GPU (e.g. missing Vulkan driver). Returns (server, backend)."""
    if not bundled_only and not build_from_source:
        found = find_existing_llama_server(hw.backend)
        if found:
            ui.info(f"Using your existing llama.cpp: {found[0]}")
            return found
    order = {"cuda": ["cuda", "vulkan", "cpu"], "vulkan": ["vulkan", "cpu"],
             "metal": ["metal"], "cpu": ["cpu"]}[hw.backend]
    for backend in order:
        server = (_build_from_source if build_from_source else _install_prebuilt)(hw, backend)
        if gpu_visible(server, backend):
            if backend != hw.backend:
                ui.warn(f"Using the {backend} build instead of {hw.backend}.")
            return server, backend
        ui.warn(f"The {backend} build of llama.cpp cannot see your GPU "
                f"(driver missing or too old?). Trying the next option...")
    raise ui.StepFailed("llama.cpp could not be started on this computer.")


def server_supports(server, flag):
    """Feature detection: does this llama-server build accept `flag`?"""
    _, out = _run_server(server, "--help")
    return flag in out


# ---- llama-swap -------------------------------------------------------------

def find_existing_llama_swap():
    env_path = read_env().get("LLAMA_SWAP_PATH") or os.environ.get("LLAMA_SWAP_PATH")
    candidates = [Path(env_path)] if env_path else []
    if shutil.which("llama-swap"):
        candidates.append(Path(shutil.which("llama-swap")))
    if os.name == "nt":  # winget installs are often missing from PATH in some shells
        winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        candidates += sorted(winget.glob(f"*llama-swap*/llama-swap{EXE}"))
    for c in candidates:
        if not c.is_file():
            continue
        try:
            r = subprocess.run([str(c), "-version"], capture_output=True, text=True, timeout=15)
            out = (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"version:\s*v?(\d+)", out)
        if m and int(m.group(1)) >= MIN_LLAMA_SWAP_VERSION:
            return c
        if m:
            ui.info(f"Found llama-swap v{m.group(1)} at {c}, older than v{MIN_LLAMA_SWAP_VERSION}; not using it.")
    return None


def install_llama_swap(hw, bundled_only=False):
    if not bundled_only:
        found = find_existing_llama_swap()
        if found:
            ui.info(f"Using your existing llama-swap: {found}")
            return found
    v = LLAMA_SWAP_VERSION
    dest = paths.BIN_DIR / f"llama-swap-{v}"
    binary = dest / f"llama-swap{EXE}"
    if binary.exists():
        return binary
    os_name = {"windows": "windows", "linux": "linux", "macos": "darwin"}[hw.os]
    arch = {"x64": "amd64", "arm64": "arm64"}.get(hw.arch, hw.arch)
    asset = f"llama-swap_{v}_{os_name}_{arch}.{'zip' if hw.os == 'windows' else 'tar.gz'}"
    archive = fetch(f"{LLAMA_SWAP_RELEASES}/v{v}/{asset}", paths.DOWNLOADS_DIR / asset, asset)

    # Verify against the release's published checksums.
    sums = fetch(f"{LLAMA_SWAP_RELEASES}/v{v}/llama-swap_{v}_checksums.txt",
                 paths.DOWNLOADS_DIR / f"llama-swap_{v}_checksums.txt", "llama-swap checksums")
    expected = next((line.split()[0] for line in sums.read_text().splitlines()
                     if line.strip().endswith(asset)), None)
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if expected and expected != actual:
        archive.unlink(missing_ok=True)
        raise ui.StepFailed("llama-swap download is corrupted; removed it. Run the launcher again.")

    tmp = paths.BIN_DIR / "_extract-llama-swap"
    shutil.rmtree(tmp, ignore_errors=True)
    _extract(archive, tmp)
    found = _find_file(tmp, f"llama-swap{EXE}")
    if not found:
        raise ui.StepFailed(f"llama-swap binary not found inside {asset}")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(found, binary)
    if os.name != "nt":
        binary.chmod(binary.stat().st_mode | 0o111)
    shutil.rmtree(tmp, ignore_errors=True)
    _remove_archives([asset, f"llama-swap_{v}_checksums.txt"])
    return binary
