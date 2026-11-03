"""Docker stacks on this node: compose wrappers, WP-CLI, disk checks, plugin install."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import REPO_DIR, Origin

PLUGIN_SRC = REPO_DIR / "plugin" / "kc-host-alias"


class StackError(Exception):
    pass


def stack_path(origin: Origin, name: str) -> Path:
    try:
        return Path(origin.stacks[name]["path"])
    except KeyError:
        raise StackError(f"stack '{name}' is not defined on node '{origin.name}'") from None


def compose(origin: Origin, name: str, *args: str, timeout: int | None = 3600, check: bool = True,
            capture: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess:
    path = stack_path(origin, name)
    cmd = ["docker", "compose", *args]
    env = dict(os.environ)
    # A parent shell's Compose project must not select another stack's containers.
    for key in list(env):
        if key.startswith('COMPOSE_'):
            env.pop(key)
    env.setdefault("COMPOSE_INTERACTIVE_NO_CLI", "1")
    return subprocess.run(cmd, cwd=path, env=env, text=True, timeout=timeout, check=check,
                          capture_output=capture, input=input_text)


def wp(origin: Origin, name: str, *args: str, timeout: int = 3600, url: str | None = None) -> subprocess.CompletedProcess:
    """Run WP-CLI inside the stack's CLI container (service 'wp-cron' here, 'cli' for instances)."""
    mode = origin.stacks[name].get("cli_service") or "wp-cron"
    running = any(c.get("service") == mode and c.get("state") == "running" for c in ps(origin, name))
    if running:
        cmd = ["exec", "-T", mode, "wp", *args, "--allow-root"]
    else:
        # A standby keeps wp-cron stopped: run WP-CLI in a one-off container of
        # the same service instead, without starting the cron loop.
        cmd = ["run", "--rm", "--no-deps", "-T", "--entrypoint", "wp", mode, *args, "--allow-root"]
    if url:
        cmd.insert(-1, f"--url={url}")
    return compose(origin, name, *cmd, timeout=timeout, check=False)


def ps(origin: Origin, name: str) -> list[dict[str, Any]]:
    import json
    r = compose(origin, name, "ps", "--format", "json", check=False)
    out: list[dict[str, Any]] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list):
            out.extend(obj)
        else:
            out.append(obj)
    return [{"name": c.get("Name"), "service": c.get("Service"), "state": c.get("State"),
             "health": c.get("Health", ""), "status": c.get("Status")} for c in out]


def disk_free(path: Path) -> dict[str, int]:
    st = shutil.disk_usage(path)
    return {"total": st.total, "used": st.used, "free": st.free}


def require_free(path: Path, needed: int, label: str) -> None:
    free = disk_free(path)["free"]
    if free < needed:
        raise StackError(f"not enough disk for {label}: need {needed // 2**20} MB, free {free // 2**20} MB on {path}")


def wp_content(origin: Origin, name: str) -> Path:
    return stack_path(origin, name) / "data" / "wordpress" / "wp-content"


def install_host_alias(origin: Origin, name: str, log=print) -> dict[str, Any]:
    """Copy the host-alias mu-plugin (and sunrise.php for multisite) into the stack."""
    wc = wp_content(origin, name)
    if not wc.exists():
        raise StackError(f"{wc} does not exist (stack not initialised?)")
    mu = wc / "mu-plugins"
    mu.mkdir(exist_ok=True)
    changed = []
    src = PLUGIN_SRC / "kc-host-alias.php"
    dst = mu / "kc-host-alias.php"
    if not dst.exists() or dst.read_bytes() != src.read_bytes():
        shutil.copyfile(src, dst)
        changed.append(str(dst))
    mode = origin.stacks[name].get("mode", "multisite")
    if mode == "multisite":
        src = PLUGIN_SRC / "sunrise.php"
        dst = wc / "sunrise.php"
        if dst.exists() and dst.read_bytes() != src.read_bytes() and b"KC Host Alias" not in dst.read_bytes():
            raise StackError(f"{dst} exists and is not ours; refusing to overwrite (merge by hand)")
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            shutil.copyfile(src, dst)
            changed.append(str(dst))
    for p in changed:
        try:
            os.chmod(p, 0o644)
        except OSError:
            pass
        log(f"installed {p}")
    aliases = wc / "wpops-host-aliases.json"
    if not aliases.exists():
        aliases.write_text('{"generated": 0, "aliases": {}}\n', encoding="utf-8")
        aliases.chmod(0o644)
        changed.append(str(aliases))
    return {"changed": changed, "sunrise_required": mode == "multisite"}


EDGE_NETWORK = "wpops-edge"
# Fixed so Nginx can trust CF-Connecting-IP from the connectors on it
# (config/nginx/conf.d/connector-real-ip.conf). Outside the stacks' own
# 10.214.1-254.x networks.
EDGE_SUBNET = "10.214.255.0/24"


def compose_project(stack_cfg: dict[str, Any]) -> str:
    """Compose project name of a stack: inventory `project`, else its directory name."""
    project = stack_cfg.get("project") or Path(stack_cfg["path"]).name.lower()
    return "".join(ch for ch in project if ch.isalnum() or ch in "_-")


def ingress_service(origin: Origin, name: str, default: str = "http://nginx:80") -> str:
    """Tunnel service URL for a stack.

    The connectors join every stack's ingress network. Each stack's Nginx also
    answers to the plain name "nginx" there, so on a node with more than one
    stack, http://nginx:80 could reach another stack's site. A legacy stack is
    then addressed by its own container name instead.
    """
    st = origin.stacks[name]
    service = st.get("service") or default
    if (len(origin.stacks) > 1 and st.get("ingress_network") != EDGE_NETWORK
            and service.startswith("http://nginx:")):
        service = f"http://{compose_project(st)}-nginx-1:{service.split(':', 2)[2]}"
    return service


def env_flag(path: Path, key: str) -> str:
    for line in (path / ".env").read_text(encoding="utf-8").splitlines() if (path / ".env").exists() else []:
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""
