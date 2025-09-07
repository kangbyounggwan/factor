"""
3D 프린터 타입별 특화 기능
FDM, SLA, 펠릿, 멀티헤드 등 다양한 프린터 지원
"""

import re
import time
from typing import Dict, Any, Optional, List, Tuple
from enum import Enum
from dataclasses import dataclass
from abc import ABC, abstractmethod

from .data_models import *
from .logger import get_logger


class PrinterType(Enum):
    """프린터 타입"""
    FDM = "fdm"              # 필라멘트 기반
    SLA = "sla"              # 레진 기반
    PELLET = "pellet"        # 펠릿 기반
    MULTI_HEAD = "multi_head" # 멀티 헤드
    BELT = "belt"            # 벨트 프린터
    DELTA = "delta"          # 델타 프린터
    POLAR = "polar"          # 폴라 프린터
    UNKNOWN = "unknown"


class FirmwareType(Enum):
    """펌웨어 타입"""
    MARLIN = "marlin"
    KLIPPER = "klipper"
    REPRAP = "reprapfirmware"
    SMOOTHIEWARE = "smoothieware"
    GRBL = "grbl"
    REPETIER = "repetier"
    CHITUBOX = "chitubox"    # SLA 전용
    ELEGOO = "elegoo"        # SLA 전용
    ANYCUBIC = "anycubic"    # SLA 전용
    UNKNOWN = "unknown"


@dataclass
class PrinterCapabilities:
    """프린터 기능"""
    has_heated_bed: bool = False
    has_heated_chamber: bool = False
    has_auto_leveling: bool = False
    has_filament_sensor: bool = False
    has_power_recovery: bool = False
    has_multi_extruder: bool = False
    has_mixing_extruder: bool = False
    has_uv_led: bool = False           # SLA 전용
    has_resin_tank: bool = False       # SLA 전용
    has_fep_sensor: bool = False       # SLA 전용
    max_extruders: int = 1
    max_temp_hotend: int = 300
    max_temp_bed: int = 120
    max_temp_chamber: int = 80
    build_volume: Tuple[float, float, float] = (200, 200, 200)
    layer_height_min: float = 0.1      # mm
    layer_height_max: float = 0.4      # mm
    uv_power_max: int = 100            # SLA UV 파워 (%)


