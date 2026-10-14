"""Laptop mode for the dashboard: `titan2 dash` is titan2.dash.app.serve() (werkzeug's own threaded server), on
127.0.0.1:5000 by default, never 0.0.0.0 without the explicit flag, with the Windows paths (TITAN2_VAR or
%LOCALAPPDATA%\\titan2\\var; the dashboard env file %APPDATA%\\titan2\\dash.env). Nothing here leaves the machine:
werkzeug's make_server and the listening socket are faked, except for a few binds on 127.0.0.1."""

from __future__ import annotations

import ast
import base64
import inspect
import json
import logging
import os
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path, PureWindowsPath

import pytest
import werkzeug.serving

from titan2.dash import app as dash
from titan2.dash.app import DashConfigError, create_app, hash_password, serve

AT_DIR = Path(__file__).resolve().parents[2]                 # agentic_trader/
USER, PASSWORD, SECRET = "operator", "correct horse battery staple", "s3cret-" * 6
LAN = "192.168.1.5"
NONCE = "nonce-0001-0123456789"
ON_WINDOWS = os.name == "nt"


@pytest.fixture(scope="module")
def pass_hash() -> str:
    return hash_password(PASSWORD, n=2 ** 14)               # the cheapest hash the app accepts: keeps tests fast


@pytest.fixture
def creds(pass_hash):
    return {"DASH_USER": USER, "DASH_PASS_SCRYPT": pass_hash, "DASH_SECRET": SECRET}


@pytest.fixture(autouse=True)
def linux_by_default(monkeypatch):
    """Each test picks its platform: the EC2 defaults unless it switches dash.WINDOWS on."""
    monkeypatch.setattr(dash, "WINDOWS", False)


class FakeSocket:
    """listen_socket's stand-in: nothing is bound (port 5000 may be busy on a developer's machine)."""

    def __init__(self, host, port):
        self.addr, self.closed = (host, port), False

    def getsockname(self):
        return self.addr

    def fileno(self):
        return -1

    def close(self):
        self.closed = True


@pytest.fixture
def made(monkeypatch):
    """werkzeug's make_server faked: each server serve() starts is recorded, and its serve_forever is a Ctrl+C."""
    calls = []

    class FakeServer:
        def __init__(self, host, port, app, **kw):
            self.closed = False
            calls.append({"app": app, "host": host, "port": port, "kw": kw, "server": self})

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            self.closed = True

    wz = logging.getLogger("werkzeug")
    level = wz.level
    monkeypatch.setattr(werkzeug.serving, "make_server", FakeServer)
    yield calls
    wz.setLevel(level)                                         # serve() turns werkzeug's request log down


@pytest.fixture
def servers(monkeypatch, made):
    monkeypatch.setattr(dash, "listen_socket", FakeSocket)
    return made


def env_for(tmp_path, creds, **extra):
    """An explicit env: credentials, var under tmp_path and an absent dashboard env file (no real file is read)."""
    return {"TITAN2_VAR": str(tmp_path / "var"), "TITAN2_DASH_ENV_FILE": str(tmp_path / "absent.env"), **creds,
            **extra}


def basic(user=USER, password=PASSWORD):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def status_code(application, base, headers=None):
    headers = basic() if headers is None else headers
    return application.test_client().get("/api/status", base_url=base, headers=headers).status_code


