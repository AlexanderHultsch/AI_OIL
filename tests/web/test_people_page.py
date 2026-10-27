import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from ai_oil.domain import issues, people
from ai_oil.governance import audit

ERROR_PREFIX = '<div class="error" role="alert">'


@pytest.fixture
def post(client: TestClient, csrf_token: str):
    def send(path: str, data: dict | None = None, *, csrf: bool = True):
        form = {**({"csrf_token": csrf_token} if csrf else {}), **(data or {})}
        return client.post(path, data=form, follow_redirects=False)

    return send


def row_of(page: str, person_id: int) -> str:
    match = re.search(rf'<tr id="person-{person_id}">.*?</tr>', page, re.DOTALL)
    assert match is not None, f"no row for person {person_id}"
    return match.group(0)


def plain(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def person_ids(page: str) -> list[int]:
    return [int(i) for i in re.findall(r'<tr id="person-(\d+)">', page)]


def add_person(app: FastAPI, name: str, *emails: str, organisation: str | None = None) -> int:
    with app.state.engine.begin() as conn:
        person = people.create_person(conn, name, organisation=organisation)
        for address in emails:
            people.add_identity(conn, person.id, "email", address)
        return person.id


def me_id(app: FastAPI) -> int:
    with app.state.engine.begin() as conn:
        return people.get_me(conn).id


def audit_entries(app: FastAPI, prefix: str) -> list[audit.AuditEntry]:
    with app.state.engine.begin() as conn:
        return audit.list_entries(conn, action_prefix=prefix)


def assign(app: FastAPI, assignee: int, reporter: int) -> None:
    """OIL-1 gets the assignee, OIL-2 the reporter and OIL-3 both."""
    me = me_id(app)
    with app.state.engine.begin() as conn:
        issues.update_issue(conn, "OIL-1", {"assignee_id": assignee}, actor_id=me)
        issues.update_issue(conn, "OIL-2", {"reporter_id": reporter}, actor_id=me)
        issues.update_issue(conn, "OIL-3", {"assignee_id": assignee, "reporter_id": reporter}, actor_id=me)


# --- the list -----------------------------------------------------------------------------------------------


def test_the_list_shows_persons_with_organisation_me_badge_and_identities(
    app: FastAPI, client: TestClient
) -> None:
    bea = add_person(app, "Bea Brandt", "bea@acme.com", "b.brandt@acme.com", organisation="Acme")
    with app.state.engine.begin() as conn:
        people.add_identity(conn, bea, "atlassian", "acc-123")

    response = client.get("/people")

    page = response.text
    assert response.status_code == 200
    assert person_ids(page) == [me_id(app), bea]
    me_row = row_of(page, me_id(app))
    assert '<span class="badge">me</span>' in me_row
    assert "email: alex@example.com" in me_row
    bea_row = row_of(page, bea)
    assert "badge" not in bea_row
    assert plain(bea_row) == (
        f"{bea} Bea Brandt Acme atlassian: acc-123 email: b.brandt@acme.com email: bea@acme.com"
    )
    assert re.search(r'<a class="active" href="/people"|href="/people" class="active"', page)


def test_merged_persons_are_listed_only_with_all(app: FastAPI, client: TestClient) -> None:
    bea = add_person(app, "Bea Brandt", "bea@acme.com")
    twin = add_person(app, "Bea B.", "bea.b@acme.com")
    me = me_id(app)
    with app.state.engine.begin() as conn:
        people.merge_persons(conn, twin, bea, actor_id=me)

    default = client.get("/people").text
    everyone = client.get("/people?all=1").text

    assert person_ids(default) == [me_id(app), bea]
    assert "merged into" not in default
    assert person_ids(everyone) == [me_id(app), twin, bea]
    assert '<span class="badge">merged into Bea Brandt</span>' in row_of(everyone, twin)
    assert "email: bea.b@acme.com" in row_of(everyone, bea)
    assert 'href="/people"' in everyone and 'href="/people?all=1"' in default


def test_the_merge_selects_offer_the_non_merged_persons_only(app: FastAPI, client: TestClient) -> None:
    bea = add_person(app, "Bea Brandt")
    twin = add_person(app, "Bea B.")
    me = me_id(app)
    with app.state.engine.begin() as conn:
        people.merge_persons(conn, twin, bea, actor_id=me)

    page = client.get("/people?all=1").text

    for name in ("source", "target"):
        select = re.search(rf'<select id="merge-{name}" name="{name}".*?</select>', page, re.DOTALL)
        assert select is not None
        assert re.findall(r'<option value="(\d+)">', select.group(0)) == [str(me_id(app)), str(bea)]
    assert re.search(r'<form class="inline-form" action="/people/merge" method="post">', page)
    assert 'name="confirm"' not in page


# --- adding -------------------------------------------------------------------------------------------------


def test_adding_a_person_with_two_emails_and_an_atlassian_id(app: FastAPI, client: TestClient, post) -> None:
    response = post(
        "/people",
        {
            "name": " Bea Brandt ",
            "organisation": " Acme ",
            "emails": "Bea@Acme.com, b.brandt@acme.com\r\n\r\n",
            "atlassian": " acc-123 ",
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/people"
    with app.state.engine.begin() as conn:
        [person] = [p for p in people.list_persons(conn) if p.display_name == "Bea Brandt"]
        identities = [(i.system, i.external_id) for i in people.list_identities(conn, person.id)]
    assert person.organisation == "Acme"
    assert identities == [("atlassian", "acc-123"), ("email", "b.brandt@acme.com"), ("email", "bea@acme.com")]
    [entry] = audit_entries(app, "person.create")
    assert (entry.action_class, entry.actor_type, entry.target) == ("B", "user", str(person.id))
    assert entry.detail == {
        "name": "Bea Brandt",
        "organisation": "Acme",
        "emails": ["Bea@Acme.com", "b.brandt@acme.com"],
        "atlassian": "acc-123",
        "via": "web",
    }
    assert "email: bea@acme.com" in row_of(client.get("/people").text, person.id)


def test_emails_may_be_newline_separated(app: FastAPI, post) -> None:
    post("/people", {"name": "Cem", "emails": "a@acme.com\nb@acme.com"})

    with app.state.engine.begin() as conn:
        [person] = [p for p in people.list_persons(conn) if p.display_name == "Cem"]
        assert [i.external_id for i in people.list_identities(conn, person.id)] == [
            "a@acme.com",
            "b@acme.com",
        ]
    [entry] = audit_entries(app, "person.create")
    assert entry.detail["organisation"] is None and entry.detail["atlassian"] is None


def test_a_name_alone_is_enough(app: FastAPI, post) -> None:
    assert post("/people", {"name": "Dora"}).status_code == 303

    [entry] = audit_entries(app, "person.create")
    assert entry.detail["emails"] == []


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"name": "  "}, "display name must not be blank"),
        ({"name": "Dup", "emails": "alex@example.com"}, "already belongs to person"),
        ({"name": "Dup", "emails": "fine@acme.com, alex@example.com"}, "already belongs to person"),
    ],
)
def test_an_invalid_person_shows_the_error_and_stores_nothing(
    app: FastAPI, post, data: dict, message: str
) -> None:
    response = post("/people", data)

    assert response.status_code == 422
    assert ERROR_PREFIX in response.text
    assert message in response.text
    assert audit_entries(app, "person.create") == []
    with app.state.engine.begin() as conn:
        assert [p.display_name for p in people.list_persons(conn)] == ["Alex"]
        assert conn.execute(text("SELECT count(*) FROM person_identity")).scalar_one() == 1


