import os
import tomllib
from pathlib import Path

import pytest

from ai_oil.config import CONFIG_TEMPLATE, load_config, set_value
from ai_oil.db.engine import create_db_engine
from ai_oil.domain import people
from ai_oil.errors import ConfigError, ConflictError
from ai_oil.setup.console import SkipStage
from ai_oil.setup.stages_basic import BackupStage, DataStage, MeStage


def _me(home: Path) -> tuple[people.Person, list[str]]:
    engine = create_db_engine(load_config(home).db_path)
    try:
        with engine.connect() as conn:
            me = people.get_me(conn)
            emails = [i.external_id for i in people.list_identities(conn, me.id) if i.system == "email"]
    finally:
        engine.dispose()
    return me, emails


# --- data folder -------------------------------------------------------------------------------


def test_data_fresh_run_creates_folder_config_and_database(make_ctx, fresh_home: Path) -> None:
    ctx = make_ctx(fresh_home)

    result = DataStage().run(ctx)

    assert result.status == "done"
    assert result.lines == [f"Data folder: {fresh_home}", f"Database: {fresh_home / 'ai_oil.db'}"]
    assert result.open_items == []
    assert (fresh_home / "config.toml").read_text(encoding="utf-8") == CONFIG_TEMPLATE
    assert (fresh_home / "blobs").is_dir() and (fresh_home / "logs").is_dir()
    assert (fresh_home / "ai_oil.db").is_file()
    assert ctx.console.prompts == []


def test_data_rerun_keeps_everything(make_ctx, initialised_home: Path) -> None:
    config_path = initialised_home / "config.toml"
    set_value(initialised_home, "backup", "keep", 3)
    before = config_path.read_text(encoding="utf-8")
    ctx = make_ctx(initialised_home)

    first = DataStage().run(ctx)
    second = DataStage().run(ctx)

    assert first == second
    assert config_path.read_text(encoding="utf-8") == before
    assert _me(initialised_home)[0].display_name == "Alex"
    assert ctx.console.prompts == []


def test_data_inside_onedrive_asks_for_another_folder(
    make_ctx, probe, onedrive: Path, tmp_path: Path
) -> None:
    probe.roots = [onedrive]
    probe.environ["LOCALAPPDATA"] = str(tmp_path / "local")
    inside = onedrive / "AI_OIL"
    ctx = make_ctx(inside, [""], probe=probe)

    result = DataStage().run(ctx)

    local = tmp_path / "local" / "AI_OIL"
    assert ctx.console.prompts == [f"Folder for the AI_OIL data [{local}]: "]
    assert f"The data folder {inside} is inside OneDrive ({onedrive})" in ctx.console.output
    assert "corrupt" in ctx.console.output
    assert ctx.home == local
    assert result.lines == [f"Data folder: {local}", f"Database: {local / 'ai_oil.db'}"]
    assert (local / "ai_oil.db").is_file()
    assert not inside.exists()
    assert len(result.open_items) == 1
    assert "AI_OIL_HOME" in result.open_items[0] and str(local) in result.open_items[0]


def test_data_refuses_an_alternative_inside_onedrive_and_unwritable_ones(
    make_ctx, probe, onedrive: Path, tmp_path: Path
) -> None:
    probe.roots = [onedrive]
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    good = tmp_path / "elsewhere" / "AI_OIL"
    answers = [str(onedrive / "Other"), "relative/dir", str(blocker / "sub"), f'"{good}"']
    ctx = make_ctx(onedrive / "AI_OIL", answers, probe=probe)

    result = DataStage().run(ctx)

    output = ctx.console.output
    assert f"{onedrive / 'Other'} is inside OneDrive ({onedrive}) — choose a folder outside it." in output
    assert "Enter the full path of the folder." in output
    assert f"Cannot write to {blocker / 'sub'}" in output
    assert ctx.home == good
    assert result.status == "done"
    assert not (onedrive / "Other").exists()
    assert not os.listdir(onedrive)
    assert not list(good.glob(".ai_oil-write-test-*"))


