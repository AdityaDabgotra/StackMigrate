"""
Docker-backed sandbox runner.

Threat model: the code being run here was written by an LLM against an
LLM-generated plan, applied to disk by our own code — untrusted in the
same sense as any user-submitted code would be. Two containers are run
per task, not one, specifically to separate a step that legitimately
needs network access (installing dependencies) from the step that must
not have it (running the migrated code's test suite):

  1. INSTALL container — network enabled (has to reach PyPI), otherwise
     identically hardened to the test container.
  2. TEST container — network disabled entirely (`network_disabled=True`).
     Migrated application code has no legitimate reason to make outbound
     network calls during a unit-test run, and disabling it closes off
     an entire class of exfiltration/SSRF concerns from a misbehaving
     or adversarially-prompted editor output.

Both containers additionally run: as a non-root uid, with all Linux
capabilities dropped, with `no-new-privileges`, with a hard memory/CPU/
pids ceiling, and with the workspace as the only writable bind mount
(container rootfs is read-only; scratch space is an in-memory tmpfs
that disappears with the container).

`_build_container_kwargs` is deliberately pure (no docker import, no
I/O) so its correctness — the actual security-relevant configuration —
is unit-testable without a Docker daemon. `_run_container` and `run`
are the thin, untested-by-unit-tests layer that actually calls the
Docker SDK.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from app.adapters.target_test_base import TargetTestAdapter
from app.graph.state import TestOutcome, TestResult
from app.sandbox.interface import SandboxRunResult

_MAX_CAPTURED_OUTPUT_CHARS = 20_000  # truncate before it ever reaches an LLM prompt or gets stored


class SandboxTimeoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxLimits:
    memory_limit: str = "512m"
    nano_cpus: int = 1_000_000_000  # 1.0 CPU
    pids_limit: int = 256
    install_timeout_seconds: int = 180
    test_timeout_seconds: int = 300


def _truncate(output: str) -> str:
    if len(output) <= _MAX_CAPTURED_OUTPUT_CHARS:
        return output
    return output[:_MAX_CAPTURED_OUTPUT_CHARS] + "\n... [truncated]"


def _build_container_kwargs(
    *,
    workspace_root: str,
    image: str,
    command: list[str],
    network_disabled: bool,
    limits: SandboxLimits,
) -> dict:
    """
    Pure function: given inputs, returns the exact kwargs dict that
    would be passed to `docker_client.containers.run(**kwargs)`. No
    Docker SDK import, no side effects — safe to unit test directly.
    """
    return {
        "image": image,
        "command": command,
        "working_dir": "/workspace",
        "volumes": {os.path.realpath(workspace_root): {"bind": "/workspace", "mode": "rw"}},
        "network_disabled": network_disabled,
        "mem_limit": limits.memory_limit,
        "nano_cpus": limits.nano_cpus,
        "pids_limit": limits.pids_limit,
        "user": "1000:1000",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "read_only": True,
        "tmpfs": {"/tmp": "rw,size=256m"},
        "detach": True,
        "stdout": True,
        "stderr": True,
    }


class DockerSandboxRunner:
    def __init__(self, limits: SandboxLimits | None = None) -> None:
        self._limits = limits or SandboxLimits()
        self._client = None  # lazily constructed, see _get_client

    def _get_client(self):
        if self._client is None:
            import docker  # lazy import: only needed when actually running a sandbox

            self._client = docker.from_env()
        return self._client

    def _run_container(self, kwargs: dict, timeout_seconds: int) -> tuple[str, int, float]:
        """Runs one container to completion, returns (combined_output, exit_code, duration_seconds)."""
        client = self._get_client()
        start = time.monotonic()
        container = client.containers.run(**kwargs)
        try:
            try:
                result = container.wait(timeout=timeout_seconds)
            except Exception as exc:  # docker-py raises a requests timeout on wait() expiry
                container.kill()
                raise SandboxTimeoutError(f"Sandbox container exceeded {timeout_seconds}s timeout") from exc

            exit_code = result.get("StatusCode", -1)
            output = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
            duration = time.monotonic() - start
            return _truncate(output), exit_code, duration
        finally:
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001 — cleanup best-effort, never masks the real result/error
                pass

    def run(
        self, *, workspace_root: str, adapter: TargetTestAdapter, task_id: str | None = None
    ) -> SandboxRunResult:
        install_kwargs = _build_container_kwargs(
            workspace_root=workspace_root,
            image=adapter.base_docker_image,
            command=adapter.install_command(),
            network_disabled=False,
            limits=self._limits,
        )

        try:
            install_output, install_exit, install_duration = self._run_container(
                install_kwargs, self._limits.install_timeout_seconds
            )
        except SandboxTimeoutError as exc:
            return SandboxRunResult(
                test_result=TestResult(
                    task_id=task_id, outcome=TestOutcome.TIMEOUT, raw_output=str(exc), duration_seconds=0.0
                ),
                setup_output="",
                setup_succeeded=False,
            )

        if install_exit != 0:
            return SandboxRunResult(
                test_result=TestResult(
                    task_id=task_id,
                    outcome=TestOutcome.ERROR,
                    raw_output=install_output,
                    duration_seconds=install_duration,
                ),
                setup_output=install_output,
                setup_succeeded=False,
            )

        test_kwargs = _build_container_kwargs(
            workspace_root=workspace_root,
            image=adapter.base_docker_image,
            command=adapter.test_command(),
            network_disabled=True,
            limits=self._limits,
        )

        try:
            test_output, test_exit, test_duration = self._run_container(
                test_kwargs, self._limits.test_timeout_seconds
            )
        except SandboxTimeoutError as exc:
            return SandboxRunResult(
                test_result=TestResult(
                    task_id=task_id, outcome=TestOutcome.TIMEOUT, raw_output=str(exc), duration_seconds=0.0
                ),
                setup_output=install_output,
                setup_succeeded=True,
            )

        outcome, failing = adapter.parse_test_output(test_output, test_exit)
        return SandboxRunResult(
            test_result=TestResult(
                task_id=task_id,
                outcome=outcome,
                raw_output=test_output,
                failing_tests=failing,
                duration_seconds=test_duration,
            ),
            setup_output=install_output,
            setup_succeeded=True,
        )
