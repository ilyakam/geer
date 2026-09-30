from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from geer.assets import AssetError, Workspace
from geer.skills import ensure_skills_directory, inspect_skill_commands, skill_paths


def test_private_skills_directory_preserves_legacy_content(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    legacy = workspace.pi_config / "skills" / "custom"
    legacy.mkdir(parents=True)
    original = legacy / "SKILL.md"
    content = b"---\nname: custom\ndescription: My original skill\n---\nOriginal body\n"
    original.write_bytes(content)

    directory = ensure_skills_directory(workspace)

    assert directory == workspace.runtime / "skills"
    assert directory.is_dir() and not directory.is_symlink()
    assert directory.stat().st_mode & 0o777 == 0o700
    assert original.read_bytes() == content
    private = directory / "private.md"
    private.write_bytes(b"Private user content")
    assert ensure_skills_directory(workspace) == directory
    assert private.read_bytes() == b"Private user content"


@pytest.mark.parametrize("symlink", [False, True])
def test_private_skills_directory_refuses_unexpected_paths(tmp_path: Path, symlink: bool) -> None:
    workspace = Workspace(tmp_path)
    workspace.runtime.mkdir()
    directory = workspace.runtime / "skills"
    if symlink:
        target = tmp_path / "custom-skills"
        target.mkdir()
        (target / "keep").write_text("Retain custom target")
        directory.symlink_to(target, target_is_directory=True)
    else:
        directory.write_text("Retain unexpected file")

    with pytest.raises(AssetError, match="unexpected Geer skills path"):
        ensure_skills_directory(workspace)

    if symlink:
        assert directory.is_symlink()
        assert directory.readlink() == target
        assert (target / "keep").read_text() == "Retain custom target"
    else:
        assert directory.read_text() == "Retain unexpected file"


def test_skill_paths_reference_all_conventional_roots_without_mutation(tmp_path: Path) -> None:
    home = tmp_path / "user"
    workspace = Workspace(tmp_path, home / ".geer")
    expected = [
        home / ".geer/skills",
        home / ".geer/pi/skills",
        home / ".agents/skills",
        home / ".pi/agent/skills",
        home / ".claude/skills",
        home / ".codex/skills",
        home / ".config/opencode/skills",
        home / ".cursor/skills",
    ]
    originals = {}
    for index, directory in enumerate(expected):
        directory.mkdir(parents=True)
        path = directory / "SKILL.md"
        content = f"Original skill bytes {index}".encode()
        path.write_bytes(content)
        originals[path] = content

    assert skill_paths(workspace, home=home, environ={}) == [path.resolve() for path in expected]
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert not any(path.is_symlink() for path in expected)


def test_skill_paths_do_not_create_missing_roots(tmp_path: Path) -> None:
    home = tmp_path / "user"
    workspace = Workspace(tmp_path, home / ".geer")

    assert skill_paths(workspace, home=home, environ={}) == []
    assert not home.exists()


def test_skill_paths_preserve_legacy_symlink_and_deduplicate_aliases(tmp_path: Path) -> None:
    home = tmp_path / "user"
    workspace = Workspace(tmp_path, home / ".geer")
    private = ensure_skills_directory(workspace)
    target = tmp_path / "original-skills"
    target.mkdir()
    original = target / "SKILL.md"
    original.write_bytes(b"Original borrowed bytes")
    legacy = workspace.pi_config / "skills"
    legacy.parent.mkdir()
    legacy.symlink_to(target, target_is_directory=True)
    shared = home / ".agents/skills"
    shared.parent.mkdir()
    shared.symlink_to(target, target_is_directory=True)
    pi = home / ".pi/agent/skills"
    pi.parent.mkdir(parents=True)
    pi.symlink_to(private, target_is_directory=True)

    assert skill_paths(workspace, home=home, environ={}) == [private.resolve(), target.resolve()]
    assert legacy.is_symlink() and legacy.readlink() == target
    assert shared.is_symlink() and shared.readlink() == target
    assert original.read_bytes() == b"Original borrowed bytes"


def test_skill_paths_keep_custom_legacy_reference_ahead_of_shared(tmp_path: Path) -> None:
    home = tmp_path / "user"
    workspace = Workspace(tmp_path, home / ".geer")
    private = ensure_skills_directory(workspace)
    custom = tmp_path / "custom-legacy"
    custom.mkdir()
    legacy = workspace.pi_config / "skills"
    legacy.parent.mkdir()
    legacy.symlink_to(custom, target_is_directory=True)
    shared = home / ".agents/skills"
    shared.mkdir(parents=True)

    assert skill_paths(workspace, home=home, environ={}) == [
        private.resolve(), custom.resolve(), shared.resolve(),
    ]
    assert legacy.readlink() == custom


def test_skill_paths_honor_configured_pi_and_xdg_directories(tmp_path: Path) -> None:
    home = tmp_path / "user"
    workspace = Workspace(tmp_path, home / ".geer")
    default_pi = home / ".pi/agent/skills"
    configured_pi = home / "custom-pi/skills"
    configured_opencode = tmp_path / "xdg/opencode/skills"
    for directory in (default_pi, configured_pi, configured_opencode):
        directory.mkdir(parents=True)

    assert skill_paths(workspace, home=home, environ={
        "PI_CODING_AGENT_DIR": "~/custom-pi",
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    }) == [configured_pi.resolve(), configured_opencode.resolve()]


def _probe_executable(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "pi-fixture"
    executable.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    executable.chmod(0o700)
    return executable


def test_native_inspection_isolated_and_preserves_command_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "borrowed"
    source.mkdir()
    skill = source / "SKILL.md"
    skill.write_bytes(b"Unmodified borrowed skill")
    capture = tmp_path / "capture.json"
    executable = _probe_executable(tmp_path, """
        import json, os, pathlib, sys
        requests = sys.stdin.read().splitlines()
        assert len(requests) == 1
        request = json.loads(requests[0])
        assert request['type'] == 'get_commands'
        agent = pathlib.Path(os.environ['PI_CODING_AGENT_DIR'])
        home = pathlib.Path(os.environ['HOME'])
        assert agent.parent == home == pathlib.Path.cwd()
        assert os.environ['PI_OFFLINE'] == '1'
        assert os.environ['PI_TELEMETRY'] == '0'
        assert os.environ['PI_SKIP_VERSION_CHECK'] == '1'
        assert os.environ['PI_CODING_AGENT_SESSION_DIR'] == str(home / 'sessions')
        assert json.loads((agent / 'settings.json').read_text())['enableAnalytics'] is False
        assert (agent / 'models.json').stat().st_mode & 0o777 == 0o600
        assert agent.stat().st_mode & 0o777 == 0o700
        assert not (agent / 'mcp.json').exists()
        assert not (agent / 'auth.json').exists()
        for flag in ['--offline', '--no-session', '--no-approve', '--no-context-files',
                     '--no-tools', '--no-extensions', '--no-skills',
                     '--no-prompt-templates', '--no-themes']:
            assert flag in sys.argv
        pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({
            'home': str(home), 'argv': sys.argv,
        }))
        print(json.dumps({'type': 'response', 'command': 'get_commands',
                          'id': request['id'], 'success': True,
                          'data': {'commands': [
            {'name': 'skill:manual', 'description': 'Explicit invocation only',
             'source': 'skill', 'sourceInfo': {'path': os.environ['SKILL'],
              'baseDir': str(pathlib.Path(os.environ['SKILL']).parent), 'scope': 'temporary'}},
            {'name': 'other', 'source': 'extension'},
        ]}}))
    """)
    environment = {
        "HOME": str(tmp_path / "personal-home"),
        "PI_CODING_AGENT_DIR": str(tmp_path / "personal-pi"),
        "CAPTURE": str(capture),
        "SKILL": str(skill),
    }
    before = dict(environment)
    monkeypatch.chdir(tmp_path)

    commands = inspect_skill_commands(Path(executable.name), iter([source]), environment)

    assert commands == [{
        "name": "skill:manual", "description": "Explicit invocation only", "source": "skill",
        "sourceInfo": {"path": str(skill), "baseDir": str(source), "scope": "temporary"},
    }]
    probe = json.loads(capture.read_text())
    arguments = probe["argv"]
    assert arguments[arguments.index("--skill") + 1] == str(source)
    assert not Path(probe["home"]).exists()
    assert environment == before
    assert not (tmp_path / "personal-home").exists()
    assert not (tmp_path / "personal-pi").exists()
    assert skill.read_bytes() == b"Unmodified borrowed skill"


@pytest.mark.parametrize("payload, error", [
    ("not JSON", "invalid RPC JSON"),
    ("[]", "invalid RPC JSON"),
    ("{}", "no get_commands response"),
    ('{"type":"response","command":"prompt","id":"geer-skills","success":true}',
     "no get_commands response"),
    ('{"type":"response","command":"get_commands","id":"geer-skills","success":true,"data":{}}',
     "invalid command catalog"),
    ('{"type":"response","command":"get_commands","id":"geer-skills","success":true,'
     '"data":{"commands":[1]}}', "invalid command catalog"),
    ('{"type":"response","command":"get_commands","id":"geer-skills","success":false,'
     '"error":"Rejected"}', "rejected get_commands: Rejected"),
])
def test_native_inspection_rejects_invalid_responses(
    tmp_path: Path, payload: str, error: str,
) -> None:
    capture = tmp_path / "probe-home"
    executable = _probe_executable(tmp_path, f"""
        import os, pathlib
        pathlib.Path(os.environ['CAPTURE']).write_text(os.environ['HOME'])
        print({payload!r})
    """)

    with pytest.raises(AssetError, match=error):
        inspect_skill_commands(executable, [], {"CAPTURE": str(capture)})

    assert not Path(capture.read_text()).exists()


@pytest.mark.parametrize("command", [
    {"name": 7, "description": "Invalid name", "sourceInfo": {"path": "/original/SKILL.md"}},
    {"name": "skill:", "description": "Empty name", "sourceInfo": {"path": "/original/SKILL.md"}},
    {"name": "skill:example", "description": "Invalid source", "sourceInfo": {"path": 7}},
    {"name": "skill:example", "description": "Missing source"},
])
def test_native_inspection_rejects_malformed_skill_records(
    tmp_path: Path, command: dict,
) -> None:
    command = {"source": "skill", **command}
    payload = json.dumps({
        "type": "response", "command": "get_commands", "id": "geer-skills", "success": True,
        "data": {"commands": [command]},
    })
    executable = _probe_executable(tmp_path, f"print({payload!r})")

    with pytest.raises(AssetError, match="invalid skill metadata"):
        inspect_skill_commands(executable, [], {})


def test_native_inspection_reports_child_failure_and_diagnostics(tmp_path: Path) -> None:
    executable = _probe_executable(tmp_path, """
        import sys
        print('Invalid Pi configuration fixture', file=sys.stderr)
        sys.exit(7)
    """)
    with pytest.raises(AssetError, match=r"exit 7.*Invalid Pi configuration fixture"):
        inspect_skill_commands(executable, [], {})


def test_native_inspection_supplied_timeout_cleans_up(tmp_path: Path) -> None:
    capture = tmp_path / "probe-home"
    executable = _probe_executable(tmp_path, """
        import os, pathlib, time
        pathlib.Path(os.environ['CAPTURE']).write_text(os.environ['HOME'])
        time.sleep(10)
    """)
    started = time.monotonic()
    with pytest.raises(AssetError, match="skill inspection timed out"):
        inspect_skill_commands(executable, [], {"CAPTURE": str(capture)}, timeout=0.2)

    assert time.monotonic() - started < 1
    assert not Path(capture.read_text()).exists()


def test_native_inspection_reports_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(AssetError, match="cannot inspect Pi skills"):
        inspect_skill_commands(tmp_path / "missing-pi", [], {})


def test_cached_native_pi_catalog_handles_aliases_collisions_and_manual_skills(
    tmp_path: Path,
) -> None:
    binary = Path(__file__).resolve().parents[1] / "build/pi-runtime/pi/pi"
    if not binary.is_file():
        pytest.skip("cached pinned Pi runtime is unavailable")
    private = tmp_path / "private"
    shared = tmp_path / "shared"
    codex = tmp_path / "codex"
    skills = [
        (private / "example/SKILL.md", "name: example\ndescription: Private winner"),
        (shared / "example/SKILL.md", "name: example\ndescription: Borrowed loser"),
        (shared / "manual/SKILL.md", "name: manual\ndescription: Manual only\n"
         "disable-model-invocation: true"),
        (shared / "folder-name/SKILL.md", "description: Native fallback name"),
        (shared / "ignored/SKILL.md", "name: ignored\ndescription: Ignored skill"),
        (shared / "malformed/SKILL.md", "name: [not valid\ndescription: Malformed YAML"),
        (codex / ".system/internal/SKILL.md", "name: internal\ndescription: System skill"),
    ]
    originals = {}
    for path, metadata in skills:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"---\n{metadata}\n---\nRead references/example.txt.\n".encode()
        path.write_bytes(content)
        originals[path] = content
    support = private / "example/references/example.txt"
    support.parent.mkdir()
    support.write_bytes(b"Original supporting asset")
    originals[support] = support.read_bytes()
    (shared / ".ignore").write_text("ignored/\n")
    alias = tmp_path / "private-alias"
    alias.symlink_to(private, target_is_directory=True)

    started = time.monotonic()
    commands = inspect_skill_commands(binary, [private, alias, shared, codex], {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    })

    assert time.monotonic() - started < 4
    assert {command["name"]: command["description"] for command in commands} == {
        "skill:example": "Private winner",
        "skill:manual": "Manual only",
        "skill:folder-name": "Native fallback name",
    }
    winner = next(command for command in commands if command["name"] == "skill:example")
    assert Path(winner["sourceInfo"]["path"]).resolve() == private / "example/SKILL.md"
    assert Path(winner["sourceInfo"]["baseDir"]).resolve() / "references/example.txt" == support
    assert all(path.read_bytes() == content for path, content in originals.items())
