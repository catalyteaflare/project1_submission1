"""Inherited subprocess guard for offline CLI tests; never used by the app."""

import os
import socket
import ssl  # Load socket subclasses before denying new socket construction.


def denied(*args, **kwargs):
    raise AssertionError("CLI test attempted external network access")


class DeniedSocket(socket.socket):
    def __new__(cls, *args, **kwargs):
        return denied()


socket.socket = DeniedSocket
socket.create_connection = denied
socket.getaddrinfo = denied
_original_getitem = type(os.environ).__getitem__


def guarded_getitem(environ, key):
    if key == "API_KEY":
        raise AssertionError("CLI test attempted to read API_KEY")
    return _original_getitem(environ, key)


type(os.environ).__getitem__ = guarded_getitem
os.environ["ACCESS_INTAKE_OFFLINE_TEST"] = "1"
