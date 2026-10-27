import io

import pytest

from ai_oil.setup.console import Console, SkipStage


def test_ask_returns_the_answer(make_console) -> None:
    console = make_console(["  hello  "])
    assert console.ask("Name") == "hello"
    assert console.prompts == ["Name: "]


def test_ask_shows_and_uses_the_default(make_console) -> None:
    console = make_console([""])
    assert console.ask("Folder", default="C:/x") == "C:/x"
    assert console.prompts == ["Folder [C:/x]: "]


def test_ask_prefers_a_typed_answer_over_the_default(make_console) -> None:
    assert make_console(["other"]).ask("Folder", default="C:/x") == "other"


def test_ask_without_default_returns_an_empty_answer(make_console) -> None:
    assert make_console([""]).ask("Name") == ""


def test_ask_reasks_with_the_reason_until_valid(make_console) -> None:
    console = make_console(["no", "still no", "yes@x"])

    def validate(text: str) -> str | None:
        return None if "@" in text else f"{text!r} has no @"

    assert console.ask("Mail", validate=validate) == "yes@x"
    assert "'no' has no @" in console.output
    assert "'still no' has no @" in console.output
    assert len(console.prompts) == 3


def test_ask_validates_the_default_too(make_console) -> None:
    console = make_console(["", "fine"])
    result = console.ask("Folder", default="bad", validate=lambda t: "bad value" if t == "bad" else None)
    assert result == "fine"
    assert "bad value" in console.output


@pytest.mark.parametrize("answer", ["s", "S", " s "])
def test_ask_skips_on_s(make_console, answer: str) -> None:
    with pytest.raises(SkipStage):
        make_console([answer]).ask("Name", default="x")


def test_ask_can_forbid_skipping(make_console) -> None:
    assert make_console(["s"]).ask("Letter", allow_skip=False) == "s"


def test_confirm_defaults_and_answers(make_console) -> None:
    assert make_console([""]).confirm("Keep?") is True
    assert make_console([""]).confirm("Keep?", default=False) is False
    assert make_console(["y"]).confirm("Keep?", default=False) is True
    assert make_console(["YES"]).confirm("Keep?", default=False) is True
    assert make_console(["n"]).confirm("Keep?") is False
    assert make_console(["No"]).confirm("Keep?") is False


def test_confirm_shows_the_default_in_the_prompt(make_console) -> None:
    yes, no = make_console([""]), make_console([""])
    yes.confirm("Keep?")
    no.confirm("Keep?", default=False)
    assert yes.prompts == ["Keep? [Y/n]: "]
    assert no.prompts == ["Keep? [y/N]: "]


def test_confirm_reasks_on_other_input(make_console) -> None:
    console = make_console(["maybe", "y"])
    assert console.confirm("Keep?") is True
    assert "Please answer y or n." in console.output


def test_choose_returns_a_zero_based_index_of_the_numbered_list(make_console) -> None:
    console = make_console(["2"])
    assert console.choose("Pick", ["a", "b", "c"]) == 1
    assert "  1) a" in console.output
    assert "  3) c" in console.output


def test_choose_default_and_invalid_input(make_console) -> None:
    console = make_console(["", "0", "4", "x", "3"])
    assert console.choose("Pick", ["a", "b", "c"], default=1) == 1
    assert console.choose("Pick", ["a", "b", "c"]) == 2
    assert console.prompts[0] == "Number [2]: "
    assert console.output.count("Enter a number between 1 and 3.") == 3


def test_choose_without_default_reasks_on_empty(make_console) -> None:
    console = make_console(["", "1"])
    assert console.choose("Pick", ["a"]) == 0
    assert "Enter a number between 1 and 1." in console.output


def test_choose_skips_unless_forbidden(make_console) -> None:
    with pytest.raises(SkipStage):
        make_console(["s"]).choose("Pick", ["a"])
    console = make_console(["s", "1"])
    assert console.choose("Pick", ["a"], allow_skip=False) == 0


def test_choose_many_parses_numbers(make_console) -> None:
    options = ["a", "b", "c", "d"]
    assert make_console(["1,3"]).choose_many("Pick", options) == [0, 2]
    assert make_console([" 3 , 1 ,3"]).choose_many("Pick", options) == [0, 2]
    assert make_console(["2 4"]).choose_many("Pick", options) == [1, 3]


def test_choose_many_all_and_defaults(make_console) -> None:
    options = ["a", "b", "c"]
    assert make_console(["a"]).choose_many("Pick", options) == [0, 1, 2]
    assert make_console(["A"]).choose_many("Pick", options, defaults=[1]) == [0, 1, 2]
    console = make_console([""])
    assert console.choose_many("Pick", options, defaults=[2, 0]) == [0, 2]
    assert console.prompts == ["Numbers, comma-separated, a = all [1,3]: "]
    assert make_console([""]).choose_many("Pick", options) == []


def test_choose_many_reasks_on_invalid_input(make_console) -> None:
    console = make_console(["1,x", "0", "9", "2"])
    assert console.choose_many("Pick", ["a", "b", "c"]) == [1]
    assert "'x' is not valid" in console.output
    assert "'0' is not valid" in console.output
    assert "'9' is not valid" in console.output


def test_secret_uses_its_own_queue(make_console) -> None:
    console = make_console(["visible"], secrets=["hidden"])
    assert console.secret("Token") == "hidden"
    assert console.ask("Name") == "visible"
    assert "hidden" not in console.output


def test_end_of_input_aborts_instead_of_skipping(make_console) -> None:
    for call in (
        lambda c: c.ask("Name"),
        lambda c: c.confirm("Keep?"),
        lambda c: c.choose("Pick", ["a"]),
        lambda c: c.choose_many("Pick", ["a"]),
        lambda c: c.secret("Token"),
    ):
        with pytest.raises(KeyboardInterrupt):
            call(make_console())


def test_say_and_heading(make_console) -> None:
    console = make_console()
    console.say("plain")
    console.heading("Title")
    assert console.output == "plain\n\nTitle\n-----\n"


def test_default_streams(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("typed\n"))
    console = Console()
    console.say("hello")
    assert console.ask("Name") == "typed"
    assert "hello" in capsys.readouterr().out