# ---------------------------------------------------------------------------------------------------------------
# Binding defaults
# ---------------------------------------------------------------------------------------------------------------
def test_serve_defaults_to_loopback_5000(tmp_path, creds, servers, monkeypatch, capsys):
    params = inspect.signature(serve).parameters                # the interface `titan2 dash` codes against
    assert [(n, params[n].default) for n in ("host", "port", "laptop")] == [
        ("host", "127.0.0.1"), ("port", 5000), ("laptop", False)]
    assert params["allow_public"].default is False and params["allow_public"].kind is inspect.Parameter.KEYWORD_ONLY

    socks = []
    monkeypatch.setattr(dash, "listen_socket", lambda host, port: socks.append(FakeSocket(host, port)) or socks[-1])
    assert serve(laptop=True, env=env_for(tmp_path, creds)) == 0
    (call,) = servers
    assert (call["host"], call["port"]) == ("127.0.0.1", 5000) and [s.addr for s in socks] == [("127.0.0.1", 5000)]
    assert socks[0].closed and call["server"].closed
    application = call["app"]
    assert (tmp_path / "var" / "dash" / "commands_dash.db").is_file()     # laptop: no bootstrap.sh makes dash/
    assert application.config["TITAN2_COMMANDS_DB"] == tmp_path / "var" / "dash" / "commands_dash.db"

    # the Host guard follows the served port
    assert status_code(application, "http://127.0.0.1:5000") == 200
    assert status_code(application, "http://localhost:5000") == 200
    for base in ("http://127.0.0.1:8050", "http://127.0.0.1", f"http://{LAN}:5000", "http://[::1]:5000",
                 "http://0.0.0.0:5000", "http://evil.example:5000"):
        assert status_code(application, base) == 403, base
    assert status_code(application, "http://127.0.0.1:5000", headers={}) == 401          # auth is unchanged

    out = capsys.readouterr().out
    assert "open http://127.0.0.1:5000" in out and "(laptop profile)" in out and "stopped (werkzeug)" in out
    assert PASSWORD not in out and SECRET not in out and creds["DASH_PASS_SCRYPT"] not in out


def test_non_laptop_linux_still_needs_bootstrap_dash_dir(tmp_path, creds, servers, capsys):
    env = env_for(tmp_path, creds)
    assert serve(env=env) == 2                                   # EC2 rule kept: bootstrap.sh makes <var>/dash
    assert servers == [] and not (tmp_path / "var").exists()
    assert "refused:" in capsys.readouterr().out
    (tmp_path / "var" / "dash").mkdir(parents=True)
    assert serve(env=env, port=5050) == 0
    assert (servers[0]["host"], servers[0]["port"]) == ("127.0.0.1", 5050)
    assert status_code(servers[0]["app"], "http://localhost:5050") == 200
    assert status_code(servers[0]["app"], "http://localhost:5000") == 403


def test_werkzeug_serves_our_socket(tmp_path, creds, servers, capsys):
    assert serve(laptop=True, env=env_for(tmp_path, creds)) == 0
    assert logging.getLogger("werkzeug").level == logging.WARNING      # no request line for every 2 s poll
    (call,) = servers
    assert (call["host"], call["port"]) == ("127.0.0.1", 5000) and call["server"].closed
    assert call["kw"] == {"threaded": True, "fd": -1}           # serves our socket; no debugger, no reloader
    assert status_code(call["app"], "http://127.0.0.1:5000") == 200
    assert "stopped (werkzeug)" in capsys.readouterr().out


def test_real_loopback_round_trip(tmp_path, creds, monkeypatch):
    """werkzeug's real server on our own socket on 127.0.0.1: the browser's Host carries the served port."""
    monkeypatch.setattr(dash, "WINDOWS", ON_WINDOWS)
    probe = dash.listen_socket("127.0.0.1", 0)
    port = probe.getsockname()[1]
    probe.close()
    seen = {}
    real = werkzeug.serving.make_server

    def make(host, bound_port, app, **kw):
        srv = real(host, bound_port, app, **kw)

        def browse():
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))     # never a system proxy
            try:
                for name, headers in (("ok", basic()), ("rebound", {**basic(), "Host": f"evil.example:{port}"})):
                    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status", headers=headers)
                    try:
                        with opener.open(req, timeout=10) as r:
                            seen[name] = (r.status, json.loads(r.read()))
                    except urllib.error.HTTPError as exc:
                        seen[name] = (exc.code, json.loads(exc.read()))
            finally:
                srv.shutdown()

        threading.Thread(target=browse, daemon=True).start()
        return srv

    monkeypatch.setattr(werkzeug.serving, "make_server", make)
    wz = logging.getLogger("werkzeug")
    level = wz.level
    try:
        assert serve(port=port, laptop=True, env=env_for(tmp_path, creds)) == 0
    finally:
        wz.setLevel(level)
    assert seen["ok"][0] == 200 and seen["ok"][1]["offline"] is True    # no status.json yet: the offline banner
    assert seen["rebound"][0] == 403 and "host not allowed" in seen["rebound"][1]["error"]


def test_port_in_use_is_refused(tmp_path, creds, made, monkeypatch, capsys):
    monkeypatch.setattr(dash, "WINDOWS", ON_WINDOWS)
    first = dash.listen_socket("127.0.0.1", 0)                   # a dashboard (or anything) already there
    try:
        port = first.getsockname()[1]
        assert serve(port=port, laptop=True, env=env_for(tmp_path, creds)) == 2
        assert f"refused: cannot listen on 127.0.0.1 port {port}" in capsys.readouterr().out
        assert made == []
    finally:
        first.close()


