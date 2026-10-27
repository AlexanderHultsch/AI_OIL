from pathlib import Path

import pytest

from ai_oil.config import CONFIG_TEMPLATE, load_config, set_value, write_default_config
from ai_oil.errors import ConfigError


def test_replaces_the_template_line_and_keeps_everything_else(tmp_path: Path) -> None:
    config_path = write_default_config(tmp_path)

    set_value(tmp_path, "backup", "folder", "C:/Users/a/OneDrive/AI_OIL-Backup")

    assert config_path.read_text(encoding="utf-8") == CONFIG_TEMPLATE.replace(
        'folder = ""', 'folder = "C:/Users/a/OneDrive/AI_OIL-Backup"'
    )
    assert load_config(tmp_path).backup.folder == Path("C:/Users/a/OneDrive/AI_OIL-Backup")


def test_replaces_a_value_set_before(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    set_value(tmp_path, "backup", "folder", "C:/one")
    set_value(tmp_path, "backup", "folder", "C:/two")
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert text.count("folder =") == 1
    assert 'folder = "C:/two"' in text


def test_integers_and_booleans_are_toml_literals(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    set_value(tmp_path, "backup", "keep", 30)
    set_value(tmp_path, "sync", "enabled", True)
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "keep = 30\n" in text
    assert "enabled = true\n" in text
    assert load_config(tmp_path).backup.keep == 30


def test_appends_a_missing_key_to_its_section(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        "[backup]\n# where to\nkeep = 14\n\n# next section\n[sync]\ndone_grace_days = 3\n", encoding="utf-8"
    )
    set_value(tmp_path, "backup", "folder", "C:/b")
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == (
        '[backup]\n# where to\nkeep = 14\nfolder = "C:/b"\n\n# next section\n[sync]\ndone_grace_days = 3\n'
    )


def test_appends_to_the_last_section_without_trailing_newline(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[sync]\ndone_grace_days = 3", encoding="utf-8")
    set_value(tmp_path, "sync", "other", 1)
    assert (tmp_path / "config.toml").read_text(
        encoding="utf-8"
    ) == "[sync]\ndone_grace_days = 3\nother = 1\n"


def test_appends_a_missing_section(tmp_path: Path) -> None:
    config_path = write_default_config(tmp_path)
    set_value(tmp_path, "outlook", "profile", "Alex")
    assert config_path.read_text(encoding="utf-8") == CONFIG_TEMPLATE + '\n[outlook]\nprofile = "Alex"\n'


def test_a_commented_out_key_is_not_mistaken_for_the_key(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[backup] # target\n# folder = "old"\nkeep = 3\n', encoding="utf-8")
    set_value(tmp_path, "backup", "folder", "C:/b")
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == (
        '[backup] # target\n# folder = "old"\nkeep = 3\nfolder = "C:/b"\n'
    )


def test_strings_are_escaped_and_read_back_unchanged(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    tricky = 'C:\\Users\\a "quoted"\\Ünï'
    set_value(tmp_path, "backup", "folder", tricky)
    assert 'folder = "C:\\\\Users\\\\a \\"quoted\\"\\\\Ünï"' in (tmp_path / "config.toml").read_text(
        encoding="utf-8"
    )
    assert load_config(tmp_path).backup.folder == Path(tricky)


def test_crlf_line_endings_are_kept(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_bytes(b'[backup]\r\nfolder = ""\r\nkeep = 14\r\n')
    set_value(tmp_path, "backup", "folder", "C:/b")
    assert (tmp_path / "config.toml").read_bytes() == b'[backup]\r\nfolder = "C:/b"\r\nkeep = 14\r\n'


def test_no_temp_file_is_left_behind(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    set_value(tmp_path, "backup", "keep", 5)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["blobs", "config.toml", "logs"]


def test_a_value_that_would_break_the_file_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    config_path = write_default_config(tmp_path)
    with pytest.raises(ConfigError, match="invalid config"):
        set_value(tmp_path, "backup", "folder", "bad\x7fvalue")
    assert config_path.read_text(encoding="utf-8") == CONFIG_TEMPLATE


def test_an_unparsable_existing_file_is_refused_and_left_alone(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[backup\nfolder = \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid config"):
        set_value(tmp_path, "backup", "folder", "C:/b")
    assert config_path.read_text(encoding="utf-8") == "[backup\nfolder = \n"


@pytest.mark.parametrize(
    ("section", "key"), [("back up", "folder"), ("backup", "fol der"), ("backup", ""), ("", "a")]
)
def test_invalid_names_are_refused(tmp_path: Path, section: str, key: str) -> None:
    config_path = write_default_config(tmp_path)
    with pytest.raises(ConfigError, match="invalid config key"):
        set_value(tmp_path, section, key, "x")
    assert config_path.read_text(encoding="utf-8") == CONFIG_TEMPLATE


def test_unsupported_value_type_is_refused(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    with pytest.raises(ConfigError, match="unsupported"):
        set_value(tmp_path, "backup", "folder", Path("C:/b"))  # type: ignore[arg-type]


def test_uninitialised_folder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="oil init"):
        set_value(tmp_path / "nowhere", "backup", "folder", "x")
