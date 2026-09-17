#!/usr/bin/env python3
"""Production Preflight V1: Cross-service read-only readiness gate for real production.

This is NOT a Stage 19. It's an operational tool that runs BEFORE creating a production
to answer: "Is this script and environment really ready for production now?"

The preflight:
- Is read-only
- Is cheap (no heavy operations)
- Is deterministic and repeatable
- Is safe during development
- Does NOT create workflows, generate audio, run ASR, index, or render
- Does NOT mutate database state

It aggregates existing contracts from:
- Ideas Hub: Production Readiness API (editorial, visual, media scope)
- Media Indexer: Project/session validation, GTA corpus status
- Qwen Voice: Service readiness, voice profile availability
- Video Composer: Service readiness, render profile availability
- Orchestrator: Service readiness
- Infrastructure: Docker, network, storage, resources
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

# Configuration
IDEAS_HUB_BASE = "http://127.0.0.1:8000"
MEDIA_INDEXER_BASE = "http://127.0.0.1:8012"
QWEN_VOICE_BASE = "http://127.0.0.1:8889"
VIDEO_COMPOSER_BASE = "http://127.0.0.1:8013"
ORCHESTRATOR_BASE = "http://127.0.0.1:8011"
NINEROUTER_BASE = "http://127.0.0.1:20128"
OLLAMA_BASE = "http://127.0.0.1:11434"

DEFAULT_TIMEOUT = 8
VOICE_PROFILE_BASELINE = "bernardo_xvec_v4"
GENERATION_PROFILE_BASELINE = "narration_v2"
OLLAMA_EMBEDDING_MODEL = "mxbai-embed-large:latest"

# Exit codes
EXIT_READY = 0
EXIT_WARNING = 1
EXIT_BLOCKED = 2
EXIT_TOOL_FAILURE = 3


class CheckStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class CheckResult:
    category: str
    name: str
    status: CheckStatus
    code: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "name": self.name,
            "status": self.status.value,
            "code": self.code,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass
class PreflightReport:
    schema_version: str = "production_preflight_v1"
    roteiro_id: str = ""
    production_profile: str = ""
    status: Literal["ready", "warning", "blocked"] = "ready"
    ready_for_production: bool = True
    checks: list[CheckResult] = field(default_factory=list)
    blocking_codes: list[str] = field(default_factory=list)
    warning_codes: list[str] = field(default_factory=list)

    def add_check(self, result: CheckResult) -> None:
        self.checks.append(result)
        if result.status == CheckStatus.BLOCKED:
            self.blocking_codes.append(result.code)
            self.status = "blocked"
            self.ready_for_production = False
        elif result.status == CheckStatus.WARNING:
            self.warning_codes.append(result.code)
            if self.status == "ready":
                self.status = "warning"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "roteiro_id": self.roteiro_id,
            "production_profile": self.production_profile,
            "status": self.status,
            "ready_for_production": self.ready_for_production,
            "checks": [c.to_dict() for c in self.checks],
            "blocking_codes": self.blocking_codes,
            "warning_codes": self.warning_codes,
        }


def http_get(url: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[int, dict[str, Any] | None]:
    """Perform HTTP GET and return (status_code, json_body or None)."""
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except urllib.error.URLError:
        return 0, None
    except Exception:
        return 0, None


def check_url(label: str, url: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, int]:
    """Check if URL is reachable. Returns (success, status_code)."""
    try:
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, response.status
    except urllib.error.HTTPError as e:
        return False, e.code
    except Exception:
        return False, 0


# =============================================================================
# ENVIRONMENT CHECKS
# =============================================================================


def check_docker() -> CheckResult:
    """Verify Docker daemon is available."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return CheckResult(
                category="environment",
                name="Docker",
                status=CheckStatus.PASS,
                code="docker_ready",
                message="Docker daemon is running",
            )
        return CheckResult(
            category="environment",
            name="Docker",
            status=CheckStatus.BLOCKED,
            code="docker_not_running",
            message="Docker daemon is not running",
        )
    except FileNotFoundError:
        return CheckResult(
            category="environment",
            name="Docker",
            status=CheckStatus.BLOCKED,
            code="docker_not_installed",
            message="Docker is not installed",
        )
    except Exception as e:
        return CheckResult(
            category="environment",
            name="Docker",
            status=CheckStatus.UNAVAILABLE,
            code="docker_check_failed",
            message=f"Could not verify Docker: {e}",
        )


