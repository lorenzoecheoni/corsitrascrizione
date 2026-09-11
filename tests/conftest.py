"""Shared pytest configuration for Bunny Video Report."""
import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def offline_network_only(request, monkeypatch):
    """Allow local media fixtures; fail before external DNS or socket traffic."""
    if request.node.get_closest_marker("live"):
        return
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    getaddrinfo = socket.getaddrinfo

    def require_loopback(host):
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise AssertionError("External network access is forbidden in offline tests")

    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            require_loopback(address[0])
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            require_loopback(address[0])
        return connect_ex(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host is not None:
            require_loopback(host)
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