def test_listen_socket_is_exclusive(monkeypatch):
    monkeypatch.setattr(dash, "WINDOWS", ON_WINDOWS)
    first = dash.listen_socket("127.0.0.1", 0)
    port = first.getsockname()[1]
    try:
        with pytest.raises(OSError):
            dash.listen_socket("127.0.0.1", port)                # a second dashboard
        if ON_WINDOWS:                                           # nor a dev server that sets SO_REUSEADDR
            other = socket.socket()
            try:
                other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with pytest.raises(OSError):
                    other.bind(("127.0.0.1", port))
            finally:
                other.close()
    finally:
        first.close()
    dash.listen_socket("127.0.0.1", port).close()               # free again once closed


def test_gunicorn_app_keeps_8050(tmp_path, creds):
    (tmp_path / "dash").mkdir()
    application = create_app(creds, var_dir=tmp_path)            # what titan2-dash.service runs, unchanged
    assert status_code(application, "http://127.0.0.1:8050") == 200
    for base in ("http://127.0.0.1:5000", f"http://{LAN}:8050", "http://[::1]:8050"):
        assert status_code(application, base) == 403, base


# ---------------------------------------------------------------------------------------------------------------
# 0.0.0.0 (every interface) is refused unless the explicit flag is given
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", " 0.0.0.0 ", LAN, "::1", "my-laptop.local"])
@pytest.mark.parametrize("laptop", [True, False])
def test_refuses_every_interface_without_the_flag(tmp_path, creds, servers, capsys, host, laptop):
    assert serve(host, laptop=laptop, env=env_for(tmp_path, creds)) == 2
    out = capsys.readouterr().out
    assert out.startswith("refused: the dashboard listens on 127.0.0.1 only") and "allow-public" in out
    assert servers == [] and not (tmp_path / "var").exists()   # refused before anything is built or bound


@pytest.mark.parametrize("port", [0, 80, 1023, 65536, "5000", 5000.0, True, None])
def test_refuses_bad_ports(tmp_path, creds, servers, capsys, port):
    assert serve(port=port, laptop=True, env=env_for(tmp_path, creds)) == 2
    assert "port must be a whole number from 1024 to 65535" in capsys.readouterr().out and servers == []


def test_every_interface_only_with_the_explicit_flag(tmp_path, creds, servers, capsys):
    assert serve("0.0.0.0", laptop=True, allow_public=True, env=env_for(tmp_path, creds)) == 0
    (call,) = servers
    assert (call["host"], call["port"]) == ("0.0.0.0", 5000)
    out = capsys.readouterr().out
    assert "WARNING: listening on 0.0.0.0 port 5000" in out and "plain HTTP" in out
    application = call["app"]
    # an IP-literal Host on the served port passes; a DNS name never does (rebinding), nor another port
    for base in (f"http://{LAN}:5000", "http://[fe80::1]:5000", "http://127.0.0.1:5000", "http://localhost:5000"):
        assert status_code(application, base) == 200, base
    for base in ("http://evil.example:5000", "http://my-laptop.local:5000", f"http://{LAN}:5001", f"http://{LAN}"):
        assert status_code(application, base) == 403, base
    # a command from the LAN page: same-origin passes, a foreign Origin does not; CSRF and auth still apply
    client = application.test_client()
    token = client.get("/api/csrf", base_url=f"http://{LAN}:5000", headers=basic()).get_json()["csrf"]
    body = {"kind": "PAUSE", "nonce": NONCE}
    for origin, code in (("http://evil.example", 403), (f"http://{LAN}:5001", 403), (f"http://{LAN}:5000", 201)):
        r = client.post("/api/command", base_url=f"http://{LAN}:5000", json=body,
                        headers={**basic(), "X-Titan-CSRF": token, "Origin": origin})
        assert r.status_code == code, origin
    r = client.post("/api/command", base_url=f"http://{LAN}:5000", json={**body, "nonce": NONCE + "x"},
                    headers={**basic(), "Origin": f"http://{LAN}:5000"})
    assert r.status_code == 403                                  # no CSRF token


