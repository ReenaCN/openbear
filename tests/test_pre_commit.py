"""Exercise the real hook in isolated Git repositories with synthetic inputs only."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _env(repo):
    return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "TMPDIR": str(repo.parent / "hook-tmp")}


def _git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=repo, env=_env(repo), check=check,
                          capture_output=True, text=True, timeout=30)


def _hook(repo):
    result = subprocess.run(["bash", str(ROOT / ".githooks/pre-commit")], cwd=repo,
                            env=_env(repo), capture_output=True, text=True, timeout=30)
    assert not list((repo.parent / "hook-tmp").iterdir())
    return result


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "hook-tmp").mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "core.hooksPath", ".git/hooks")
    shutil.copyfile(ROOT / ".gitignore", repo / ".gitignore")
    (repo / "README.md").write_text("public fixture\n")
    _git(repo, "add", ".gitignore", "README.md")
    _git(repo, "commit", "-m", "fixture base")
    return repo


@pytest.mark.parametrize("name", ["evals/case.py", "evals/nested/run.json", "evals/中文.py", "evals/line\nbreak.py"])
def test_root_evals_ignored_and_force_staging_rejected(repo, name):
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("local fixture")
    assert _git(repo, "check-ignore", "--", name).returncode == 0
    _git(repo, "add", "-f", "--", name)
    result = _hook(repo)
    assert result.returncode == 1
    assert "仅本机保留的 evals" in result.stderr
    assert target.read_text() == "local fixture"


def test_evals_cached_removal_keeps_local_file_and_is_allowed(repo):
    target = repo / "evals/case.py"
    target.parent.mkdir()
    target.write_text("local fixture")
    _git(repo, "add", "-f", "evals/case.py")
    _git(repo, "commit", "-m", "legacy fixture without hook")
    _git(repo, "rm", "--cached", "--", "evals/case.py")
    assert target.read_text() == "local fixture"
    assert _hook(repo).returncode == 0
    assert not _git(repo, "ls-files", "--", "evals").stdout


def test_hook_rejects_rename_into_evals(repo):
    (repo / "evals").mkdir()
    _git(repo, "mv", "README.md", "evals/renamed.md")
    assert _hook(repo).returncode == 1


def test_root_evals_boundary_does_not_exclude_nested_example(repo):
    target = repo / "docs/evals/example.md"
    target.parent.mkdir(parents=True)
    target.write_text("public example")
    assert _git(repo, "check-ignore", "--", "docs/evals/example.md", check=False).returncode == 1
    _git(repo, "add", "docs/evals/example.md")
    assert _hook(repo).returncode == 0


def _synthetic_token():
    # Build a pattern match at runtime; never put an actual credential in fixtures.
    return "ghp_" + "A" * 36


def test_text_credential_prevents_real_commit(repo):
    token = _synthetic_token()
    (repo / "sample.txt").write_text(token + "\n")
    _git(repo, "add", "sample.txt")
    hook = repo / ".git/hooks/pre-commit"
    shutil.copyfile(ROOT / ".githooks/pre-commit", hook)
    hook.chmod(0o755)
    head = _git(repo, "rev-parse", "HEAD").stdout
    result = _git(repo, "commit", "-m", "must not commit", check=False)
    assert result.returncode == 1
    assert "命中敏感模式" in result.stderr
    assert token not in result.stdout + result.stderr
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert not list((repo.parent / "hook-tmp").iterdir())


@pytest.mark.parametrize("placement", ["start", "end", "many"])
def test_large_text_scan_is_not_affected_by_sigpipe(repo, placement):
    token = _synthetic_token()
    filler = "ordinary text\n" * 50000
    text = {"start": token + "\n" + filler,
            "end": filler + token + "\n",
            "many": (token + "\n") * 10000}[placement]
    (repo / "sample.txt").write_text(text)
    _git(repo, "add", "sample.txt")
    for _ in range(3):
        result = _hook(repo)
        assert result.returncode == 1
        assert "共命中 1 处潜在凭证" in result.stderr
        assert token not in result.stdout + result.stderr


@pytest.mark.parametrize("nul_at_end", [False, True])
def test_real_binary_with_nul_skips_content_scan(repo, nul_at_end):
    body = (_synthetic_token() + "\n" + "ordinary text\n" * 50000).encode()
    body = body + b"\0" if nul_at_end else b"\0" + body
    (repo / "sample.bin").write_bytes(body)
    _git(repo, "add", "sample.bin")
    result = _hook(repo)
    assert result.returncode == 0
    assert "命中敏感模式" not in result.stderr


@pytest.mark.parametrize("staged_secret", [False, True])
def test_hook_scans_index_instead_of_worktree(repo, staged_secret):
    token = _synthetic_token()
    target = repo / "sample.txt"
    target.write_text(token if staged_secret else "public")
    _git(repo, "add", "sample.txt")
    target.write_text("public" if staged_secret else token)
    result = _hook(repo)
    assert result.returncode == (1 if staged_secret else 0)


@pytest.mark.parametrize("name", ["中文.py", "line\nbreak.py", "tab\tname.py", "quoted\"name.py", "with space.py"])
def test_credentials_in_special_filenames_are_rejected(repo, name):
    token = _synthetic_token()
    (repo / name).write_text(token + "\n")
    _git(repo, "add", "--", name)
    result = _hook(repo)
    assert result.returncode == 1
    assert "命中敏感模式" in result.stderr
    assert token not in result.stdout + result.stderr


@pytest.mark.parametrize("name", [".env", "openbear.json", "state.db", "data/renamed.txt"])
def test_rename_to_sensitive_path_is_rejected(repo, name):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", "README.md", name)
    assert _hook(repo).returncode == 1


def test_rename_from_scan_exception_checks_destination(repo):
    token = _synthetic_token()
    (repo / "openbear.json.example").write_text(token)
    _git(repo, "add", "openbear.json.example")
    _git(repo, "commit", "-m", "fixture template")
    _git(repo, "mv", "openbear.json.example", "中文.py")
    result = _hook(repo)
    assert result.returncode == 1
    assert "命中敏感模式" in result.stderr
    assert token not in result.stderr


def test_only_deleted_sensitive_file_is_allowed(repo):
    (repo / "old.env").write_text("local fixture")
    _git(repo, "add", "-f", "old.env")
    _git(repo, "commit", "-m", "legacy fixture")
    _git(repo, "rm", "--cached", "old.env")
    assert _hook(repo).returncode == 0
    assert (repo / "old.env").read_text() == "local fixture"


def test_plain_large_text_is_allowed(repo):
    (repo / "sample.txt").write_text("ordinary text\n" * 50000)
    _git(repo, "add", "sample.txt")
    assert _hook(repo).returncode == 0
