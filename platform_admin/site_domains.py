"""Host-only website/SNI registry. Never confused with service Domain Mapping.

The UI can request three fixed operations; neither commands nor paths are input.
Certbot only writes its own webroot lineage. Existing sites are never removed.
"""

from copy import deepcopy
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class SiteError(Exception):
    pass


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "script" and attrs.get("src"):
            self.urls.append(attrs["src"])
        elif tag == "link" and attrs.get("rel") == "stylesheet" and attrs.get("href"):
            self.urls.append(attrs["href"])


def hostname(value):
    if not isinstance(value, str) or len(value) > 253 or value != value.lower():
        raise SiteError("invalid_domain")
    labels = value.split(".")
    if len(labels) < 2 or labels[-1].isdigit() or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in labels):
        raise SiteError("invalid_domain")
    return value


def atomic(path, data, mode=0o600):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise SiteError("unsafe_site_storage")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".site-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.chmod(name, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_registry(value):
    if (not isinstance(value, dict) or set(value) != {"version", "primary", "domains"}
            or value["version"] != 1 or not isinstance(value["domains"], list)
            or not 1 <= len(value["domains"]) <= 30):
        raise SiteError("invalid_site_registry")
    names = []
    for row in value["domains"]:
        if not isinstance(row, dict) or set(row) != {"domain", "fullchain", "private_key"}:
            raise SiteError("invalid_site_registry")
        names.append(hostname(row["domain"]))
        for key in ("fullchain", "private_key"):
            path = row[key]
            if not isinstance(path, str) or not Path(path).is_absolute() or re.search(r'[\s;{}$"\\]', path):
                raise SiteError("invalid_certificate_path")
    if len(set(names)) != len(names) or value["primary"] not in names:
        raise SiteError("invalid_site_registry")
    return value


