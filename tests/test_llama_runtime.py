from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import tarfile
from pathlib import Path

import pytest

from geer import llama_distribution as distribution
from geer import llama_runtime as llama
from geer import runtime
from geer.assets import AssetError, Workspace
from geer.hardware import GIB


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path)
    archive = tmp_path / "runtime.tar.gz"
    files = {"llama-server": b"#!/bin/sh\nexit 0\n", "libllama.0.dylib": b"library"}
    with tarfile.open(archive, "w:gz") as output:
        for name, content in files.items():
            entry = tarfile.TarInfo(f"{distribution.LLAMA_ARCHIVE_ROOT}/{name}")
            entry.size, entry.mode = len(content), 0o755
            output.addfile(entry, io.BytesIO(content))
        entry = tarfile.TarInfo(f"{distribution.LLAMA_ARCHIVE_ROOT}/libllama.dylib")
        entry.type, entry.linkname = tarfile.SYMTYPE, "libllama.0.dylib"
        output.addfile(entry)
    manifest = {
        "schema_version": 1,
        "name": "llama.cpp",
        "version": distribution.LLAMA_VERSION,
        "revision": distribution.LLAMA_REVISION,
        "archive": {
            "bytes": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        },
        "extracted_bytes": sum(map(len, files.values())),
        "files_sha256": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
        "symlinks": {"libllama.dylib": "libllama.0.dylib"},
    }
    monkeypatch.setattr(distribution, "llama_distribution_manifest", lambda _: manifest)
    monkeypatch.setattr(llama, "llama_distribution_manifest", lambda _: manifest)
    return workspace, archive, manifest


def test_runtime_install_verifies_libraries_and_reuses_exact_release(bundle):
    workspace, archive, _ = bundle
    executable = distribution.install_llama(workspace, archive=archive)
    assert executable.is_file() and os.access(executable, os.X_OK)
    assert distribution.installed(workspace)
    assert distribution.install_llama(workspace, archive=archive) == executable
    library = executable.parent / "libllama.0.dylib"
    library.write_bytes(b"corrupt")
    assert not distribution.installed(workspace)
    distribution.install_llama(workspace, archive=archive)
    assert library.read_bytes() == b"library"


def test_install_preserves_unmanaged_directory_and_rejects_bad_archive(bundle):
    workspace, archive, _ = bundle
    directory = distribution.runtime_directory(workspace)
    directory.mkdir(parents=True)
    (directory / "personal").write_text("keep")
    with pytest.raises(AssetError, match="unmanaged"):
        distribution.install_llama(workspace, archive=archive)
    assert (directory / "personal").read_text() == "keep"
    archive.write_bytes(b"broken")
    with pytest.raises(AssetError, match="pinned"):
        distribution.verify_archive(archive, bundle[2])


def test_runtime_link_and_extra_file_corruption_are_rejected(bundle):
    workspace, archive, _ = bundle
    executable = distribution.install_llama(workspace, archive=archive)
    link = executable.parent / "libllama.dylib"
    link.unlink()
    link.symlink_to("/private/unrelated")
    assert not distribution.installed(workspace)
    distribution.install_llama(workspace, archive=archive)
    (executable.parent / "injected.dylib").write_text("unexpected")
    assert not distribution.installed(workspace)


def test_install_restores_previous_runtime_when_replacement_fails(bundle, monkeypatch):
    workspace, archive, _ = bundle
    executable = distribution.install_llama(workspace, archive=archive)
    marker = executable.parent / distribution.LLAMA_MARKER
    previous = json.loads(marker.read_text())
    previous["revision"] = "prior"
    marker.write_text(json.dumps(previous))
    replace = distribution.os.replace

    def fail_staged_install(source, destination):
        if Path(source).name == "runtime":
            raise OSError("synthetic replacement failure")
        replace(source, destination)

    monkeypatch.setattr(distribution.os, "replace", fail_staged_install)
    with pytest.raises(OSError, match="synthetic"):
        distribution.install_llama(workspace, archive=archive)
    assert executable.is_file()
    assert json.loads(marker.read_text())["revision"] == "prior"


def test_archive_extraction_rejects_links_outside_release(bundle, tmp_path):
    _, _, manifest = bundle
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        entry = tarfile.TarInfo(f"{distribution.LLAMA_ARCHIVE_ROOT}/libllama.dylib")
        entry.type, entry.linkname = tarfile.SYMTYPE, "../../escape"
        output.addfile(entry)
    with pytest.raises(AssetError, match="unexpected"):
        distribution._extract(archive, tmp_path / "stage", manifest)


def test_dependency_plan_counts_official_runtime_without_omlx(bundle):
    workspace, archive, manifest = bundle
    initial = llama.dependency_plan(workspace)
    assert initial["runtime_installed_bytes"] == manifest["extracted_bytes"]
    assert initial["runtime_download_bytes"] == archive.stat().st_size
    distribution.install_llama(workspace, archive=archive)
    plan = llama.dependency_plan(workspace)
    assert plan["runtime_reusable"]
    assert plan["runtime_download_bytes"] == plan["runtime_required_free_bytes"] == 0