class PrinterDetector:
    """프린터 타입 자동 감지"""
    
    def __init__(self):
        self.logger = get_logger('printer-detector')
        
        # 펌웨어 감지 패턴
        self.firmware_patterns = {
            FirmwareType.MARLIN: [
                r'FIRMWARE_NAME:Marlin',
                r'Marlin \d+\.\d+\.\d+',
                r'echo:Marlin'
            ],
            FirmwareType.KLIPPER: [
                r'Klipper',
                r'// Klipper',
                r'printer.cfg'
            ],
            FirmwareType.REPRAP: [
                r'RepRapFirmware',
                r'FIRMWARE_NAME:RepRapFirmware',
                r'Duet'
            ],
            FirmwareType.SMOOTHIEWARE: [
                r'Smoothieware',
                r'FIRMWARE_NAME:Smoothieware'
            ],
            FirmwareType.REPETIER: [
                r'Repetier',
                r'FIRMWARE_NAME:Repetier'
            ],
            FirmwareType.CHITUBOX: [
                r'ChiTuBox',
                r'CTB-'
            ],
            FirmwareType.ELEGOO: [
                r'Elegoo',
                r'Mars',
                r'Saturn'
            ],
            FirmwareType.ANYCUBIC: [
                r'Anycubic',
                r'Photon'
            ]
        }
        
        # 프린터 타입 감지 키워드
        self.printer_type_keywords = {
            PrinterType.SLA: [
                'resin', 'uv', 'photon', 'mars', 'saturn', 'elegoo',
                'anycubic', 'sla', 'lcd', 'msla', 'fep'
            ],
            PrinterType.DELTA: [
                'delta', 'kossel', 'rostock', 'flsun'
            ],
            PrinterType.BELT: [
                'belt', 'conveyor', 'blackbelt'
            ],
            PrinterType.POLAR: [
                'polar', 'theta'
            ]
        }
    
    def detect_firmware(self, response_lines: List[str]) -> FirmwareType:
        """펌웨어 타입 감지"""
        combined_text = ' '.join(response_lines).lower()
        
        for firmware_type, patterns in self.firmware_patterns.items():
            for pattern in patterns:
                if re.search(pattern, combined_text, re.IGNORECASE):
                    self.logger.info(f"펌웨어 감지: {firmware_type.value}")
                    return firmware_type
        
        return FirmwareType.UNKNOWN
    
    def detect_printer_type(self, firmware_info: str, capabilities: List[str]) -> PrinterType:
        """프린터 타입 감지"""
        combined_text = (firmware_info + ' ' + ' '.join(capabilities)).lower()
        
        for printer_type, keywords in self.printer_type_keywords.items():
            for keyword in keywords:
                if keyword in combined_text:
                    self.logger.info(f"프린터 타입 감지: {printer_type.value}")
                    return printer_type
        
        # 기본값은 FDM
        return PrinterType.FDM
    
    def detect_capabilities(self, firmware_type: FirmwareType, config_lines: List[str]) -> PrinterCapabilities:
        """프린터 기능 감지"""
        capabilities = PrinterCapabilities()
        combined_text = ' '.join(config_lines).lower()
        
        # 공통 기능 감지
        if 'heated_bed' in combined_text or 'temp_bed' in combined_text:
            capabilities.has_heated_bed = True
        
        if 'heated_chamber' in combined_text or 'temp_chamber' in combined_text:
            capabilities.has_heated_chamber = True
        
        if 'auto_bed_leveling' in combined_text or 'abl' in combined_text:
            capabilities.has_auto_leveling = True
        
        if 'filament_sensor' in combined_text or 'runout' in combined_text:
            capabilities.has_filament_sensor = True
        
        if 'power_loss' in combined_text or 'power_recovery' in combined_text:
            capabilities.has_power_recovery = True
        
        # 멀티 익스트루더 감지
        extruder_count = len(re.findall(r'extruder\d+', combined_text))
        if extruder_count > 1:
            capabilities.has_multi_extruder = True
            capabilities.max_extruders = extruder_count
        
        # SLA 전용 기능
        if 'uv_led' in combined_text or 'resin' in combined_text:
            capabilities.has_uv_led = True
            capabilities.has_resin_tank = True
        
        if 'fep' in combined_text:
            capabilities.has_fep_sensor = True
        
        # 펌웨어별 특화 기능
        if firmware_type == FirmwareType.KLIPPER:
            capabilities.has_auto_leveling = True  # Klipper는 대부분 지원
        
        return capabilities


class BasePrinterHandler(ABC):
    """프린터 핸들러 기본 클래스"""
    
    def __init__(self, printer_type: PrinterType, firmware_type: FirmwareType):
        self.printer_type = printer_type
        self.firmware_type = firmware_type
        self.logger = get_logger(f'printer-{printer_type.value}-{firmware_type.value}')
        self.capabilities = PrinterCapabilities()
    
    @abstractmethod
    def get_status_commands(self) -> List[str]:
        """상태 확인 명령어 목록"""
        pass
    
    @abstractmethod
    def parse_temperature(self, line: str) -> Optional[TemperatureInfo]:
        """온도 정보 파싱"""
        pass
    
    @abstractmethod
    def parse_position(self, line: str) -> Optional[Position]:
        """위치 정보 파싱"""
        pass
    
    @abstractmethod
    def get_initialization_commands(self) -> List[str]:
        """초기화 명령어 목록"""
        pass


