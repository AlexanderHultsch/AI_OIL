"""Types shared by the wizard and its stages (re-exported by `ai_oil.setup.wizard`)."""

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import sqlalchemy

from ai_oil.config import Config, load_config
from ai_oil.db.engine import create_db_engine
from ai_oil.db.migrate import upgrade
from ai_oil.setup.console import Console
from ai_oil.setup.probe import SystemProbe


@dataclass
class StageResult:
    """Outcome of one stage; `status` is "done", "skipped" or "failed"."""

    status: str
    lines: list[str] = field(default_factory=list)
    open_items: list[str] = field(default_factory=list)


@dataclass
class SetupContext:
    """Everything a stage may use. The configuration and the engine are created on first access."""

    home: Path
    console: Console
    probe: SystemProbe
    args: argparse.Namespace
    state: dict[str, object] = field(default_factory=dict)
    _config: Config | None = field(default=None, init=False, repr=False)
    _engine: sqlalchemy.Engine | None = field(default=None, init=False, repr=False)

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = load_config(self.home)
        return self._config

    @property
    def engine(self) -> sqlalchemy.Engine:
        """The database engine, migrated to the latest schema on first access."""
        if self._engine is None:
            engine = create_db_engine(self.config.db_path)
            try:
                upgrade(engine)
            except BaseException:
                engine.dispose()
                raise
            self._engine = engine
        return self._engine

    def reload_config(self) -> None:
        """Forget the cached configuration, e.g. after `config.set_value`."""
        self._config = None

    def use_home(self, home: Path) -> None:
        """Switch to another data folder; engine and configuration are opened again on demand."""
        self.close()
        self._config = None
        self.home = home

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None


class Stage(Protocol):
    """One step of the wizard (installer concept, section 5)."""

    name: str
    title: str

    def run(self, ctx: SetupContext) -> StageResult: ...