def test_ip_literal_host():
    assert dash.ip_literal_host(f"{LAN}:5000", 5000) and dash.ip_literal_host("[::1]:5000", 5000)
    for host in (f"{LAN}:5001", LAN, "evil.example:5000", "[evil]:5000", ":5000", "", "1.2.3:5000"):
        assert not dash.ip_literal_host(host, 5000), host


# ---------------------------------------------------------------------------------------------------------------
# Windows paths
# ---------------------------------------------------------------------------------------------------------------
def test_windows_var_path(monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "WINDOWS", True)
    local = tmp_path / "Local"
    assert dash.resolve_var_dir({"LOCALAPPDATA": str(local)}) == local / "titan2" / "var"
    got = dash.resolve_var_dir({"LOCALAPPDATA": r"C:\Users\op\AppData\Local"})
    assert str(PureWindowsPath(str(got))) == r"C:\Users\op\AppData\Local\titan2\var"
    assert dash.resolve_var_dir({"LOCALAPPDATA": str(local), "TITAN2_VAR": str(tmp_path / "v")}) == tmp_path / "v"
    with pytest.raises(DashConfigError, match="LOCALAPPDATA"):
        dash.resolve_var_dir({"TITAN2_VAR": ""})                 # never a guess relative to the current folder
    monkeypatch.setattr(dash, "WINDOWS", False)                  # EC2 and Linux: unchanged
    assert dash.resolve_var_dir({"LOCALAPPDATA": str(local)}) == Path("/var/lib/titan2")
    assert dash.resolve_var_dir({"TITAN2_VAR": "/srv/titan2"}) == Path("/srv/titan2")


def test_windows_dash_env_file_location_and_format(monkeypatch, tmp_path, pass_hash):
    monkeypatch.setattr(dash, "WINDOWS", True)
    roaming = tmp_path / "Roaming"
    assert dash.dash_env_path({"APPDATA": str(roaming)}) == roaming / "titan2" / "dash.env"
    got = dash.dash_env_path({"APPDATA": r"C:\Users\op\AppData\Roaming"})
    assert str(PureWindowsPath(str(got))) == r"C:\Users\op\AppData\Roaming\titan2\dash.env"
    assert dash.dash_env_path({"APPDATA": str(roaming), "TITAN2_DASH_ENV_FILE": str(tmp_path / "d.env")}) == \
        tmp_path / "d.env"
    assert dash.dash_env_path({}) is None
    monkeypatch.setattr(dash, "WINDOWS", False)
    assert dash.dash_env_path({"APPDATA": str(roaming)}) == Path("/etc/titan2/dash.env")

    text = (f"# the dashboard only\n\nDASH_USER = {USER}\nexport DASH_SECRET=\"{SECRET}\"\n"
            f"DASH_PASS_SCRYPT='{pass_hash}'\nnot a line\n1BAD=x\n=x\n")
    want = {"DASH_USER": USER, "DASH_SECRET": SECRET, "DASH_PASS_SCRYPT": pass_hash}
    path = tmp_path / "dash.env"
    for encoding in ("utf-8", "utf-8-sig", "utf-16"):            # utf-16 with a BOM: Windows PowerShell 5.1's `>`
        path.write_bytes(text.replace("\n", "\r\n").encode(encoding))
        assert dash.parse_env_file(path) == want, encoding


def test_serve_on_windows_uses_localappdata_and_appdata(monkeypatch, tmp_path, pass_hash, servers, capsys):
    monkeypatch.setattr(dash, "WINDOWS", True)
    local, roaming = tmp_path / "Local", tmp_path / "Roaming"
    env_file = roaming / "titan2" / "dash.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"DASH_USER=from-file\nDASH_PASS_SCRYPT='{pass_hash}'\nDASH_SECRET={SECRET}\n",
                        encoding="utf-8")
    env = {"LOCALAPPDATA": str(local), "APPDATA": str(roaming), "DASH_USER": USER}    # the process env wins
    assert serve(env=env) == 0                                   # Windows has no bootstrap.sh: dash/ is made
    (call,) = servers
    application, var = call["app"], local / "titan2" / "var"
    assert application.config["TITAN2_COMMANDS_DB"] == var / "dash" / "commands_dash.db"
    assert application.config["TITAN2_PUB_DIR"] == var / "pub"
    assert (var / "dash" / "commands_dash.db").is_file()
    (var / "pub").mkdir()
    (var / "pub" / "g1.json").write_text('{"strategies": []}', encoding="utf-8")
    r = application.test_client().get("/api/g1", base_url="http://127.0.0.1:5000", headers=basic())
    assert r.status_code == 200 and r.get_json()["data"] == {"strategies": []}
    assert status_code(application, "http://127.0.0.1:5000", headers=basic(user="from-file")) == 401
    out = capsys.readouterr().out
    assert f"dashboard env file {env_file}" in out and str(var) in out and "(laptop profile)" not in out
    assert pass_hash not in out and SECRET not in out and PASSWORD not in out


