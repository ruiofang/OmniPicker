from __future__ import annotations

from pymodbus.client import ModbusSerialClient

from omnipicker_tool.constants import DEFAULT_TIMEOUT_SECONDS
from omnipicker_tool.constants import REGISTER_CURRENT_FORCE
from omnipicker_tool.constants import REGISTER_FAULT_CODE
from omnipicker_tool.constants import REGISTER_FIRMWARE_VERSION
from omnipicker_tool.constants import REGISTER_TARGET_POSITION
from omnipicker_tool.protocol_service import DeviceStatus
from omnipicker_tool.protocol_service import ProtocolServiceError


ModbusServiceError = ProtocolServiceError


class OmniPickerModbusService:
    def __init__(self) -> None:
        self._client: ModbusSerialClient | None = None
        self._slave_id = 1

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.connected

    def connect(
        self,
        port: str,
        baudrate: int,
        slave_id: int,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        stopbits: float = 1,
        retries: int = 0,
    ) -> None:
        self.disconnect()
        client = ModbusSerialClient(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity="N",
            stopbits=stopbits,
            timeout=timeout,
            retries=retries,
        )
        try:
            if not client.connect():
                raise ModbusServiceError(f"无法连接串口 {port}")
        except Exception as exc:
            client.close()
            raise ModbusServiceError(f"串口初始化失败: {exc}") from exc
        self._client = client
        self._slave_id = slave_id

    def probe(self) -> int:
        client = self._require_client()
        try:
            response = client.read_holding_registers(
                address=REGISTER_FAULT_CODE,
                count=1,
                device_id=self._slave_id,
            )
        except Exception as exc:
            self._discard_broken_client(client)
            raise ModbusServiceError(f"探测设备失败: {exc}") from exc
        if response.isError():
            raise ModbusServiceError(f"探测设备失败: {response}")
        return self._clamp_u8(response.registers[0])

    def disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def write_motion(
        self,
        position: int,
        speed: int,
        force: int,
        acceleration: int,
        deceleration: int,
    ) -> None:
        client = self._require_client()
        values = [
            self._clamp_u8(position),
            self._clamp_u8(speed),
            self._clamp_u8(force),
            self._clamp_u8(acceleration),
            self._clamp_u8(deceleration),
            1,
        ]
        try:
            response = client.write_registers(
                address=REGISTER_TARGET_POSITION,
                values=values,
                device_id=self._slave_id,
            )
        except Exception as exc:
            self._discard_broken_client(client)
            raise ModbusServiceError(f"写入运动寄存器失败: {exc}") from exc
        if response.isError():
            raise ModbusServiceError(f"写入运动寄存器失败: {response}")

    def read_status(self) -> DeviceStatus:
        client = self._require_client()
        try:
            firmware_response = client.read_holding_registers(
                address=REGISTER_FIRMWARE_VERSION,
                count=1,
                device_id=self._slave_id,
            )
        except Exception as exc:
            self._discard_broken_client(client)
            raise ModbusServiceError(f"读取固件版本失败: {exc}") from exc
        firmware_version = None
        if not firmware_response.isError():
            firmware_version = firmware_response.registers[0]

        try:
            status_response = client.read_holding_registers(
                address=REGISTER_FAULT_CODE,
                count=REGISTER_CURRENT_FORCE - REGISTER_FAULT_CODE + 1,
                device_id=self._slave_id,
            )
        except Exception as exc:
            self._discard_broken_client(client)
            raise ModbusServiceError(f"读取状态寄存器失败: {exc}") from exc
        if status_response.isError():
            raise ModbusServiceError(f"读取状态寄存器失败: {status_response}")

        fault_code, state, position, speed, force = [self._clamp_u8(value) for value in status_response.registers]
        return DeviceStatus(
            firmware_version=firmware_version,
            fault_code=fault_code,
            state=state,
            position=position,
            speed=speed,
            force=force,
        )

    def _require_client(self) -> ModbusSerialClient:
        if self._client is None:
            raise ModbusServiceError("设备尚未连接")
        return self._client

    def _discard_broken_client(self, client: ModbusSerialClient) -> None:
        if self._client is client:
            self._client = None
        try:
            client.close()
        except Exception:
            pass

    @staticmethod
    def _clamp_u8(value: int) -> int:
        return max(0, min(255, int(value)))
