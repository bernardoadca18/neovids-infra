#!/usr/bin/env python3
"""Read-only NeoVids RAM, container, GPU and disk diagnostics."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
REPOSITORIES = (
    "ideas-hub",
    "media-indexer",
    "orchestrator",
    "qwen3-tts-workshop",
    "video-composer",
    "neovids-infra",
)
COMPOSE_PROJECTS = {
    "ideas-hub",
    "media-indexer",
    "orchestrator",
    "qwen3-tts-workshop",
    "video-composer",
}


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def parse_meminfo(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, raw = line.partition(":")
        if not separator:
            continue
        parts = raw.strip().split()
        if not parts:
            continue
        multiplier = 1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1
        try:
            result[key] = int(parts[0]) * multiplier
        except ValueError:
            continue
    return result


def _command(runner: Runner, command: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return runner(command)
    except OSError:
        return None


def collect_host_memory(meminfo_path: Path = Path("/proc/meminfo")) -> dict[str, Any]:
    try:
        values = parse_meminfo(meminfo_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {"available": False, "reason": type(exc).__name__}
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    return {
        "available": total > 0,
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": max(0, total - available),
        "used_ratio": round((total - available) / total, 6) if total else None,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": max(0, swap_total - swap_free),
    }


def collect_containers(runner: Runner = run_command) -> dict[str, Any]:
    info = _command(runner, ["docker", "info"])
    if info is None or info.returncode != 0:
        return {"available": False, "reason": "docker_unavailable", "containers": []}
    listing = _command(
        runner,
        [
            "docker",
            "ps",
            "--all",
            "--filter",
            "label=com.docker.compose.project",
            "--format",
            "{{json .}}",
        ],
    )
    if listing is None or listing.returncode != 0:
        return {"available": False, "reason": "docker_listing_failed", "containers": []}
    rows: list[dict[str, Any]] = []
    for line in listing.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        labels = str(item.get("Labels", ""))
        project = next(
            (
                value.partition("=")[2]
                for value in labels.split(",")
                if value.startswith("com.docker.compose.project=")
            ),
            None,
        )
        service = next(
            (
                value.partition("=")[2]
                for value in labels.split(",")
                if value.startswith("com.docker.compose.service=")
            ),
            None,
        )
        if project not in COMPOSE_PROJECTS:
            continue
        rows.append(
            {
                "id": item.get("ID"),
                "name": item.get("Names"),
                "project": project,
                "service": service,
                "state": item.get("State"),
                "status": item.get("Status"),
                "cpu_percent": None,
                "memory_usage": None,
                "memory_percent": None,
                "pids": None,
                "configured_memory_limit_bytes": None,
                "configured_memory_limit_status": "unavailable",
                "configured_cpu_limit": None,
                "configured_cpu_limit_status": "unavailable",
            }
        )
    ids = [str(row["id"]) for row in rows if row["id"]]
    if ids:
        stats = _command(
            runner,
            ["docker", "stats", "--all", "--no-stream", "--format", "{{json .}}", *ids],
        )
        if stats is not None and stats.returncode == 0:
            by_id: dict[str, dict[str, Any]] = {}
            for line in stats.stdout.splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                by_id[str(item.get("ID", ""))] = item
            for row in rows:
                item = by_id.get(str(row["id"]))
                if item:
                    row.update(
                        cpu_percent=item.get("CPUPerc"),
                        memory_usage=item.get("MemUsage"),
                        memory_percent=item.get("MemPerc"),
                        pids=item.get("PIDs"),
                    )
        inspected = _command(
            runner,
            [
                "docker",
                "inspect",
                "--format",
                "{{.Id}}\t{{.HostConfig.Memory}}\t{{.HostConfig.NanoCpus}}\t{{.HostConfig.CpuQuota}}\t{{.HostConfig.CpuPeriod}}",
                *ids,
            ],
        )
        if inspected is not None and inspected.returncode == 0:
            limits: dict[str, tuple[int, float | None]] = {}
            for line in inspected.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) != 5:
                    continue
                try:
                    memory, nano, quota, period = map(int, parts[1:])
                except ValueError:
                    continue
                cpu_limit = nano / 1_000_000_000 if nano > 0 else None
                if cpu_limit is None and quota > 0 and period > 0:
                    cpu_limit = quota / period
                limits[parts[0]] = (memory, cpu_limit)
            for row in rows:
                match = next(
                    (value for identity, value in limits.items() if identity.startswith(str(row["id"]))),
                    None,
                )
                if match:
                    row["configured_memory_limit_bytes"] = match[0] or None
                    row["configured_memory_limit_status"] = (
                        "configured" if match[0] > 0 else "unbounded"
                    )
                    row["configured_cpu_limit"] = match[1]
                    row["configured_cpu_limit_status"] = (
                        "configured" if match[1] is not None else "unbounded"
                    )
    return {"available": True, "reason": None, "containers": rows}


def collect_gpu(runner: Runner = run_command) -> dict[str, Any]:
    query = _command(
        runner,
        [
            "nvidia-smi",
            "--query-gpu=index,name,uuid,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
    )
    if query is None or query.returncode != 0:
        return {"available": False, "reason": "nvidia_smi_unavailable", "gpus": [], "processes": []}
    gpus: list[dict[str, Any]] = []
    for line in query.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 6:
            gpus.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "uuid": parts[2],
                    "memory_total_mib": int(parts[3]),
                    "memory_used_mib": int(parts[4]),
                    "utilization_percent": int(parts[5]),
                }
            )
    process_query = _command(
        runner,
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
    )
    processes: list[dict[str, Any]] = []
    if process_query is not None and process_query.returncode == 0:
        for line in process_query.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) == 4:
                processes.append(
                    {
                        "gpu_uuid": parts[0],
                        "pid": int(parts[1]),
                        "process_name": parts[2],
                        "memory_used_mib": None if parts[3] == "[Not Supported]" else int(parts[3]),
                    }
                )
    return {"available": True, "reason": None, "gpus": gpus, "processes": processes}


def collect_disk(workspace: Path, runner: Runner = run_command) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(workspace)
    except OSError as exc:
        return {"available": False, "reason": type(exc).__name__, "repositories": []}
    repositories: list[dict[str, Any]] = []
    for name in REPOSITORIES:
        target = workspace / name
        if not target.is_dir():
            continue
        measured = _command(runner, ["du", "-s", "--block-size=1", str(target)])
        size = None
        if measured is not None and measured.returncode == 0:
            try:
                size = int(measured.stdout.split()[0])
            except (IndexError, ValueError):
                pass
        repositories.append({"name": name, "size_bytes": size})
    return {
        "available": True,
        "reason": None,
        "filesystem": {
            "path": str(workspace),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        },
        "repositories": repositories,
    }


def collect_snapshot(workspace: Path, runner: Runner = run_command) -> dict[str, Any]:
    return {
        "schema_version": "neovids_resources_v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "host_memory": collect_host_memory(),
        "containers": collect_containers(runner),
        "gpu": collect_gpu(runner),
        "disk": collect_disk(workspace, runner),
    }


def human_bytes(value: int | None) -> str:
    if value is None:
        return "unavailable"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return "unavailable"


def render_human(snapshot: dict[str, Any]) -> str:
    lines = [f"NeoVids resources (read-only) {snapshot['captured_at']}"]
    memory = snapshot["host_memory"]
    if memory["available"]:
        lines.extend(
            [
                "",
                "Host memory:",
                f"  used {human_bytes(memory['used_bytes'])} / {human_bytes(memory['total_bytes'])}",
                f"  available {human_bytes(memory['available_bytes'])}; swap used {human_bytes(memory['swap_used_bytes'])}",
            ]
        )
    else:
        lines.extend(["", f"Host memory: unavailable ({memory['reason']})"])
    containers = snapshot["containers"]
    lines.extend(["", "Containers:"])
    if not containers["available"]:
        lines.append(f"  unavailable ({containers['reason']})")
    elif not containers["containers"]:
        lines.append("  no Compose containers found")
    else:
        for item in containers["containers"]:
            identity = "/".join(
                value for value in (item["project"], item["service"]) if value
            ) or str(item["name"])
            limit = (
                human_bytes(item["configured_memory_limit_bytes"])
                if item["configured_memory_limit_status"] == "configured"
                else item["configured_memory_limit_status"]
            )
            lines.append(
                f"  {identity}: {item['state']} cpu={item['cpu_percent'] or 'unavailable'} "
                f"memory={item['memory_usage'] or 'unavailable'} configured_limit={limit}"
            )
    gpu = snapshot["gpu"]
    lines.extend(["", "GPU:"])
    if not gpu["available"]:
        lines.append(f"  unavailable ({gpu['reason']})")
    else:
        for item in gpu["gpus"]:
            lines.append(
                f"  GPU {item['index']} {item['name']}: {item['memory_used_mib']} / "
                f"{item['memory_total_mib']} MiB, utilization {item['utilization_percent']}%"
            )
        if gpu["processes"]:
            lines.append("  compute processes:")
            for item in gpu["processes"]:
                lines.append(
                    f"    pid={item['pid']} memory={item['memory_used_mib']} MiB "
                    f"name={item['process_name']}"
                )
        else:
            lines.append("  compute processes: none reported")
    disk = snapshot["disk"]
    lines.extend(["", "Disk:"])
    if not disk["available"]:
        lines.append(f"  unavailable ({disk['reason']})")
    else:
        filesystem = disk["filesystem"]
        lines.append(
            f"  workspace filesystem used {human_bytes(filesystem['used_bytes'])} / "
            f"{human_bytes(filesystem['total_bytes'])}; free {human_bytes(filesystem['free_bytes'])}"
        )
        for item in disk["repositories"]:
            lines.append(f"  {item['name']}: {human_bytes(item['size_bytes'])}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--watch", nargs="?", const=2.0, type=float, metavar="SECONDS")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.watch is not None and args.watch < 0.5:
        raise SystemExit("--watch interval must be at least 0.5 seconds")
    try:
        while True:
            snapshot = collect_snapshot(args.workspace_root.resolve())
            print(json.dumps(snapshot, sort_keys=True) if args.json else render_human(snapshot))
            if args.watch is None:
                return 0
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
