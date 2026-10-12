from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from time import monotonic
from typing import Any

from omnipicker_tool.protocol_service import DeviceStatus
from omnipicker_tool.protocol_service import ProtocolServiceError
from omnipicker_tool.socketcan_setup import ensure_socketcan_ready


CAN_FRAME_LENGTH = 8


@dataclass(slots=True)
class CanMotionResult:
    statuses: dict[int, DeviceStatus]
    errors: dict[int, str]


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
        node_id = self._validate_node_id(slave_id)
        self.disconnect()
        try:
            import can
        except ImportError as exc:
            raise ProtocolServiceError(
                "缺少 python-can，请执行 pip install -r requirements.txt"
            ) from exc

        ensure_socketcan_ready(port, self._is_fd)
        try:
            bus = can.Bus(
                interface="socketcan", channel=port,
                receive_own_messages=False, fd=self._is_fd,
            )
        except Exception as exc:
            raise ProtocolServiceError(f"打开 SocketCAN 接口 {port} 失败: {exc}") from exc

        self._can = can
        self._bus = bus
        self._node_id = node_id
        self._timeout = max(0.05, float(timeout))
        self._retries = max(0, int(retries))
        self._last_tx = b""
        self._last_rx = b""

    def set_node_id(self, node_id: int) -> None:
        """Change the single-command target without reopening the interface."""
        self._node_id = self._validate_node_id(node_id)

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
        node_id = self._node_id
        result = self.write_motion_many(
            [node_id], position, speed, force, acceleration, deceleration,
        )
        if node_id in result.statuses:
            return result.statuses[node_id]
        raise ProtocolServiceError(result.errors[node_id])

    def write_motion_many(
        self,
        node_ids: list[int],
        position: int,
        speed: int,
        force: int,
        acceleration: int,
        deceleration: int,
    ) -> CanMotionResult:
        commands = {
            self._validate_node_id(node_id):
                (position, speed, force, acceleration, deceleration)
            for node_id in node_ids
        }
        return self.write_motion_commands(commands)

    def write_motion_commands(
        self,
        commands: dict[int, tuple[int, int, int, int, int]],
    ) -> CanMotionResult:
        """Send every target first, then collect their replies in one window.

        Every retry only resends missing targets. Successful replies survive
        timeouts and transport failures affecting other targets in the batch.
        """
        if not commands:
            raise ProtocolServiceError("请至少选择一个 CAN 设备 ID")
        payloads: dict[int, bytes] = {}
        for node_id, motion in commands.items():
            node_id = self._validate_node_id(node_id)
            if len(motion) != 5:
                raise ProtocolServiceError(f"CAN ID=0x{node_id:X} 的运动参数必须为五项")
            try:
                payloads[node_id] = bytes([0, *(self._clamp_u8(v) for v in motion), 0, 0])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ProtocolServiceError(f"CAN ID=0x{node_id:X} 的运动参数无效") from exc

        self._require_bus()
        self._last_tx = b""
        self._last_rx = b""
        result = CanMotionResult(statuses={}, errors={})
        received: deque[str] = deque(maxlen=16)
        try:
            # Drain once per new operation. Draining on a retry could discard
            # an already queued reply to the same operation.
            self._clear_receive_queue()
        except ProtocolServiceError as exc:
            result.errors = dict.fromkeys(payloads, str(exc))
            return result

        for attempt in range(self._retries + 1):
            pending = {node_id: payload for node_id, payload in payloads.items()
                       if node_id not in result.statuses}
            try:
                for node_id, payload in pending.items():
                    self._send(payload, node_id)
                self._receive_statuses(pending, result.statuses, received)
            except ProtocolServiceError as exc:
                result.errors.update({node_id: str(exc) for node_id in pending
                                      if node_id not in result.statuses})
                break
            if len(result.statuses) == len(payloads):
                break
            if attempt == self._retries:
                rx_text = " ".join(received) if received else "<空>"
                result.errors.update({
                    node_id: f"读取 CAN 状态失败: ID=0x{node_id:X} "
                             f"TX={payload.hex(' ')}; RX={rx_text}"
                    for node_id, payload in pending.items() if node_id not in result.statuses
                })
        self._last_rx = " ".join(received).encode()
        return result

    def read_status(self) -> DeviceStatus:
        raise ProtocolServiceError("CAN 协议不支持独立读取状态；请发送运动命令以获取状态回帧")

    def _send(self, payload: bytes, node_id: int) -> None:
        bus, can_module = self._require_bus()
        self._last_tx = payload
        try:
            message = can_module.Message(
                arbitration_id=node_id,
                data=payload,
                is_extended_id=False,
                is_fd=self._is_fd,
                bitrate_switch=self._is_fd,
            )
            bus.send(message, timeout=self._timeout)
        except Exception as exc:
            self._discard_broken_bus(bus)
            if getattr(exc, "errno", None) in {100, 101} or "网络已关闭" in str(exc):
                raise ProtocolServiceError(
                    f"SocketCAN 接口 {self._channel_name(bus)} 已停用，"
                    "请重新连接以自动配置并启用接口"
                ) from exc
            raise ProtocolServiceError(
                f"发送 CAN 控制帧失败，接口已关闭: ID=0x{node_id:X} "
                f"DATA={payload.hex(' ')}; {exc}"
            ) from exc

    def _clear_receive_queue(self) -> None:
        bus, _can_module = self._require_bus()
        deadline = monotonic() + 0.05
        try:
            for _ in range(4096):
                if bus.recv(timeout=0) is None:
                    return
                if monotonic() >= deadline:
                    break
        except Exception as exc:
            self._discard_broken_bus(bus)
            raise ProtocolServiceError(f"清理 CAN 接收缓存失败，接口已关闭: {exc}") from exc
        raise ProtocolServiceError("CAN 接收缓存持续拥塞，请减少总线流量后重试")

    def _receive_statuses(
        self,
        payloads: dict[int, bytes],
        statuses: dict[int, DeviceStatus],
        received: deque[str],
    ) -> None:
        bus, _can_module = self._require_bus()
        deadline = monotonic() + self._timeout
        pending = set(payloads) - statuses.keys()

        try:
            while pending and monotonic() < deadline:
                message = bus.recv(timeout=max(0, deadline - monotonic()))
                if message is None:
                    break
                data = bytes(message.data)
                received.append(f"0x{message.arbitration_id:X}#{data.hex()}")
                node_id = message.arbitration_id
                if (message.is_extended_id or node_id not in pending
                        or getattr(message, "is_error_frame", False)
                        or getattr(message, "is_remote_frame", False)
                        or not getattr(message, "is_rx", True)):
                    continue
                if len(data) != CAN_FRAME_LENGTH or data == payloads[node_id]:
                    continue
                statuses[node_id] = DeviceStatus(
                    firmware_version=None,
                    fault_code=self._clamp_u8(data[0]),
                    state=self._clamp_u8(data[1]),
                    position=self._clamp_u8(data[2]),
                    speed=self._clamp_u8(data[3]),
                    force=self._clamp_u8(data[4]),
                )
                pending.remove(node_id)
        except Exception as exc:
            self._discard_broken_bus(bus)
            raise ProtocolServiceError(f"接收 CAN 状态帧失败，接口已关闭: {exc}") from exc

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

    @staticmethod
    def _validate_node_id(node_id: int) -> int:
        if isinstance(node_id, bool) or not isinstance(node_id, int) or not 0 <= node_id <= 0x7FF:
            raise ProtocolServiceError(f"CAN 设备 ID 必须为 0～2047 的整数，收到: {node_id!r}")
        return node_id

    @staticmethod
    def _channel_name(bus: Any) -> str:
        channel_info = str(getattr(bus, "channel_info", "can0"))
        if "socketcan channel '" in channel_info:
            return channel_info.split("socketcan channel '", 1)[1].split("'", 1)[0]
        return "can0"

    @staticmethod
    def _clamp_u8(value: int) -> int:
        return max(0, min(255, int(value)))
