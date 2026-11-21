#!/usr/bin/env python3
"""Local-only ChannelHub pilot. No production targets or arbitrary command execution."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import http.client
import http.server
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile

SOURCE_SHA = "001e50997d94ed108382a09d1d968cffee6b8919"
HOST_SHA = "fff563b2d33ad8aa93474714e050619a07e353e4"
HOSTS = {"reference": HOST_SHA,
         "tencent-source": "a4ab674a403f10b7f91fcfc242719204621b2bb9",
         "tencent-runtime": "ce904c476f58e0857aa9ee828619158a82d75e7e",
         "accepted": "165a126b5b43a743822047801efc2b6d5e436f48"}
DEFAULT_HOST = "accepted"
ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
CACHE = HOME / ".cache/agents-os-dev/channelhub"
STATE = HOME / ".local/share/agents-os-dev/channelhub"
NVM_VERSIONS = Path(os.environ.get("NVM_DIR") or HOME / ".nvm") / "versions/node"
NODE = NVM_VERSIONS / "v24.22.1/bin/node"
if not NODE.is_file():
    NODE = HOME / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
NPM = NVM_VERSIONS / "v22.23.1/lib/node_modules/npm/bin/npm-cli.js"
HOST_GIT = CACHE / "host.git"
SCHEMA = "channelhub-dev-pilot-r1"
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8",
       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1"}
FORBIDDEN = {".git", "state-private", "secrets", "credentials", "tokens", "cookies",
             "node_modules", "state", "logs", "cache", "runtime-data"}
TOP_INPUTS = {"package.json", "LICENSE", "README.md", "SOURCE_MIGRATION.md", ".gitignore"}


class Refusal(RuntimeError):
    pass


def run(argv, *, cwd=None, capture=False, timeout=300):
    result = subprocess.run([str(a) for a in argv], cwd=cwd, env=ENV,
                            capture_output=capture, timeout=timeout, check=False)
    if result.returncode:
        # Do not print arbitrary environment or raw subprocess stderr.
        raise Refusal(f"command failed ({result.returncode}): {Path(str(argv[0])).name}")
    return result.stdout if capture else None


def git(*args, repo=ROOT):
    return run(["git", "--no-optional-locks", "-c", "credential.helper=", "-C", repo, *args],
               capture=True).decode().strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with tmp.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def safe_chain(path):
    """Do not follow symlinks in an owned resource path, including its ancestors."""
    for part in (path, *path.parents):
        if part.is_symlink():
            raise Refusal("resource path contains a symlink")


def regular(path):
    safe_chain(path)
    if not path.is_file() or path.stat().st_uid != os.getuid():
        raise Refusal("expected an owned regular file")


def owned_dir(path):
    safe_chain(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise Refusal("resource owner mismatch")
    if path.stat().st_mode & 0o077:
        raise Refusal("resource directory must be private (0700)")


def init_resources():
    os.umask(0o077)
    for path in (CACHE, STATE):
        owned_dir(path)
        marker = path / "owner.json"
        if marker.exists():
            regular(marker)
            if json.loads(marker.read_text()) != {"schema": SCHEMA, "uid": os.getuid()}:
                raise Refusal("resource marker mismatch")
        else:
            # Bootstrap only known cache content produced by this pilot's source acquisition.
            allowed = {"host.git"} if path == CACHE else set()
            if set(p.name for p in path.iterdir()) - allowed:
                raise Refusal("unregistered resource directory is not empty")
            write_json(marker, {"schema": SCHEMA, "uid": os.getuid()})


@contextlib.contextmanager
def lock(name):
    path = STATE / (name + ".lock")
    safe_chain(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise Refusal("lock owner mismatch")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal(f"{name} is busy") from None
        yield
    finally:
        os.close(fd)


def admitted(name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return False
    if any(x in FORBIDDEN for x in p.parts):
        return False
    if p.name.startswith(".env") or p.name == "config.env":
        return False
    if p.suffix.lower() in {".pem", ".key", ".db", ".sqlite", ".sqlite3", ".log", ".p12", ".pfx"}:
        return False
    return True


def input_name(name):
    return admitted(name) and (name in TOP_INPUTS or name.startswith(
        ("Plugin/ChannelHub/", "tests/", "scripts/", "dev/")))


def snapshot(destination, *, committed=False):
    """Admit known source files only. No .git, ignored state or arbitrary root files."""
    head = git("rev-parse", "HEAD")
    if git("rev-parse", SOURCE_SHA + "^{commit}") != SOURCE_SHA:
        raise Refusal("missing source baseline")
    if committed and git("status", "--porcelain=v1", "--untracked-files=all"):
        raise Refusal("prepare requires a clean committed worktree")
    names = set(git("ls-files").splitlines())
    # New development helpers are usable before their first commit; never scan other trees.
    if not committed:
        for base in (ROOT / "dev",):
            if base.exists():
                for path in base.rglob("*"):
                    if path.is_symlink():
                        raise Refusal("source helper symlink refused")
                    if path.is_file() and path.suffix in {".py", ".cjs", ".md", ".json"}:
                        names.add(path.relative_to(ROOT).as_posix())
    expected = git("ls-tree", "-r", "--name-only", SOURCE_SHA, "Plugin/ChannelHub").splitlines()
    if len(expected) != 46:
        raise Refusal("unexpected frozen plugin file set")
    hashes = {}
    destination.mkdir(parents=True)
    for name in sorted(n for n in names if input_name(n)):
        path = ROOT / name
        regular(path)
        data = run(["git", "-C", ROOT, "show", f"{head}:{name}"], capture=True) if committed else path.read_bytes()
        if name.startswith("Plugin/ChannelHub/"):
            if name not in expected or data != run(["git", "-C", ROOT, "show", f"{SOURCE_SHA}:{name}"], capture=True):
                raise Refusal("stable plugin content changed; outside pilot scope")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[name] = digest(data)
    actual = [n for n in hashes if n.startswith("Plugin/ChannelHub/")]
    # Detect untracked plugin files without consuming their contents.
    if sorted(actual) != sorted(expected) or any(
        p.is_symlink() or (p.is_file() and p.relative_to(ROOT).as_posix() not in expected)
        for p in (ROOT / "Plugin/ChannelHub").rglob("*")
    ):
        raise Refusal("stable plugin file set changed")
    identity = {"commit": head, "dirty": bool(git("status", "--porcelain=v1", "--untracked-files=all")),
                "snapshot_sha256": digest(json.dumps(hashes, sort_keys=True).encode()),
                "plugin_sha256": digest(json.dumps({n: hashes[n] for n in actual}, sort_keys=True).encode())}
    return identity


def profile_sha(profile):
    if profile not in HOSTS:
        raise Refusal("unknown fixed host profile")
    return HOSTS[profile]


def host_is_cached(profile):
    sha = profile_sha(profile)
    safe_chain(HOST_GIT)
    if not HOST_GIT.exists():
        return False
    probe = subprocess.run(["git", f"--git-dir={HOST_GIT}", "cat-file", "-e", sha + "^{commit}"],
                           env=ENV, capture_output=True)
    return probe.returncode == 0


def ensure_host(profile):
    sha = profile_sha(profile)
    safe_chain(HOST_GIT)
    if not HOST_GIT.exists():
        run(["git", "init", "--bare", HOST_GIT])
    if not host_is_cached(profile):
        run(["git", "-c", "credential.helper=", f"--git-dir={HOST_GIT}", "fetch", "--depth=1",
             "https://github.com/JENN2046/VCPToolBox.git", sha], timeout=300)


def host_archive(destination, profile=DEFAULT_HOST):
    """Export ordinary pinned source only; exclude agents, diary, config and unrelated plugins."""
    ensure_host(profile)
    sha = profile_sha(profile)
    refs = ["Plugin.js", "FileFetcherServer.js", "server.js", "modules", "shared", "package.json", "package-lock.json",
            "Plugin/ChannelHub", "Plugin/vcp-dingtalk-adapter", "Plugin/vcp-wecom-adapter", "Plugin/vcp-onebot-adapter"]
    names = run(["git", f"--git-dir={HOST_GIT}", "ls-tree", "-r", "--name-only", sha], capture=True).decode().splitlines()
    names = [n for n in names if admitted(n) and any(n == a or n.startswith(a + "/") for a in refs)]
    payload = run(["git", f"--git-dir={HOST_GIT}", "archive", sha, "--", *names], capture=True)
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        for entry in archive:
            if entry.isdir():
                continue
            if not entry.isfile() or not admitted(entry.name):
                raise Refusal("host archive contains an unsupported entry")
            target = destination / entry.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(entry).read())


def base_sandbox():
    return ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/home/dev",
            "--dir", "/tool", "--ro-bind", str(NODE), "/tool/node",
            "--setenv", "PATH", "/tool:/usr/bin:/bin", "--setenv", "HOME", "/home/dev",
            "--setenv", "LANG", "C.UTF-8", "--setenv", "TMPDIR", "/tmp"]


def sandbox(task, argv):
    host = json.loads((task / "host-profile.json").read_text())
    profile_sha(host["profile"])
    binds = []
    # Tests may create private runtime files, but cannot change host core inputs.
    for name in ("modules", "shared", "Plugin.js", "FileFetcherServer.js", "server.js", "package.json", "package-lock.json"):
        if (task / "host" / name).exists():
            binds += ["--ro-bind", str(task / "host" / name), "/work/host/" + name]
    return base_sandbox() + ["--bind", str(task / "host"), "/work/host", *binds,
        "--ro-bind", str(task / "source"), "/source", "--bind", str(task / "state"), "/runstate",
        "--setenv", "CH_DEV_HOST_PROFILE", host["profile"],
        "--setenv", "CH_DEV_SOCKET", "/runstate/backend.sock",
        "--setenv", "CH_DEV_STATE", "/runstate/data", "--setenv", "CH_DEV_READY", "/runstate/ready.json",
        "--chdir", "/work/host", "--", *argv]


def tools_ok():
    if not NODE.is_file() or run([NODE, "--version"], capture=True).strip() != b"v24.22.1":
        raise Refusal("Node 24.22.1 required; install it with (nvm install 24.22.1)")
    if run([NODE, NPM, "--version"], capture=True).strip() != b"10.9.8":
        raise Refusal("npm 10.9.8 required")
    for name in ("bwrap", "git", "zip", "unzip", "cp"):
        if not shutil.which(name, path=ENV["PATH"]):
            raise Refusal(f"missing {name}")
    run(base_sandbox() + ["--", "/tool/node", "-e", "if(process.env.SSH_AUTH_SOCK)process.exit(1)"])


def verify_baseline():
    expected = git("ls-tree", "-r", "--name-only", SOURCE_SHA, "Plugin/ChannelHub").splitlines()
    if len(expected) != 46:
        raise Refusal("unexpected source baseline")
    for name in expected:
        regular(ROOT / name)
        if (ROOT / name).read_bytes() != run(["git", "-C", ROOT, "show", f"{SOURCE_SHA}:{name}"], capture=True):
            raise Refusal("stable plugin content differs from baseline")
    for path in (CACHE, STATE):
        safe_chain(path)
        if path.exists():
            if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                raise Refusal("resource directory ownership or mode mismatch")
            marker = path / "owner.json"
            if marker.exists():
                regular(marker)
                if json.loads(marker.read_text()) != {"schema": SCHEMA, "uid": os.getuid()}:
                    raise Refusal("resource identity mismatch")


def dependency_identity(profile):
    sha = profile_sha(profile)
    lock_data = run(["git", f"--git-dir={HOST_GIT}", "show", sha + ":package-lock.json"], capture=True)
    return {"host": sha, "profile": profile, "node": "24.22.1", "npm": "10.9.8", "lockfile_sha256": digest(lock_data)}


def dependencies(profile=DEFAULT_HOST):
    ensure_host(profile)
    identity = dependency_identity(profile)
    target = CACHE / ("deps-r2-" + identity["host"][:12] + "-" + identity["lockfile_sha256"][:12] + "-node24.22.1-npm10.9.8")
    marker = target / "complete.json"
    if marker.exists():
        regular(marker)
        if (json.loads(marker.read_text()) != identity
                or digest((target / "package-lock.json").read_bytes()) != identity["lockfile_sha256"]):
            raise Refusal("dependency cache identity mismatch")
        return target
    if target.exists():
        raise Refusal("incomplete dependency cache retained; inspect before retry")
    print("Preparing pinned dependencies (networked, isolated install; scripts disabled)", flush=True)
    host_archive(target, profile)
    npm_root = NPM.parents[1]
    cmd = base_sandbox() + ["--share-net", "--ro-bind", str(npm_root), "/tool/npm",
        "--bind", str(target), "/work", "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf",
        "--ro-bind", "/etc/hosts", "/etc/hosts", "--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs",
        "--setenv", "NPM_CONFIG_USERCONFIG", "/tmp/user-npmrc",
        "--setenv", "NPM_CONFIG_GLOBALCONFIG", "/tmp/global-npmrc",
        "--setenv", "NPM_CONFIG_CACHE", "/work/npm-cache", "--chdir", "/work", "--",
        "/tool/node", "/tool/npm/bin/npm-cli.js", "ci", "--ignore-scripts", "--no-audit", "--no-fund",
        "--registry=https://registry.npmjs.org", "--fetch-retries=1"]
    run(cmd, timeout=900)
    if digest((target / "package-lock.json").read_bytes()) != identity["lockfile_sha256"]:
        raise Refusal("dependency install changed pinned lockfile")
    write_json(marker, identity)
    return target


def new_task(kind, *, committed=False, source=None, identity=None, profile=DEFAULT_HOST):
    profile_sha(profile)
    parent = CACHE / "tasks"
    owned_dir(parent)
    task = parent / (kind + "-" + uuid.uuid4().hex)
    task.mkdir(mode=0o700)
    write_json(task / "owner.json", {"schema": SCHEMA, "kind": kind, "uid": os.getuid()})
    if source:
        shutil.copytree(source, task / "source")
    else:
        identity = snapshot(task / "source", committed=committed)
    print(f"TASK {task.name} ({profile})", flush=True)
    deps = dependencies(profile)
    host_archive(task / "host", profile)
    core = {p.relative_to(task / "host").as_posix(): digest(p.read_bytes())
            for p in (task / "host").rglob("*") if p.is_file() and "Plugin" not in p.relative_to(task / "host").parts}
    write_json(task / "host-core.json", core)
    write_json(task / "host-profile.json", {"profile": profile, **dependency_identity(profile)})
    run(["cp", "-a", "--reflink=auto", deps / "node_modules", task / "host/node_modules"])
    if (task / "host/Plugin/ChannelHub").exists():
        shutil.rmtree(task / "host/Plugin/ChannelHub")
    shutil.copytree(task / "source/Plugin/ChannelHub", task / "host/Plugin/ChannelHub")
    shutil.copytree(task / "source/dev", task / "host/dev")
    (task / "host/tests").mkdir(exist_ok=True)
    for p in (task / "source/tests").glob("channelHub*.test.js"):
        shutil.copy2(p, task / "host/tests" / p.name)
    (task / "state").mkdir(mode=0o700)
    write_json(task / "identity.json", identity)
    return task, identity


def remove_task(task):
    if task.parent != CACHE / "tasks" or task.is_symlink():
        raise Refusal("task cleanup path refused")
    regular(task / "owner.json")
    if json.loads((task / "owner.json").read_text()).get("schema") != SCHEMA:
        raise Refusal("task cleanup owner mismatch")
    shutil.rmtree(task)


def fixture_only(task):
    for path in (task / "host/Plugin").iterdir():
        if path.name != "ChannelHub":
            shutil.rmtree(path) if path.is_dir() else path.unlink()


def verify_core(task):
    expected = json.loads((task / "host-core.json").read_text())
    for name, value in expected.items():
        path = task / "host" / name
        if not path.is_file() or path.is_symlink() or digest(path.read_bytes()) != value:
            raise Refusal("host core changed during verification")
    return {"status": "PASS", "files": len(expected), "sha256": digest(json.dumps(expected, sort_keys=True).encode())}


def static_host_evidence(task, profile):
    if profile == "reference":
        return {"mode": "native-auth-module-in-fixture", "full_server_executed": False}
    server = (task / "host/server.js").read_text()
    begin = server.index("// General API authentication")
    end = server.index("// This function", begin)
    excerpt = server[begin:end]
    checks = {"admin_exemption": "req.path.startsWith('/admin_api')" in excerpt,
              "callback_exemption": "req.path.startsWith('/plugin-callback')" in excerpt,
              "bearer_check": "authHeader !== `Bearer ${serverKey}`" in excerpt,
              "service_mount": "await pluginManager.initializeServices(app, adminPanelRoutes, __dirname)" in server}
    if not all(checks.values()) or (task / "host/modules/apiBearerAuth.js").exists():
        raise Refusal("target host differs from fixed static contract")
    return {"mode": "static-only", "full_server_executed": False, "checks": checks,
            "server_sha256": digest((task / "host/server.js").read_bytes()),
            "auth_excerpt_normalized_sha256": digest(excerpt.encode())}


def check_task(task, profile=DEFAULT_HOST):
    verify_core(task)
    commands = [
        ("source-layout", ["/tool/node", "--test", "/source/tests/source-layout.test.cjs"]),
        ("dev-controls", ["/usr/bin/python3", "-B", "/source/dev/test_devctl.py"]),
        ("isolation", ["/tool/node", "-e", "const fs=require('fs'),net=require('net');if(fs.existsSync('/home/jenn')||process.env.SSH_AUTH_SOCK||process.env.OPENAI_API_KEY)process.exit(1);const s=net.connect({host:'1.1.1.1',port:443});s.on('connect',()=>process.exit(1));s.on('error',()=>process.exit(0));setTimeout(()=>process.exit(1),2000)"]),
    ]
    if profile == "reference":
        commands[1:1] = [
            ("channelhub-regression", ["/tool/node", "--test", *["tests/" + p.name for p in sorted((task / "host/tests").glob("channelHub*.test.js"))]]),
            ("onebot-regression", ["/tool/node", "--test", "Plugin/vcp-onebot-adapter/test/onebot-adapter.test.js"])]
    else:
        commands.append(("target-host-compatibility", ["/tool/node", "--test", "dev/host-compat.test.cjs"]))
    results = {}
    for label, command in commands:
        print(f"CHECK {label}", flush=True)
        output = run(sandbox(task, command), timeout=180, capture=True).decode(errors="replace")
        for line in output.splitlines():
            if re.match(r"[ℹ#] (?:tests|pass|fail|skipped) \d+$", line):
                print(line, flush=True)
        results[label] = "PASS"
    fixture_only(task)
    run(sandbox(task, ["/tool/node", "dev/fixture.cjs", "--smoke"]), timeout=45, capture=True)
    ready = json.loads((task / "state/verification.json").read_text())
    if not ready.get("mockVerified"):
        raise Refusal("fixture did not verify mock message")
    if ready.get("hostProfile") != profile or ready.get("authMode") != ("native" if profile == "reference" else "synthetic"):
        raise Refusal("fixture host or auth evidence mismatch")
    results["manager-and-mock"] = ready
    results["host-core"] = verify_core(task)
    results["server-auth-evidence"] = static_host_evidence(task, profile)
    return results


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def relay_allowed(method, target, host, origin, expected):
    if host != expected or (origin is not None and origin != "http://" + expected):
        return False
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        return False
    # Native admin routes only; refuse normalization tricks before forwarding.
    if not target.startswith("/admin_api/channelHub/") or re.search(r"[%\\\s#]", target):
        return False
    return not any(p in {".", ".."} for p in target.split("?", 1)[0].split("/"))


def make_relay(sockpath):
    class Relay(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def forward(self):
            expected = "127.0.0.1:" + str(self.server.server_port)
            if not relay_allowed(self.command, self.path, self.headers.get("Host"), self.headers.get("Origin"), expected):
                self.send_error(403)
                return
            if self.headers.get("Transfer-Encoding"):
                self.send_error(400)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if size < 0 or size > 1024 * 1024:
                self.send_error(413)
                return
            self.connection.settimeout(10)
            body = self.rfile.read(size)
            connection = UnixHTTP(sockpath)
            try:
                connection.request(self.command, self.path, body, {
                    "Host": "localhost", "x-synthetic-admin": "yes",
                    "Content-Type": self.headers.get("Content-Type", "application/json")})
                response = connection.getresponse()
                payload = response.read(4 * 1024 * 1024 + 1)
                if len(payload) > 4 * 1024 * 1024:
                    raise Refusal("oversized response")
                self.send_response(response.status)
                self.send_header("Content-Type", response.getheader("Content-Type", "application/octet-stream"))
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
            except (OSError, http.client.HTTPException, Refusal):
                self.send_error(502)
            finally:
                connection.close()

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = forward
    server = http.server.HTTPServer(("127.0.0.1", 0), Relay)
    server.timeout = 0.2
    return server


def process_identity(pid):
    try:
        proc = Path("/proc") / str(pid)
        if proc.stat().st_uid != os.getuid():
            return None
        return (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def active_record():
    path = STATE / "active.json"
    regular(path)
    record = json.loads(path.read_text())
    if (record.get("schema") != SCHEMA or record.get("uid") != os.getuid()
            or not isinstance(record.get("pid"), int) or record["pid"] <= 1
            or not re.fullmatch(r"[0-9a-f]{32}", record.get("instance", ""))):
        raise Refusal("instance identity refused")
    if process_identity(record["pid"]) != record.get("process_start"):
        raise Refusal("instance is stale or owner cannot be confirmed; preserve for inspection")
    return record


def start(profile=DEFAULT_HOST):
    with lock("instance"):
        if (STATE / "active.json").exists():
            raise Refusal("existing instance record must be resolved first")
        if (STATE / "last-run.json").exists():
            raise Refusal("previous stopped test state retained; run reset before a new start")
        with lock("setup"):
            task, identity = new_task("start", profile=profile)
        fixture_only(task)
        instance = uuid.uuid4().hex
        record = {"schema": SCHEMA, "uid": os.getuid(), "instance": instance,
                  "pid": os.getpid(), "process_start": process_identity(os.getpid()), "task": task.name,
                  "host_profile": profile, "host": profile_sha(profile)}
        write_json(STATE / "active.json", record)
        stopped = False
        relay = None
        process = None
        native_output = None
        previous = {}

        def request_stop(_signum, _frame):
            nonlocal stopped
            stopped = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, request_stop)
        try:
            native_output = (task / "state/native-output.txt").open("xb")
            # Ctrl-C belongs to the supervisor; drain the separate child over its owned UDS.
            process = subprocess.Popen(sandbox(task, ["/tool/node", "dev/fixture.cjs"]), env=ENV,
                                       stdout=native_output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            deadline = time.monotonic() + 30
            while not (task / "state/ready.json").exists():
                if process.poll() is not None or time.monotonic() > deadline or stopped:
                    raise Refusal("fixture exited or did not become ready")
                time.sleep(0.1)
            ready = json.loads((task / "state/ready.json").read_text())
            if not ready.get("mockVerified"):
                raise Refusal("mock verification missing")
            if ready.get("hostProfile") != profile:
                raise Refusal("fixture profile mismatch")
            relay = make_relay(task / "state/backend.sock")
            record.update({"port": relay.server_port, "snapshot": identity})
            write_json(STATE / "active.json", record)
            print(json.dumps({"status": "DEV_ONLY", "host_profile": profile, "host": profile_sha(profile),
                              "auth_mode": ready["authMode"], "url": f"http://127.0.0.1:{relay.server_port}/admin_api/channelHub/ui", "snapshot": identity, "mockVerified": True}), flush=True)
            while not stopped and process.poll() is None:
                relay.handle_request()
            if not stopped:
                raise Refusal("fixture exited unexpectedly; task retained")
        finally:
            if relay:
                relay.server_close()
            graceful = process is None
            if process and process.poll() is None:
                try:
                    connection = UnixHTTP(task / "state/backend.sock")
                    connection.request("POST", "/__dev/shutdown", b"", {"x-synthetic-admin": "yes"})
                    response = connection.getresponse()
                    response.read()
                    connection.close()
                    if response.status != 202:
                        raise Refusal("shutdown refused")
                    graceful = process.wait(timeout=15) == 0
                except (OSError, http.client.HTTPException, subprocess.TimeoutExpired, Refusal):
                    graceful = False
            elif process:
                graceful = process.returncode == 0 and stopped
            if graceful:
                (STATE / "active.json").unlink()
                write_json(STATE / "last-run.json", {"task": task.name, "status": "STOPPED", "instance": instance})
                print("STOPPED; private test state retained until reset", flush=True)
            else:
                print("STOP_FAILED; instance record and task preserved; reset refused", file=sys.stderr)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if native_output:
                native_output.close()
            if not graceful:
                raise Refusal("graceful shutdown not confirmed")


def stop():
    record = active_record()
    # Bind the signal to this process, including if a PID is recycled after inspection.
    fd = os.pidfd_open(record["pid"])
    try:
        if process_identity(record["pid"]) != record["process_start"]:
            raise Refusal("process changed before stop; no signal sent")
        signal.pidfd_send_signal(fd, signal.SIGTERM)
    finally:
        os.close(fd)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if not (STATE / "active.json").exists():
            print("STOPPED")
            return
        if process_identity(record["pid"]) != record["process_start"]:
            break
        time.sleep(0.1)
    raise Refusal("stop not confirmed; preserve instance and refuse reset")


def reset():
    with lock("instance"):
        if (STATE / "active.json").exists():
            raise Refusal("active or unresolved instance; reset refused")
        path = STATE / "last-run.json"
        if not path.exists():
            print("NOTHING_TO_RESET")
            return
        regular(path)
        record = json.loads(path.read_text())
        if record.get("status") != "STOPPED" or not re.fullmatch(r"start-[0-9a-f]{32}", record.get("task", "")):
            raise Refusal("stopped instance record refused")
        remove_task(CACHE / "tasks" / record["task"])
        path.unlink()
        print("RESET; dependency cache and candidates retained")


def validate_zip(path, source):
    expected = {"ChannelHub/" + p.relative_to(source / "Plugin/ChannelHub").as_posix(): digest(p.read_bytes())
                for p in (source / "Plugin/ChannelHub").rglob("*") if p.is_file()}
    if len(expected) != 46:
        raise Refusal("candidate must contain 46 plugin files")
    actual = {}
    with zipfile.ZipFile(path) as archive:
        seen = set()
        for item in archive.infolist():
            name = item.filename
            if name in seen or not admitted(name) or stat.S_ISLNK(item.external_attr >> 16):
                raise Refusal("unsafe or duplicate ZIP entry")
            seen.add(name)
            if not item.is_dir():
                actual[name] = digest(archive.read(item))
    if actual != expected:
        raise Refusal("candidate content mismatch")
    return digest(path.read_bytes())


def require_matrix(matrix, profiles, artifact_hash):
    if set(matrix) != set(profiles):
        raise Refusal("incomplete host verification matrix")
    for profile in profiles:
        row = matrix[profile]
        if (row.get("status") != "PASS" or row.get("host") != profile_sha(profile)
                or row.get("artifact_sha256") != artifact_hash):
            raise Refusal("host matrix failed or refers to a different candidate")


def verify_install(task):
    source = task / "source/Plugin/ChannelHub"
    installed = task / "host/Plugin/ChannelHub"
    expected = {p.relative_to(source).as_posix(): digest(p.read_bytes()) for p in source.rglob("*") if p.is_file()}
    actual = {p.relative_to(installed).as_posix(): digest(p.read_bytes()) for p in installed.rglob("*") if p.is_file()}
    if actual != expected:
        raise Refusal("installed candidate content changed")


def prepare(profile=DEFAULT_HOST):
    profiles = list(HOSTS) if profile == "all" else [profile]
    with lock("setup"):
        task, identity = new_task("prepare", committed=True, profile=profiles[0])
        # Original script resolves its own repo root. It must live in this private export.
        exported = task / "host/candidate-source"
        shutil.copytree(task / "source", exported)
        run(sandbox(task, ["/usr/bin/bash", "/work/host/candidate-source/scripts/package-plugin.sh"]))
        artifact = exported / "dist/ChannelHub-v0.4.1.zip"
        artifact_hash = validate_zip(artifact, task / "source")
        matrix = {name: {"status": "NOT_RUN", "host": profile_sha(name), "artifact_sha256": artifact_hash} for name in profiles}
        tasks = [task]
        try:
            for name in profiles:
                current = task
                if name != profiles[0]:
                    current, _ = new_task("prepare", source=task / "source", identity=identity, profile=name)
                    tasks.append(current)
                installed = current / "host/Plugin/ChannelHub"
                shutil.rmtree(installed)
                with zipfile.ZipFile(artifact) as archive:
                    # Install the same already validated ZIP on every selected host.
                    archive.extractall(current / "host/Plugin")
                print(f"VERIFY CANDIDATE on {name}", flush=True)
                results = check_task(current, name)
                verify_install(current)
                if digest(artifact.read_bytes()) != artifact_hash:
                    raise Refusal("candidate changed during verification")
                matrix[name].update({"status": "PASS", "results": results,
                                     "dependencies": json.loads((current / "host-profile.json").read_text())})
                write_json(task / "matrix-progress.json", matrix)
            require_matrix(matrix, profiles, artifact_hash)
        except Exception as exc:
            # Keep successful rows and failed/not-run rows distinct; never publish a PASS candidate.
            if "name" in locals() and matrix[name]["status"] == "NOT_RUN":
                matrix[name].update({"status": "FAIL", "error_type": type(exc).__name__})
            write_json(task / "matrix-failure.json", matrix)
            print(f"FAILED matrix retained in task {task.name}; no verified candidate created", file=sys.stderr)
            raise
        parent = CACHE / "candidates"
        owned_dir(parent)
        candidate = parent / (identity["commit"][:12] + "-" + artifact_hash[:12] + "-" + uuid.uuid4().hex[:8])
        candidate.mkdir(mode=0o700)
        name = artifact.name
        shutil.copyfile(artifact, candidate / name)
        (candidate / (name + ".sha256")).write_text(artifact_hash + "  " + name + "\n")
        write_json(candidate / "verification.json", {
            "schema": SCHEMA, "source": identity, "baseline": SOURCE_SHA, "host": profile_sha(profiles[0]),
            "node": "24.22.1", "npm": "10.9.8", "artifact": name, "sha256": artifact_hash,
            "results": matrix[profiles[0]]["results"], "verification_matrix": matrix,
            "verified_profiles": profiles, "full_server_executed": False, "live_channel_executed": False,
            "production": False})
        for current in tasks:
            remove_task(current)
        status = ("MULTI_HOST_CANDIDATE_VERIFIED_ONLY" if len(profiles) > 2 else
                  "DUAL_HOST_CANDIDATE_VERIFIED_ONLY" if len(profiles) == 2 else "CANDIDATE_VERIFIED_ONLY")
        print(json.dumps({"status": status,
                          "candidate": str(candidate), "sha256": artifact_hash, "verified_profiles": profiles}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["doctor", "check", "start", "stop", "reset", "prepare"])
    parser.add_argument("--host", choices=[*HOSTS, "all"], default=None,
                        help="fixed host profile; all is only accepted by prepare")
    args = parser.parse_args()
    if args.host is not None and (args.command in {"stop", "reset"} or (args.host == "all" and args.command != "prepare")):
        parser.error("host selection is unavailable for this command")
    profile = args.host or DEFAULT_HOST
    if args.command == "doctor":
        tools_ok()
        verify_baseline()
        print(json.dumps({"status": "PASS", "source_baseline": SOURCE_SHA, "host_profile": profile, "host_fixture": profile_sha(profile),
                          "host_cached": host_is_cached(profile), "source_commit": git("rev-parse", "HEAD"),
                          "node": "24.22.1", "npm": "10.9.8", "isolation": "bwrap", "production": False}))
        return
    if args.command in {"check", "start", "prepare"}:
        tools_ok()
    init_resources()
    if args.command == "check":
        with lock("setup"):
            task, identity = new_task("check", profile=profile)
            results = check_task(task, profile)
            write_json(STATE / "last-check.json", {"snapshot": identity, "profile": profile, "host": profile_sha(profile), "results": results})
            remove_task(task)
            print(f"PASS: {profile} checks, isolation, manager/mock; test task reclaimed")
    else:
        if args.command in {"start", "prepare"}:
            {"start": start, "prepare": prepare}[args.command](profile)
        else:
            {"stop": stop, "reset": reset}[args.command]()


if __name__ == "__main__":
    try:
        main()
    except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        sys.exit(1)