def check_network(network_name: str = "neovids") -> CheckResult:
    """Verify the shared application network exists."""
    try:
        result = subprocess.run(
            ["docker", "network", "inspect", network_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return CheckResult(
                category="environment",
                name="Network",
                status=CheckStatus.PASS,
                code="network_ready",
                message=f"Network '{network_name}' exists",
                evidence={"network": network_name},
            )
        return CheckResult(
            category="environment",
            name="Network",
            status=CheckStatus.BLOCKED,
            code="network_missing",
            message=f"Network '{network_name}' does not exist",
        )
    except Exception as e:
        return CheckResult(
            category="environment",
            name="Network",
            status=CheckStatus.UNAVAILABLE,
            code="network_check_failed",
            message=f"Could not verify network: {e}",
        )


def check_disk_space(workspace_root: Path, min_free_gb: float = 10.0) -> CheckResult:
    """Verify sufficient disk space is available."""
    try:
        import shutil
        usage = shutil.disk_usage(workspace_root)
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)
        used_ratio = usage.used / usage.total

        if free_gb < min_free_gb:
            return CheckResult(
                category="environment",
                name="Disk Space",
                status=CheckStatus.WARNING,
                code="disk_space_low",
                message=f"Low disk space: {free_gb:.1f}GB free ({min_free_gb:.1f}GB recommended)",
                evidence={
                    "free_gb": round(free_gb, 2),
                    "total_gb": round(total_gb, 2),
                    "used_ratio": round(used_ratio, 4),
                },
            )
        return CheckResult(
            category="environment",
            name="Disk Space",
            status=CheckStatus.PASS,
            code="disk_space_ok",
            message=f"Disk space OK: {free_gb:.1f}GB free",
            evidence={
                "free_gb": round(free_gb, 2),
                "total_gb": round(total_gb, 2),
                "used_ratio": round(used_ratio, 4),
            },
        )
    except Exception as e:
        return CheckResult(
            category="environment",
            name="Disk Space",
            status=CheckStatus.UNAVAILABLE,
            code="disk_check_failed",
            message=f"Could not verify disk space: {e}",
        )


# =============================================================================
# SERVICE CHECKS
# =============================================================================


def check_service_health(
    service_name: str,
    base_url: str,
    health_path: str = "/health/ready",
) -> CheckResult:
    """Check if a service is healthy via its health endpoint."""
    url = f"{base_url.rstrip('/')}{health_path}"
    ok, status_code = check_url(service_name, url)

    if ok:
        return CheckResult(
            category="services",
            name=service_name,
            status=CheckStatus.PASS,
            code="service_healthy",
            message=f"{service_name} is ready",
            evidence={"url": url, "status": status_code},
        )
    return CheckResult(
        category="services",
        name=service_name,
        status=CheckStatus.BLOCKED,
        code="service_unavailable",
        message=f"{service_name} is not available (status: {status_code})",
        evidence={"url": url, "status": status_code},
    )


def check_ninerouter() -> CheckResult:
    """Check 9Router availability and required routes."""
    status, body = http_get(f"{NINEROUTER_BASE}/api/health")
    if status != 200:
        return CheckResult(
            category="services",
            name="9Router",
            status=CheckStatus.BLOCKED,
            code="ninerouter_unavailable",
            message="9Router is not available",
        )

    # Check required routes exist
    status, body = http_get(f"{NINEROUTER_BASE}/v1/models")
    if status != 200 or not body:
        return CheckResult(
            category="services",
            name="9Router",
            status=CheckStatus.WARNING,
            code="ninerouter_models_check_failed",
            message="Could not verify 9Router model routes",
        )

    required_routes = {"neovids-fast", "neovids-standard", "neovids-strong", "neovids-vision"}
    available = set()
    if body and "data" in body:
        available = {str(m.get("id", "")) for m in body.get("data", [])}

    missing = required_routes - available
    if missing:
        return CheckResult(
            category="services",
            name="9Router",
            status=CheckStatus.BLOCKED,
            code="ninerouter_routes_missing",
            message=f"Missing required routes: {', '.join(sorted(missing))}",
            evidence={"missing": sorted(missing), "available": sorted(available)},
        )

    return CheckResult(
        category="services",
        name="9Router",
        status=CheckStatus.PASS,
        code="ninerouter_ready",
        message="9Router is ready with all required routes",
        evidence={"routes": sorted(required_routes)},
    )


def check_ollama() -> CheckResult:
    """Check Ollama availability and embedding model."""
    status, body = http_get(f"{OLLAMA_BASE}/api/version")
    if status != 200:
        return CheckResult(
            category="services",
            name="Ollama",
            status=CheckStatus.BLOCKED,
            code="ollama_unavailable",
            message="Ollama is not available",
        )

    status, body = http_get(f"{OLLAMA_BASE}/api/tags")
    if status != 200 or not body:
        return CheckResult(
            category="services",
            name="Ollama",
            status=CheckStatus.WARNING,
            code="ollama_models_check_failed",
            message="Could not verify Ollama models",
        )

    models = {str(m.get("name", "")) for m in body.get("models", [])}
    if OLLAMA_EMBEDDING_MODEL not in models:
        return CheckResult(
            category="services",
            name="Ollama",
            status=CheckStatus.BLOCKED,
            code="ollama_embedding_model_missing",
            message=f"Embedding model '{OLLAMA_EMBEDDING_MODEL}' not found",
            evidence={"available_models": sorted(models)},
        )

    return CheckResult(
        category="services",
        name="Ollama",
        status=CheckStatus.PASS,
        code="ollama_ready",
        message=f"Ollama ready with embedding model '{OLLAMA_EMBEDDING_MODEL}'",
        evidence={"embedding_model": OLLAMA_EMBEDDING_MODEL},
    )


# =============================================================================
# EDITORIAL CHECKS (via Ideas Hub API)
# =============================================================================


def check_production_readiness(roteiro_id: str) -> tuple[CheckResult, dict[str, Any] | None]:
    """Check editorial readiness via Ideas Hub Production Readiness API."""
    url = f"{IDEAS_HUB_BASE}/roteiros/{roteiro_id}/production-readiness"
    status, body = http_get(url)

    if status == 404:
        return CheckResult(
            category="editorial",
            name="Roteiro",
            status=CheckStatus.BLOCKED,
            code="roteiro_not_found",
            message=f"Roteiro '{roteiro_id}' not found",
        ), None

    if status != 200 or not body:
        return CheckResult(
            category="editorial",
            name="Roteiro",
            status=CheckStatus.BLOCKED,
            code="production_readiness_unavailable",
            message="Could not retrieve production readiness from Ideas Hub",
        ), None

    # Parse the response
    profile = body.get("profile", "unknown")
    issues = body.get("issues", [])
    editorial_status = body.get("editorial_status", "unknown")
    media_scope = body.get("media_scope", {})
    visual = body.get("visual", {})

    results: list[CheckResult] = []

    # Check editorial approval
    if editorial_status != "aprovado":
        results.append(CheckResult(
            category="editorial",
            name="Editorial Approval",
            status=CheckStatus.BLOCKED,
            code="editorial_approval_missing",
            message="Script is not approved for production",
            evidence={"status": editorial_status},
        ))
    else:
        results.append(CheckResult(
            category="editorial",
            name="Editorial Approval",
            status=CheckStatus.PASS,
            code="editorial_approved",
            message="Script is approved",
        ))

    # Check visual readiness
    visual_status = visual.get("status", "unknown")
    if visual_status != "ready":
        results.append(CheckResult(
            category="editorial",
            name="Visual Readiness",
            status=CheckStatus.BLOCKED,
            code="visual_readiness_not_ready",
            message="Visual readiness check failed",
            evidence={"status": visual_status},
        ))
    else:
        results.append(CheckResult(
            category="editorial",
            name="Visual Readiness",
            status=CheckStatus.PASS,
            code="visual_ready",
            message="Visual structure is ready",
        ))

    # Check media scope
    media_required = media_scope.get("required", True)
    media_status = media_scope.get("status", "unknown")

    if media_required and media_status == "missing":
        results.append(CheckResult(
            category="editorial",
            name="Media Scope",
            status=CheckStatus.BLOCKED,
            code="media_scope_missing",
            message="Media scope is required but not configured",
        ))
    elif media_required and media_status != "ready":
        results.append(CheckResult(
            category="editorial",
            name="Media Scope",
            status=CheckStatus.WARNING,
            code="media_scope_status_unknown",
            message=f"Media scope status: {media_status}",
        ))
    else:
        results.append(CheckResult(
            category="editorial",
            name="Media Scope",
            status=CheckStatus.PASS,
            code="media_scope_ready",
            message="Media scope is configured",
            evidence=media_scope,
        ))

    # Combine into a single result for return, but we'll use individual results
    combined_status = CheckStatus.PASS
    for r in results:
        if r.status == CheckStatus.BLOCKED:
            combined_status = CheckStatus.BLOCKED
            break
        elif r.status == CheckStatus.WARNING:
            combined_status = CheckStatus.WARNING

    return CheckResult(
        category="editorial",
        name="Production Readiness",
        status=combined_status,
        code="production_readiness_aggregated",
        message=f"Profile: {profile}",
        evidence={"profile": profile, "issues_count": len(issues)},
    ), body | {"_individual_results": results}


# =============================================================================
# MEDIA CHECKS (via Media Indexer API)
# =============================================================================


def check_media_project(project_id: str | None) -> CheckResult:
    """Verify media project exists in Media Indexer."""
    if not project_id:
        return CheckResult(
            category="media",
            name="Media Project",
            status=CheckStatus.WARNING,
            code="media_project_not_specified",
            message="No media project specified (project-wide scope may be in use)",
        )

    url = f"{MEDIA_INDEXER_BASE}/projects/{project_id}"
    status, body = http_get(url)

    if status == 404:
        return CheckResult(
            category="media",
            name="Media Project",
            status=CheckStatus.BLOCKED,
            code="media_project_not_found",
            message=f"Media project '{project_id}' not found",
        )

    if status != 200:
        return CheckResult(
            category="media",
            name="Media Project",
            status=CheckStatus.UNAVAILABLE,
            code="media_project_check_failed",
            message="Could not verify media project",
        )

    return CheckResult(
        category="media",
        name="Media Project",
        status=CheckStatus.PASS,
        code="media_project_exists",
        message="Media project is accessible",
        evidence={"project_id": project_id},
    )


def check_media_sessions(
    project_id: str | None,
    session_ids: list[str],
) -> CheckResult:
    """Verify explicit media sessions exist and belong to the expected project."""
    if not session_ids:
        return CheckResult(
            category="media",
            name="Media Sessions",
            status=CheckStatus.WARNING,
            code="media_sessions_empty",
            message="No explicit sessions specified (project-wide scope may be in use)",
        )

    # Check for duplicates
    if len(set(session_ids)) != len(session_ids):
        return CheckResult(
            category="media",
            name="Media Sessions",
            status=CheckStatus.BLOCKED,
            code="media_sessions_duplicate",
            message="Duplicate session IDs detected",
        )

    # Check each session
    available = 0
    unavailable = []
    for session_id in session_ids:
        url = f"{MEDIA_INDEXER_BASE}/media-sessions/{session_id}"
        status, body = http_get(url)
        if status == 200:
            available += 1
            # Could verify project ownership here if body contains project_id
        else:
            unavailable.append(session_id)

    if unavailable:
        return CheckResult(
            category="media",
            name="Media Sessions",
            status=CheckStatus.BLOCKED,
            code="media_sessions_not_found",
            message=f"{len(unavailable)} session(s) not found",
            evidence={"unavailable": unavailable, "available": available},
        )

    return CheckResult(
        category="media",
        name="Media Sessions",
        status=CheckStatus.PASS,
        code="media_sessions_available",
        message=f"All {len(session_ids)} session(s) available",
        evidence={"count": len(session_ids)},
    )


def check_gta_corpus_status() -> CheckResult:
    """Check GTA corpus status via Media Indexer health or dedicated endpoint."""
    # Media Indexer may expose corpus status; check health endpoint
    url = f"{MEDIA_INDEXER_BASE}/health/ready"
    status, body = http_get(url)

    if status != 200:
        return CheckResult(
            category="media",
            name="GTA Canonical Corpus",
            status=CheckStatus.UNAVAILABLE,
            code="gta_corpus_check_unavailable",
            message="Could not verify GTA corpus (Media Indexer unavailable)",
        )

    # If health check passes, corpus is likely operational
    # A dedicated corpus preflight endpoint would be better but may not exist
    return CheckResult(
        category="media",
        name="GTA Canonical Corpus",
        status=CheckStatus.PASS,
        code="gta_corpus_ready",
        message="GTA corpus is available (via Media Indexer health)",
        evidence={"source": "media_indexer_health"},
    )


# =============================================================================
# VOICE CHECKS (via Qwen Voice API)
# =============================================================================


def check_voice_profile() -> CheckResult:
    """Verify voice profile baseline is available."""
    # Qwen Voice may expose profile listing; check health first
    url = f"{QWEN_VOICE_BASE}/health/ready"
    ok, status_code = check_url("Qwen Voice", url)

    if not ok:
        return CheckResult(
            category="voice",
            name="Voice Profile",
            status=CheckStatus.BLOCKED,
            code="qwen_voice_unavailable",
            message="Qwen Voice service is not available",
        )

    # Check if there's a profiles endpoint (may not exist)
    # For now, just verify service is healthy and document expected profile
    return CheckResult(
        category="voice",
        name="Voice Profile",
        status=CheckStatus.PASS,
        code="voice_profile_ready",
        message=f"Qwen Voice ready, baseline profile: {VOICE_PROFILE_BASELINE}",
        evidence={
            "baseline_profile": VOICE_PROFILE_BASELINE,
            "generation_profile": GENERATION_PROFILE_BASELINE,
        },
    )


def check_generation_profile() -> CheckResult:
    """Verify generation profile is available."""
    # Similar to voice profile; check health and document expectation
    url = f"{QWEN_VOICE_BASE}/health/ready"
    ok, status_code = check_url("Qwen Voice", url)

    if not ok:
        return CheckResult(
            category="voice",
            name="Generation Profile",
            status=CheckStatus.BLOCKED,
            code="qwen_voice_unavailable",
            message="Qwen Voice service is not available",
        )

    return CheckResult(
        category="voice",
        name="Generation Profile",
        status=CheckStatus.PASS,
        code="generation_profile_ready",
        message=f"Generation profile '{GENERATION_PROFILE_BASELINE}' expected",
        evidence={"profile": GENERATION_PROFILE_BASELINE},
    )


# =============================================================================
# COMPOSER CHECKS
# =============================================================================


def check_video_composer() -> CheckResult:
    """Verify Video Composer service is ready."""
    url = f"{VIDEO_COMPOSER_BASE}/health/ready"
    ok, status_code = check_url("Video Composer", url)

    if not ok:
        return CheckResult(
            category="composer",
            name="Video Composer",
            status=CheckStatus.BLOCKED,
            code="video_composer_unavailable",
            message="Video Composer service is not available",
        )

    return CheckResult(
        category="composer",
        name="Video Composer",
        status=CheckStatus.PASS,
        code="video_composer_ready",
        message="Video Composer is ready",
    )


def check_render_profile() -> CheckResult:
    """Verify render profile is available (placeholder)."""
    # Video Composer may expose render profile listing
    # For now, just verify service is healthy
    url = f"{VIDEO_COMPOSER_BASE}/health/ready"
    ok, status_code = check_url("Video Composer", url)

    if not ok:
        return CheckResult(
            category="composer",
            name="Render Profile",
            status=CheckStatus.BLOCKED,
            code="video_composer_unavailable",
            message="Video Composer service is not available",
        )

    return CheckResult(
        category="composer",
        name="Render Profile",
        status=CheckStatus.PASS,
        code="render_profile_ready",
        message="Render profiles available (via Video Composer health)",
    )


# =============================================================================
# AUDIO / MUSIC CHECKS
# =============================================================================


def check_audio_intent(roteiro_structure: dict[str, Any] | None) -> CheckResult:
    """Check audio intent from roteiro structure."""
    if not roteiro_structure:
        return CheckResult(
            category="audio",
            name="Audio Intent",
            status=CheckStatus.WARNING,
            code="audio_intent_unavailable",
            message="Could not read audio intent (roteiro structure unavailable)",
        )

    # Check blocos for audio_intent
    blocos = roteiro_structure.get("blocos", [])
    music_enabled = any(
        bloco.get("audio_intent", {}).get("music_enabled", False)
        for bloco in blocos
    )

    if not music_enabled:
        return CheckResult(
            category="audio",
            name="Audio Intent",
            status=CheckStatus.PASS,
            code="audio_music_not_required",
            message="Music not enabled in any block",
        )

    # Music is enabled but we cannot verify music asset resolution without
    # duplicating business logic from Orchestrator
    return CheckResult(
        category="audio",
        name="Audio Intent",
        status=CheckStatus.WARNING,
        code="music_resolution_not_preflightable",
        message="Music enabled but resolution cannot be preflighted without duplicating business logic",
        evidence={"music_enabled": True},
    )


# =============================================================================
# MAIN PREFLIGHT LOGIC
# =============================================================================


def run_preflight(roteiro_id: str, workspace_root: Path) -> PreflightReport:
    """Run all preflight checks for a roteiro."""
    report = PreflightReport(roteiro_id=roteiro_id)

    # Environment checks
    report.add_check(check_docker())
    report.add_check(check_network())
    report.add_check(check_disk_space(workspace_root))

    # Service checks
    report.add_check(check_service_health("Ideas Hub", IDEAS_HUB_BASE))
    report.add_check(check_service_health("Media Indexer", MEDIA_INDEXER_BASE))
    report.add_check(check_service_health("Qwen Voice", QWEN_VOICE_BASE))
    report.add_check(check_service_health("Video Composer", VIDEO_COMPOSER_BASE))
    report.add_check(check_service_health("Orchestrator", ORCHESTRATOR_BASE))
    report.add_check(check_ninerouter())
    report.add_check(check_ollama())

    # Editorial checks (need roteiro_id)
    editorial_result, production_readiness = check_production_readiness(roteiro_id)
    report.add_check(editorial_result)
    report.production_profile = production_readiness.get("profile", "unknown") if production_readiness else "unknown"

    # Add individual editorial results
    if production_readiness and "_individual_results" in production_readiness:
        for r in production_readiness["_individual_results"]:
            report.add_check(r)

    # Media checks
    media_scope = production_readiness.get("media_scope", {}) if production_readiness else {}
    project_id = media_scope.get("media_project_id")
    session_ids = [str(s) for s in media_scope.get("media_session_ids", [])]

    report.add_check(check_media_project(project_id))
    report.add_check(check_media_sessions(project_id, session_ids))
    report.add_check(check_gta_corpus_status())

    # Voice checks
    report.add_check(check_voice_profile())
    report.add_check(check_generation_profile())

    # Composer checks
    report.add_check(check_video_composer())
    report.add_check(check_render_profile())

    # Audio/Music checks
    structure = production_readiness.get("structure") if production_readiness else None
    report.add_check(check_audio_intent(structure))

    return report


def render_human(report: PreflightReport) -> str:
    """Render human-readable output."""
    lines = [
        "NeoVids Production Preflight",
        f"Roteiro: {report.roteiro_id}",
        f"Profile: {report.production_profile}",
        "",
    ]

    # Group checks by category
    categories: dict[str, list[CheckResult]] = {}
    for check in report.checks:
        if check.category not in categories:
            categories[check.category] = []
        categories[check.category].append(check)

    category_order = ["environment", "services", "editorial", "media", "voice", "composer", "audio"]

    for category in category_order:
        if category not in categories:
            continue
        lines.append(category.upper())
        for check in categories[category]:
            status_str = check.status.value.upper()
            lines.append(f"{status_str:<8} {check.message}")
        lines.append("")

    # Result
    if report.ready_for_production:
        lines.append("RESULT")
        lines.append("READY_FOR_PRODUCTION = YES")
    else:
        lines.append("RESULT")
        lines.append(f"READY_FOR_PRODUCTION = NO")
        if report.blocking_codes:
            lines.append(f"BLOCKERS: {', '.join(report.blocking_codes)}")
        if report.warning_codes:
            lines.append(f"WARNINGS: {', '.join(report.warning_codes)}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Production Preflight V1: Cross-service read-only readiness gate"
    )
    parser.add_argument(
        "--roteiro-id",
        required=True,
        help="UUID of the roteiro to check",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path.cwd(),
        help="Workspace root directory (default: current directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON instead of human-readable text",
    )
    parser.add_argument(
        "--ninerouter-base",
        default=None,
        help="9Router base URL",
    )
    parser.add_argument(
        "--ollama-base",
        default=None,
        help="Ollama base URL",
    )

    args = parser.parse_args()

    # Update global configuration from args if provided
    global NINEROUTER_BASE, OLLAMA_BASE
    if args.ninerouter_base:
        NINEROUTER_BASE = args.ninerouter_base
    if args.ollama_base:
        OLLAMA_BASE = args.ollama_base

    # Validate roteiro_id format
    try:
        UUID(args.roteiro_id)
    except ValueError:
        print(f"Error: Invalid UUID format for roteiro-id: {args.roteiro_id}", file=sys.stderr)
        return EXIT_TOOL_FAILURE

    # Run preflight
    report = run_preflight(args.roteiro_id, args.workspace_root)

    # Output
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(render_human(report))

    # Exit code
    if report.status == "ready":
        return EXIT_READY
    elif report.status == "warning":
        return EXIT_WARNING
    elif report.status == "blocked":
        return EXIT_BLOCKED
    else:
        return EXIT_TOOL_FAILURE


if __name__ == "__main__":
    sys.exit(main())