class FDMMarlinHandler(BasePrinterHandler):
    """FDM Marlin 핸들러"""
    
    def __init__(self):
        super().__init__(PrinterType.FDM, FirmwareType.MARLIN)
    
    def get_status_commands(self) -> List[str]:
        return [
            "M105",  # 온도 정보
            "M114",  # 위치 정보
            "M119",  # 엔드스톱 상태
            "M27",   # SD 카드 진행률
        ]
    
    def parse_temperature(self, line: str) -> Optional[TemperatureInfo]:
        # 기존 온도 파싱 로직
        pattern = re.compile(r'([TBCP])(\d*):(-?\d+\.?\d*)\s*/(-?\d+\.?\d*)')
        matches = pattern.findall(line)
        
        if not matches:
            return None
        
        tools = {}
        bed = None
        chamber = None
        
        for match in matches:
            sensor_type, sensor_num, actual, target = match
            actual = float(actual)
            target = float(target)
            
            temp_data = TemperatureData(actual=actual, target=target)
            
            if sensor_type == 'T':
                tool_name = f"tool{sensor_num}" if sensor_num else "tool0"
                tools[tool_name] = temp_data
            elif sensor_type == 'B':
                bed = temp_data
            elif sensor_type == 'C':
                chamber = temp_data
        
        return TemperatureInfo(tool=tools, bed=bed, chamber=chamber)
    
    def parse_position(self, line: str) -> Optional[Position]:
        # 위치 파싱은 core/core_collection.py의 단일 로직을 사용하도록 이 구현은 제거합니다.
        return None
    
    def get_initialization_commands(self) -> List[str]:
        return [
            "M115",  # 펌웨어 정보
            "M503",  # 설정 정보
            "M105",  # 온도 정보
            "M114",  # 위치 정보
            "M119",  # 엔드스톱 상태
        ]


class SLAHandler(BasePrinterHandler):
    """SLA 프린터 핸들러"""
    
    def __init__(self, firmware_type: FirmwareType = FirmwareType.MARLIN):
        super().__init__(PrinterType.SLA, firmware_type)
    
    def get_status_commands(self) -> List[str]:
        return [
            "M105",    # 온도 정보 (레진 가열)
            "M114",    # Z축 위치
            "M6054",   # 레진 탱크 정보
            "M6055",   # FEP 필름 상태
            "M6056",   # 레진 레벨
        ]
    
    def parse_temperature(self, line: str) -> Optional[TemperatureInfo]:
        # SLA는 주로 레진 가열만 있음
        if 'T:' in line:
            match = re.search(r'T:(-?\d+\.?\d*)\s*/(-?\d+\.?\d*)', line)
            if match:
                actual, target = match.groups()
                resin_temp = TemperatureData(actual=float(actual), target=float(target))
                return TemperatureInfo(tool={'resin': resin_temp})
        
        return None
    
    def parse_position(self, line: str) -> Optional[Position]:
        # SLA는 주로 Z축만 중요
        if 'Z:' in line:
            match = re.search(r'Z:(-?\d+\.?\d*)', line)
            if match:
                z_pos = float(match.group(1))
                return Position(x=0, y=0, z=z_pos, e=0)
        
        return None
    
    def get_initialization_commands(self) -> List[str]:
        return [
            "M115",    # 펌웨어 정보
            "M105",    # 온도 정보
            "M114",    # 위치 정보
            "M6054",   # 레진 탱크 정보
        ]


