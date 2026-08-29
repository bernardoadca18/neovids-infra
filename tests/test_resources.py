from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "neovids_resources.py"
SPEC = importlib.util.spec_from_file_location("neovids_resources", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
resources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resources)


def unavailable(_: object) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError


def docker_only(command: object) -> subprocess.CompletedProcess[str]:
    values = list(command)
    if values[:2] == ["docker", "info"]:
        return subprocess.CompletedProcess(values, 0, "ok", "")
    if values[:2] == ["docker", "ps"]:
        return subprocess.CompletedProcess(
            values,
            0,
            '{"ID":"abc123","Names":"api","State":"running","Status":"Up",'
            '"Labels":"com.docker.compose.project=orchestrator,'
            'com.docker.compose.service=api"}\n',
            "",
        )
    if values[:2] == ["docker", "stats"]:
        return subprocess.CompletedProcess(
            values,
            0,
            '{"ID":"abc123","CPUPerc":"2.00%","MemUsage":"100MiB / 1GiB",'
            '"MemPerc":"9.77%","PIDs":"5"}\n',
            "",
        )
    if values[:2] == ["docker", "inspect"]:
        return subprocess.CompletedProcess(
            values,
            0,
            "abc123full\t1073741824\t2000000000\t0\t0\n",
            "",
        )
    raise FileNotFoundError


def test_meminfo_parser_and_human_units() -> None:
    parsed = resources.parse_meminfo("MemTotal: 1024 kB\nMemAvailable: 256 kB\n")
    assert parsed == {"MemTotal": 1_048_576, "MemAvailable": 262_144}
    assert resources.human_bytes(1_048_576) == "1.0 MiB"


def test_optional_tools_degrade_without_failing_snapshot(tmp_path: Path) -> None:
    snapshot = resources.collect_snapshot(tmp_path, unavailable)
    assert snapshot["read_only"] is True
    assert snapshot["containers"] == {
        "available": False,
        "reason": "docker_unavailable",
        "containers": [],
    }
    assert snapshot["gpu"] == {
        "available": False,
        "reason": "nvidia_smi_unavailable",
        "gpus": [],
        "processes": [],
    }
    assert snapshot["disk"]["available"] is True


def test_docker_metrics_and_limits_remain_available_without_nvidia() -> None:
    containers = resources.collect_containers(docker_only)
    gpu = resources.collect_gpu(docker_only)
    assert containers["available"] is True
    assert containers["containers"][0] == {
        "id": "abc123",
        "name": "api",
        "project": "orchestrator",
        "service": "api",
        "state": "running",
        "status": "Up",
        "cpu_percent": "2.00%",
        "memory_usage": "100MiB / 1GiB",
        "memory_percent": "9.77%",
        "pids": "5",
        "configured_memory_limit_bytes": 1_073_741_824,
        "configured_memory_limit_status": "configured",
        "configured_cpu_limit": 2.0,
        "configured_cpu_limit_status": "configured",
    }
    assert gpu["available"] is False