# --- merging ------------------------------------------------------------------------------------------------


def test_the_first_step_shows_both_persons_and_the_issue_fields_and_merges_nothing(
    app: FastAPI, client: TestClient, post
) -> None:
    bea = add_person(app, "Bea Brandt", organisation="Acme")
    twin = add_person(app, "Bea B.")
    assign(app, assignee=twin, reporter=twin)

    response = post("/people/merge", {"source": str(twin), "target": str(bea)})

    page = response.text
    assert response.status_code == 200
    assert "Merge people" in page
    assert re.search(rf"Merge away</dt>\s*<dd>Bea B\. \(#{twin}\)</dd>", page)
    assert re.search(rf"Keep</dt>\s*<dd>Bea Brandt \(#{bea}\), Acme</dd>", page)
    assert '<p id="merge-fields">4 issue fields (assignee, reporter) will be re-pointed' in page
    hidden = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', page))
    assert {k: v for k, v in hidden.items() if k != "csrf_token"} == {
        "source": str(twin),
        "target": str(bea),
        "confirm": "1",
    }
    assert ">Merge</button>" in page
    assert audit_entries(app, "person.merge") == []
    with app.state.engine.begin() as conn:
        assert people.get_person(conn, twin).merged_into_id is None
        assert issues.get_issue(conn, "OIL-1").assignee_id == twin
    assert twin in person_ids(client.get("/people").text)


def test_one_field_is_counted_in_the_singular(app: FastAPI, post) -> None:
    bea = add_person(app, "Bea Brandt")
    twin = add_person(app, "Bea B.")
    me = me_id(app)
    with app.state.engine.begin() as conn:
        issues.update_issue(conn, "OIL-1", {"assignee_id": twin}, actor_id=me)

    page = post("/people/merge", {"source": str(twin), "target": str(bea)}).text

    assert '<p id="merge-fields">1 issue field (assignee, reporter)' in page


