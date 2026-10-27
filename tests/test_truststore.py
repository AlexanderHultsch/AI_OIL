import ssl

import truststore
from requests.adapters import HTTPAdapter

from ai_oil.connectors import atlassian
from ai_oil.connectors.atlassian import system_certificate_session


def test_the_session_mounts_a_system_certificate_adapter_for_https() -> None:
    session = system_certificate_session()

    adapter = session.get_adapter("https://acme.atlassian.net/rest/api/3/myself")
    context = adapter.poolmanager.connection_pool_kw["ssl_context"]
    assert isinstance(context, truststore.SSLContext)


def test_plain_http_keeps_the_default_adapter() -> None:
    session = system_certificate_session()

    adapter = session.get_adapter("http://example.com")
    assert type(adapter) is HTTPAdapter
    assert "ssl_context" not in adapter.poolmanager.connection_pool_kw


def test_each_session_gets_its_own_adapter_and_context() -> None:
    first, second = system_certificate_session(), system_certificate_session()

    first_adapter = first.get_adapter("https://a.example")
    second_adapter = second.get_adapter("https://a.example")
    assert first_adapter is not second_adapter
    assert (
        first_adapter.poolmanager.connection_pool_kw["ssl_context"]
        is not second_adapter.poolmanager.connection_pool_kw["ssl_context"]
    )


def test_a_proxy_manager_carries_the_system_certificate_context() -> None:
    adapter = system_certificate_session().get_adapter("https://acme.atlassian.net")

    manager = adapter.proxy_manager_for("http://proxy.example:3128")

    assert isinstance(manager.connection_pool_kw["ssl_context"], truststore.SSLContext)


def test_the_process_wide_ssl_module_is_not_patched() -> None:
    system_certificate_session()

    assert ssl.SSLContext is not truststore.SSLContext
    assert atlassian.ssl is ssl
