from pathlib import Path

import pytest

from ai_oil.config import CONFIG_TEMPLATE, Config, default_home, load_config, write_default_config
from ai_oil.errors import ConfigError


def test_default_home_prefers_ai_oil_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AI_OIL_HOME", str(tmp_path / "explicit"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert default_home() == tmp_path / "explicit"


def test_default_home_uses_localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AI_OIL_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert default_home() == tmp_path / "local" / "AI_OIL"


def test_default_home_falls_back_to_user_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_OIL_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_home() == Path.home() / ".local" / "share" / "ai_oil"


def test_missing_config_raises_with_hint(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="oil init"):
        load_config(tmp_path / "nowhere")


def test_load_config_uses_default_home(tmp_path: Path) -> None:
    write_default_config(tmp_path / "home")
    assert load_config().home == tmp_path / "home"


def test_write_default_config_creates_layout(tmp_path: Path) -> None:
    config_path = write_default_config(tmp_path / "home")
    assert config_path == tmp_path / "home" / "config.toml"
    assert config_path.read_text(encoding="utf-8") == CONFIG_TEMPLATE
    assert (tmp_path / "home" / "blobs").is_dir()
    assert (tmp_path / "home" / "logs").is_dir()


def test_write_default_config_does_not_overwrite(tmp_path: Path) -> None:
    config_path = write_default_config(tmp_path)
    config_path.write_text("[sync]\ndone_grace_days = 3\n", encoding="utf-8")
    write_default_config(tmp_path)
    assert config_path.read_text(encoding="utf-8") == "[sync]\ndone_grace_days = 3\n"


def test_template_loads_as_defaults(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    config = load_config(tmp_path)
    assert config.storage.max_blob_mb == 25
    assert config.storage.max_text_chars == 2_000_000
    assert config.backup.folder is None
    assert config.backup.keep == 14
    assert config.sync.done_grace_days == 14


def test_custom_values_are_parsed(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[storage]\nmax_blob_mb = 5\n\n[backup]\nfolder = "D:/backup"\nkeep = 3\n\n'
        "[sync]\ndone_grace_days = 7\nunknown = 1\n\n[other]\nx = 1\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    assert config.storage.max_blob_mb == 5
    assert config.storage.max_text_chars == 2_000_000
    assert config.backup.folder == Path("D:/backup")
    assert config.backup.keep == 3
    assert config.sync.done_grace_days == 7


def test_missing_sections_take_defaults(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    config = load_config(tmp_path)
    assert config.storage.max_blob_mb == 25
    assert config.backup.folder is None


def test_wrong_type_names_the_key(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[storage]\nmax_blob_mb = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="storage.max_blob_mb"):
        load_config(tmp_path)


def test_wrong_type_for_folder_names_the_key(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[backup]\nfolder = 3\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="backup.folder"):
        load_config(tmp_path)


def test_invalid_toml_raises_config_error(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[storage\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_path_properties(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    config: Config = load_config(tmp_path)
    assert config.config_path == tmp_path / "config.toml"
    assert config.db_path == tmp_path / "ai_oil.db"
    assert config.blob_dir == tmp_path / "blobs"
    assert config.log_dir == tmp_path / "logs"