def test_confirming_merges_repoints_the_issues_and_audits(app: FastAPI, client: TestClient, post) -> None:
    bea = add_person(app, "Bea Brandt", "bea@acme.com")
    twin = add_person(app, "Bea B.", "bea.b@acme.com")
    assign(app, assignee=twin, reporter=twin)

    response = post("/people/merge", {"source": str(twin), "target": str(bea), "confirm": "1"})

    assert response.status_code == 303
    assert response.headers["location"] == "/people"
    with app.state.engine.begin() as conn:
        assert people.get_person(conn, twin).merged_into_id == bea
        assert issues.get_issue(conn, "OIL-1").assignee_id == bea
        assert issues.get_issue(conn, "OIL-2").reporter_id == bea
        third = issues.get_issue(conn, "OIL-3")
        assert (third.assignee_id, third.reporter_id) == (bea, bea)
        assert (
            conn.execute(
                text("SELECT count(*) FROM issue WHERE assignee_id = :id OR reporter_id = :id"), {"id": twin}
            ).scalar_one()
            == 0
        )
    [entry] = audit_entries(app, "person.merge")
    assert (entry.action_class, entry.actor_type, entry.target) == ("B", "user", str(twin))
    assert entry.detail == {
        "source_id": twin,
        "target_id": bea,
        "issue_fields_repointed": 4,
        "via": "web",
    }
    page = client.get("/people").text
    assert person_ids(page) == [me_id(app), bea]
    assert "email: bea.b@acme.com" in row_of(page, bea)


@pytest.mark.parametrize("confirm", ["", "1"])
def test_merging_me_away_is_an_error_in_both_steps(app: FastAPI, post, confirm: str) -> None:
    bea = add_person(app, "Bea Brandt")

    response = post("/people/merge", {"source": str(me_id(app)), "target": str(bea), "confirm": confirm})

    assert response.status_code == 422
    assert ERROR_PREFIX in response.text
    assert "the &#39;me&#39; person cannot be merged into another person" in response.text
    assert audit_entries(app, "person.merge") == []
    with app.state.engine.begin() as conn:
        assert people.get_me(conn).merged_into_id is None


def test_merging_into_me_is_allowed(app: FastAPI, post) -> None:
    twin = add_person(app, "Alex Twin")
    me = me_id(app)

    response = post("/people/merge", {"source": str(twin), "target": str(me), "confirm": "1"})

    assert response.status_code == 303
    with app.state.engine.begin() as conn:
        assert people.get_person(conn, twin).merged_into_id == me


@pytest.mark.parametrize("confirm", ["", "1"])
def test_merging_a_person_into_itself_is_an_error(app: FastAPI, post, confirm: str) -> None:
    bea = add_person(app, "Bea Brandt")

    response = post("/people/merge", {"source": str(bea), "target": str(bea), "confirm": confirm})

    assert response.status_code == 422
    assert "cannot merge a person into itself" in response.text


@pytest.mark.parametrize("confirm", ["", "1"])
def test_merging_an_already_merged_person_is_an_error(app: FastAPI, post, confirm: str) -> None:
    bea = add_person(app, "Bea Brandt")
    twin = add_person(app, "Bea B.")
    other = add_person(app, "Cem")
    me = me_id(app)
    with app.state.engine.begin() as conn:
        people.merge_persons(conn, twin, bea, actor_id=me)

    as_source = post("/people/merge", {"source": str(twin), "target": str(other), "confirm": confirm})
    as_target = post("/people/merge", {"source": str(other), "target": str(twin), "confirm": confirm})

    assert as_source.status_code == as_target.status_code == 422
    assert f"person {twin} is already merged" in as_source.text
    assert f"person {twin} is already merged" in as_target.text
    assert len(audit_entries(app, "person.merge")) == 0


@pytest.mark.parametrize(
    ("data", "status", "message"),
    [
        ({"source": "999", "target": "1"}, 404, "person 999 not found"),
        ({"source": "1", "target": "999"}, 404, "person 999 not found"),
        ({"source": "", "target": "1"}, 422, "choose a person from the list"),
        ({"source": "abc", "target": "1"}, 422, "choose a person from the list"),
        ({"target": "1"}, 422, "choose a person from the list"),
    ],
)
def test_unknown_or_missing_persons_are_errors(post, data: dict, status: int, message: str) -> None:
    response = post("/people/merge", data)

    assert response.status_code == status
    assert ERROR_PREFIX in response.text
    assert message in response.text


def test_adding_a_person_without_the_csrf_token_is_refused(app: FastAPI, post) -> None:
    response = post("/people", {"name": "No token"}, csrf=False)

    assert response.status_code == 403
    assert audit_entries(app, "person.create") == []
    with app.state.engine.begin() as conn:
        assert [p.display_name for p in people.list_persons(conn)] == ["Alex"]


def test_merging_without_the_csrf_token_is_refused(app: FastAPI, post) -> None:
    bea = add_person(app, "Bea Brandt")
    twin = add_person(app, "Bea B.")

    response = post("/people/merge", {"source": str(twin), "target": str(bea), "confirm": "1"}, csrf=False)

    assert response.status_code == 403
    with app.state.engine.begin() as conn:
        assert people.get_person(conn, twin).merged_into_id is None
