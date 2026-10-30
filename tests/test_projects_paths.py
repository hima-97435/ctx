import os
import sys

import pytest

from ctx.core import paths
from ctx.core.blobs import BlobStore
from ctx.core.db import connect
from ctx.core.projects import (
    ProjectConfig,
    link_project,
    load_projects_config,
    load_roots,
    normalize_path,
    sync_projects,
)


class _FakeNtOs:
    """Fake os module claiming to be Windows (patching os.name globally breaks pathlib)."""

    name = "nt"

    def __init__(self):
        self.calls = []

    def __getattr__(self, attr):
        value = getattr(os, attr)
        if attr in ("open", "fsync"):

            def wrapper(*a, **k):
                self.calls.append(attr)
                return value(*a, **k)

            return wrapper
        return value


ROOTS = [("a", "/x/a"), ("ab", "/x/a/b"), ("home", "/x")]


def test_link_longest_prefix_and_boundaries():
    assert link_project("/x/a/b/c", ROOTS) == "ab"
    assert link_project("/x/a/c", ROOTS) == "a"
    assert link_project("/x/a", ROOTS) == "a"
    assert link_project("/x/abc", ROOTS) == "home"  # not under /x/a
    assert link_project("/y", ROOTS) is None
    assert link_project(None, ROOTS) is None
    assert link_project("/x/a/b/", ROOTS) == "ab"


def test_unicode_and_spaces_in_roots():
    roots = [("s", "/Users/h/Projects/Speech2Speech[ 🗣️2🗣️]/mid-sem")]
    assert link_project("/Users/h/Projects/Speech2Speech[ 🗣️2🗣️]/mid-sem/src", roots) == "s"


def test_normalize_path():
    assert normalize_path("/a/b/") == "/a/b"
    assert normalize_path("/") == "/"
    if os.name != "nt":
        assert normalize_path("C:\\a\\b") == "C:/a/b"


def test_load_config_and_sync_projects(tmp_path):
    cfg = tmp_path / "p.toml"
    cfg.write_text(
        '[projects.a]\nname = "A"\nallowed = true\nroots = ["/r1", "/r2"]\n[projects.b]\n'
    )
    configs = load_projects_config(cfg)
    assert [c.id for c in configs] == ["a", "b"]
    assert configs[1].allowed is False and configs[1].local_only is True
    conn = connect(tmp_path / "d.db")
    sync_projects(conn, configs)
    assert conn.execute("SELECT allowed, local_only FROM projects WHERE id='a'").fetchone()[:] == (
        1,
        1,
    )
    assert {r[1] for r in load_roots(conn)} == {"/r1", "/r2"}
    sync_projects(conn, [ProjectConfig(id="a", name="A2", allowed=False, roots=["/r3"])])
    assert conn.execute("SELECT name, allowed FROM projects WHERE id='a'").fetchone()[:] == (
        "A2",
        0,
    )
    assert {r[1] for r in load_roots(conn) if r[0] == "a"} == {"/r3"}


def test_load_config_none_and_repo_config_parses():
    assert load_projects_config(None) == []
    repo_cfg = paths.repo_root() / "config" / "projects.toml"
    configs = load_projects_config(repo_cfg)
    assert {c.id for c in configs} >= {"ctx", "home"}
    assert next(c for c in configs if c.id == "home").allowed is False


def test_config_path_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_HOME", str(tmp_path))
    assert paths.config_path() == paths.repo_root() / "config" / "projects.toml"
    (tmp_path / "projects.toml").write_text("")
    assert paths.config_path() == tmp_path.resolve() / "projects.toml"
    other = tmp_path / "o.toml"
    other.write_text("")
    monkeypatch.setenv("CTX_CONFIG", str(other))
    assert paths.config_path() == other


@pytest.mark.parametrize(
    ("platform", "name", "expected_part"),
    [("darwin", "posix", "Application Support"), ("linux", "posix", ".local")],
)
def test_default_data_dir_per_os(monkeypatch, platform, name, expected_part):
    if os.name == "nt":
        pytest.skip("posix-only path construction")
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert expected_part in str(paths.default_data_dir())
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg")
    if platform == "linux":
        assert str(paths.default_data_dir()).replace("\\", "/") == "/xdg/ctx"


def test_default_data_dir_windows(monkeypatch):
    monkeypatch.setattr(paths, "os", _FakeNtOs())
    monkeypatch.setenv("LOCALAPPDATA", "/local")
    assert paths.default_data_dir().name == "ctx"
    assert "local" in str(paths.default_data_dir())


def test_blobs_skip_dir_fsync_on_windows(tmp_path, monkeypatch):
    import ctx.core.blobs as blobs

    fake = _FakeNtOs()
    monkeypatch.setattr(blobs, "os", fake)
    ref = BlobStore(tmp_path).spill("hello")
    assert (tmp_path / ref).read_text() == "hello"
    assert fake.calls == ["fsync"]  # file fsync only; no directory fd opened


def test_blobs_fsync_dir_on_posix(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("posix only")
    import ctx.core.blobs as blobs

    class Posix(_FakeNtOs):
        name = "posix"

    fake = Posix()
    monkeypatch.setattr(blobs, "os", fake)
    BlobStore(tmp_path).spill("hello2")
    assert fake.calls == ["fsync", "open", "fsync"]  # file, then directory fd
