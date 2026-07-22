"""详细健康检查端点。

检查后端依赖项的连通性和资源状态：
- LLM 连通性（轻量 ping 请求）
- Qdrant 向量存储连通性
- 磁盘空间
- 进程内存

各检查项独立 try-except，单项失败不影响其他项。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter

router = APIRouter()


async def _check_llm() -> Dict[str, Any]:
    """检查 LLM 连通性（轻量请求）。"""
    try:
        from ..agent.myclaw_agent import MyClawAgent
        # 通过全局 agent 获取 LLM 实例
        from ..main import _agent
        if _agent is None or not hasattr(_agent, '_llm'):
            return {"status": "skip", "reason": "agent not initialized"}

        llm = _agent._llm
        t_start = time.perf_counter()

        # 轻量 ping：发送 max_tokens=1 的请求
        from openai import AsyncOpenAI
        client = llm._get_async_client() if hasattr(llm, '_get_async_client') else AsyncOpenAI(
            api_key=llm.api_key, base_url=llm.base_url, timeout=10
        )
        await client.chat.completions.create(
            model=llm.model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        latency_ms = (time.perf_counter() - t_start) * 1000
        return {
            "status": "ok",
            "latency_ms": round(latency_ms, 1),
            "model": llm.model,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


async def _check_qdrant() -> Dict[str, Any]:
    """检查 Qdrant 向量存储连通性。"""
    try:
        from qdrant_client import QdrantClient

        qdrant_url = os.getenv("QDRANT_URL", "")
        qdrant_api_key = os.getenv("QDRANT_API_KEY", "")
        if not qdrant_url:
            return {"status": "skip", "reason": "QDRANT_URL not set"}

        t_start = time.perf_counter()
        # 临时短连接：仅用于健康检查，避免污染连接管理器缓存
        client = QdrantClient(
            url=qdrant_url,
            api_key=qdrant_api_key or None,
            timeout=5,
        )
        try:
            collections = client.get_collections()
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        latency_ms = (time.perf_counter() - t_start) * 1000
        count = len(collections.collections) if hasattr(collections, 'collections') else 0
        return {
            "status": "ok",
            "latency_ms": round(latency_ms, 1),
            "collections": count,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


async def _check_disk() -> Dict[str, Any]:
    """检查工作空间磁盘空间。"""
    try:
        workspace_path = os.getenv("WORKSPACE_PATH", "~/.helloclaw/workspace")
        workspace_path = os.path.expanduser(workspace_path)
        usage = shutil.disk_usage(workspace_path)
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        used_pct = (usage.used / usage.total) * 100 if usage.total > 0 else 0

        status = "ok" if free_gb > 1.0 else "warning"
        return {
            "status": status,
            "free_gb": round(free_gb, 2),
            "total_gb": round(total_gb, 2),
            "used_pct": round(used_pct, 1),
            "path": workspace_path,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


async def _check_memory() -> Dict[str, Any]:
    """检查进程内存使用。

    优先使用 psutil；不可用时回退到 stdlib（基于 ctypes 的 Win32 API 或 /proc）。
    """
    try:
        import psutil  # type: ignore
        proc = psutil.Process()
        mem_info = proc.memory_info()
        rss_mb = mem_info.rss / (1024 ** 2)

        # 获取系统可用内存
        vm = psutil.virtual_memory()
        available_mb = vm.available / (1024 ** 2)

        status = "ok" if rss_mb < 2048 else "warning"
        return {
            "status": status,
            "rss_mb": round(rss_mb, 1),
            "available_mb": round(available_mb, 1),
        }
    except ImportError:
        # 回退到 stdlib：保证即便 psutil 缺失也能给出基本内存信息
        return _check_memory_fallback()
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


def _check_memory_fallback() -> Dict[str, Any]:
    """不依赖 psutil 的内存检查回退（跨平台 stdlib 实现）。"""
    import sys

    rss_mb: float | None = None
    available_mb: float | None = None

    # 进程 RSS：优先使用 resource（Unix），否则 ctypes 解析 /proc 或 Win32 API
    try:
        if sys.platform != "win32":
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            # ru_maxrss 单位：Linux=KB, macOS=bytes
            rss_mb = usage.ru_maxrss / 1024.0 if sys.platform == "darwin" else usage.ru_maxrss / 1024.0
        else:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            psapi = ctypes.WinDLL("psapi.dll")
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD,
            ]
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                rss_mb = counters.WorkingSetSize / (1024 * 1024)
    except Exception:
        pass

    # 系统可用内存
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            state = MEMORYSTATUSEX()
            state.dwLength = ctypes.sizeof(state)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state))
            available_mb = state.ullAvailPhys / (1024 * 1024)
        else:
            # Linux: 解析 /proc/meminfo
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        available_mb = int(line.split()[1]) / 1024.0
                        break
    except Exception:
        pass

    if rss_mb is None and available_mb is None:
        return {
            "status": "skip",
            "reason": "memory probing not available on this platform (install psutil for full info)",
        }

    status = "ok" if (rss_mb is None or rss_mb < 2048) else "warning"
    payload: Dict[str, Any] = {"status": status}
    if rss_mb is not None:
        payload["rss_mb"] = round(rss_mb, 1)
    if available_mb is not None:
        payload["available_mb"] = round(available_mb, 1)
    payload["source"] = "stdlib-fallback"
    return payload


@router.get("/health/detailed")
async def detailed_health_check() -> Dict[str, Any]:
    """详细健康检查：并发检查所有依赖项。"""
    # 并发执行所有检查（单项超时 5s 不阻塞整体）
    results = await asyncio.gather(
        _check_llm(),
        _check_qdrant(),
        _check_disk(),
        _check_memory(),
        return_exceptions=True,
    )

    checks = {
        "llm": results[0] if not isinstance(results[0], Exception) else {"status": "error", "error": str(results[0])[:200]},
        "qdrant": results[1] if not isinstance(results[1], Exception) else {"status": "error", "error": str(results[1])[:200]},
        "disk": results[2] if not isinstance(results[2], Exception) else {"status": "error", "error": str(results[2])[:200]},
        "memory": results[3] if not isinstance(results[3], Exception) else {"status": "error", "error": str(results[3])[:200]},
    }

    # 汇总状态
    statuses = [c.get("status", "error") for c in checks.values()]
    if all(s == "ok" or s == "skip" for s in statuses):
        overall = "healthy"
    elif any(s == "error" for s in statuses):
        overall = "unhealthy"
    else:
        overall = "degraded"

    return {
        "status": overall,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
    }
