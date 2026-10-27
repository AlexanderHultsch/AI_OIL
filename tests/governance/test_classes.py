import pytest

from ai_oil.errors import AiOilError
from ai_oil.governance.classes import ApprovalRequired, action, action_class_of, ensure_allowed


def test_decorator_sets_class_and_returns_same_function() -> None:
    def tool() -> None:
        return None

    decorated = action("C")(tool)

    assert decorated is tool
    assert tool.__action_class__ == "C"


def test_unknown_class_rejected_at_decoration_time() -> None:
    with pytest.raises(ValueError):
        action("Z")


def test_action_class_of_decorated_function() -> None:
    @action("B")
    def tool() -> None:
        return None

    assert action_class_of(tool) == "B"


def test_action_class_of_connector_attribute() -> None:
    class Connector:
        action_class = "A"

    assert action_class_of(Connector) == "A"
    assert action_class_of(Connector()) == "A"


def test_undeclared_is_class_e() -> None:
    def plain() -> None:
        return None

    assert action_class_of(plain) == "E"


def test_unknown_attribute_value_is_class_e() -> None:
    class Connector:
        action_class = "Q"

    assert action_class_of(Connector) == "E"


def test_ensure_allowed_returns_class_a_and_b() -> None:
    @action("A")
    def read_only() -> None:
        return None

    @action("B")
    def write_local() -> None:
        return None

    assert ensure_allowed(read_only) == "A"
    assert ensure_allowed(write_local) == "B"


@pytest.mark.parametrize("action_class", ["C", "D", "E"])
def test_ensure_allowed_refuses_c_to_e(action_class: str) -> None:
    @action(action_class)
    def tool() -> None:
        return None

    with pytest.raises(ApprovalRequired, match="GOV-6") as excinfo:
        ensure_allowed(tool)
    assert isinstance(excinfo.value, AiOilError)


def test_ensure_allowed_refuses_undeclared() -> None:
    def plain() -> None:
        return None

    with pytest.raises(ApprovalRequired, match="GOV-6"):
        ensure_allowed(plain)
