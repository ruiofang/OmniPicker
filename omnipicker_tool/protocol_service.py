from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProtocolServiceError(RuntimeError):
    pass


@dataclass(slots=True)
class DeviceStatus:
    firmware_version: int | None
    fault_code: int
    state: int
    position: int
    speed: int
    force: int


class SerialProtocolService(Protocol):
    @property
    def is_connected(self) -> bool:
        ...

    def connect(
        self,
        port: str,
        baudrate: int,
        slave_id: int,
        timeout: float,
        stopbits: float,
        retries: int,
    ) -> None:
        ...

    def probe(self) -> int:
        ...

    def disconnect(self) -> None:
        ...

    def write_motion(
        self,
        position: int,
        speed: int,
        force: int,
        acceleration: int,
        deceleration: int,
    ) -> DeviceStatus | None:
        ...

    def read_status(self) -> DeviceStatus:
        ...
