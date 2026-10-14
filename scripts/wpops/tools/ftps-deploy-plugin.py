#!/usr/bin/env python3
"""Deploy a WordPress plugin directory to a host reachable only over FTPS
(an Azure App Service managed with its publish credentials).

  ftps-deploy-plugin.py --env /root/.config/wpops/primary.env \
      --src /opt/gitlab/SiteGuard-Backup --plugin siteguardbackup \
      [--exclude .git tests ...] [--backup-dir /path] [--dry-run]

Steps: mirror the current remote plugin into --backup-dir, upload the new tree
to <plugin>.new, swap directories (<plugin> -> <plugin>.prev-<ts>, .new -> <plugin>),
then delete the old copy. Any failure before the swap leaves the live plugin
untouched; a failure after it leaves .prev-<ts> in place to swap back.

The env file holds FTPS_HOST, FTPS_USER, FTPS_PASS (bash-style KEY='value').
"""

from __future__ import annotations

import argparse
import ftplib
import io
import os
import posixpath
import shlex
import ssl
import sys
import time
from pathlib import Path

DEFAULT_EXCLUDES = {".git", ".github", ".gitlab-ci.yml", "tests", "node_modules", "__pycache__", ".DS_Store"}


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = "".join(shlex.split(v.strip())) if v.strip() else ""
    return env


class _IisFtpTls(ftplib.FTP_TLS):
    """IIS (Azure App Service) never sends TLS close_notify on data channels, so
    ftplib's conn.unwrap() blocks until the socket times out. Skip the unwrap."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
            conn.unwrap = lambda: conn  # type: ignore[method-assign]
        return conn, size


class Ftps:
    def __init__(self, host: str, user: str, password: str):
        ctx = ssl.create_default_context()
        self.ftp = _IisFtpTls(context=ctx, timeout=120)
        self.ftp.connect(host, 21)
        self.ftp.login(user, password)
        self.ftp.prot_p()
        self.ftp.set_pasv(True)

    def exists(self, path: str) -> bool:
        parent, name = posixpath.split(path.rstrip("/"))
        try:
            return name in self.ftp.nlst(parent or "/")
        except ftplib.error_perm:
            return False

    def listdir(self, path: str) -> list[tuple[str, bool]]:
        """[(name, is_dir)] via MLSD when available, else LIST parsing (IIS style)."""
        out: list[tuple[str, bool]] = []
        try:
            for name, facts in self.ftp.mlsd(path):
                if name in (".", ".."):
                    continue
                out.append((name, facts.get("type") == "dir"))
            return out
        except (ftplib.error_perm, ftplib.error_proto):
            pass
        lines: list[str] = []
        self.ftp.retrlines(f"LIST {path}", lines.append)
        for line in lines:
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            is_dir = parts[2] == "<DIR>" or line.startswith("d")
            out.append((parts[3], is_dir))
        return out

    def mkdirs(self, path: str) -> None:
        cur = ""
        for part in path.strip("/").split("/"):
            cur += "/" + part
            try:
                self.ftp.mkd(cur)
            except ftplib.error_perm:
                pass

    def download_tree(self, remote: str, local: Path) -> int:
        n = 0
        local.mkdir(parents=True, exist_ok=True)
        for name, is_dir in self.listdir(remote):
            rp = posixpath.join(remote, name)
            lp = local / name
            if is_dir:
                n += self.download_tree(rp, lp)
            else:
                with open(lp, "wb") as fh:
                    self.ftp.retrbinary(f"RETR {rp}", fh.write)
                n += 1
        return n

    def upload_tree(self, local: Path, remote: str, excludes: set[str], log) -> int:
        n = 0
        self.mkdirs(remote)
        for entry in sorted(local.iterdir()):
            if entry.name in excludes:
                continue
            rp = posixpath.join(remote, entry.name)
            if entry.is_dir():
                n += self.upload_tree(entry, rp, excludes, log)
            else:
                with open(entry, "rb") as fh:
                    self.ftp.storbinary(f"STOR {rp}", fh)
                n += 1
                if n % 50 == 0:
                    log(f"  {n} files uploaded")
        return n

    def delete_tree(self, remote: str) -> None:
        for name, is_dir in self.listdir(remote):
            rp = posixpath.join(remote, name)
            if is_dir:
                self.delete_tree(rp)
            else:
                self.ftp.delete(rp)
        self.ftp.rmd(remote)

    def rename(self, src: str, dst: str) -> None:
        self.ftp.rename(src, dst)

    def read(self, remote: str) -> bytes:
        buf = io.BytesIO()
        self.ftp.retrbinary(f"RETR {remote}", buf.write)
        return buf.getvalue()

    def write(self, remote: str, data: bytes) -> None:
        self.ftp.storbinary(f"STOR {remote}", io.BytesIO(data))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--plugin", required=True)
    ap.add_argument("--remote-root", default="/site/wwwroot/wp-content/plugins")
    ap.add_argument("--exclude", nargs="*", default=[])
    ap.add_argument("--backup-dir", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    env = load_env(args.env)
    log = lambda m: print(m, flush=True)  # noqa: E731
    excludes = DEFAULT_EXCLUDES | set(args.exclude)
    src = Path(args.src)
    live = posixpath.join(args.remote_root, args.plugin)
    new = live + ".new"
    prev = live + ".prev-" + time.strftime("%Y%m%d%H%M%S")
    f = Ftps(env["FTPS_HOST"], env["FTPS_USER"], env["FTPS_PASS"])
    log(f"connected to {env['FTPS_HOST']}")
    if args.backup_dir:
        n = f.download_tree(live, Path(args.backup_dir))
        log(f"backed up {n} remote files to {args.backup_dir}")
    if args.dry_run:
        log("dry run: not uploading")
        return 0
    if f.exists(new):
        f.delete_tree(new)
    n = f.upload_tree(src, new, excludes, log)
    log(f"uploaded {n} files to {new}")
    f.rename(live, prev)
    log(f"moved live -> {prev}")
    try:
        f.rename(new, live)
    except Exception as exc:  # noqa: BLE001
        log(f"swap failed ({exc}); restoring")
        f.rename(prev, live)
        return 1
    log(f"activated {live}")
    f.delete_tree(prev)
    log("removed previous copy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
