"""Hardware detection using only the standard library and tools that ship with
GPU drivers (nvidia-smi), so it works before any Python packages are installed.

Detects: OS, CPU architecture, core count, RAM (total/available), free disk
space, and GPUs with their VRAM. From that it picks the llama.cpp backend:
cuda / vulkan / metal / cpu.
"""
import ctypes
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict

GB = 1024 ** 3


@dataclass
class GPU:
    vendor: str            # nvidia | amd | intel | apple
    name: str
    vram_gb: float
    cuda_version: tuple | None = None   # max CUDA version the driver supports
    unified_memory: bool = False        # Apple Silicon: VRAM is shared system RAM


@dataclass
class Hardware:
    os: str                # windows | linux | macos
    arch: str              # x64 | arm64
    cpu_cores: int
    ram_total_gb: float
    ram_available_gb: float
    disk_free_gb: float
    gpus: list = field(default_factory=list)
    backend: str = "cpu"   # cuda | vulkan | metal | cpu

    @property
    def best_gpu(self):
        return max(self.gpus, key=lambda g: g.vram_gb, default=None)

    def to_dict(self):
        return asdict(self)


def _run(cmd, timeout=10):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ---- OS / arch ---------------------------------------------------------------

def _os_name():
    system = platform.system()
    return {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(system, system.lower())


def _arch():
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine


# ---- RAM --------------------------------------------------------------------

def _ram_windows():
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    return stat.ullTotalPhys / GB, stat.ullAvailPhys / GB


def _ram_linux():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0]) * 1024
    return info["MemTotal"] / GB, info.get("MemAvailable", info["MemFree"]) / GB


def _ram_macos():
    total = int(_run(["sysctl", "-n", "hw.memsize"]) or 0)
    # macOS aggressively caches; "available" is roughly free + inactive pages.
    vm = _run(["vm_stat"])
    page = int(re.search(r"page size of (\d+)", vm).group(1)) if "page size" in vm else 4096
    pages = sum(int(m) for m in re.findall(r"Pages (?:free|inactive|speculative):\s+(\d+)", vm))
    available = pages * page if pages else total * 0.5
    return total / GB, available / GB


def _ram(os_name):
    try:
        return {"windows": _ram_windows, "linux": _ram_linux, "macos": _ram_macos}[os_name]()
    except Exception:
        return 0.0, 0.0


# ---- GPUs -------------------------------------------------------------------

def _nvidia_gpus():
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    if not out.strip():
        return []
    cuda = None
    match = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", _run(["nvidia-smi"]))
    if match:
        cuda = (int(match.group(1)), int(match.group(2)))
    gpus = []
    for line in out.strip().splitlines():
        name, mem = [p.strip() for p in line.rsplit(",", 1)]
        try:
            gpus.append(GPU("nvidia", name, int(mem) / 1024, cuda))
        except ValueError:
            pass
    return gpus


def _windows_other_gpus():
    """AMD/Intel GPUs from the registry. WMI's AdapterRAM caps at 4 GB, the
    registry's qwMemorySize does not."""
    import winreg
    gpus = []
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return gpus
    for i in range(64):
        try:
            sub = winreg.OpenKey(root, winreg.EnumKey(root, i))
        except OSError:
            break
        try:
            def value(name):
                try:
                    return winreg.QueryValueEx(sub, name)[0]
                except OSError:
                    return None
            device = (value("MatchingDeviceId") or "").lower()
            vendor = "amd" if "ven_1002" in device else "intel" if "ven_8086" in device else None
            if not vendor:
                continue  # NVIDIA is handled by nvidia-smi; skip virtual adapters
            mem = value("HardwareInformation.qwMemorySize")
            if mem is None:
                raw = value("HardwareInformation.MemorySize")
                mem = int.from_bytes(raw, "little") if isinstance(raw, bytes) else (raw or 0)
            gpus.append(GPU(vendor, value("DriverDesc") or vendor.upper(), int(mem) / GB))
        finally:
            sub.Close()
    return gpus


def _linux_other_gpus():
    gpus = []
    drm = "/sys/class/drm"
    if not os.path.isdir(drm):
        return gpus
    for card in sorted(os.listdir(drm)):
        if not re.fullmatch(r"card\d+", card):
            continue
        dev = os.path.join(drm, card, "device")
        try:
            vendor_id = open(os.path.join(dev, "vendor")).read().strip()
        except OSError:
            continue
        if vendor_id == "0x1002":  # AMD exposes VRAM size via sysfs
            try:
                vram = int(open(os.path.join(dev, "mem_info_vram_total")).read()) / GB
            except (OSError, ValueError):
                vram = 0.0
            gpus.append(GPU("amd", "AMD GPU", vram))
    return gpus


def _gpus(os_name, arch, ram_total):
    if os_name == "macos":
        if arch == "arm64":
            return [GPU("apple", "Apple Silicon", ram_total, unified_memory=True)]
        return []
    gpus = _nvidia_gpus()
    try:
        if os_name == "windows":
            gpus += _windows_other_gpus()
        elif os_name == "linux":
            gpus += _linux_other_gpus()
    except Exception:
        pass
    return gpus


# ---- backend choice -----------------------------------------------------------

# Below this much dedicated VRAM a GPU is almost certainly integrated (shared
# memory) and not worth offloading to.
MIN_USEFUL_VRAM_GB = 3.5


def choose_backend(hw, force_cpu=False):
    if force_cpu:
        return "cpu"
    if hw.os == "macos" and hw.arch == "arm64":
        return "metal"
    gpu = hw.best_gpu
    if not gpu or gpu.vram_gb < MIN_USEFUL_VRAM_GB:
        return "cpu"
    if gpu.vendor == "nvidia" and gpu.cuda_version and gpu.cuda_version >= (12, 4):
        return "cuda"
    return "vulkan"  # AMD/Intel discrete GPUs, or NVIDIA with a very old driver


def detect(force_cpu=False, disk_path="."):
    os_name, arch = _os_name(), _arch()
    ram_total, ram_avail = _ram(os_name)
    hw = Hardware(
        os=os_name,
        arch=arch,
        cpu_cores=os.cpu_count() or 1,
        ram_total_gb=round(ram_total, 1),
        ram_available_gb=round(ram_avail, 1),
        disk_free_gb=round(shutil.disk_usage(disk_path).free / GB, 1),
        gpus=_gpus(os_name, arch, ram_total),
    )
    hw.backend = choose_backend(hw, force_cpu)
    return hw


def describe(hw):
    lines = [f"System: {hw.os} ({hw.arch}), {hw.cpu_cores} CPU threads",
             f"Memory: {hw.ram_total_gb:.1f} GB RAM ({hw.ram_available_gb:.1f} GB free)",
             f"Disk:   {hw.disk_free_gb:.1f} GB free"]
    for g in hw.gpus:
        extra = " (shared with system RAM)" if g.unified_memory else ""
        cuda = f", CUDA {g.cuda_version[0]}.{g.cuda_version[1]}" if g.cuda_version else ""
        lines.append(f"GPU:    {g.name}, {g.vram_gb:.1f} GB{extra}{cuda}")
    if not hw.gpus:
        lines.append("GPU:    none usable detected (will run on the CPU)")
    lines.append(f"AI engine backend: {hw.backend}")
    return lines
