"""FastAPI dependencies: configuration, engine, database transactions and templates."""

from collections.abc import Iterator

import sqlalchemy
from fastapi import Request
from starlette.templating import Jinja2Templates

from ai_oil.config import Config


def get_engine(request: Request) -> sqlalchemy.Engine:
    return request.app.state.engine


def get_config(request: Request) -> Config:
    return request.app.state.config


def db_read(request: Request) -> Iterator[sqlalchemy.Connection]:
    """A connection in one short transaction, for GET pages."""
    with request.app.state.engine.begin() as conn:
        yield conn


def db_write(request: Request) -> Iterator[sqlalchemy.Connection]:
    """A connection in one transaction per request, for writes (kept apart from `db_read` for auditing)."""
    with request.app.state.engine.begin() as conn:
        yield conn


def templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates
