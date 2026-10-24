"""系统资源监控：CPU/内存/（可选）GPU。看板首页与运行页用。

依赖纪律：psutil 装了就用（信息全）；没装走 stdlib 兜底（os.times 差值 +
ctypes/sysconf 内存）。GPU 只在有 nvidia-smi 时显示——模型算力在网关侧，
本机多数情况没有 GPU 可看，这时如实说明而不是编一个数。
"""
from __future__ import annotations

import shutil
import subprocess
import time
from datetime import datetime, timezone

try:
    import psutil  # 可选依赖（本机装了；CI/最小安装没有也能跑）
except ImportError:
    psutil = None

_proc = None  # psutil.Process 惰性单例
_prev_times: tuple[float, float] | None = None  # stdlib CPU 采样的前一次 (wall, cpu)


def _sample_psutil(root_drive: str) -> dict:
    global _proc
    if _proc is None:
        _proc = psutil.Process()
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(root_drive)
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.15),
        "mem_percent": mem.percent,
        "mem_used_gb": round((mem.total - mem.available) / 1e9, 1),
        "mem_total_gb": round(mem.total / 1e9, 1),
        "proc_cpu_percent": _proc.cpu_percent(),
        "proc_mem_mb": round(_proc.memory_info().rss / 1e6),
        "disk_free_gb": round(disk.free / 1e9, 1),
    }


def _sample_stdlib(root_drive: str) -> dict:
    """无 psutil 的兜底：进程 CPU 用 os.times 差值，系统内存用 ctypes（Win）/
    sysconf（POSIX）。系统级 CPU% 没有可靠的零依赖来源 → 返回 None 如实显示。"""
    import os

    global _prev_times
    out: dict = {"cpu_percent": None}
    times = os.times()
    now = time.monotonic()
    cpu_now = times.user + times.system
    if _prev_times is not None:
        wall_dt = now - _prev_times[0]
        if wall_dt > 0:
            out["proc_cpu_percent"] = round(
                (cpu_now - _prev_times[1]) / wall_dt * 100 / (os.cpu_count() or 1), 1)
    _prev_times = (now, cpu_now)
    try:
        if os.name == "nt":
            import ctypes

            class _MEMSTAT(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("_pad1", ctypes.c_ulonglong), ("_pad2", ctypes.c_ulonglong),
                            ("_pad3", ctypes.c_ulonglong), ("_pad4", ctypes.c_ulonglong)]

            stat = _MEMSTAT()
            stat.dwLength = ctypes.sizeof(_MEMSTAT)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                out["mem_percent"] = float(stat.dwMemoryLoad)
                out["mem_total_gb"] = round(stat.ullTotalPhys / 1e9, 1)
                out["mem_used_gb"] = round((stat.ullTotalPhys - stat.ullAvailPhys) / 1e9, 1)
        else:
            pages_total = os.sysconf("SC_PHYS_PAGES")
            pages_avail = os.sysconf("SC_AVPHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            total = pages_total * page_size
            avail = pages_avail * page_size
            out["mem_percent"] = round((total - avail) / total * 100, 1)
            out["mem_total_gb"] = round(total / 1e9, 1)
            out["mem_used_gb"] = round((total - avail) / 1e9, 1)
    except (OSError, AttributeError, ValueError):
        pass
    try:
        usage = shutil.disk_usage(root_drive)
        out["disk_free_gb"] = round(usage.free / 1e9, 1)
    except OSError:
        pass
    return out


_GPU_MISSING: bool | None = None  # 探测结果缓存：None=未探测


def _sample_gpu() -> dict | None:
    """NVIDIA GPU（有 nvidia-smi 才查）。无 → None（调用方显示「无本地 GPU」）。"""
    global _GPU_MISSING
    if _GPU_MISSING:
        return None
    if not shutil.which("nvidia-smi"):
        _GPU_MISSING = True
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            _GPU_MISSING = True
            return None
        name, util, mem_used, mem_total = [p.strip() for p in out.stdout.splitlines()[0].split(",")]
        return {"name": name, "util": int(util),
                "mem_used_mb": int(mem_used), "mem_total_mb": int(mem_total)}
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        _GPU_MISSING = True
        return None


def sample(root: str = ".") -> dict:
    """一次采样。psutil 优先，stdlib 兜底；GPU 可选。"""
    drive = str(root)
    if psutil is not None:
        data = _sample_psutil(drive)
        data["backend"] = "psutil"
    else:
        data = _sample_stdlib(drive)
        data["backend"] = "stdlib"
    data["gpu"] = _sample_gpu()
    data["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return data


def render_card(data: dict) -> str:
    """系统状态卡 HTML（首页/运行页共用；轮询 JS 整卡替换）。"""
    from .ui import ESC

    def bar(pct: float | None, label: str, key: str) -> str:
        if pct is None:
            return f"<div style='flex:1;min-width:140px'><span class='muted'>{label}</span>：暂不可得</div>"
        color = "var(--good)" if pct < 70 else ("var(--warn)" if pct < 90 else "var(--crit)")
        return (f"<div style='flex:1;min-width:140px' data-sys='{key}'>"
                f"<span class='muted'>{label}</span> <b>{pct:.0f}%</b>"
                f"<div class='progress' style='margin-top:3px;min-width:0'>"
                f"<div style='width:{min(pct, 100):.0f}%;background:{color}'></div></div></div>")

    gpu = data.get("gpu")
    if gpu:
        gpu_html = bar(gpu["util"] * 1.0, f"GPU（{ESC(gpu['name'])}）", "gpu") + (
            f"<div class='muted' style='font-size:12px'>显存 {gpu['mem_used_mb']}"
            f"/{gpu['mem_total_mb']} MB</div>")
    else:
        gpu_html = ("<div style='flex:1;min-width:140px' data-sys='gpu'>"
                    "<span class='muted'>GPU</span>：本机无 NVIDIA GPU"
                    "<div class='muted' style='font-size:12px'>模型算力在 USTC 网关侧，这里看不到</div></div>")
    parts = [
        bar(data.get("cpu_percent"), "CPU", "cpu"),
        bar(data.get("mem_percent"), "内存", "mem"),
        gpu_html,
    ]
    if data.get("disk_free_gb") is not None:
        parts.append(f"<div style='flex:1;min-width:110px' data-sys='disk'>"
                     f"<span class='muted'>磁盘余量</span> <b>{data['disk_free_gb']} GB</b></div>")
    backend = "psutil" if data.get("backend") == "psutil" else "stdlib 兜底"
    return ("<div style='display:flex;gap:18px;flex-wrap:wrap;align-items:center'>"
            + "".join(parts)
            + f"<span class='muted' style='font-size:11px'>采集：{backend} · 3 秒刷新</span></div>")


# 轮询 JS：整卡替换模式——页面渲染 <div data-sys-host>…render_card…</div>，
# 每 3 秒用 /api/sys 返回的 html 整卡换掉（无该元素则空转一次即停）
SYSMON_JS = """
<script>
(function () {
  if (!document.querySelector('[data-sys-host]')) return;
  async function tick() {
    try {
      const r = await fetch('/api/sys');
      if (r.ok) {
        const d = await r.json();
        document.querySelectorAll('[data-sys-host]').forEach(el => {
          el.innerHTML = d.html;
        });
      }
    } catch (e) {}
    setTimeout(tick, 3000);
  }
  tick();
})();
</script>
"""
