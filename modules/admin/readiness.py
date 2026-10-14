"""Portable, read-only probe for both current and pre-readiness Admin images."""

import http.client
import json
import os
import socket
import urllib.error
import urllib.request


def request_json(url, token=""):
    headers = {"Authorization": "Bearer " + token} if token else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers=headers), timeout=3) as response:
        if response.status != 200:
            raise ValueError("not_ready")
        return json.loads(response.read(1048576))


def maintenance_state(env):
    connection = http.client.HTTPConnection("localhost", timeout=3)
    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.sock.settimeout(3)
    try:
        connection.sock.connect(env["ADMIN_MAINTENANCE_SOCKET"])
        connection.request("GET", "/state", headers={"Authorization": "Bearer " + env["ADMIN_MAINTENANCE_TOKEN"]})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("not_ready")
        return json.loads(response.read(1048576))
    finally:
        connection.close()


def check(env):
    if request_json("http://localhost:" + env["AI_ADMIN_CONTAINER_PORT"] + "/health") != {"status": "ok"}:
        raise ValueError("not_ready")
    base = env["ADMIN_API_URL"].rstrip("/")
    token = env["SERVICE_MANAGEMENT_TOKEN"]
    if len(token) < 32 or len(env["ADMIN_MAINTENANCE_TOKEN"]) < 32:
        raise ValueError("not_ready")
    for path in ("/management/services", "/management/domains", "/management/profiles/ai"):
        if not isinstance(request_json(base + path, token), list):
            raise ValueError("not_ready")
    try:
        request_json(base + "/management/services")
    except urllib.error.HTTPError as error:
        if error.code not in {401, 403}:
            raise ValueError("not_ready") from None
    else:
        raise ValueError("authentication_not_enforced")
    state = maintenance_state(env)
    if not isinstance(state, dict) or not isinstance(state.get("jobs"), list):
        raise ValueError("not_ready")


if __name__ == "__main__":
    try:
        check(os.environ)
    except Exception:
        raise SystemExit(1) from None
