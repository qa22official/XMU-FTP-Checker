#!/usr/bin/env python3
"""Check FTP subfolders by key matching rules from one JSON config."""

from __future__ import annotations

import argparse
import ftplib
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote


@dataclass
class FtpConfig:
    host: str
    port: int
    username: str
    password: str
    base_path: str


@dataclass
class AppConfig:
    ftp: FtpConfig
    key: str
    paths: list[str]
    timeout: int


def _normalize_list_path(item: str) -> str:
    target = unquote(item.replace("\\", "/")).strip()
    if target == "/":
        return ""
    if target.startswith("/"):
        target = target[1:]
    return target.strip("/")


def safe_join_remote_path(base: str, extra: str | None) -> str:
    if not extra:
        return base

    if extra.startswith("/"):
        return extra

    left = base.rstrip("/")
    right = extra.lstrip("/")
    if not left:
        return f"/{right}"
    return f"{left}/{right}"


def load_app_config(config_path: Path) -> AppConfig:
    raw = config_path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"配置文件为空: {config_path}")

    data = json.loads(raw)
    ftp_raw = data.get("ftp")
    if not isinstance(ftp_raw, dict):
        raise ValueError("配置缺少 ftp 对象")

    host = str(ftp_raw.get("host", "")).strip()
    if not host:
        raise ValueError("ftp.host 不能为空")

    username = str(ftp_raw.get("username", "")).strip()
    password = str(ftp_raw.get("password", "")).strip()
    if not username or not password:
        raise ValueError("ftp.username 和 ftp.password 不能为空")

    port = int(ftp_raw.get("port", 21))
    base_path = _normalize_list_path(str(ftp_raw.get("base_path", "")))

    key = str(data.get("key", "")).strip()
    if not key:
        raise ValueError("key 不能为空")

    raw_paths = data.get("paths")
    if not isinstance(raw_paths, list):
        raise ValueError("paths 必须是数组")

    paths = [_normalize_list_path(str(item)) for item in raw_paths]
    paths = [p for p in paths if p]
    if not paths:
        raise ValueError("paths 中没有有效路径")

    timeout = int(data.get("timeout", 15))
    if timeout <= 0:
        raise ValueError("timeout 必须大于 0")

    return AppConfig(
        ftp=FtpConfig(
            host=host,
            port=port,
            username=username,
            password=password,
            base_path=base_path,
        ),
        key=key,
        paths=paths,
        timeout=timeout,
    )


def _normalize_child_name(raw_name: str) -> str:
    item = raw_name.rstrip("/")
    if "/" in item:
        item = item.split("/")[-1]
    return item.strip()


def _join_remote_path(parent: str, name: str) -> str:
    if parent in ("", "/"):
        return f"/{name}"
    return f"{parent.rstrip('/')}/{name}"


def _list_entries_in_current_dir(ftp: ftplib.FTP) -> list[tuple[str, bool, str]]:
    """Return entries in CWD as (name, is_dir, absolute_path)."""
    cwd = ftp.pwd()

    lines: list[str] = []
    try:
        ftp.retrlines("LIST", lines.append)
    except ftplib.error_perm as exc:
        # Many FTP servers return 550 for empty directories.
        if str(exc).startswith("550"):
            return []
        raise

    parsed_items: list[tuple[str, bool]] = []
    for line in lines:
        row = line.strip()
        if not row:
            continue

        # Typical UNIX LIST format: drwxr-xr-x 1 user group 0 Jan 01 00:00 dirname
        parts = row.split(maxsplit=8)
        if len(parts) >= 9:
            perm = parts[0]
            name = parts[8].strip()
            is_dir = perm.startswith("d")
            parsed_items.append((name, is_dir))
            continue

        # Fallback for non-standard formats: use whole line as name candidate.
        parsed_items.append((row, False))

    entries: list[tuple[str, bool, str]] = []
    seen: set[str] = set()

    for raw_name, hinted_is_dir in parsed_items:
        name = _normalize_child_name(raw_name)
        if name in ("", ".", ".."):
            continue
        if name in seen:
            continue
        seen.add(name)

        is_dir = hinted_is_dir
        abs_path = _join_remote_path(cwd, name)
        if abs_path == cwd:
            continue
        try:
            ftp.cwd(name)
            is_dir = True
            abs_path = ftp.pwd()
            if abs_path == cwd:
                ftp.cwd(cwd)
                continue
        except ftplib.all_errors:
            is_dir = hinted_is_dir and False
        finally:
            ftp.cwd(cwd)

        entries.append((name, is_dir, abs_path))

    entries.sort(key=lambda x: (not x[1], x[0]))
    return entries