def test_data_without_localappdata_has_no_default(make_ctx, probe, onedrive: Path, tmp_path: Path) -> None:
    probe.roots = [onedrive]
    target = tmp_path / "t"
    ctx = make_ctx(onedrive / "AI_OIL", ["", str(target)], probe=probe)

    DataStage().run(ctx)

    assert ctx.console.prompts == ["Folder for the AI_OIL data: "] * 2
    assert "Enter a folder." in ctx.console.output
    assert ctx.home == target


def test_data_skip_in_the_question(make_ctx, probe, onedrive: Path) -> None:
    probe.roots = [onedrive]
    with pytest.raises(SkipStage):
        DataStage().run(make_ctx(onedrive / "AI_OIL", ["s"], probe=probe))
    assert not (onedrive / "AI_OIL").exists()


def test_data_unwritable_home_outside_onedrive_fails(make_ctx, tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="Cannot write to"):
        DataStage().run(make_ctx(blocker / "data"))


# --- me ------------------------------------------------------------------------------------------


def test_me_fresh_run_creates_me_from_the_detected_defaults(make_ctx, probe, fresh_home: Path) -> None:
    probe.name = "Alexander Hultsch"
    probe.upn = "Alexander.Hultsch@corp.com"
    ctx = make_ctx(fresh_home, ["", ""], probe=probe)
    DataStage().run(ctx)

    result = MeStage().run(ctx)

    assert ctx.console.prompts == [
        "Your name [Alexander Hultsch]: ",
        "Your e-mail address(es), separated by commas [Alexander.Hultsch@corp.com]: ",
    ]
    assert result.lines == ["Me: Alexander Hultsch <alexander.hultsch@corp.com>"]
    me, emails = _me(fresh_home)
    assert me.is_me and me.display_name == "Alexander Hultsch"
    assert emails == ["alexander.hultsch@corp.com"]


def test_me_without_detection_asks_everything_and_reasks_on_bad_input(make_ctx, fresh_home: Path) -> None:
    ctx = make_ctx(fresh_home, ["", "Alex D", "", "alex.corp", "a@corp.com, B@corp.com ,"])
    DataStage().run(ctx)

    result = MeStage().run(ctx)

    output = ctx.console.output
    assert "The name must not be blank." in output
    assert "Enter at least one e-mail address." in output
    assert "'alex.corp' is not an e-mail address (it needs an @)." in output
    assert result.lines == ["Me: Alex D <a@corp.com, b@corp.com>"]
    assert _me(fresh_home)[1] == ["a@corp.com", "b@corp.com"]


def test_me_keep_changes_nothing(make_ctx, initialised_home: Path) -> None:
    ctx = make_ctx(initialised_home, [""])

    result = MeStage().run(ctx)

    assert result.status == "done"
    assert result.lines == ["Me: Alex <alex@example.com>"]
    assert "Currently stored: Alex <alex@example.com>" in ctx.console.output
    assert ctx.console.prompts == ["Keep these details? [Y/n]: "]
    assert _me(initialised_home)[1] == ["alex@example.com"]


def test_me_update_offers_current_values_and_keeps_existing_identities(
    make_ctx, probe, initialised_home: Path
) -> None:
    probe.name = "Detected Name"
    probe.upn = "detected@corp.com"
    ctx = make_ctx(initialised_home, ["n", "Alexander Hultsch", "alexander@corp.com"], probe=probe)

    result = MeStage().run(ctx)

    assert ctx.console.prompts[1:] == [
        "Your name [Alex]: ",
        "Your e-mail address(es), separated by commas [alex@example.com]: ",
    ]
    assert result.lines == ["Me: Alexander Hultsch <alex@example.com, alexander@corp.com>"]
    me, emails = _me(initialised_home)
    assert me.display_name == "Alexander Hultsch"
    assert emails == ["alex@example.com", "alexander@corp.com"]


