"""Host-only terminal recovery controller; standard library, no HTTP routes."""

import argparse
import getpass
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import subprocess
import sys
import warnings
from urllib.parse import urlsplit

if __package__:
    from .password_reset import ResetError, validate_password
else:
    from password_reset import ResetError, validate_password


class RecoveryError(Exception):
    pass


class Terminal:
    def __enter__(self):
        try:
            descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
            self.stream = os.fdopen(descriptor, "w", encoding="utf-8", buffering=1)
            if not self.stream.isatty():
                self.stream.close()
                raise RecoveryError("Recovery requires a local controlling terminal.")
        except OSError:
            raise RecoveryError("Recovery requires a local controlling terminal; do not pipe passwords.") from None
        return self

    def __exit__(self, *_):
        self.stream.close()

    def write(self, message):
        self.stream.write(message)
        self.stream.flush()

    def password(self, prompt):
        # Fail rather than getpass falling back to visible/redirected input.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt, stream=self.stream)


class LocalDocker:
    def __init__(self, terminal):
        self.terminal = terminal
        self.environment = os.environ.copy()
        self.command = ["docker"]
        if not shutil.which("docker"):
            raise RecoveryError("Local Docker is required.")
        if os.environ.get("DOCKER_CONTEXT"):
            endpoint = self.call(["context", "inspect", os.environ["DOCKER_CONTEXT"], "--format", "{{.Endpoints.docker.Host}}"])
        elif os.environ.get("DOCKER_HOST"):
            endpoint = os.environ["DOCKER_HOST"]
        else:
            endpoint = self.call(["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
        parsed = urlsplit(endpoint.strip())
        if (parsed.scheme != "unix" or parsed.netloc or not parsed.path.startswith("/")
                or parsed.query or parsed.fragment or any(c in endpoint for c in ("\0", "\r", "\n"))):
            raise RecoveryError("Recovery is restricted to a local Unix Docker socket, not remote Docker contexts.")
        # Make sudo and direct Docker use exactly the same local endpoint.
        self.environment.pop("DOCKER_HOST", None)
        self.environment.pop("DOCKER_CONTEXT", None)
        self.command = ["docker", "--host", endpoint.strip()]
        if self.result(["version", "--format", "{{.Server.Version}}"]).returncode:
            if getattr(os, "geteuid", lambda: -1)() == 0 or not shutil.which("sudo"):
                raise RecoveryError("Root, Docker administration privileges, or authorized sudo are required.")
            authorization = subprocess.run(["sudo", "-v"], stdin=terminal.stream, stdout=terminal.stream,
                                           stderr=terminal.stream, env=self.environment)
            if authorization.returncode:
                raise RecoveryError("Sudo authorization failed; no password was changed.")
            self.command = ["sudo", "-n", "--", *self.command]
            self.call(["version", "--format", "{{.Server.Version}}"])

    def result(self, arguments, payload=None):
        try:
            return subprocess.run([*self.command, *arguments], input=payload, capture_output=True,
                                  stdin=subprocess.DEVNULL if payload is None else None,
                                  env=self.environment, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise RecoveryError("Docker operation could not be confirmed; inspect the local service before retrying recovery.") from None

    def call(self, arguments, payload=None):
        result = self.result(arguments, payload)
        if result.returncode:
            raise RecoveryError("Local Docker operation failed; no recovery success was confirmed.")
        return result.stdout.decode("utf-8").strip()

    def admin_container(self, project, service):
        ids = self.call(["ps", "--all", "--filter", "label=com.docker.compose.project=" + project,
                         "--filter", "label=com.docker.compose.service=" + service,
                         "--filter", "label=com.docker.compose.oneoff=False", "--format", "{{.ID}}"]).split()
        if len(ids) != 1:
            raise RecoveryError("Exactly one existing unified Admin container is required.")
        container = ids[0]
        metadata = ('{"State":{"Running":{{json .State.Running}}},'
                    '"Config":{"Labels":{{json .Config.Labels}}},"Mounts":{{json .Mounts}}}')
        inspected = json.loads(self.call(["inspect", "--format", metadata, container]))
        # Inspect output stays in memory; never print a Docker error/body/env.
        labels = inspected.get("Config", {}).get("Labels", {})
        overlays = labels.get("com.docker.compose.project.config_files", "").replace("\\", "/").split(",")
        if not inspected.get("State", {}).get("Running"):
            raise RecoveryError("Start the existing unified Admin container before recovery; no container was created.")
        if not any(path.endswith("/stacks/admin/compose.yaml") for path in overlays):
            raise RecoveryError("The selected container is not the deployed unified Admin service.")
        probe = ("import json,os;from platform_admin.auth import AuthStore;"
                 "print(json.dumps({'path':str(AuthStore(os.environ).path),"
                 "'compatible':getattr(AuthStore,'recovery_protocol',None)==1 and "
                 "all(hasattr(AuthStore,k) for k in ('_save_password','_initialize_password','_legacy_record'))}))")
        state = json.loads(self.call(["exec", container, "python", "-c", probe]))
        if not state.get("compatible"):
            raise RecoveryError("Upgrade the legacy Admin image before recovery; no data was changed.")
        path = PurePosixPath(state["path"])
        if not path.is_absolute() or path.name != "auth.sqlite3" or ".." in path.parts:
            raise RecoveryError("Invalid deployed Admin authentication path.")
        parent = str(path.parent)
        if not any(mount.get("Type") == "volume" and mount.get("Name") and mount.get("RW")
                   and mount.get("Destination") == parent for mount in inspected.get("Mounts", [])):
            raise RecoveryError("The Admin authentication directory must use its existing writable persistent volume.")
        return container

    def reset(self, container, password):
        # Only code and the container ID are arguments. docker exec stdin is
        # not the container's main process, and the worker never prints secrets.
        source = Path(__file__).with_name("password_reset.py").read_text(encoding="utf-8")
        receipt = self.call(["exec", "--interactive", container, "python", "-c", source],
                            json.dumps({"password": password}, ensure_ascii=False).encode("utf-8"))
        if receipt != "reset-ok":
            raise RecoveryError("Password recovery returned no success receipt.")


def recover(project, service, manual=False):
    with Terminal() as terminal:
        docker = LocalDocker(terminal)
        container = docker.admin_container(project, service)
        terminal.write("统一管理员恢复将注销全部旧会话并解除登录锁定。\n")
        if manual:
            password = terminal.password("请输入新密码（至少 6 位）：")
            validate_password(password)
            confirmation = terminal.password("请再次输入新密码：")
            if not secrets.compare_digest(password.encode(), confirmation.encode()):
                raise RecoveryError("Passwords did not match; nothing was changed.")
        else:
            password = secrets.token_urlsafe(32)
        docker.reset(container, password)
        terminal.write("[OK] admin 密码已重置；登录锁定及旧会话已清除。\n")
        if not manual:
            terminal.write("新密码（仅显示一次，请妥善保存）：\n" + password + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private local unified administrator recovery")
    parser.add_argument("--project", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--manual", action="store_true")
    options = parser.parse_args(argv)
    try:
        recover(options.project, options.service, options.manual)
    except (RecoveryError, ResetError) as error:
        print("[ERROR] " + str(error), file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print("[ERROR] Recovery was interrupted or could not be confirmed; no credentials are logged.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
