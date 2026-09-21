from __future__ import annotations

import os
from pathlib import Path

from unshackle.core.config import (
    Config,
    _prefer_logical_home,
    _project_home,
    _resolve_user_path,
    get_config_candidates,
)


def test_bare_directory_names_resolve_under_home(tmp_path: Path) -> None:
    cfg = Config(
        directories={
            "home": str(tmp_path),
            "downloads": "downloads",
            "cache": "cache",
            "watchers": "watchers",
        }
    )
    assert cfg.directories.downloads == tmp_path / "downloads"
    assert cfg.directories.cache == tmp_path / "cache"
    assert cfg.directories.watchers == tmp_path / "watchers"


def test_dot_relative_paths_resolve_under_home(tmp_path: Path) -> None:
    cfg = Config(
        directories={
            "home": str(tmp_path),
            "downloads": "./downloads",
            "cache": "./cache",
        }
    )
    assert cfg.directories.downloads == tmp_path / "downloads"
    assert cfg.directories.cache == tmp_path / "cache"


def test_relative_code_dirs_stay_in_the_package(tmp_path: Path) -> None:
    cfg = Config(directories={"home": str(tmp_path), "vaults": "./vaults", "fonts": "./fonts"})
    assert cfg.directories.vaults == cfg.directories.namespace_dir / "vaults"
    assert cfg.directories.fonts == cfg.directories.namespace_dir / "fonts"


def test_sqlite_vault_path_joins_home(tmp_path: Path) -> None:
    cfg = Config(directories={"home": str(tmp_path)}, key_vaults=[{"type": "SQLite", "name": "Local", "path": "./key_store.db"}])
    assert cfg.key_vaults[0]["path"] == str(tmp_path / "key_store.db")


def test_tilde_and_absolute_directory_paths_are_kept(tmp_path: Path) -> None:
    cfg = Config(directories={"downloads": str(tmp_path / "media"), "logs": "~/unshackle/logs"})
    assert cfg.directories.downloads == tmp_path / "media"
    assert cfg.directories.logs == (Path.home() / "unshackle" / "logs")


def test_omitted_code_dirs_stay_package_relative() -> None:
    cfg = Config()
    assert cfg.directories.vaults == cfg.directories.namespace_dir / "vaults"
    assert cfg.directories.fonts == cfg.directories.namespace_dir / "fonts"


def test_resolve_user_path_joins_relative() -> None:
    base = Path("/tmp/unshackle-home")
    assert _resolve_user_path("downloads", relative_to=base) == base / "downloads"
    assert _resolve_user_path("./downloads", relative_to=base) == base / "downloads"
    assert _resolve_user_path("/abs/out", relative_to=base) == Path("/abs/out")


def test_default_data_dirs_follow_venv_parent(tmp_path: Path, monkeypatch) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = python\n", encoding="utf-8")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.chdir(tmp_path)
    assert _project_home() == tmp_path
    cfg = Config()
    assert cfg.directories.home == tmp_path
    assert cfg.directories.downloads == tmp_path / "downloads"
    assert cfg.directories.cache == tmp_path / "cache"
    assert cfg.directories.vaults == cfg.directories.namespace_dir / "vaults"


def test_prefer_logical_home_uses_cwd_when_samefile(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "clone"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    monkeypatch.chdir(target)
    assert _prefer_logical_home(alias) == Path.cwd()


def test_prefer_logical_home_rewrites_homeNN_via_samefile(monkeypatch) -> None:
    def fake_samefile(left, right) -> bool:
        def norm(value) -> str:
            return os.fspath(value).replace("/home28/alice", "/home/alice")

        return norm(left) == norm(right)

    monkeypatch.setattr(os.path, "samefile", fake_samefile)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home28/alice")))
    assert _prefer_logical_home(Path("/home28/alice/.local/unshackle")) == Path("/home/alice/.local/unshackle")
    assert _prefer_logical_home(Path("/var/unshackle")) == Path("/var/unshackle")


def test_clone_services_dir_is_prepended(tmp_path: Path) -> None:
    (tmp_path / "services").mkdir()
    cfg = Config(directories={"home": str(tmp_path)})
    assert cfg.directories.services[0] == tmp_path / "services"
    assert cfg.directories.namespace_dir / "services" in cfg.directories.services


def test_config_candidates_include_venv_sibling(tmp_path: Path, monkeypatch) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = python\n", encoding="utf-8")
    yaml_path = tmp_path / "unshackle" / "unshackle.yaml"
    yaml_path.parent.mkdir()
    yaml_path.write_text("tag: TEST\n", encoding="utf-8")
    monkeypatch.setattr("unshackle.core.config.sys.prefix", str(venv))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    candidates = get_config_candidates()
    assert yaml_path.resolve() in {path.resolve() for path in candidates}
