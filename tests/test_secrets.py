from ai_oil.secrets import delete_secret, get_secret, set_secret


def test_round_trip() -> None:
    set_secret("jira", "token-123")
    assert get_secret("jira") == "token-123"
    delete_secret("jira")
    assert get_secret("jira") is None


def test_delete_absent_does_not_raise() -> None:
    delete_secret("never-set")


def test_get_absent_is_none() -> None:
    assert get_secret("never-set") is None
