"""
데이터 모델 정의
OctoPrint에서 수집할 데이터의 구조를 정의합니다.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
from datetime import datetime
import json


@dataclass
class PrinterStatus:
    """프린터 상태 정보"""
    state: str  # "idle", "printing", "paused", "error", "connecting", "disconnected"
    timestamp: float
    error_message: Optional[str] = None
    flags: Dict[str, bool] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'state': self.state,
            'timestamp': self.timestamp,
            'error_message': self.error_message,
            'flags': self.flags
        }


@dataclass
class TemperatureData:
    """온도 정보"""
    actual: float
    target: float
    offset: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'actual': self.actual,
            'target': self.target,
            'offset': self.offset
        }


@dataclass
class TemperatureInfo:
    """온도 정보 전체"""
    tool: Dict[str, TemperatureData]
    bed: Optional[TemperatureData] = None
    chamber: Optional[TemperatureData] = None
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'tool': {k: v.to_dict() for k, v in self.tool.items()},
            'bed': self.bed.to_dict() if self.bed else None,
            'chamber': self.chamber.to_dict() if self.chamber else None,
            'timestamp': self.timestamp
        }


@dataclass
class Position:
    """위치 정보"""
    x: float
    y: float
    z: float
    e: float
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'x': self.x,
            'y': self.y,
            'z': self.z,
            'e': self.e,
            'timestamp': self.timestamp
        }


@dataclass
class PrintProgress:
    """프린트 진행률"""
    completion: Optional[float] = None  # 0.0 ~ 1.0
    file_position: Optional[int] = None  # byte
    file_size: Optional[int] = None  # byte
    print_time: Optional[int] = None  # seconds
    print_time_left: Optional[int] = None  # seconds
    filament_used: Optional[float] = None  # mm
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'completion': self.completion,
            'file_position': self.file_position,
            'file_size': self.file_size,
            'print_time': self.print_time,
            'print_time_left': self.print_time_left,
            'filament_used': self.filament_used,
            'timestamp': self.timestamp
        }


@dataclass
class GCodeResponse:
    """G-code 명령 응답 래퍼"""
    command: str
    response: str
    timestamp: float
    success: bool
    error_message: Optional[str] = None


@dataclass
class PrinterInfo:
    """기타 프린터 정보"""
    feedrate: Optional[float] = None  # mm/min
    flowrate: Optional[float] = None  # %
    fan_speed: Optional[int] = None  # 0~255
    connected: bool = False
    printing: bool = False
    idle: bool = False
    paused: bool = False
    error: bool = False
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'feedrate': self.feedrate,
            'flowrate': self.flowrate,
            'fan_speed': self.fan_speed,
            'connected': self.connected,
            'printing': self.printing,
            'idle': self.idle,
            'paused': self.paused,
            'error': self.error,
            'timestamp': self.timestamp
        }


@dataclass
class FirmwareInfo:
    """펌웨어 정보"""
    # 기존 호환 필드
    type: Optional[str] = None  # 호환성 유지: firmware_name과 동일 의미로 사용
    version: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)
    sensor_support: Dict[str, bool] = field(default_factory=dict)
    # 통합 필드(PrinterTypeInfo + FirmwareInfo)
    firmware_name: Optional[str] = None
    firmware_type: Optional[str] = None
    printer_type: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'type': self.type,  # backward-compatible
            'version': self.version,
            'capabilities': self.capabilities,
            'sensor_support': self.sensor_support,
            'firmware_name': self.firmware_name or self.type,
            'firmware_type': self.firmware_type,
            'printer_type': self.printer_type,
        }


@dataclass
class PrinterTypeInfo:
    """프린터/펌웨어 타입 요약 정보"""
    printer_type: str
    firmware_type: str
    firmware_name: str
    firmware_version: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'printer_type': self.printer_type,
            'firmware_type': self.firmware_type,
            'firmware_name': self.firmware_name,
            'firmware_version': self.firmware_version,
        }


@dataclass
class CameraInfo:
    """카메라 정보"""
    stream_url: Optional[str] = None
    snapshot_url: Optional[str] = None
    stream_type: str = "mjpg"  # "mjpg", "hls", "webrtc"
    enabled: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'stream_url': self.stream_url,
            'snapshot_url': self.snapshot_url,
            'stream_type': self.stream_type,
            'enabled': self.enabled
        }


@dataclass
class PrintJob:
    """프린트 작업 정보"""
    file_name: Optional[str] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    file_origin: Optional[str] = None
    estimated_print_time: Optional[int] = None
    user: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'file_name': self.file_name,
            'file_path': self.file_path,
            'file_size': self.file_size,
            'file_origin': self.file_origin,
            'estimated_print_time': self.estimated_print_time,
            'user': self.user
        }


@dataclass
class SystemInfo:
    """시스템 정보"""
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    disk_usage: float = 0.0
    temperature: float = 0.0
    uptime: int = 0
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'cpu_usage': self.cpu_usage,
            'memory_usage': self.memory_usage,
            'disk_usage': self.disk_usage,
            'temperature': self.temperature,
            'uptime': self.uptime,
            'timestamp': self.timestamp
        }


class DataEncoder(json.JSONEncoder):
    """데이터 모델을 JSON으로 인코딩하는 클래스"""
    
    def default(self, obj):
        if hasattr(obj, 'to_dict'):
            return obj.to_dict()
        return super().default(obj) 