def test_me_update_accepting_the_defaults_changes_nothing(make_ctx, initialised_home: Path) -> None:
    ctx = make_ctx(initialised_home, ["n", "", ""])
    MeStage().run(ctx)
    me, emails = _me(initialised_home)
    assert (me.display_name, emails) == ("Alex", ["alex@example.com"])


def test_me_address_of_another_person_is_a_conflict(make_ctx, initialised_home: Path) -> None:
    engine = create_db_engine(load_config(initialised_home).db_path)
    try:
        with engine.begin() as conn:
            other = people.create_person(conn, "Pat")
            people.add_identity(conn, other.id, "email", "pat@corp.com")
    finally:
        engine.dispose()
    ctx = make_ctx(initialised_home, ["n", "Alex", "alex@example.com, pat@corp.com"])

    with pytest.raises(ConflictError, match="Pat"):
        MeStage().run(ctx)


def test_me_skip(make_ctx, initialised_home: Path) -> None:
    with pytest.raises(SkipStage):
        MeStage().run(make_ctx(initialised_home, ["n", "s"]))
    assert _me(initialised_home)[0].display_name == "Alex"


def test_me_on_an_uninitialised_folder_fails_with_the_init_hint(make_ctx, fresh_home: Path) -> None:
    with pytest.raises(ConfigError, match="oil init"):
        MeStage().run(make_ctx(fresh_home))


# --- backup --------------------------------------------------------------------------------------


def _backup_folder(home: Path) -> Path | None:
    return load_config(home).backup.folder


def test_backup_proposes_documents_when_documents_is_in_onedrive(
    make_ctx, probe, onedrive: Path, initialised_home: Path
) -> None:
    documents = onedrive / "Documents"
    documents.mkdir()
    probe.roots = [onedrive]
    probe.documents = documents
    ctx = make_ctx(initialised_home, [""], probe=probe)

    result = BackupStage().run(ctx)

    target = documents / "AI_OIL-Backup"
    assert ctx.console.prompts == [f"Folder for the daily backups [{target}]: "]
    assert result.lines == [f"Backup folder: {target.as_posix()}"]
    assert target.is_dir()
    assert not list(target.iterdir())
    assert _backup_folder(initialised_home) == target


def test_backup_proposes_the_onedrive_root_when_documents_is_not_synced(
    make_ctx, probe, onedrive: Path, initialised_home: Path, tmp_path: Path
) -> None:
    probe.roots = [tmp_path / "missing-root", onedrive]
    probe.documents = tmp_path / "Documents"
    ctx = make_ctx(initialised_home, [""], probe=probe)

    BackupStage().run(ctx)

    assert ctx.console.prompts == [f"Folder for the daily backups [{onedrive / 'AI_OIL-Backup'}]: "]
    assert _backup_folder(initialised_home) == onedrive / "AI_OIL-Backup"


def test_backup_without_onedrive_warns_and_has_no_proposal(
    make_ctx, probe, initialised_home: Path, tmp_path: Path
) -> None:
    probe.documents = tmp_path / "Documents"
    probe.roots = [tmp_path / "missing-root"]
    target = tmp_path / "local-backups"
    ctx = make_ctx(initialised_home, ["", str(target)], probe=probe)

    BackupStage().run(ctx)

    assert "no OneDrive folder was found" in ctx.console.output
    assert "stay on this laptop" in ctx.console.output
    assert ctx.console.prompts == ["Folder for the daily backups: "] * 2
    assert "Enter a folder." in ctx.console.output
    assert _backup_folder(initialised_home) == target


def test_backup_rerun_offers_the_current_value_over_the_proposal(
    make_ctx, probe, onedrive: Path, initialised_home: Path, tmp_path: Path
) -> None:
    probe.roots = [onedrive]
    chosen = tmp_path / "chosen"
    BackupStage().run(make_ctx(initialised_home, [str(chosen)], probe=probe))
    ctx = make_ctx(initialised_home, [""], probe=probe)

    result = BackupStage().run(ctx)

    assert ctx.console.prompts == [f"Folder for the daily backups [{chosen}]: "]
    assert result.lines == [f"Backup folder: {chosen.as_posix()}"]
    assert _backup_folder(initialised_home) == chosen


