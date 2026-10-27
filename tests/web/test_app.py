import re
import socket
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Body, FastAPI, Form
from fastapi.testclient import TestClient

from ai_oil.cli import serve
from ai_oil.cli.main import main
from ai_oil.errors import NotFoundError, ValidationError


def _row_cells(html: str, key: str) -> list[str]:
    row = re.search(rf"<tr>\s*<td>{key}</td>(.*?)</tr>", html, re.DOTALL)
    assert row is not None, f"no row for {key}"
    return re.findall(r"<td[^>]*>(.*?)</td>", row.group(0), re.DOTALL)


def test_home_lists_projects_with_counts_per_category(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert _row_cells(response.text, "OIL") == ["OIL", "Open issues", "2", "1", "1"]
    assert _row_cells(response.text, "ABC") == ["ABC", "Alphabet", "0", "0", "0"]
    assert "OLD" not in response.text


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_htmx_is_served_from_the_package(client: TestClient) -> None:
    response = client.get("/static/htmx.min.js")

    assert response.status_code == 200
    assert len(response.content) > 30_000
    assert response.text.startswith("var htmx=function(){")
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_base_layout(client: TestClient, csrf_token: str) -> None:
    html = client.get("/").text

    assert f'<meta name="csrf-token" content="{csrf_token}">' in html
    assert f"""hx-headers='{{"X-CSRF-Token": "{csrf_token}"}}'""" in html
    for href, label in [
        ("/issues", "Issues"),
        ("/filters", "Filters"),
        ("/projects", "Projects"),
        ("/connectors", "Connectors"),
        ("/sync", "Sync log"),
        ("/people", "People"),
    ]:
        assert f'<a href="{href}"' in html
        assert f">{label}</a>" in html
    assert '<form class="search" action="/issues" method="get"' in html
    assert 'name="q" placeholder="Search or OQL…"' in html
    assert '<script src="/static/htmx.min.js" defer>' in html
    assert '<script src="/static/app.js" defer>' in html
    assert '<link rel="stylesheet" href="/static/app.css">' in html
    assert 'id="flash"' in html


def test_foreign_host_is_rejected(client: TestClient) -> None:
    for host in ("evil.example:8765", "127.0.0.1:9999", "127.0.0.1", "localhost"):
        response = client.get("/health", headers={"Host": host})
        assert (response.status_code, response.text) == (400, "invalid host"), host


def test_localhost_host_is_accepted(client: TestClient) -> None:
    assert client.get("/health", headers={"Host": "localhost:8765"}).status_code == 200
    assert client.get("/health", headers={"Host": "127.0.0.1:8765"}).status_code == 200


@pytest.fixture
def echo_app(app: FastAPI) -> FastAPI:
    async def echo_form(name: str = Form(...), count: int = Form(...)) -> dict:
        return {"name": name, "count": count}

    async def echo_json(payload: Annotated[dict, Body()]) -> dict:
        return payload

    app.add_api_route("/echo-form", echo_form, methods=["POST"])
    app.add_api_route("/echo-json", echo_json, methods=["POST", "PUT", "PATCH", "DELETE"])
    return app


@pytest.fixture
def echo_client(echo_app: FastAPI) -> TestClient:
    return TestClient(echo_app, base_url="http://127.0.0.1:8765")


def test_post_without_token_is_rejected(echo_client: TestClient) -> None:
    for response in (
        echo_client.post("/echo-json", json={"a": 1}),
        echo_client.post("/echo-form", data={"name": "x", "count": "1"}),
        echo_client.put("/echo-json", json={"a": 1}),
        echo_client.patch("/echo-json", json={"a": 1}),
        echo_client.delete("/echo-json"),
    ):
        assert (response.status_code, response.text) == (403, "missing or invalid CSRF token")


def test_header_token_is_accepted(echo_client: TestClient, csrf_token: str) -> None:
    response = echo_client.post("/echo-json", json={"a": 1}, headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 200
    assert response.json() == {"a": 1}


def test_form_field_token_is_accepted_and_the_endpoint_still_gets_its_fields(
    echo_client: TestClient, csrf_token: str
) -> None:
    urlencoded = echo_client.post("/echo-form", data={"name": "Bob", "count": "3", "csrf_token": csrf_token})
    multipart = echo_client.post(
        "/echo-form",
        data={"name": "Eve", "count": "4", "csrf_token": csrf_token},
        files={"attachment": ("note.txt", b"hello", "text/plain")},
    )

    assert (urlencoded.status_code, urlencoded.json()) == (200, {"name": "Bob", "count": 3})
    assert (multipart.status_code, multipart.json()) == (200, {"name": "Eve", "count": 4})


def test_wrong_token_is_rejected(echo_client: TestClient, csrf_token: str) -> None:
    wrong = csrf_token[:-1] + ("A" if csrf_token[-1] != "A" else "B")

    by_header = echo_client.post("/echo-json", json={}, headers={"X-CSRF-Token": wrong})
    by_field = echo_client.post("/echo-form", data={"name": "x", "count": "1", "csrf_token": wrong})
    non_ascii = echo_client.post("/echo-json", json={}, headers={"X-CSRF-Token": "tökén".encode("latin-1")})

    assert by_header.status_code == by_field.status_code == non_ascii.status_code == 403


def test_form_token_is_not_accepted_in_a_json_body(echo_client: TestClient, csrf_token: str) -> None:
    response = echo_client.post("/echo-json", json={"csrf_token": csrf_token})

    assert response.status_code == 403


def test_get_needs_no_token(client: TestClient) -> None:
    assert client.get("/").status_code == 200


@pytest.fixture
def error_client(app: FastAPI) -> TestClient:
    def not_found() -> None:
        raise NotFoundError("issue OIL-99 not found")

    def invalid() -> None:
        raise ValidationError("summary must not be blank")

    app.add_api_route("/raise-not-found", not_found, methods=["GET"])
    app.add_api_route("/raise-invalid", invalid, methods=["GET"])
    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.mark.parametrize(
    ("path", "status", "message"),
    [
        ("/raise-not-found", 404, "issue OIL-99 not found"),
        ("/raise-invalid", 422, "summary must not be blank"),
    ],
)
def test_domain_errors_render_a_full_page(
    error_client: TestClient, path: str, status: int, message: str
) -> None:
    response = error_client.get(path)

    assert response.status_code == status
    assert "<html" in response.text
    assert f'<div class="error" role="alert">{message}</div>' in response.text


@pytest.mark.parametrize(
    ("path", "status", "message"),
    [
        ("/raise-not-found", 404, "issue OIL-99 not found"),
        ("/raise-invalid", 422, "summary must not be blank"),
    ],
)
def test_domain_errors_render_a_fragment_for_htmx(
    error_client: TestClient, path: str, status: int, message: str
) -> None:
    response = error_client.get(path, headers={"HX-Request": "true"})

    assert response.status_code == status
    assert response.text.strip() == f'<div class="error" role="alert">{message}</div>'


def test_error_message_is_escaped(app: FastAPI) -> None:
    def invalid() -> None:
        raise ValidationError("<script>alert(1)</script>")

    app.add_api_route("/raise-markup", invalid, methods=["GET"])
    response = TestClient(app, base_url="http://127.0.0.1:8765").get(
        "/raise-markup", headers={"HX-Request": "true"}
    )

    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


@pytest.fixture
def serve_calls(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls: dict = {"run": [], "browser": []}

    def fake_run(app, **kwargs) -> None:
        calls["run"].append({"app": app, **kwargs})

    class ImmediateTimer:
        def __init__(self, interval, function, args=()) -> None:
            self.function, self.args = function, args

        def start(self) -> None:
            self.function(*self.args)

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setattr("webbrowser.open", lambda url: calls["browser"].append(url))
    monkeypatch.setattr(serve.threading, "Timer", ImmediateTimer)
    return calls


def test_serve_binds_loopback_on_the_default_port(
    web_home: Path, serve_calls: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--home", str(web_home), "serve"]) == 0

    [call] = serve_calls["run"]
    assert (call["host"], call["port"]) == ("127.0.0.1", 8765)
    assert isinstance(call["app"], FastAPI)
    assert "AI_OIL is running at http://127.0.0.1:8765/ — press Ctrl+C to stop" in capsys.readouterr().out
    assert serve_calls["browser"] == ["http://127.0.0.1:8765/"]


def test_serve_no_browser(web_home: Path, serve_calls: dict) -> None:
    assert main(["--home", str(web_home), "serve", "--no-browser", "--port", "9123"]) == 0

    assert serve_calls["run"][0]["port"] == 9123
    assert serve_calls["browser"] == []


def test_serve_port_zero_picks_a_free_port(
    web_home: Path, serve_calls: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--home", str(web_home), "serve", "--no-browser", "--port", "0"]) == 0

    [call] = serve_calls["run"]
    assert call["host"] == "127.0.0.1"
    assert call["port"] > 0
    assert f"http://127.0.0.1:{call['port']}/" in capsys.readouterr().out
    with socket.socket() as sock:  # the port was released again
        sock.bind(("127.0.0.1", call["port"]))
    assert call["app"].state.csrf.token