@pytest.fixture
def qwen(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path)
    manifest = {
        "format": "gguf",
        "backend": "llama.cpp",
        "model_id": "geer-local",
        "model_file": "Qwen3.5-9B-Q4_K_M.gguf",
        "projector_file": "mmproj-F16.gguf",
        "linked_model": str(workspace.models / "geer-local"),
        "context_length": 262_144,
        "input_modalities": ["text", "image"],
        "build": {"recipe": "qwen3.5-9b-q4-k-m"},
        "files": [{"name": "Qwen3.5-9B-Q4_K_M.gguf"}, {"name": "mmproj-F16.gguf"}],
    }
    monkeypatch.setattr(llama, "verify_assets", lambda _: manifest)
    monkeypatch.setattr(runtime, "verify_assets", lambda _: manifest)
    monkeypatch.setattr(llama, "command", lambda _: "/managed/llama-server")
    monkeypatch.setattr(
        llama,
        "select_hardware_profile",
        lambda *a, **k: type(
            "Profile",
            (),
            {"max_context_window": 65_536},
        )(),
    )
    return workspace, manifest


def test_serve_command_keeps_vision_and_context_without_private_paths(qwen):
    workspace, _ = qwen
    command = runtime.serve_command(workspace, "127.0.0.1", 12345)
    assert command[0] == "/managed/llama-server"
    assert command[command.index("--ctx-size") + 1] == "65536"
    assert command[command.index("--predict") + 1] == "4096"
    assert command[command.index("--mmproj") + 1].endswith("mmproj-F16.gguf")
    assert command[command.index("--ctx-checkpoints") + 1] == "4"
    assert command[command.index("--cache-type-v") + 1] == "q4_0"
    assert "--no-context-shift" in command and "--offline" in command
    assert "--api-key-file" in command
    assert not any("experiments" in value or "Homebrew" in value for value in command)


@pytest.mark.parametrize("pressure,swap", [(1, 0), (2, 1024**3)])
def test_memory_guard_accepts_normal_warning_and_swap_allowance(pressure, swap):
    llama.memory_guard((pressure, swap), 0)


@pytest.mark.parametrize("sample", [(4, 0), (0, 0), (1, 1024**3 + 1)])
def test_memory_guard_stops_critical_unknown_and_excess_swap(sample):
    with pytest.raises(AssetError):
        llama.memory_guard(sample, 0)


def test_supervisor_stops_owned_child_on_memory_failure(qwen, monkeypatch):
    workspace, _ = qwen
    calls = []

    class Child:
        pid, returncode = 5678, None

        def poll(self):
            return self.returncode

        def terminate(self):
            calls.append("terminate")
            self.returncode = 0

        def wait(self, timeout=None):
            calls.append("wait")
            return self.returncode

    samples = iter([(1, 0), (4, 0)])
    monkeypatch.setattr(llama, "memory_sample", lambda: next(samples))
    monkeypatch.setattr(llama, "_identity", lambda _: "owned")
    monkeypatch.setattr(llama.subprocess, "Popen", lambda *a, **k: Child())
    with pytest.raises(AssetError, match="critical"):
        llama.run_server(workspace, "127.0.0.1", 12345)
    assert calls == ["terminate", "wait"]
    assert not (workspace.state / "llama-server.json").exists()
    assert not workspace.server_pid.exists()
    assert "critical" in json.loads((workspace.state / "llama-outcome.json").read_text())["reason"]


@pytest.mark.parametrize("model", ["geer-local", "wrong-model"])
def test_start_reuses_only_owned_authenticated_matching_model(qwen, monkeypatch, model):
    workspace, manifest = qwen
    record = {
        "pid": 5678,
        "supervisor_pid": 1234,
        "port": 12345,
        "identity": "owned",
        "supervisor_identity": "owned",
        "model_id": "geer-local",
        "asset_identity": llama._asset_identity(manifest),
    }
    monkeypatch.setattr(llama, "owned_record", lambda _: record)
    monkeypatch.setattr(llama, "_identity", lambda _: "owned")
    monkeypatch.setattr(llama, "managed_server_process", lambda *a: True)
    requests = []

    def get_json(url, **kwargs):
        requests.append(kwargs["api_key"])
        return {"status": "ok"} if url.endswith("health") else {"data": [{"id": model}]}

    monkeypatch.setattr(runtime, "get_json", get_json)
    if model == "wrong-model":
        with pytest.raises(AssetError, match="identity"):
            runtime.start_server(workspace, port=12345)
    else:
        result = runtime.start_server(workspace, port=12345)
        assert result["started"] is False and result["pid"] == 5678
    assert len(requests) == 2 and all(len(key) >= 32 for key in requests)


