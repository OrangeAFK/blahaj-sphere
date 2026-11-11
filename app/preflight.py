"""Preflight checks with fix hints (SPEC §6.4). Every probe goes through `Probe` so tests can fake the machine."""

import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Literal

import httpx
from pydantic import BaseModel

from app.settings import Settings

DISK_MARGIN_GB = 10
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"


class Check(BaseModel):
    id: str
    label: str
    status: Literal["ok", "warn", "fail"]
    detail: str = ""
    fix: str = ""
    fix_action: str | None = None  # POST /api/preflight/fix/{fix_action} starts a job that repairs it


class Probe:
    """Real-machine probes. Each returns plain data and never raises."""

    def run(self, argv: list[str], timeout: float = 20) -> tuple[int, str]:
        # CreateProcess does not resolve bare names via PATHEXT, so npm shims like `claude.CMD` need their full path.
        argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return 127, ""
        return r.returncode, r.stdout

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def openrouter_status(self, key: str) -> int | None:
        try:
            return httpx.get(OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {key}"}, timeout=10).status_code
        except httpx.HTTPError:
            return None

    def disk_free_gb(self, path: Path) -> float:
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(probe).free / 1e9

    def microphones(self) -> list[str] | None:
        """Names of capture endpoints, or None when this platform can't be probed."""
        if sys.platform != "win32":
            return None
        script = (
            "Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction SilentlyContinue | "
            "Where-Object { $_.InstanceId -like '*{0.0.1.*' } | Select-Object -ExpandProperty FriendlyName"
        )
        code, out = self.run(["powershell", "-NoProfile", "-Command", script], timeout=30)
        return [ln.strip() for ln in out.splitlines() if ln.strip()] if code == 0 else None


def check_docker(p: Probe) -> Check:
    code, out = p.run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
    if code == 0 and out.strip():
        return Check(id="docker", label="Docker running", status="ok", detail=f"Docker {out.strip()}")
    return Check(
        id="docker",
        label="Docker running",
        status="fail",
        detail="docker daemon not reachable" if p.which("docker") else "docker not found on PATH",
        fix="Install/start Docker Desktop (WSL2 backend), wait for it to finish starting, then re-run.",
    )


def check_claude(p: Probe) -> Check:
    label = "Claude CLI logged in"
    if not p.which("claude"):
        return Check(
            id="claude", label=label, status="fail", detail="`claude` not found on PATH",
            fix="Install Claude Code (https://claude.com/claude-code), then run `claude auth login`.",
        )  # fmt: skip
    code, out = p.run(["claude", "auth", "status"], timeout=30)
    try:
        st = json.loads(out)
    except ValueError:
        st = {}
    if code == 0 and st.get("loggedIn"):
        return Check(id="claude", label=label, status="ok", detail=f"{st.get('authMethod', '?')} ({st.get('subscriptionType') or '?'})")
    return Check(
        id="claude", label=label, status="fail", detail="not logged in",
        fix="Run `claude auth login` in a terminal and sign in with your subscription account.",
    )  # fmt: skip


def check_openrouter(p: Probe, settings: Settings) -> Check:
    label = "OpenRouter key valid"
    if not settings.openrouter_api_key:
        return Check(
            id="openrouter", label=label, status="warn", detail="no key set (free-model fallback disabled)",
            fix="Create a free key at openrouter.ai/keys and paste it in Settings.",
        )  # fmt: skip
    status = p.openrouter_status(settings.openrouter_api_key)
    if status == 200:
        return Check(id="openrouter", label=label, status="ok", detail="key accepted")
    if status in (401, 403):
        return Check(
            id="openrouter", label=label, status="fail", detail=f"key rejected (HTTP {status})",
            fix="Re-copy the key from openrouter.ai/keys into Settings.",
        )  # fmt: skip
    return Check(
        id="openrouter", label=label, status="warn",
        detail="could not reach OpenRouter" if status is None else f"unexpected HTTP {status}",
        fix="Check your internet connection and retry.",
    )  # fmt: skip


def check_models(data: Path) -> Check:
    """Voice models (`python -m app.voice download`). Optional: oral exams have a text fallback (SPEC §7.6)."""
    label = "Voice models downloaded"

    def present(name: str) -> bool:
        d = data / "models" / name
        return d.is_dir() and any(d.iterdir())

    missing = [n for n in ("whisper", "piper") if not present(n)]
    if not missing:
        return Check(id="models", label=label, status="ok", detail="whisper + piper present")
    return Check(
        id="models", label=label, status="warn", detail=f"missing: {', '.join(missing)}",
        fix="Voice is optional (text mode works). Download the models (~520 MB): uv run python -m app.voice download",
    )  # fmt: skip


def check_disk(p: Probe, data: Path, image_gb: float) -> Check:
    need = image_gb + DISK_MARGIN_GB
    free = p.disk_free_gb(data)
    detail = f"{free:.0f} GB free, need {need:.0f} GB (image {image_gb:.1f} + {DISK_MARGIN_GB})"
    if free >= need:
        return Check(id="disk", label="Disk space", status="ok", detail=detail)
    return Check(id="disk", label="Disk space", status="fail", detail=detail, fix="Free disk space (e.g. `docker system prune -a` for unused images).")


def check_microphone(p: Probe) -> Check:
    label = "Microphone available"
    mics = p.microphones()
    if mics is None:
        return Check(id="microphone", label=label, status="warn", detail="could not probe audio devices", fix="Voice is optional; check OS privacy/audio settings.")
    if mics:
        return Check(id="microphone", label=label, status="ok", detail=mics[0])
    return Check(id="microphone", label=label, status="warn", detail="no capture device found", fix="Plug in a microphone, or use text mode for oral exams.")


def run_preflight(domain, settings: Settings, data: Path, probe: Probe | None = None) -> list[Check]:
    """Run all checks in parallel (several are slow subprocess/network calls). Result order is stable."""
    p = probe or Probe()
    image_gb = domain.image_size_gb if domain else 0.0
    jobs: list[Callable[[], list[Check]]] = [
        lambda: [check_docker(p)],
        lambda: domain.checks() if domain else [],
        lambda: [check_claude(p)],
        lambda: [check_openrouter(p, settings)],
        lambda: [check_models(data)],
        lambda: [check_disk(p, data, image_gb)],
        lambda: [check_microphone(p)],
    ]
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        results = list(ex.map(lambda j: j(), jobs))
    return [c for group in results for c in group]
