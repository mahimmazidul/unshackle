from __future__ import annotations

from pathlib import Path

from unshackle.core.config import (
    Config,
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
            "vaults": "./vaults",
            "fonts": "./fonts",
        }
    )
    assert cfg.directories.downloads == tmp_path / "downloads"
    assert cfg.directories.cache == tmp_path / "cache"
    assert cfg.directories.vaults == tmp_path / "vaults"
    assert cfg.directories.fonts == tmp_path / "fonts"


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