class WebsiteDomains:
    def __init__(self, env, execute=None):
        self.env = env
        self.root = Path(env["INSTALLER_ROOT"])
        self.directory = Path(env["ADMIN_MAINTENANCE_STATE_DIR"])
        self.path = self.directory / "website-domains.json"
        self.site = Path(env["NGINX_SITE_CONFIG_PATH"])
        self.execute = execute or subprocess.run

    def command(self, args, *, input=None):
        try:
            result = self.execute(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=360, shell=False, cwd=self.root)
        except (OSError, subprocess.SubprocessError):
            raise SiteError("site_operation_failed") from None
        if result.returncode:
            raise SiteError("site_operation_failed")
        return result.stdout

    def task(self, action, *args):
        return self.command(["/bin/bash", str(self.root / "install.sh"), "__website-task", action, *args])

    def switch(self, registry, previous):
        row = next(r for r in registry["domains"] if r["domain"] == registry["primary"])
        self.task("primary", row["domain"], previous, row["fullchain"], row["private_key"])

    def read(self):
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise SiteError("unsafe_site_storage")
        if self.path.exists():
            if self.path.stat().st_size > 65536:
                raise SiteError("invalid_site_registry")
            return validate_registry(json.loads(self.path.read_text(encoding="utf-8")))
        domain = hostname(self.env["DOMAIN"])
        return validate_registry({"version": 1, "primary": domain, "domains": [{"domain": domain,
            "fullchain": self.env["CERTBOT_FULLCHAIN_PATH"], "private_key": self.env["CERTBOT_PRIVATE_KEY_PATH"]}]})

    def save(self, registry):
        atomic(self.path, json.dumps(validate_registry(registry)).encode())

    def certificate(self, row):
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_cert_chain(row["fullchain"], row["private_key"])
            cert = ssl._ssl._test_decode_cert(row["fullchain"])
            # Match DNS SAN, not just the certificate's dates or existence.
            names = [value.lower() for kind, value in cert.get("subjectAltName", ()) if kind == "DNS"]
            domain = row["domain"]
            if not any(name == domain or (name.startswith("*.") and domain.count(".") == name.count(".")
                                          and domain.endswith(name[1:])) for name in names):
                raise ValueError
            expires = ssl.cert_time_to_seconds(cert["notAfter"])
            status = "已过期" if expires <= time.time() else ("尚未生效" if ssl.cert_time_to_seconds(cert["notBefore"]) > time.time() else "有效")
            return {"ssl_status": status, "expires": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expires))}
        except (OSError, ValueError, ssl.SSLError):
            return {"ssl_status": "需要检查", "expires": ""}

    def tls_loadable(self, row):
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_cert_chain(row["fullchain"], row["private_key"])
            return True
        except (OSError, ValueError, ssl.SSLError):
            return False

    def state(self):
        value = self.read()
        return {"domains": [{"domain": r["domain"], "primary": r["domain"] == value["primary"],
                              **self.certificate(r)} for r in value["domains"]]}

    def challenge(self, domain, nonce):
        return ((self.root / "templates/domain/challenge.conf.template").read_text(encoding="utf-8")
            .replace("{{DOMAIN}}", hostname(domain)).replace("{{NGINX_LISTEN_PORT}}", self.env["NGINX_LISTEN_PORT"])
            .replace("{{CERTBOT_WEBROOT}}", self.env["WEBSITE_CERTBOT_WEBROOT"]).replace("{{NONCE}}", nonce))

    def render(self, registry, *, challenge="", nonce=""):
        validate_registry(registry)
        fragments = []
        for row in registry["domains"]:
            # An expired/lost certificate on the failed old primary must not
            # prevent a verified backup from becoming the new primary. Keep
            # its registry record (visible as needing attention), not a broken
            # TLS vhost which would make every nginx reload fail.
            if self.certificate(row)["ssl_status"] != "有效" and not self.tls_loadable(row):
                # Keep HTTP-01 available so renewal can repair an expired
                # certificate. Only the unusable HTTPS listener is omitted.
                fragments.append(self.challenge(row["domain"], nonce))
                continue
            public_keys = ("NGINX_LISTEN_PORT", "DOMAIN_HTTPS_PORT", "WEBSITE_CERTBOT_WEBROOT",
                "NGINX_UPSTREAM_HOST", "CHATWOOT_HTTP_PORT", "NGINX_CLIENT_MAX_BODY_SIZE",
                "NGINX_PROXY_READ_TIMEOUT_SECONDS", "ADMIN_UPSTREAM_HOST", "AI_ADMIN_HTTP_PORT", "AI_SERVICE_HTTP_PORT")
            values = {**{key: self.env[key] for key in public_keys if key in self.env},
                      "DOMAIN": row["domain"], "CERTBOT_FULLCHAIN_PATH": row["fullchain"],
                      "CERTBOT_PRIVATE_KEY_PATH": row["private_key"],
                      "HTTPS_PORT_SUFFIX": "" if self.env["DOMAIN_HTTPS_PORT"] == "443" else ":" + self.env["DOMAIN_HTTPS_PORT"]}
            site = Path(self.env["DOMAIN_HTTPS_TEMPLATE_FILE"]).read_text(encoding="utf-8")
            snippet = ""
            if self.env.get("ADMIN_ENABLED") == "true":
                snippet = (self.root / "templates/nginx/admin.conf.template").read_text(encoding="utf-8")
                if self.env.get("AI_RUNTIME_BUILD") == "true":
                    snippet += (self.root / "templates/nginx/chatwoot-ai.conf.template").read_text(encoding="utf-8").replace("{{AI_RUNTIME_UPSTREAM_HOST}}", self.env["AI_SERVICE_BIND_ADDRESS"])
            site = site.replace("# PLATFORM_ADMIN_LOCATION", snippet)
            if nonce:
                site = site.replace("    client_max_body_size", '    location = /.__platform-domain-check { return 200 "' + nonce + '"; }\n    client_max_body_size')
            # The header parsing directive is inherited once in http context.
            site = re.sub(r"^underscores_in_headers on;\n", "", site, flags=re.M)
            for key, value in values.items():
                site = site.replace("{{" + key + "}}", value)
            if re.search(r"{{[A-Z_0-9]+}}", site):
                raise SiteError("invalid_site_template")
            fragments.append(site)
        if challenge:
            fragments.append(self.challenge(challenge, nonce))
        return "# Managed by AI Customer Platform Installer.\nunderscores_in_headers on;\n" + "\n".join(fragments)

    def apply(self, content):
        # Existing installer does ownership checks, nginx -t and rollback/reload.
        fd, path = tempfile.mkstemp(prefix="website-nginx-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
            self.task("apply", path)
        finally:
            os.unlink(path)

    def addresses(self, domain):
        try:
            addresses = sorted({str(ipaddress.ip_address(info[4][0])) for info in socket.getaddrinfo(domain, None, type=socket.SOCK_STREAM)})
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise ValueError
            return addresses
        except (OSError, ValueError):
            raise SiteError("domain_dns_failed") from None

    def probe(self, domain, nonce, *, https=True):
        port = self.env["DOMAIN_HTTPS_PORT" if https else "NGINX_LISTEN_PORT"]
        scheme = "https" if https else "http"
        # Every published A/AAAA record must reach OUR staged vhost. This also
        # handles NAT without guessing a server's public IP from a third party.
        for address in self.addresses(domain):
            ip = "[" + address + "]" if ":" in address else address
            args = ["curl", "--noproxy", "*", "--silent", "--show-error", "--fail", "--max-time", "15",
                    "--resolve", f"{domain}:{port}:{ip}"]
            origin = f"{scheme}://{domain}:{port}"
            if self.command(args + [origin + "/.__platform-domain-check"]).decode() != nonce:
                raise SiteError("domain_access_failed")
            if https:
                if self.env.get("ADMIN_ENABLED") == "true":
                    page = self.command(args + [origin + "/admin/login"])
                    if b'name="csrf"' not in page:
                        raise SiteError("domain_access_failed")
                page = self.command(args + [origin + "/"])
                if not page:
                    raise SiteError("domain_access_failed")
                assets = Assets()
                assets.feed(page.decode("utf-8"))
                if not assets.urls:
                    raise SiteError("chatwoot_assets_missing")
                for source in assets.urls[:8]:
                    url = urljoin(origin + "/", source)
                    parsed = urlsplit(url)
                    if parsed.scheme != "https" or parsed.hostname != domain:
                        raise SiteError("chatwoot_cross_origin_asset")
                    status = self.command(args + ["--output", os.devnull, "--write-out", "%{http_code}", url])
                    if status != b"200":
                        raise SiteError("chatwoot_assets_unavailable")
                self.websocket(args, origin)

    def websocket(self, args, origin):
        # A real RFC6455 upgrade, not HTML 200. No visitor/management token is
        # necessary for the transport handshake; RoomChannel still authenticates
        # its own pubsub subscription as before.
        import base64
        import hashlib
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        command = args + ["--http1.1", "--include", "--max-time", "3",
            "--header", "Connection: Upgrade", "--header", "Upgrade: websocket",
            "--header", "Sec-WebSocket-Version: 13", "--header", "Sec-WebSocket-Key: " + key,
            "--header", "Origin: " + origin, origin + "/cable"]
        try:
            result = self.execute(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=10, shell=False, cwd=self.root)
        except (OSError, subprocess.SubprocessError):
            raise SiteError("chatwoot_websocket_unavailable") from None
        # curl times out after a successful upgrade because the socket remains
        # open. Only accept that specific bounded timeout with a real 101.
        if result.returncode not in {0, 28} or not re.match(rb"HTTP/1\.1 101\b", result.stdout):
            raise SiteError("chatwoot_websocket_unavailable")
        headers = dict(line.lower().split(b":", 1) for line in result.stdout.split(b"\r\n\r\n", 1)[0].split(b"\r\n")[1:] if b":" in line)
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        # Lowercasing the header name must not lowercase its Base64 value.
        accept = next((line.split(b":", 1)[1].strip() for line in result.stdout.split(b"\r\n")
                       if line.lower().startswith(b"sec-websocket-accept:")), b"")
        if headers.get(b"upgrade", b"").strip() != b"websocket" or accept != expected:
            raise SiteError("chatwoot_websocket_unavailable")

    def operation(self, action, domain="", *, restored=None):
        if action not in {"add", "primary", "delete", "reconcile", "restore-check", "restore"}:
            raise SiteError("operation_not_allowed")
        if domain:
            hostname(domain)
        old = self.read()
        had_registry = self.path.exists()
        new = deepcopy(restored if restored is not None else old)
        validate_registry(new)
        names = {r["domain"] for r in old["domains"]}
        if action == "add" and domain in names:
            raise SiteError("domain_already_exists")
        if action in {"primary", "delete"} and domain not in names:
            raise SiteError("domain_not_found")
        if action == "delete" and (domain == old["primary"] or len(names) <= 1):
            raise SiteError("primary_domain_protected")
        if action == "restore-check":
            for row in new["domains"]:
                if self.certificate(row)["ssl_status"] != "有效":
                    raise SiteError("restore_certificate_invalid")
                self.addresses(row["domain"])
            original = self.site.read_text(encoding="utf-8")
            combined = deepcopy(old)
            combined["domains"] += [r for r in new["domains"] if r["domain"] not in names]
            nonce = secrets.token_hex(24)
            try:
                self.apply(self.render(combined, nonce=nonce))
                for row in new["domains"]:
                    self.probe(row["domain"], nonce)
            finally:
                self.apply(original)
            return
        if not self.site.is_file() or self.site.is_symlink():
            raise SiteError("managed_site_required")
        original = self.site.read_text(encoding="utf-8")
        nonce = secrets.token_hex(24)
        switched = False
        journal = self.directory / "website-pending.json"
        transaction = {"registry": old, "nginx": original, "switch_started": False,
            "primary": self.env["DOMAIN"], "target": domain if action == "primary" else new["primary"]}
        atomic(journal, json.dumps(transaction).encode())
        try:
            if action == "add":
                if len(names) >= 30:
                    raise SiteError("domain_limit")
                self.addresses(domain)
                self.apply(self.render(old, challenge=domain, nonce=nonce))
                self.probe(domain, nonce, https=False)
                self.task("issue", domain)
                base = Path(self.env["CERTBOT_CONFIG_DIR"]) / "live" / domain
                new["domains"].append({"domain": domain, "fullchain": (base / "fullchain.pem").as_posix(), "private_key": (base / "privkey.pem").as_posix()})
            if action == "delete":
                new["domains"] = [r for r in new["domains"] if r["domain"] != domain]
            if action == "primary":
                new["primary"] = domain
            for row in new["domains"]:
                required = action == "restore" or row["domain"] == (domain if action in {"add", "primary"} else new["primary"])
                if required and self.certificate(row)["ssl_status"] != "有效":
                    raise SiteError("certificate_invalid")
            self.apply(self.render(old if action == "delete" else new, nonce=nonce))
            # Do not depend on a failed old primary during failover/deletion.
            if action in {"add", "primary"}:
                self.probe(domain, nonce)
            elif action in {"restore", "delete"}:
                self.probe(new["primary"], nonce)
            if new["primary"] != self.env["DOMAIN"] and self.env.get("WEBSITE_DEFER_PRIMARY") != "true":
                if action not in {"add", "primary"}:
                    self.probe(new["primary"], nonce)
                transaction["switch_started"] = True
                atomic(journal, json.dumps(transaction).encode())
                switched = True
                self.switch(new, self.env["DOMAIN"])
            if action == "add":
                self.task("alias", domain)
            if action == "delete":
                # A bot may have been bound while Admin was visited via this
                # backup domain. Move its callback/API origin to the working
                # primary BEFORE publishing removal; never recreate the bot.
                self.task("retire", domain, new["primary"])
            self.apply(self.render(new))
            self.save(new)
            journal.unlink()
        except Exception:
            # No new state is published until HTTPS and dependencies succeed.
            if switched:
                self.switch(old, new["primary"])
            self.apply(original)
            if had_registry:
                self.save(old)
            elif self.path.exists():
                self.path.unlink()
            journal.unlink(missing_ok=True)
            raise

    def recover(self):
        journal = self.directory / "website-pending.json"
        if not journal.exists():
            return
        if journal.is_symlink() or journal.stat().st_size > 1_048_576:
            raise SiteError("unsafe_site_storage")
        previous = json.loads(journal.read_text(encoding="utf-8"))
        validate_registry(previous["registry"])
        if previous.get("switch_started", True):
            self.switch(previous["registry"], hostname(previous["target"]))
        self.apply(previous["nginx"])
        self.save(previous["registry"])
        journal.unlink()


def main():
    import sys
    env = dict(os.environ)
    manager = WebsiteDomains(env)
    action = sys.argv[1]
    if action == "state":
        print(json.dumps(manager.state()))
        return
    # One cross-process lock for installer, restore and web maintenance callers.
    import fcntl
    manager.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = manager.directory / "website.lock"
    if lock.is_symlink():
        raise SiteError("unsafe_site_storage")
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manager.recover()
        if action == "recover":
            return
        restored = None
        if action in {"restore-check", "restore"}:
            restored = validate_registry(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))
        manager.operation(action, "" if restored else (sys.argv[2] if len(sys.argv) > 2 else ""), restored=restored)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("website_domain_operation_failed") from None
