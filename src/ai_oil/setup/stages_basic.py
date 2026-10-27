"""The first wizard stages: data folder, me, backup (installer concept, section 5, stages 1-3)."""

import uuid
from pathlib import Path

from ai_oil import config as config_module
from ai_oil.config import default_home, write_default_config
from ai_oil.domain import people
from ai_oil.errors import ConfigError, NotFoundError
from ai_oil.setup.model import SetupContext, StageResult
from ai_oil.setup.probe import inside_onedrive

_BACKUP_FOLDER_NAME = "AI_OIL-Backup"


def _as_path(text: str) -> Path:
    return Path(text.strip().strip('"').strip("'")).expanduser()


def _write_test(folder: Path) -> str | None:
    """Create `folder` and prove it is writable; returns an error text or None."""
    probe_file = folder / f".ai_oil-write-test-{uuid.uuid4().hex}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe_file.write_text("test", encoding="utf-8")
        probe_file.unlink()
    except OSError as exc:
        return f"Cannot write to {folder}: {exc}"
    return None


def _is_inside(path: Path, parent: Path) -> bool:
    return path.resolve() == parent.resolve() or parent.resolve() in path.resolve().parents


class DataStage:
    name = "data"
    title = "Data folder"

    def run(self, ctx: SetupContext) -> StageResult:
        console = ctx.console
        home = ctx.home
        roots = ctx.probe.onedrive_roots()
        open_items: list[str] = []

        root = inside_onedrive(home, roots)
        if root is not None:
            console.say(f"The data folder {home} is inside OneDrive ({root}).")
            console.say(
                "The database must not live under a sync client: a sync can corrupt a database "
                "that is being written (spec 4.3). Please choose a folder outside OneDrive."
            )
            local_app_data = ctx.probe.env("LOCALAPPDATA")
            default = str(Path(local_app_data) / "AI_OIL") if local_app_data else None

            def validate(text: str) -> str | None:
                if not text:
                    return "Enter a folder."
                folder = _as_path(text)
                if not folder.is_absolute():
                    return "Enter the full path of the folder."
                found = inside_onedrive(folder, roots)
                if found is not None:
                    return f"{folder} is inside OneDrive ({found}) — choose a folder outside it."
                return _write_test(folder)

            home = _as_path(console.ask("Folder for the AI_OIL data", default=default, validate=validate))
            if home != default_home():
                open_items.append(
                    f"Data folder: other commands only find {home} if AI_OIL_HOME is set to it"
                    f' (e.g. `setx AI_OIL_HOME "{home}"`, then open a new window)'
                )
        else:
            error = _write_test(home)
            if error is not None:
                raise ConfigError(error)

        ctx.use_home(home)
        try:
            write_default_config(home)
        except OSError as exc:
            raise ConfigError(f"cannot create the data folder {home}: {exc}") from exc
        _ = ctx.engine  # creates the database and brings it to the latest schema
        return StageResult("done", [f"Data folder: {home}", f"Database: {ctx.config.db_path}"], open_items)


def _validate_name(text: str) -> str | None:
    return None if text.strip() else "The name must not be blank."


def _split_addresses(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _validate_addresses(text: str) -> str | None:
    addresses = _split_addresses(text)
    if not addresses:
        return "Enter at least one e-mail address."
    for address in addresses:
        if "@" not in address:
            return f"{address!r} is not an e-mail address (it needs an @)."
    return None


class MeStage:
    name = "me"
    title = "About you"

    def run(self, ctx: SetupContext) -> StageResult:
        console = ctx.console
        with ctx.engine.begin() as conn:
            try:
                me = people.get_me(conn)
            except NotFoundError:
                me = None
            known = (
                [i.external_id for i in people.list_identities(conn, me.id) if i.system == "email"]
                if me is not None
                else []
            )

        if me is not None:
            console.say(f"Currently stored: {me.display_name} <{', '.join(known)}>")
            if console.confirm("Keep these details?", default=True):
                return StageResult("done", [f"Me: {me.display_name} <{', '.join(known)}>"])

        def validate_addresses(text: str) -> str | None:
            error = _validate_addresses(text)
            if error is not None:
                return error
            with ctx.engine.connect() as conn:
                for address in _split_addresses(text):
                    owner = people.find_person_by_identity(conn, "email", address)
                    if owner is not None and (me is None or owner.id != me.id):
                        return (
                            f"{address} belongs to {owner.display_name} — leave it out, "
                            "or merge that person into you on the People page first"
                        )
            return None

        name = console.ask(
            "Your name",
            default=me.display_name if me is not None else ctx.probe.display_name(),
            validate=_validate_name,
        ).strip()
        default_addresses = ", ".join(known) if known else ctx.probe.user_principal_name()
        addresses = _split_addresses(
            console.ask(
                "Your e-mail address(es), separated by commas",
                default=default_addresses,
                validate=validate_addresses,
            )
        )

        with ctx.engine.begin() as conn:
            if me is None:
                me = people.create_person(conn, name, is_me=True)
            else:
                me = people.update_person(conn, me.id, display_name=name)
            for address in addresses:
                people.add_identity(conn, me.id, "email", address)
            stored = [i.external_id for i in people.list_identities(conn, me.id) if i.system == "email"]
        return StageResult("done", [f"Me: {me.display_name} <{', '.join(stored)}>"])


class BackupStage:
    name = "backup"
    title = "Backups"

    def run(self, ctx: SetupContext) -> StageResult:
        console = ctx.console
        roots = ctx.probe.onedrive_roots()
        documents = ctx.probe.documents_folder()

        proposal: Path | None = None
        if documents is not None and inside_onedrive(documents, roots) is not None:
            proposal = documents / _BACKUP_FOLDER_NAME
        else:
            existing = next((root for root in roots if root.is_dir()), None)
            if existing is not None:
                proposal = existing / _BACKUP_FOLDER_NAME
        if proposal is None:
            console.say(
                "Warning: no OneDrive folder was found, so backups would stay on this laptop "
                "and be lost with it. Choose a folder on a network share or a synced drive if you can."
            )

        current = ctx.config.backup.folder
        chosen_default = current if current is not None else proposal
        data_folder = ctx.home

        def validate(text: str) -> str | None:
            if not text:
                return "Enter a folder."
            folder = _as_path(text)
            if not folder.is_absolute():
                return "Enter the full path of the folder."
            if _is_inside(folder, data_folder):
                return f"The backup folder must not be inside the data folder ({data_folder})."
            return _write_test(folder)

        folder = _as_path(
            console.ask(
                "Folder for the daily backups",
                default=str(chosen_default) if chosen_default is not None else None,
                validate=validate,
            )
        )
        config_module.set_value(ctx.home, "backup", "folder", folder.as_posix())
        ctx.reload_config()
        return StageResult("done", [f"Backup folder: {folder.as_posix()}"])
