"""
These tests exercise `_build_container_kwargs` directly — the pure
function that decides the actual security posture of every sandbox
container. No `docker` package import, no daemon required: this is
what makes it possible to verify the hardening is correct in plain CI
without Docker-in-Docker.
"""

from __future__ import annotations

import os
import tempfile

from app.sandbox.docker_runner import SandboxLimits, _build_container_kwargs


def test_install_container_has_network_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        kwargs = _build_container_kwargs(
            workspace_root=tmp,
            image="python:3.12-slim",
            command=["pip", "install", "-r", "requirements.txt"],
            network_disabled=False,
            limits=SandboxLimits(),
        )
        assert kwargs["network_disabled"] is False


def test_test_container_has_network_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        kwargs = _build_container_kwargs(
            workspace_root=tmp,
            image="python:3.12-slim",
            command=["pytest"],
            network_disabled=True,
            limits=SandboxLimits(),
        )
        assert kwargs["network_disabled"] is True


def test_container_runs_as_non_root_with_capabilities_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        kwargs = _build_container_kwargs(
            workspace_root=tmp,
            image="python:3.12-slim",
            command=["pytest"],
            network_disabled=True,
            limits=SandboxLimits(),
        )
        assert kwargs["user"] != "root"
        assert kwargs["user"] == "1000:1000"
        assert kwargs["cap_drop"] == ["ALL"]
        assert "no-new-privileges" in kwargs["security_opt"]


def test_container_filesystem_is_read_only_except_tmpfs_and_workspace():
    with tempfile.TemporaryDirectory() as tmp:
        kwargs = _build_container_kwargs(
            workspace_root=tmp,
            image="python:3.12-slim",
            command=["pytest"],
            network_disabled=True,
            limits=SandboxLimits(),
        )
        assert kwargs["read_only"] is True
        assert "/tmp" in kwargs["tmpfs"]


def test_workspace_is_bind_mounted_readwrite_at_correct_path():
    with tempfile.TemporaryDirectory() as tmp:
        kwargs = _build_container_kwargs(
            workspace_root=tmp,
            image="python:3.12-slim",
            command=["pytest"],
            network_disabled=True,
            limits=SandboxLimits(),
        )
        real_tmp = os.path.realpath(tmp)
        assert kwargs["volumes"][real_tmp] == {"bind": "/workspace", "mode": "rw"}
        assert kwargs["working_dir"] == "/workspace"


def test_resource_limits_are_applied_from_config():
    with tempfile.TemporaryDirectory() as tmp:
        limits = SandboxLimits(memory_limit="256m", nano_cpus=500_000_000, pids_limit=64)
        kwargs = _build_container_kwargs(
            workspace_root=tmp,
            image="python:3.12-slim",
            command=["pytest"],
            network_disabled=True,
            limits=limits,
        )
        assert kwargs["mem_limit"] == "256m"
        assert kwargs["nano_cpus"] == 500_000_000
        assert kwargs["pids_limit"] == 64


def test_command_is_passed_through_as_argv():
    with tempfile.TemporaryDirectory() as tmp:
        kwargs = _build_container_kwargs(
            workspace_root=tmp,
            image="python:3.12-slim",
            command=["pytest", "-q", "--tb=short"],
            network_disabled=True,
            limits=SandboxLimits(),
        )
        assert kwargs["command"] == ["pytest", "-q", "--tb=short"]
