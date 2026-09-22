"""Check first-run credentials, repeat startup, paths and failure behavior without deploying."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def launcher(tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    for name in ("start.sh", ".env.example", "docker-compose.yml", "docker-compose.gpu.yml"):
        shutil.copy2(ROOT / name, project / name)
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + """
import json
import os
import sys
from pathlib import Path
from dotenv import dotenv_values
args = sys.argv[1:]
with open(os.environ['FAKE_DOCKER_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args[:2] == ['update', '--restart=no']:
    assert args[-1] == 'existing-worker'
    sys.exit(0)
if args[:3] == ['inspect', '--format', '{{.Config.Hostname}}']:
    assert args[-1] == 'existing-encoder'
    print('encoder-container-host')
    sys.exit(0)
if args == ['info']:
    sys.exit(int(os.environ.get('FAKE_INFO_ERROR', '0')))
if args == ['compose', 'version']:
    print('Docker Compose version v2.40.0')
    sys.exit(0)
assert args[0] == 'compose', args
path = Path(args[args.index('--env-file') + 1])
assert Path.cwd() == path.parent
if 'config' in args:
    values = dict(dotenv_values(path))
    values.update({key: os.environ[key] for key in values if key in os.environ})
    for key, value in values.items():
        print(f'{key}={value or ""}')
elif 'ps' in args:
    service = args[-1]
    if os.environ.get('FAKE_' + service.upper() + '_EXISTS'):
        print('existing-' + service)
elif 'stop' in args:
    assert args[-1] == 'general-upgrade'
    sys.exit(0)
elif 'build' in args:
    sys.exit(0)
elif 'up' in args:
    assert '--wait' in args
    sys.exit(int(os.environ.get('FAKE_UP_ERROR', '0')))
elif 'run' in args:
    assert args[-4:] == ['-m', 'worker.pipeline.release_migration', '--legacy-torrents', '/legacy-torrents']
    assert '--no-deps' in args and '--rm' in args
    sys.exit(int(os.environ.get('FAKE_MIGRATION_ERROR', '0')))
elif 'exec' in args:
    if '-c' in args:
        print(os.environ.get('FAKE_WORKER_STATE' if 'worker' in args else 'FAKE_ENCODER_STATE', 'ready'))
        sys.exit(0)
    if args[-2:] == ['login', 'status']:
        sys.exit(0 if os.environ.get('FAKE_LOGGED_IN') else 1)
    raise AssertionError('Noninteractive tests must never request browser authentication')
else:
    raise AssertionError(args)
"""
    )
    docker.chmod(0o755)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("API_TOKEN", "AGENT_TOKEN", "POSTGRES_PASSWORD", "STORAGE_ROOT", "PORT", "BIND_ADDRESS")
    }
    env.update(PATH=f"{binary}:{env['PATH']}", FAKE_DOCKER_LOG=str(tmp_path / "docker.log"))

    def run(*args, **overrides):
        return subprocess.run(
            [str(project / "start.sh"), *args],
            cwd=tmp_path,
            env={**env, **overrides},
            capture_output=True,
            text=True,
            timeout=20,
        )

    return project, run, Path(env["FAKE_DOCKER_LOG"])


def env_values(project):
    from dotenv import dotenv_values

    return dict(dotenv_values(project / ".env"))


def test_first_start_creates_private_distinct_secrets_and_runs_from_any_directory(launcher):
    project, run, log = launcher
    result = run()
    assert result.returncode == 0, result.stderr
    values = env_values(project)
    secrets = [values[k] for k in ("API_TOKEN", "AGENT_TOKEN", "POSTGRES_PASSWORD")]
    assert len(set(secrets)) == 3
    assert all(len(value) == 64 and all(c in "0123456789abcdef" for c in value) for value in secrets)
    assert (project / ".env").stat().st_mode & 0o777 == 0o600
    for directory in ("incoming", "jobs", "completed", "cache/agent"):
        assert (project / "data" / directory).is_dir()
    assert not list(project.glob(".env.startup.*"))
    assert all(value not in result.stdout + result.stderr + log.read_text() for value in secrets)
    assert "http://localhost:8080" in result.stdout
    assert "docker compose exec agent codex login --device-auth" in result.stdout


def test_repeat_start_does_not_rotate_credentials_or_change_custom_settings(launcher):
    project, run, log = launcher
    shutil.copy2(project / ".env.example", project / ".env")
    with (project / ".env").open("a") as stream:
        stream.write("\nCUSTOM_SETTING=preserve-me\n")
    assert run("--no-login").returncode == 0
    original = (project / ".env").read_bytes()
    result = run(FAKE_LOGGED_IN="yes")
    assert result.returncode == 0, result.stderr
    assert (project / ".env").read_bytes() == original
    assert "already authenticated" in result.stdout
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum("up" in cmd for cmd in commands) == 6


def test_storage_quotes_shell_overrides_and_env_data_are_handled_safely(launcher):
    project, run, _ = launcher
    config = (
        (project / ".env.example")
        .read_text()
        .replace("STORAGE_ROOT=./data", 'STORAGE_ROOT="./movie library"')
    )
    marker = project / "must-not-exist"
    config += f'\nUNTRUSTED=$(touch "{marker}")\n'
    (project / ".env").write_text(config)
    result = run("--no-login", PORT="18080")
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert (project / "movie library/cache/agent").is_dir()
    assert "http://localhost:18080" in result.stdout
    second_storage = project / "another library"
    result = run("--no-login", STORAGE_ROOT=str(second_storage))
    assert result.returncode == 0, result.stderr
    assert (second_storage / "incoming").is_dir()


def test_docker_unavailable_does_not_create_credentials(launcher):
    project, run, _ = launcher
    result = run(FAKE_INFO_ERROR="1")
    assert result.returncode != 0
    assert "Docker is unavailable" in result.stderr
    assert not (project / ".env").exists()


def test_startup_failure_is_reported_without_attempting_login(launcher):
    _, run, log = launcher
    result = run(FAKE_UP_ERROR="1")
    assert result.returncode != 0
    assert "docker compose logs" in result.stderr
    assert all("exec" not in json.loads(line) for line in log.read_text().splitlines())


def test_duplicate_secret_does_not_overwrite_existing_env(launcher):
    project, run, _ = launcher
    original = (project / ".env.example").read_text() + "\nAPI_TOKEN=another-placeholder\n"
    (project / ".env").write_text(original)
    result = run()
    assert result.returncode != 0
    assert "Duplicate setting" in result.stderr
    assert (project / ".env").read_text() == original


@pytest.mark.parametrize("arguments", [("--gpu", "--no-login"), ("--no-login", "--gpu")])
def test_gpu_start_adds_optional_compose_override(launcher, arguments):
    project, run, log = launcher
    result = run(*arguments)
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    startup = next(cmd for cmd in commands if "up" in cmd)
    files = [startup[i + 1] for i, arg in enumerate(startup) if arg == "-f"]
    assert files == [str(project / "docker-compose.yml"), str(project / "docker-compose.gpu.yml")]
    assert not list(project.glob(".env.startup.*"))


def test_startup_relocates_legacy_exports_and_removes_only_empty_torrent_directory(launcher):
    project, run, log = launcher
    torrents = project / "data" / "torrents"
    torrents.mkdir(parents=True)
    result = run("--no-login")
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    migration = next(command for command in commands if "run" in command)
    assert str(torrents) + ":/legacy-torrents" in migration
    assert not torrents.exists()
    torrents.mkdir()
    (torrents / "unrelated.txt").write_text("keep")
    assert run("--no-login").returncode == 0
    assert (torrents / "unrelated.txt").read_text() == "keep"


def test_application_updates_preserve_existing_encoder_and_active_legacy_worker(launcher):
    _, run, log = launcher
    result = run("--no-login", FAKE_ENCODER_EXISTS="1", FAKE_WORKER_EXISTS="1", FAKE_WORKER_STATE="busy")
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any("build" in cmd and "encoder" in cmd for cmd in commands)
    assert not any("up" in cmd and "worker" in cmd for cmd in commands)
    encoder = next(cmd for cmd in commands if "up" in cmd and "encoder" in cmd)
    assert "--no-recreate" in encoder and "--no-deps" in encoder
    assert "encodes continue uninterrupted" in result.stdout
    assert any("up" in cmd and "general-upgrade" in cmd for cmd in commands)
    assert any("cancel_consumer" in " ".join(cmd) for cmd in commands)


def test_idle_legacy_worker_can_switch_to_general_queue(launcher):
    _, run, log = launcher
    result = run("--no-login", FAKE_ENCODER_EXISTS="1", FAKE_WORKER_EXISTS="1")
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert any("up" in cmd and "worker" in cmd for cmd in commands)
    assert not any("build" in cmd and "encoder" in cmd for cmd in commands)


def test_encoder_update_requires_paused_queue_and_no_active_encodes(launcher):
    _, run, log = launcher
    result = run("--no-login", "--update-encoder", FAKE_ENCODER_EXISTS="1", FAKE_ENCODER_STATE="busy")
    assert result.returncode != 0
    assert "Pause the queue" in result.stderr
    assert not any("up" in json.loads(line) for line in log.read_text().splitlines())
    result = run("--no-login", "--update-encoder", FAKE_ENCODER_EXISTS="1")
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    encoder = next(cmd for cmd in commands if "up" in cmd and "encoder" in cmd)
    assert "--no-recreate" not in encoder and "--no-deps" in encoder
    assert any("build" in cmd and "encoder" in cmd for cmd in commands)
