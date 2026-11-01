"""Deny Python network access during paper-derived processing."""
import os
import socket
from contextlib import contextmanager

from .contracts import FormatterError

OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
               "DO_NOT_TRACK": "1", "ANONYMIZED_TELEMETRY": "False", "TOKENIZERS_PARALLELISM": "false"}


@contextmanager
def offline_processing():
    saved_env = {k: os.environ.get(k) for k in OFFLINE_ENV}
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    original_create = socket.create_connection
    originals = {name:getattr(socket.socket,name) for name in ('sendto','send','sendall')}
    original_dns = socket.getaddrinfo
    def forbidden(*args, **kwargs):
        raise FormatterError("DOCUMENT_NETWORK_DISABLED")
    os.environ.update(OFFLINE_ENV)
    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden
    socket.getaddrinfo = forbidden
    for name in originals:setattr(socket.socket,name,forbidden)
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex = original_connect, original_connect_ex
        socket.create_connection = original_create
        socket.getaddrinfo = original_dns
        for name,value in originals.items():setattr(socket.socket,name,value)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
