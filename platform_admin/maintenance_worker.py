"""Host-only, serialized maintenance bridge. Never mount Docker into the UI.

Only four fixed scripts and two fixed list operations are accepted. Script
output is discarded, not returned to the browser or journal. Jobs survive an
Admin container stop during the existing backup/restore/upgrade workflows.
"""

from http.server import BaseHTTPRequestHandler
from contextlib import contextmanager
import hmac
import hashlib
import json
import os
from pathlib import Path
import re
import socketserver
import sqlite3
import subprocess
import threading
import time
import uuid


VERSION = re.compile(r"[0-9]{8}T[0-9]{6}Z(?:-[0-9]{2})?\Z")


class MaintenanceError(Exception):
    pass


class Runner:
    def __init__(self, root, config_file, state_dir, execute=None):
        self.root, self.config_file, self.state_dir = Path(root), Path(config_file), Path(state_dir)
        self.execute = execute or subprocess.run
        if not all(path.is_absolute() for path in (self.root, self.config_file, self.state_dir)):
            raise MaintenanceError("invalid_worker_path")
        if any(path.is_symlink() for path in (self.state_dir, *self.state_dir.parents)):
            raise MaintenanceError("unsafe_worker_storage")
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.database = self.state_dir / "jobs.sqlite3"
        if self.database.is_symlink():
            raise MaintenanceError("unsafe_worker_storage")
        self.lock = threading.Lock()
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, operation TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL)")
            # Restarting a worker does not silently retry a destructive job.
            db.execute("UPDATE jobs SET status='interrupted' WHERE status='running'")
        os.chmod(self.database, 0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.database, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def command(self, operation, version="", *, listing=False):
        if operation in {"domain-add", "domain-primary", "domain-delete"}:
            from .site_domains import hostname, SiteError
            try:
                hostname(version)
            except SiteError:
                raise MaintenanceError("invalid_domain") from None
            if listing:
                raise MaintenanceError("operation_not_allowed")
            return ["/bin/bash", str(self.root / "install.sh"), "__website-domains", operation[7:], version]
        if operation not in {"backup", "restore", "upgrade", "rollback"}:
            raise MaintenanceError("operation_not_allowed")
        if listing:
            if operation not in {"restore", "rollback"}:
                raise MaintenanceError("operation_not_allowed")
            return ["/bin/bash", str(self.root / (operation + ".sh")), "--list"]
        if operation in {"restore", "rollback"}:
            if not VERSION.fullmatch(version):
                raise MaintenanceError("invalid_version")
        elif version:
            raise MaintenanceError("unexpected_version")
        args = ["/bin/bash", str(self.root / (operation + ".sh"))]
        if version:
            args.append(version)
        if operation != "backup":
            args.append("--yes")
        return args

    def run(self, args, *, listing=False):
        # Do not propagate the web server's environment or attacker input.
        return self.execute(args, cwd=self.root, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if listing else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30 if listing else None,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "CONFIG_FILE": str(self.config_file), "LANG": "C.UTF-8"})

    def versions(self, operation):
        try:
            result = self.run(self.command(operation, listing=True), listing=True)
            if result.returncode:
                return []
            text = result.stdout.decode("utf-8", errors="replace")[:131072]
            # Existing lists contain metadata; expose only validated version IDs.
            return sorted({word for word in text.split() if VERSION.fullmatch(word)}, reverse=True)[:100]
        except (OSError, subprocess.SubprocessError):
            return []

    def websites(self):
        try:
            result = self.run(["/bin/bash", str(self.root / "install.sh"), "__website-domains", "state"], listing=True)
            if result.returncode or len(result.stdout) > 65536:
                raise ValueError
            value = json.loads(result.stdout)
            from .site_domains import hostname
            for row in value["domains"]:
                hostname(row["domain"])
                if set(row) != {"domain", "primary", "ssl_status", "expires"}:
                    raise ValueError
            return value
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            raise MaintenanceError("website_state_unavailable") from None

    def state(self):
        with self.db() as db:
            rows = db.execute("SELECT id, operation, status FROM jobs ORDER BY created DESC, rowid DESC LIMIT 50").fetchall()
        return {"jobs": [dict(zip(("id", "operation", "status"), row)) for row in rows],
                "backups": self.versions("restore"), "upgrades": self.versions("rollback")}

    def submit(self, data, *, background=True):
        if set(data) != {"operation", "version", "confirmation"} or not all(isinstance(v, str) for v in data.values()):
            raise MaintenanceError("invalid_job")
        operation, version = data["operation"], data["version"]
        args = self.command(operation, version)
        if data["confirmation"] != operation.upper():
            raise MaintenanceError("confirmation_required")
        if not self.lock.acquire(blocking=False):
            raise MaintenanceError("maintenance_busy")
        job_id = uuid.uuid4().hex
        try:
            with self.db() as db:
                db.execute("INSERT INTO jobs VALUES (?, ?, ?, 'running', ?)", (job_id, operation, version, int(time.time())))
        except Exception:
            self.lock.release()
            raise

        def work():
            status = "failed"
            try:
                status = "succeeded" if self.run(args).returncode == 0 else "failed"
            except (OSError, subprocess.SubprocessError):
                pass
            finally:
                try:
                    with self.db() as db:
                        db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
                finally:
                    self.lock.release()
        if background:
            threading.Thread(target=work, daemon=False).start()
        else:
            work()
        return {"id": job_id, "status": "accepted"}