def test_stop_recovers_owned_listener_when_supervisor_is_gone(qwen, monkeypatch):
    workspace, _ = qwen
    stopped = []
    record = {
        "pid": 5678,
        "supervisor_pid": 1234,
        "port": 12345,
        "identity": "owned",
        "supervisor_identity": "owned",
    }
    monkeypatch.setattr(llama, "owned_record", lambda _: None if stopped else record)
    monkeypatch.setattr(
        llama, "_identity", lambda pid: "owned" if pid == 5678 and not stopped else ""
    )
    monkeypatch.setattr(llama, "_owned_child_process", lambda *a: True)
    monkeypatch.setattr(llama.os, "kill", lambda pid, sig: stopped.append((pid, sig)))
    assert runtime.stop_server(workspace)["stopped"]
    assert stopped == [(5678, signal.SIGTERM)]


def test_server_environment_contains_no_session_or_cloud_credentials(monkeypatch):
    monkeypatch.setenv("T3_MCP_BEARER_TOKEN", "session-secret")
    monkeypatch.setenv("GEER_API_KEY", "loopback-secret")
    monkeypatch.setenv("HF_TOKEN", "cloud-secret")
    monkeypatch.setenv("HOME", "/synthetic/home")
    environment = llama._server_environment()
    assert environment["HOME"] == "/synthetic/home"
    assert all("secret" not in value for value in environment.values())


def test_pi_metadata_preserves_images_context_and_generation_reserve(qwen):
    workspace, manifest = qwen
    result = runtime.ensure_pi_config(
        workspace, "http://127.0.0.1:12345", manifest, total_memory_bytes=16 * GIB
    )
    model = json.loads((workspace.pi_config / "models.json").read_text())["providers"]["geer"][
        "models"
    ][0]
    assert result["context_window"] == model["contextWindow"] == 65_536
    assert model["maxTokens"] == 4096 and model["input"] == ["text", "image"]


@pytest.mark.parametrize("thinking", ["off", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_qwen_preserves_native_thinking_selections_on_family_switch(qwen, thinking):
    workspace, manifest = qwen
    arguments = ["--mode", "rpc", "--thinking", thinking]
    result = runtime.pi_arguments(workspace, arguments, manifest)
    assert result[-len(arguments):] == arguments


def test_bootstrap_selects_qwen_before_asset_activation(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path)
    monkeypatch.setattr(llama, "bootstrap_runtime", lambda _: {"llama_cpp": "/managed/llama"})
    assert runtime.bootstrap_runtime(workspace, backend="llama.cpp") == {
        "llama_cpp": "/managed/llama"
    }
    assert not workspace.manifest.exists()


def test_qwen_stats_records_health_metadata_without_payload(qwen, monkeypatch):
    workspace, _ = qwen
    workspace.state.mkdir(parents=True)
    workspace.manifest.write_text("synthetic manifest is supplied by fixture")
    urls = []

    def health(url, **kwargs):
        urls.append(url)
        return {"status": "ok", "prompt": "private", "completion": "private"}

    monkeypatch.setattr(runtime, "get_json", health)
    result = runtime.snapshot_runtime(workspace, "http://127.0.0.1:12345")
    assert result["backend"] == "llama.cpp" and result["server_status"] == "ok"
    assert urls == ["http://127.0.0.1:12345/health"]
    assert "private" not in workspace.runtime_metrics.read_text()


def test_reused_supervisor_exit_fails_promptly(qwen, monkeypatch):
    workspace, manifest = qwen
    record = {'pid': 5678, 'supervisor_pid': 1234, 'port': 12345,
              'identity': 'owned', 'supervisor_identity': 'owned',
              'model_id': 'geer-local', 'asset_identity': llama._asset_identity(manifest)}
    records = iter([record, None])
    identities = iter(['owned', 'owned', ''])
    monkeypatch.setattr(llama, 'owned_record', lambda _: next(records, None))
    monkeypatch.setattr(llama, '_identity', lambda _: next(identities))
    monkeypatch.setattr(llama, 'managed_server_process', lambda *args: False)
    sleeps = []
    monkeypatch.setattr(llama.time, 'sleep', lambda value: sleeps.append(value))
    with pytest.raises(AssetError, match='supervisor exited'):
        runtime.start_server(workspace, port=12345)
    assert len(sleeps) <= 1


def test_old_macos_rejects_runtime_before_download(bundle, monkeypatch):
    workspace, archive, _ = bundle
    monkeypatch.setattr('geer.hardware.platform.system', lambda: 'Darwin')
    monkeypatch.setattr('geer.hardware.platform.mac_ver', lambda: ('13.2', (), ''))
    with pytest.raises(AssetError, match='macOS 13.3'):
        distribution.install_llama(workspace, archive=archive)
    assert not distribution.runtime_directory(workspace).exists()


def test_zombie_is_not_a_live_owned_identity(monkeypatch):
    import subprocess
    monkeypatch.setattr(llama.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a[0], 0,
                            f'{os.getuid()} Z Tue Nov 17 12:00:00 2026\n'))
    assert llama._identity(1234) == ''
