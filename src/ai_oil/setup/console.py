"""Console interaction of the setup wizard: ask, confirm, choose — with defaults, validation and skipping."""

import getpass
import re
import sys
from collections.abc import Callable, Sequence
from typing import TextIO


class SkipStage(Exception):
    """The user typed `s`: leave the current stage and carry on with the next."""


class Console:
    """Prompts on a terminal. End of input aborts the wizard (KeyboardInterrupt), it never skips."""

    def __init__(
        self,
        input_fn: Callable[[str], str] = input,
        secret_fn: Callable[[str], str] = getpass.getpass,
        out: TextIO | None = None,
    ) -> None:
        self._input_fn = input_fn
        self._secret_fn = secret_fn
        self._out = out

    @property
    def out(self) -> TextIO:
        return self._out if self._out is not None else sys.stdout

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def heading(self, text: str) -> None:
        self.say()
        self.say(text)
        self.say("-" * len(text))

    def _read(self, prompt: str) -> str:
        try:
            return self._input_fn(prompt).strip()
        except EOFError:
            raise KeyboardInterrupt from None

    def ask(
        self,
        prompt: str,
        *,
        default: str | None = None,
        validate: Callable[[str], str | None] | None = None,
        allow_skip: bool = True,
    ) -> str:
        """Ask until `validate` accepts; an empty answer takes `default`, `s` skips the stage."""
        shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
        while True:
            answer = self._read(shown)
            if allow_skip and answer.lower() == "s":
                raise SkipStage
            if not answer and default is not None:
                answer = default
            error = validate(answer) if validate is not None else None
            if error is None:
                return answer
            self.say(error)

    def confirm(self, prompt: str, *, default: bool = True) -> bool:
        shown = f"{prompt} [{'Y/n' if default else 'y/N'}]: "
        while True:
            answer = self._read(shown).lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.say("Please answer y or n.")

    def _show_options(self, prompt: str, options: Sequence[str]) -> None:
        self.say(prompt)
        for number, option in enumerate(options, start=1):
            self.say(f"  {number}) {option}")

    def choose(
        self,
        prompt: str,
        options: Sequence[str],
        *,
        default: int | None = None,
        allow_skip: bool = True,
    ) -> int:
        """Pick one entry of a numbered list (shown 1..n); returns its 0-based index."""
        self._show_options(prompt, options)
        shown = f"Number [{default + 1}]: " if default is not None else "Number: "
        while True:
            answer = self._read(shown)
            if allow_skip and answer.lower() == "s":
                raise SkipStage
            if not answer and default is not None:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            self.say(f"Enter a number between 1 and {len(options)}.")

    def choose_many(self, prompt: str, options: Sequence[str], *, defaults: Sequence[int] = ()) -> list[int]:
        """Pick several entries: numbers separated by commas, `a` for all, empty for the defaults.

        `defaults` and the result are 0-based indexes in ascending order.
        """
        self._show_options(prompt, options)
        hint = ",".join(str(index + 1) for index in sorted(set(defaults)))
        shown = (
            f"Numbers, comma-separated, a = all [{hint}]: " if hint else "Numbers, comma-separated, a = all: "
        )
        while True:
            answer = self._read(shown).lower()
            if answer == "a":
                return list(range(len(options)))
            if not answer:
                return sorted(set(defaults))
            tokens = [token for token in re.split(r"[,\s]+", answer) if token]
            bad = [token for token in tokens if not (token.isdigit() and 1 <= int(token) <= len(options))]
            if bad:
                self.say(
                    f"{bad[0]!r} is not valid — enter numbers between 1 and {len(options)}, "
                    "separated by commas, or a for all."
                )
                continue
            return sorted({int(token) - 1 for token in tokens})

    def secret(self, prompt: str) -> str:
        """Read a value without echo; never shown, never logged."""
        try:
            return self._secret_fn(f"{prompt}: ")
        except EOFError:
            raise KeyboardInterrupt from None