def test_backup_folder_inside_the_data_folder_is_refused(
    make_ctx, probe, initialised_home: Path, tmp_path: Path
) -> None:
    good = tmp_path / "good"
    answers = [str(initialised_home), str(initialised_home / "backups"), "relative", str(good)]
    ctx = make_ctx(initialised_home, answers, probe=probe)

    BackupStage().run(ctx)

    assert ctx.console.output.count("must not be inside the data folder") == 2
    assert "Enter the full path of the folder." in ctx.console.output
    assert not (initialised_home / "backups").exists()
    assert _backup_folder(initialised_home) == good


def test_backup_unwritable_folder_is_reasked(make_ctx, probe, initialised_home: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    good = tmp_path / "good"
    ctx = make_ctx(initialised_home, [str(blocker / "sub"), str(good)], probe=probe)

    BackupStage().run(ctx)

    assert f"Cannot write to {blocker / 'sub'}" in ctx.console.output
    assert _backup_folder(initialised_home) == good


def test_backup_writes_config_with_comments_and_other_values_intact(
    make_ctx, probe, initialised_home: Path, tmp_path: Path
) -> None:
    target = tmp_path / "Backup Ü"
    ctx = make_ctx(initialised_home, [str(target)], probe=probe)

    BackupStage().run(ctx)

    text = (initialised_home / "config.toml").read_text(encoding="utf-8")
    assert text == CONFIG_TEMPLATE.replace('folder = ""', f'folder = "{target.as_posix()}"')
    assert "# Folder for daily backups" in text
    assert tomllib.loads(text)["backup"] == {"folder": target.as_posix(), "keep": 14}
    assert ctx.config.backup.folder == target


@pytest.mark.skipif(os.name != "nt", reason="backslash separators exist only on Windows")
def test_backup_stores_a_windows_path_with_forward_slashes(make_ctx, probe, initialised_home: Path) -> None:
    folder = initialised_home.parent / "win-backup"
    ctx = make_ctx(initialised_home, [str(folder)], probe=probe)

    BackupStage().run(ctx)

    stored = tomllib.loads((initialised_home / "config.toml").read_text(encoding="utf-8"))["backup"]["folder"]
    assert "\\" not in stored and stored == folder.as_posix()


def test_backup_skip_changes_nothing(make_ctx, probe, initialised_home: Path) -> None:
    with pytest.raises(SkipStage):
        BackupStage().run(make_ctx(initialised_home, ["s"], probe=probe))
    assert (initialised_home / "config.toml").read_text(encoding="utf-8") == CONFIG_TEMPLATE


def test_backup_on_an_uninitialised_folder_fails_with_the_init_hint(make_ctx, fresh_home: Path) -> None:
    with pytest.raises(ConfigError, match="oil init"):
        BackupStage().run(make_ctx(fresh_home))


def test_all_three_stages_in_a_row_on_a_fresh_machine(
    make_ctx, probe, onedrive: Path, tmp_path: Path
) -> None:
    documents = onedrive / "Documents"
    documents.mkdir()
    probe.roots = [onedrive]
    probe.documents = documents
    probe.name = "Alex"
    probe.upn = "alex@corp.com"
    probe.environ["LOCALAPPDATA"] = str(tmp_path / "local")
    ctx = make_ctx(onedrive / "AI_OIL", ["", "", "", ""], probe=probe)

    results = [stage.run(ctx) for stage in (DataStage(), MeStage(), BackupStage())]

    assert [r.status for r in results] == ["done", "done", "done"]
    home = tmp_path / "local" / "AI_OIL"
    assert _me(home)[1] == ["alex@corp.com"]
    assert _backup_folder(home) == documents / "AI_OIL-Backup"