def make_handler(runner, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, value):
            encoded = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            self.handle_api()

        def do_POST(self):
            self.handle_api()

        def handle_api(self):
            expected = token() if callable(token) else token
            if len(expected) < 32 or not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + expected).encode()):
                self.respond(401, {"error": "unauthorized"})
                return
            try:
                self.connection.settimeout(10)
                if self.command == "GET" and self.path == "/state":
                    result = runner.state()
                elif self.command == "GET" and self.path == "/website-domains":
                    result = runner.websites()
                elif self.command == "POST" and self.path == "/jobs":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise MaintenanceError("invalid_job")
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise MaintenanceError("invalid_job")
                    result = runner.submit(data)
                else:
                    self.respond(404, {"error": "not_found"})
                    return
                self.respond(200, result)
            except (MaintenanceError, ValueError, TypeError, OSError, sqlite3.Error):
                self.respond(409, {"error": "maintenance_rejected"})
    return Handler


def main():
    import fcntl
    token = os.environ.get("ADMIN_MAINTENANCE_TOKEN", "")
    socket_dir = Path(os.environ["ADMIN_MAINTENANCE_SOCKET_DIR"])
    if len(token) < 32 or any(p.is_symlink() for p in (socket_dir, *socket_dir.parents)):
        raise MaintenanceError("worker_not_configured")
    os.umask(0o077)
    socket_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(socket_dir, 0o700)
    # Prevent multiple workers from claiming the same job database/socket.
    lock_file = (socket_dir / "worker.lock").open("a")
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    runner = Runner(os.environ["INSTALLER_ROOT"], os.environ["CONFIG_FILE"], os.environ["ADMIN_MAINTENANCE_STATE_DIR"])
    if runner.run(["/bin/bash", str(runner.root / "install.sh"), "__website-domains", "recover"]).returncode:
        raise MaintenanceError("website_recovery_required")
    from .site_domains import atomic
    atomic(runner.state_dir / "worker.version", hashlib.sha256(Path(__file__).read_bytes()).hexdigest().encode())
    socket_path = socket_dir / "worker.sock"
    if socket_path.exists():
        import stat
        if not stat.S_ISSOCK(socket_path.lstat().st_mode):
            raise MaintenanceError("unsafe_worker_socket")
        socket_path.unlink()

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
    def current_token():
        # A restore can atomically replace .env while this worker survives.
        # Read just this allow-listed scalar; never source/evaluate user text.
        try:
            values = []
            with runner.config_file.open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip().startswith("ADMIN_MAINTENANCE_TOKEN="):
                        values.append(line.strip().split("=", 1)[1].strip().strip("\"'"))
            return values[0] if len(values) == 1 else ""
        except (OSError, UnicodeError):
            return ""

    with Server(str(socket_path), make_handler(runner, current_token)) as server:
        os.chmod(socket_path, 0o600)
        server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # No exception text can include config paths, tokens or script output.
        raise SystemExit("maintenance_worker_unavailable") from None
