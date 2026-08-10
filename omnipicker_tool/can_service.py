from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import Any

from omnipicker_tool.protocol_service import DeviceStatus
from omnipicker_tool.protocol_service import ProtocolServiceError


CAN_FRAME_LENGTH = 8


class OmniPickerCanService:
    """OmniPicker CAN/CAN-FD transport over Linux SocketCAN."""

    def __init__(self, is_fd: bool = False) -> None:
        self._bus: Any | None = None
        self._can: Any | None = None
        self._node_id = 1
        self._timeout = 0.3
        self._retries = 0
        self._is_fd = is_fd
        self._last_tx = b""
        self._last_rx = b""

    @property
    def is_connected(self) -> bool:
        return self._bus is not None

    @property
    def is_fd(self) -> bool:
        return self._is_fd

    def connect(
        self,
        port: str,
        baudrate: int,
        slave_id: int,
        timeout: float,
        stopbits: float,
        retries: int,
    ) -> None:
        del baudrate, stopbits
        self.disconnect()
        try:
            import can
        except ImportError as exc:
            raise ProtocolServiceError(
                "缺少 python-can，请执行 pip install -r requirements.txt"
            ) from exc

        try:
            bus = can.Bus(interface="socketcan", channel=port, receive_own_messages=False)
        except Exception as exc:
            raise ProtocolServiceError(f"打开 SocketCAN 接口 {port} 失败: {exc}") from exc

        if not self._interface_is_up(port):
            try:
                bus.shutdown()
            except Exception:
                pass
            raise ProtocolServiceError(self._interface_down_message(port))

        self._can = can
        self._bus = bus
        self._node_id = max(0, min(0x7FF, int(slave_id)))
        self._timeout = max(0.05, float(timeout))
        self._retries = max(0, int(retries))
        self._last_tx = b""
        self._last_rx = b""

    def disconnect(self) -> None:
        bus = self._bus
        self._bus = None
        self._can = None
        if bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass

    def probe(self) -> int:
        status = self.write_motion(127, 255, 32, 255, 255)
        return status.fault_code

    def write_motion(
        self,
        position: int,
        speed: int,
        force: int,
        acceleration: int,
        deceleration: int,
    ) -> DeviceStatus:
        payload = bytes(
            [
                0,
                self._clamp_u8(position),
                self._clamp_u8(speed),
                self._clamp_u8(force),
                self._clamp_u8(acceleration),
                self._clamp_u8(deceleration),
                0,
                0,
            ]
        )
        self._last_tx = payload

        last_error: ProtocolServiceError | None = None
        for _attempt in range(self._retries + 1):
            self._send(payload)
            try:
                return self._receive_status(payload)
            except ProtocolServiceError as exc:
                last_error = exc

        assert last_error is not None
        raise last_error

    def read_status(self) -> DeviceStatus:
        raise ProtocolServiceError("CAN 协议不支持独立读取状态；请发送运动命令以获取状态回帧")

    def _send(self, payload: bytes) -> None:
        bus, can_module = self._require_bus()
        try:
            message = can_module.Message(
                arbitration_id=self._node_id,
                data=payload,
                is_extended_id=False,
                is_fd=self._is_fd,
                bitrate_switch=self._is_fd,
            )
            bus.send(message, timeout=self._timeout)
        except Exception as exc:
            self._discard_broken_bus(bus)
            if getattr(exc, "errno", None) in {100, 101} or "网络已关闭" in str(exc):
                raise ProtocolServiceError(self._interface_down_message(self._channel_name(bus))) from exc
            raise ProtocolServiceError(
                f"发送 CAN 控制帧失败，接口已关闭: ID=0x{self._node_id:X} "
                f"DATA={payload.hex(' ')}; {exc}"
            ) from exc

    def _receive_status(self, transmitted_payload: bytes) -> DeviceStatus:
        bus, _can_module = self._require_bus()
        deadline = monotonic() + self._timeout
        received: list[str] = []

        try:
            while monotonic() < deadline:
                message = bus.recv(timeout=max(0, deadline - monotonic()))
                if message is None:
                    break
                data = bytes(message.data)
                received.append(f"0x{message.arbitration_id:X}#{data.hex()}")
                if message.is_extended_id or message.arbitration_id != self._node_id:
                    continue
                if len(data) != CAN_FRAME_LENGTH or data == transmitted_payload:
                    continue
                return DeviceStatus(
                    firmware_version=None,
                    fault_code=self._clamp_u8(data[0]),
                    state=self._clamp_u8(data[1]),
                    position=self._clamp_u8(data[2]),
                    speed=self._clamp_u8(data[3]),
                    force=self._clamp_u8(data[4]),
                )
        except Exception as exc:
            self._discard_broken_bus(bus)
            raise ProtocolServiceError(f"接收 CAN 状态帧失败，接口已关闭: {exc}") from exc

        self._last_rx = " ".join(received).encode()
        rx_text = " ".join(received) if received else "<空>"
        raise ProtocolServiceError(
            f"读取 CAN 状态失败: ID=0x{self._node_id:X} "
            f"TX={self._last_tx.hex(' ')}; RX={rx_text}"
        )

    def _require_bus(self) -> tuple[Any, Any]:
        if self._bus is None or self._can is None:
            raise ProtocolServiceError("CAN 接口尚未连接")
        return self._bus, self._can

    def _discard_broken_bus(self, bus: Any) -> None:
        if self._bus is bus:
            self._bus = None
            self._can = None
        try:
            bus.shutdown()
        except Exception:
            pass

    def _interface_down_message(self, interface: str) -> str:
        if self._is_fd:
            setup = (
                f"sudo ip link set {interface} down\n"
                f"sudo ip link set {interface} type can bitrate 1000000 sample-point 0.8 "
                "dbitrate 5000000 dsample-point 0.75 fd on restart-ms 100\n"
                f"sudo ip link set {interface} up"
            )
        else:
            setup = (
                f"sudo ip link set {interface} down\n"
                f"sudo ip link set {interface} type can bitrate 1000000 sample-point 0.8 restart-ms 100\n"
                f"sudo ip link set {interface} up"
            )
        return f"SocketCAN 接口 {interface} 尚未启用，请先执行：\n{setup}"

    @staticmethod
    def _interface_is_up(interface: str) -> bool:
        try:
            flags = int(Path(f"/sys/class/net/{interface}/flags").read_text().strip(), 0)
        except (OSError, ValueError):
            # Let SocketCAN report the error when sysfs is unavailable.
            return True
        return bool(flags & 0x1)  # IFF_UP

    @staticmethod
    def _channel_name(bus: Any) -> str:
        channel_info = str(getattr(bus, "channel_info", "can0"))
        if "socketcan channel '" in channel_info:
            return channel_info.split("socketcan channel '", 1)[1].split("'", 1)[0]
        return "can0"

    @staticmethod
    def _clamp_u8(value: int) -> int:
        return max(0, min(255, int(value)))