def test_serve_refusals_name_the_env_file(monkeypatch, tmp_path, pass_hash, servers, capsys):
    monkeypatch.setattr(dash, "WINDOWS", True)
    local, roaming = tmp_path / "Local", tmp_path / "Roaming"
    env_file = roaming / "titan2" / "dash.env"
    env = {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)}

    assert serve(laptop=True, env=env) == 2                      # first run: no dash.env yet
    out = capsys.readouterr().out
    assert "refused: DASH_USER and DASH_PASS_SCRYPT must both be set" in out
    assert f"dashboard env file {env_file} (not found)" in out
    assert "python -m titan2.dash.app hash-password" in out and "python -m titan2.dash.app new-secret" in out

    env_file.parent.mkdir(parents=True)
    env_file.with_name("dash.env.txt").write_text("DASH_USER=x\n", encoding="utf-8")       # Notepad's default
    assert serve(laptop=True, env=env) == 2
    assert "dash.env.txt is: rename it" in capsys.readouterr().out

    # broker keys never reach the dashboard (refused, as on EC2); neither values nor secrets are printed
    env_file.write_text(f"DASH_USER={USER}\nDASH_PASS_SCRYPT='{pass_hash}'\nDASH_SECRET={SECRET}\n"
                        "ZERODHA_API_SECRET=broker-secret-value\n", encoding="utf-8")
    assert serve(laptop=True, env=env) == 2
    out = capsys.readouterr().out
    assert "ZERODHA_API_SECRET" in out and "broker-secret-value" not in out and pass_hash not in out
    env_file.write_text(f"DASH_USER={USER}\nDASH_PASS_SCRYPT='{pass_hash}'\nDASH_SECRET=too-short-1\n",
                        encoding="utf-8")
    assert serve(laptop=True, env=env) == 2
    out = capsys.readouterr().out
    assert "at least 16 characters" in out and "too-short-1" not in out

    env_file.write_text(f"DASH_USER={USER}\nDASH_PASS_SCRYPT='{pass_hash}'\nDASH_SECRET={SECRET}\n",
                        encoding="utf-8")
    assert serve(laptop=True, env={"APPDATA": str(roaming)}) == 2          # no LOCALAPPDATA, no TITAN2_VAR
    out = capsys.readouterr().out
    assert "LOCALAPPDATA" in out and "hash-password" not in out             # not a credentials problem
    assert servers == []
    assert serve(laptop=True, env=env) == 0 and len(servers) == 1


# ---------------------------------------------------------------------------------------------------------------
# python -m titan2.dash.app serve, and the isolation it keeps
# ---------------------------------------------------------------------------------------------------------------
def test_module_serve_command(tmp_path, creds, servers, monkeypatch):
    for key, value in env_for(tmp_path, creds).items():
        monkeypatch.setenv(key, value)
    for key in [k for k in os.environ if k.startswith("ZERODHA_")]:
        monkeypatch.delenv(key)
    assert dash.main(["serve", "--port", "5050", "--laptop"]) == 0
    assert (servers[-1]["host"], servers[-1]["port"]) == ("127.0.0.1", 5050)
    assert dash.main(["serve", "--host", "0.0.0.0"]) == 2 and len(servers) == 1
    assert dash.main(["serve", "--host", "0.0.0.0", "--allow-public", "--laptop"]) == 0
    assert servers[-1]["host"] == "0.0.0.0"


def test_dashboard_imports_only_the_clock_from_titan2():
    # titan2.laptop (token, store, self-check) stays out: the dashboard process never loads broker or core code
    tree = ast.parse((AT_DIR / "titan2" / "dash" / "app.py").read_text(encoding="utf-8"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert {m for m in mods if m and m.startswith("titan2")} == {"titan2.clock"}
