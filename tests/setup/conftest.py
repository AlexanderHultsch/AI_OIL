import argparse
import io
import shutil
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ai_oil.setup.console import Console
from ai_oil.setup.model import SetupContext
from ai_oil.setup.probe import InternetSettings


class ScriptedConsole(Console):
    """A Console fed from a queue of answers; prompts and answers are echoed into `output` like a terminal."""

    def __init__(self, answers: Sequence[str] = (), secrets: Sequence[str] = ()) -> None:
        self.answers = deque(answers)
        self.secrets = deque(secrets)
        self.prompts: list[str] = []
        self.buffer = io.StringIO()
        super().__init__(input_fn=self._input, secret_fn=self._secret, out=self.buffer)

    @property
    def output(self) -> str:
        return self.buffer.getvalue()

    def _input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.buffer.write(prompt)
        if not self.answers:
            raise EOFError
        answer = self.answers.popleft()
        self.buffer.write(answer + "\n")
        return answer

    def _secret(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.secrets:
            raise EOFError
        return self.secrets.popleft()


@dataclass
class FakeProbe:
    environ: dict[str, str] = field(default_factory=dict)
    documents: Path | None = None
    roots: list[Path] = field(default_factory=list)
    upn: str | None = None
    name: str | None = None
    windows: bool = True
    outlook: bool = True
    new_outlook: bool = False
    new_outlook_script: list[bool] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    internet: InternetSettings = field(default_factory=InternetSettings)
    user_env: dict[str, str] = field(default_factory=dict)

    def env(self, name: str) -> str | None:
        return self.environ.get(name)

    def documents_folder(self) -> Path | None:
        return self.documents

    def onedrive_roots(self) -> list[Path]:
        return list(self.roots)

    def user_principal_name(self) -> str | None:
        return self.upn

    def display_name(self) -> str | None:
        return self.name

    def is_windows(self) -> bool:
        return self.windows

    def outlook_installed(self) -> bool:
        return self.outlook

    def new_outlook_enabled(self) -> bool:
        return self.new_outlook_script.pop(0) if self.new_outlook_script else self.new_outlook

    def outlook_account_addresses(self) -> list[str]:
        return list(self.accounts)

    def internet_settings(self) -> InternetSettings:
        return self.internet

    def set_user_env(self, name: str, value: str) -> None:
        self.user_env[name] = value


@pytest.fixture
def make_console() -> Callable[..., ScriptedConsole]:
    return ScriptedConsole


@pytest.fixture
def onedrive(tmp_path: Path) -> Path:
    """A fake OneDrive sync root."""
    root = tmp_path / "OneDrive - Corp"
    root.mkdir()
    return root


@pytest.fixture
def probe(tmp_path: Path) -> FakeProbe:
    """A machine without OneDrive; tests add roots, a Documents folder and identities as needed."""
    return FakeProbe()


@pytest.fixture
def make_ctx(tmp_path: Path, probe: FakeProbe) -> Iterator[Callable[..., SetupContext]]:
    created: list[SetupContext] = []

    def build(home: Path, answers: Sequence[str] = (), *, probe: FakeProbe = probe) -> SetupContext:
        ctx = SetupContext(
            home=home,
            console=ScriptedConsole(answers),
            probe=probe,
            args=argparse.Namespace(home=home),
        )
        created.append(ctx)
        return ctx

    yield build
    for ctx in created:
        ctx.close()


@pytest.fixture
def fresh_home(tmp_path: Path) -> Path:
    """A data folder that does not exist yet."""
    return tmp_path / "data"


@pytest.fixture
def initialised_home(tmp_path: Path, home_templates) -> Path:
    """An initialised data folder whose 'me' person is Alex <alex@example.com>."""
    path = tmp_path / "initialised"
    shutil.copytree(home_templates.get("setup"), path)
    return path