class KlipperHandler(BasePrinterHandler):
    """Klipper 펌웨어 핸들러"""
    
    def __init__(self, printer_type: PrinterType = PrinterType.FDM):
        super().__init__(printer_type, FirmwareType.KLIPPER)
    
    def get_status_commands(self) -> List[str]:
        return [
            "STATUS",           # 전체 상태
            "GET_POSITION",     # 정확한 위치
            "TEMPERATURE_WAIT", # 온도 대기 상태
        ]
    
    def parse_temperature(self, line: str) -> Optional[TemperatureInfo]:
        # Klipper는 보통 더 상세한 온도 정보 제공
        if 'extruder' in line.lower() or 'heater_bed' in line.lower():
            # Klipper 스타일 파싱
            tools = {}
            bed = None
            
            # 예: "extruder: target=200.0 temp=195.5"
            extruder_match = re.search(r'extruder:\s*target=(-?\d+\.?\d*)\s*temp=(-?\d+\.?\d*)', line)
            if extruder_match:
                target, actual = extruder_match.groups()
                tools['tool0'] = TemperatureData(actual=float(actual), target=float(target))
            
            # 예: "heater_bed: target=60.0 temp=58.2"
            bed_match = re.search(r'heater_bed:\s*target=(-?\d+\.?\d*)\s*temp=(-?\d+\.?\d*)', line)
            if bed_match:
                target, actual = bed_match.groups()
                bed = TemperatureData(actual=float(actual), target=float(target))
            
            if tools or bed:
                return TemperatureInfo(tool=tools, bed=bed)
        
        return None
    
    def parse_position(self, line: str) -> Optional[Position]:
        # Klipper 위치 정보 파싱
        if 'mcu:' in line and 'stepper_' in line:
            # 더 정밀한 위치 정보 파싱
            position_data = {}
            
            for axis in ['x', 'y', 'z', 'e']:
                pattern = rf'stepper_{axis}:(-?\d+\.?\d*)'
                match = re.search(pattern, line)
                if match:
                    position_data[axis] = float(match.group(1))
            
            if position_data:
                return Position(
                    x=position_data.get('x', 0),
                    y=position_data.get('y', 0),
                    z=position_data.get('z', 0),
                    e=position_data.get('e', 0)
                )
        
        return None
    
    def get_initialization_commands(self) -> List[str]:
        return [
            "STATUS",
            "GET_POSITION",
            "HELP",
        ]


class PrinterHandlerFactory:
    """프린터 핸들러 팩토리"""
    
    @staticmethod
    def create_handler(printer_type: PrinterType, firmware_type: FirmwareType) -> BasePrinterHandler:
        """프린터 타입과 펌웨어에 맞는 핸들러 생성"""
        
        if printer_type == PrinterType.SLA:
            return SLAHandler(firmware_type)
        elif firmware_type == FirmwareType.KLIPPER:
            return KlipperHandler(printer_type)
        elif firmware_type == FirmwareType.MARLIN:
            if printer_type == PrinterType.FDM:
                return FDMMarlinHandler()
            else:
                return FDMMarlinHandler()  # 기본값
        else:
            # 기본 핸들러
            return FDMMarlinHandler()


class ExtendedDataCollector:
    """확장 데이터 수집기"""
    
    def __init__(self, handler: BasePrinterHandler):
        self.handler = handler
        self.logger = get_logger('extended-data')
    
    def collect_sensor_data(self, command_sender) -> Dict[str, Any]:
        """센서 데이터 수집"""
        sensor_data = {}
        
        # 필라멘트 센서
        if self.handler.capabilities.has_filament_sensor:
            command_sender("M119")  # 엔드스톱 상태에 포함
        
        # 전력 센서 (아날로그 핀)
        command_sender("M42 P54 M")  # 아날로그 핀 54 읽기
        
        return sensor_data
    
    def collect_environment_data(self, command_sender) -> Dict[str, Any]:
        """환경 데이터 수집"""
        env_data = {}
        
        # 챔버 온도
        if self.handler.capabilities.has_heated_chamber:
            command_sender("M105")  # 온도 정보에 포함
        
        # 습도 센서 (아날로그 핀)
        command_sender("M42 P55 M")  # 아날로그 핀 55 읽기
        
        return env_data
    
    def collect_advanced_metrics(self, command_sender) -> Dict[str, Any]:
        """고급 메트릭 수집"""
        metrics = {}
        
        # Klipper 전용 고급 기능
        if self.handler.firmware_type == FirmwareType.KLIPPER:
            command_sender("ACCELEROMETER_QUERY")  # 진동 데이터
            command_sender("QUERY_PROBE")          # 프로브 상태
        
        return metrics 