def _collect_status_with_fallback(
    ftp: ftplib.FTP,
    remote_path: str,
    key: str,
) -> list[tuple[str, str, int]]:
    # Keep control-channel encoding fixed and only fallback transfer mode.
    modes = [True, False]
    last_error: Exception | None = None

    for is_passive in modes:
        ftp.set_pasv(is_passive)
        visited: set[str] = set()
        try:
            results: list[tuple[str, str, int]] = []
            _scan_subfolders_recursive(ftp, remote_path, key, visited, results)
            return results
        except ftplib.all_errors as exc:
            last_error = exc
            continue
        except UnicodeDecodeError as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error
    raise RuntimeError("无法读取目录")


def _scan_subfolders_recursive(
    ftp: ftplib.FTP,
    remote_path: str,
    key: str,
    visited: set[str],
    results: list[tuple[str, str, int]],
) -> None:
    parent_pwd = ftp.pwd()
    target = remote_path or "."
    ftp.cwd(target)
    current = ftp.pwd()

    if current in visited:
        ftp.cwd(parent_pwd)
        return

    visited.add(current)

    entries = _list_entries_in_current_dir(ftp)
    file_names: list[str] = []
    sub_dirs: list[str] = []

    for name, is_dir, abs_path in entries:
        if is_dir:
            sub_dirs.append(abs_path)
        else:
            file_names.append(name)

    if len(file_names) > 3:
        matched = any(key in name for name in file_names)
        status = "已完成" if matched else "未完成"
        results.append((current, status, len(file_names)))

    for sub_path in sub_dirs:
        _scan_subfolders_recursive(ftp, sub_path, key, visited, results)
        ftp.cwd(current)

    ftp.cwd(parent_pwd)


def run_check_one(ftp: ftplib.FTP, remote_path: str, key: str) -> bool:
    print(f"检查目录: {remote_path or '/(home)'}")
    print("规则检查结果:")
    try:
        results = _collect_status_with_fallback(ftp, remote_path, key)
        if not results:
            print("(无符合条件的子文件夹)\n")
            return True

        for folder_path, status, file_count in results:
            print(f"{folder_path} -> {status} (文件数: {file_count})")
        print("检查完成。\n")
        return True
    except ftplib.all_errors + (UnicodeDecodeError,) as exc:
        print(f"错误: {exc}\n")
        return False


def run_check(config_path: Path, timeout_override: int | None) -> int:
    app_cfg = load_app_config(config_path)
    cfg = app_cfg.ftp
    target_paths = app_cfg.paths
    key = app_cfg.key
    timeout = timeout_override if timeout_override is not None else app_cfg.timeout

    print(f"连接 FTP: {cfg.host}:{cfg.port}")
    print(f"登录用户: {cfg.username}")
    print(f"读取配置: {config_path}")
    print(f"匹配关键字: {key}")
    print(f"待检查目录数: {len(target_paths)}\n")

    ftp = ftplib.FTP(encoding="gbk")
    try:
        ftp.connect(cfg.host, cfg.port, timeout=timeout)
        ftp.login(cfg.username, cfg.password)

        success = 0
        for raw_path in target_paths:
            remote_path = safe_join_remote_path(cfg.base_path, raw_path)
            if run_check_one(ftp, remote_path, key):
                success += 1

        failed = len(target_paths) - success
        print(f"总计: {len(target_paths)}，成功: {success}，失败: {failed}")
        return 0 if failed == 0 else 1
    except ftplib.all_errors as exc:
        print(f"错误: {exc}")
        return 1
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按 JSON 配置规则检查 FTP 子文件夹")
    parser.add_argument(
        "--config",
        default="checker_config.json",
        help="JSON 配置文件路径，默认 checker_config.json",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="连接超时（秒），为空时使用 JSON 配置中的 timeout",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return run_check(Path(args.config), args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
