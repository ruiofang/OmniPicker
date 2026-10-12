"""Prepare one SocketCAN interface without granting persistent privileges.

The same file is an isolated, standard-library-only pkexec helper.  It accepts
only an interface name and one of the two fixed OmniPicker CAN profiles.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


IP_TIMEOUT = 5
AUTH_TIMEOUT = 60
_INTERFACE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}\Z")


class _SetupError(RuntimeError):
    def __init__(self, message: str, *, permission_denied: bool = False) -> None:
        super().__init__(message)
        self.permission_denied = permission_denied


def _executable(name: str) -> str:
    # Do not resolve privileged commands from a user-controlled PATH.
    for directory in ("/usr/sbin", "/usr/bin", "/sbin", "/bin"):
        candidate = f"{directory}/{name}"
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    package = "iproute2" if name == "ip" else "polkit（提供 pkexec）"
    raise _SetupError(f"缺少 {name}，请先安装 {package}")


def _run(arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=IP_TIMEOUT,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise _SetupError(f"配置 SocketCAN 超时（{IP_TIMEOUT} 秒）：{' '.join(arguments)}") from exc
    except OSError as exc:
        raise _SetupError(f"无法执行 SocketCAN 配置命令：{exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:1000] or f"退出码 {result.returncode}"
        denied = "operation not permitted" in detail.lower() or "permission denied" in detail.lower()
        raise _SetupError(f"SocketCAN 命令失败：{detail}", permission_denied=denied)
    return result.stdout


def _read_link(ip: str, interface: str) -> dict[str, Any]:
    output = _run([ip, "-json", "-details", "link", "show", "dev", interface])
    try:
        records = json.loads(output)
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise ValueError("expected one network interface")
        link = records[0]
        if link.get("ifname") != interface:
            raise ValueError("interface name does not match")
        kind = link.get("linkinfo", {}).get("info_kind")
        if kind not in {"can", "vcan"}:
            raise _SetupError(f"接口 {interface} 不是 CAN/vcan 接口，未修改其配置")
        return link
    except (ValueError, TypeError, AttributeError) as exc:
        raise _SetupError(f"无法解析接口 {interface} 的 ip JSON 信息，请检查 iproute2 版本") from exc


def _is_up(link: dict[str, Any]) -> bool:
    # SocketCAN commonly has operstate UNKNOWN while it is working.
    return "UP" in link.get("flags", [])


def _is_fd(link: dict[str, Any]) -> bool:
    data = link.get("linkinfo", {}).get("info_data", {})
    return "FD" in data.get("ctrlmode", [])


def _sample_point_matches(timing: dict[str, Any], minimum: float, requested: float) -> bool:
    try:
        actual = float(timing["sample_point"])
    except (KeyError, ValueError, TypeError):
        return False
    return minimum - 1e-6 <= actual <= requested + 1e-6


def _ready(link: dict[str, Any], is_fd: bool) -> bool:
    if not _is_up(link):
        return False
    if link["linkinfo"]["info_kind"] == "vcan":
        return True
    data = link["linkinfo"].get("info_data", {})
    timing = data.get("bittiming", {})
    if timing.get("bitrate") != 1_000_000 or not _sample_point_matches(timing, 0.75, 0.8):
        return False
    if _is_fd(link) != is_fd or data.get("restart_ms") != 100:
        return False
    if is_fd:
        data_timing = data.get("data_bittiming", {})
        if data_timing.get("bitrate") != 5_000_000 or not _sample_point_matches(data_timing, 0.70, 0.75):
            return False
    # Drivers choose the closest representable sample point.  For example an
    # 8 MHz classic controller reports 0.75 for a 1 Mbit/s, 0.8 request. Permit
    # up to five percentage points of downward quantization, not arbitrary
    # existing timings such as 0.5.
    return data.get("state") != "BUS-OFF"


def _profile(is_fd: bool, was_fd: bool) -> list[str]:
    arguments = ["type", "can", "bitrate", "1000000", "sample-point", "0.8"]
    if is_fd:
        arguments += ["dbitrate", "5000000", "dsample-point", "0.75", "fd", "on"]
    elif was_fd:
        arguments += ["fd", "off"]
    # Avoid fd off on classic-only adapters: some drivers reject any FD flag.
    return arguments + ["restart-ms", "100"]


def _restore(ip: str, interface: str, original: dict[str, Any], requested_fd: bool) -> None:
    prefix = [ip, "link", "set", "dev", interface]
    try:
        current_fd = _is_fd(_read_link(ip, interface))
    except _SetupError:
        current_fd = requested_fd
    _run(prefix + ["down"])
    if original["linkinfo"]["info_kind"] == "can":
        data = original["linkinfo"].get("info_data", {})
        timing = data.get("bittiming", {})
        arguments = ["type", "can"]
        if timing.get("bitrate", 0) > 0:
            arguments += ["bitrate", str(int(timing["bitrate"]))]
            if timing.get("sample_point") is not None:
                arguments += ["sample-point", str(timing["sample_point"])]
        was_fd = _is_fd(original)
        if was_fd:
            data_timing = data.get("data_bittiming", {})
            if data_timing.get("bitrate", 0) > 0:
                arguments += ["dbitrate", str(int(data_timing["bitrate"]))]
                if data_timing.get("sample_point") is not None:
                    arguments += ["dsample-point", str(data_timing["sample_point"])]
        if was_fd or current_fd:
            arguments += ["fd", "on" if was_fd else "off"]
        if "restart_ms" in data:
            arguments += ["restart-ms", str(int(data["restart_ms"]))]
        if len(arguments) > 2:
            _run(prefix + arguments)
    if _is_up(original):
        _run(prefix + ["up"])


def _configure(ip: str, interface: str, is_fd: bool, original: dict[str, Any]) -> None:
    prefix = [ip, "link", "set", "dev", interface]
    changed = False
    try:
        if original["linkinfo"]["info_kind"] == "can":
            if _is_up(original):
                _run(prefix + ["down"])
                changed = True
            # A command may time out after the kernel accepted it, so recovery
            # is needed even when this particular call does not return success.
            changed = True
            _run(prefix + _profile(is_fd, _is_fd(original)))
        changed = True
        _run(prefix + ["up"])
        if not _ready(_read_link(ip, interface), is_fd):
            raise _SetupError(f"接口 {interface} 配置后仍未就绪，请检查驱动、CAN 模式和波特率")
    except _SetupError as exc:
        recovery = ""
        if changed:
            try:
                _restore(ip, interface, original, is_fd)
                recovery = "；已尝试恢复连接前的接口配置"
            except _SetupError as restore_exc:
                recovery = f"；恢复原接口配置失败：{restore_exc}，请检查接口状态"
        raise _SetupError(str(exc) + recovery, permission_denied=exc.permission_denied) from exc


def _prepare(interface: str, is_fd: bool) -> None:
    if not _INTERFACE_NAME.fullmatch(interface):
        raise _SetupError("SocketCAN 接口名无效，应为 can0、can1 或 vcan0 等 Linux 接口名")
    ip = _executable("ip")
    original = _read_link(ip, interface)
    if not _ready(original, is_fd):
        _configure(ip, interface, is_fd, original)


def _authorize(interface: str, is_fd: bool) -> None:
    pkexec = _executable("pkexec")
    python = "/usr/bin/python3"
    if not os.path.isfile(python) or not os.access(python, os.X_OK):
        raise _SetupError("自动授权配置需要 /usr/bin/python3，请安装系统 Python 3")
    command = [pkexec, python, "-I", str(Path(__file__).resolve()), "--configure", interface, "fd" if is_fd else "classic"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=AUTH_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise _SetupError(f"SocketCAN 系统授权超时（{AUTH_TIMEOUT} 秒），请重新连接并完成授权") from exc
    except OSError as exc:
        raise _SetupError(f"无法启动 SocketCAN 系统授权：{exc}") from exc
    detail = (result.stderr or result.stdout).strip()[:1600]
    if result.returncode == 126:
        raise _SetupError("已取消 SocketCAN 管理员授权，接口未完成自动配置")
    if result.returncode == 127:
        raise _SetupError(f"SocketCAN 管理员授权失败，请检查桌面 polkit 认证代理：{detail or '未获得授权'}")
    if result.returncode:
        raise _SetupError(f"SocketCAN 自动配置失败：{detail or f'退出码 {result.returncode}'}")


def ensure_socketcan_ready(interface: str, is_fd: bool) -> None:
    """Set up a CAN interface, requesting desktop authorization only if needed."""
    from omnipicker_tool.protocol_service import ProtocolServiceError

    try:
        try:
            _prepare(interface, is_fd)
        except _SetupError as exc:
            if not exc.permission_denied:
                raise
            _authorize(interface, is_fd)
    except _SetupError as exc:
        raise ProtocolServiceError(str(exc)) from exc


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--configure" or sys.argv[3] not in {"classic", "fd"}:
        print("仅支持 --configure <接口名> <classic|fd>", file=sys.stderr)
        raise SystemExit(2)
    try:
        _prepare(sys.argv[2], sys.argv[3] == "fd")
    except _SetupError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
