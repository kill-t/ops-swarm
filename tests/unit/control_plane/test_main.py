import pytest

from opsswarm.control_plane.main import parse_server_arguments


def test_server_arguments_default_to_localhost() -> None:
    assert parse_server_arguments([]) == ("127.0.0.1", 8000)


def test_server_arguments_accept_local_port_override() -> None:
    assert parse_server_arguments(["--host", "localhost", "--port", "18002"]) == (
        "localhost",
        18002,
    )


@pytest.mark.parametrize("port", ["0", "65536"])
def test_server_arguments_reject_port_outside_tcp_range(port: str) -> None:
    with pytest.raises(SystemExit):
        parse_server_arguments(["--port", port])


def test_server_arguments_reject_public_binding() -> None:
    with pytest.raises(SystemExit):
        parse_server_arguments(["--host", "0.0.0.0"])
