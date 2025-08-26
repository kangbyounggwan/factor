"""
3D 프린터 직접 통신 모듈
시리얼/USB를 통한 직접 통신
"""

import serial
import threading
import time
import re
import logging
from queue import Queue, Empty
from typing import Dict, Any, Optional, Callable, List
from dataclasses import dataclass
from enum import Enum

# 추가: 비동기 송신 프로세스용 의존성 (존재 시 사용, 없으면 기존 경로 사용)
try:
    import multiprocessing as mp
    import asyncio
    import collections
    try:
        import serial_asyncio  # type: ignore
        _HAS_SERIAL_ASYNCIO = True
    except Exception:
        _HAS_SERIAL_ASYNCIO = False
except Exception:
    _HAS_SERIAL_ASYNCIO = False

from .data_models import *
from .printer_types import (
    PrinterType, FirmwareType, PrinterDetector, 
    PrinterHandlerFactory, ExtendedDataCollector
)

# 분리된 제어/데이터 취득 모듈
from .core_control import ControlModule
from .core_collection import DataCollectionModule


class PrinterState(Enum):
    """프린터 상태"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    OPERATIONAL = "operational"
    PRINTING = "printing"
    PAUSED = "paused"
    ERROR = "error"
    CANCELLING = "cancelling"
    FINISHING = "finishing"


@dataclass
class GCodeResponse:
    """G-code 응답"""
    command: str
    response: str
    timestamp: float
    success: bool
    error_message: Optional[str] = None


class PrinterCommunicator:
    """3D 프린터 직접 통신 클래스"""
    
    def __init__(self, port: Optional[str] = None, baudrate: int = 115200):
        self.logger = logging.getLogger('printer-comm')
        
        # 시리얼 연결 설정
        self.port = port
        self.baudrate = baudrate
        self.serial_conn = None
        self.connected = False
        
        # 상태 관리
        self.state = PrinterState.DISCONNECTED
        self.last_response_time = time.time()
        
        # 스레드/프로세스 관리
        self.running = False
        self.read_thread = None
        self.send_thread = None
        self.tx_bridge = None  # 비동기 송신 브리지(프로세스 기반)
        
        # 큐 및 버퍼
        self.command_queue = Queue()
        self.response_queue = Queue()
        self.send_buffer = []
        self.line_number = 1
        # 동기 전송/수신 보호
        self.serial_lock = threading.Lock()
        self.sync_mode = False  # True일 때 read worker는 일시 대기
        
        # 데이터 파싱
        self.current_temps = {}
        self.current_position = Position(0, 0, 0, 0)
        self.printer_info = {}
        self.sd_card_info = {}
        self._last_temp_info = None  # 최근 온도 정보 캐시
        
        # 진단용 최근 TX/RX 정보
        self.last_tx_command: Optional[str] = None
        self.last_tx_time: Optional[float] = None
        self.last_rx_line: Optional[str] = None
        self.last_rx_time: Optional[float] = None
        # 최근 관심 응답 라인 스냅샷 (동기 대기용)
        self._last_temp_line = None
        self._last_pos_line = None
        
        # 콜백
        self.callbacks = {
            'on_state_change': [],
            'on_temperature_update': [],
            'on_position_update': [],
            'on_response': [],
            'on_error': []
        }
        
        # G-code 패턴
        self.temp_pattern = re.compile(r'([TBCP])(\d*):(-?\d+\.?\d*)\s*/(-?\d+\.?\d*)')
        self.position_pattern = re.compile(r'([XYZE]):(-?\d+\.?\d*)')
        self.ok_pattern = re.compile(r'^ok')
        self.error_pattern = re.compile(r'^Error:|^!!|ALARM')
        
        # 프린터 능력 및 설정
        self.firmware_name = "Unknown"
        self.firmware_version = "Unknown"
        self.capabilities = []
        self.max_temp_extruder = 300
        self.max_temp_bed = 120

        # 윈도우/크레딧 전송을 위한 설정값 (설정에 연동 가능)
        self.window_size = 50
        
        # 프린터 타입 감지 및 핸들러
        self.printer_detector = PrinterDetector()
        self.printer_type = PrinterType.UNKNOWN
        self.firmware_type = FirmwareType.UNKNOWN
        self.printer_handler = None
        self.extended_collector = None
        self.detection_responses = []  # 감지용 응답 저장
        
        self.logger.info("프린터 통신 모듈 초기화 완료")
        
        # 분리된 모듈 초기화
        self.control = ControlModule(self)
        self.collector = DataCollectionModule(self)
    
    def add_callback(self, event_type: str, callback: Callable):
        """콜백 함수 추가"""
        if event_type in self.callbacks:
            self.callbacks[event_type].append(callback)
    
    def _trigger_callback(self, event_type: str, data: Any):
        """콜백 함수 실행"""
        for callback in self.callbacks.get(event_type, []):
            try:
                callback(data)
            except Exception as e:
                self.logger.error(f"콜백 실행 오류 ({event_type}): {e}")
    
    def connect(self, port: Optional[str] = None, baudrate: Optional[int] = None) -> bool:
        """프린터 연결 - 제어 모듈 위임"""
        return self.control.connect(port, baudrate)
    
    def disconnect(self):
        """프린터 연결 해제 - 제어 모듈 위임"""
        return self.control.disconnect()
    
    def _auto_detect_port(self) -> Optional[str]:
        """프린터 포트 자동 감지"""
        import serial.tools.list_ports
        
        self.logger.info("프린터 포트 자동 감지 중...")
        
        # 일반적인 3D 프린터 VID/PID
        known_vendors = [
            (0x2341, None),  # Arduino
            (0x1A86, 0x7523), # CH340
            (0x0403, 0x6001), # FTDI
            (0x10C4, 0xEA60), # CP210x
            (0x2E8A, 0x0005), # Raspberry Pi Pico
        ]
        
        ports = serial.tools.list_ports.comports()
        
        for port in ports:
            # VID/PID 확인
            for vid, pid in known_vendors:
                if port.vid == vid and (pid is None or port.pid == pid):
                    self.logger.info(f"프린터 포트 감지: {port.device} ({port.description})")
                    return port.device
            
            # 설명으로 확인
            if any(keyword in port.description.lower() for keyword in 
                   ['arduino', 'ch340', 'ftdi', 'cp210', 'usb serial']):
                self.logger.info(f"프린터 포트 감지: {port.device} ({port.description})")
                return port.device
        
        self.logger.warning("프린터 포트를 자동으로 찾을 수 없습니다")
        return None
    
    def _initialize_printer(self):
        """프린터 초기화"""
        # 기본 감지 명령어
        detection_commands = [
            "M115",  # 펌웨어 정보
            "M503",  # 설정 출력
            "M105",  # 온도 정보
            "M114",  # 위치 정보
        ]
        
        # 감지 명령어 전송
        for cmd in detection_commands:
            self.send_command(cmd)
        
        # 1초 후 프린터 타입 감지 시작
        import threading
        threading.Timer(1.0, self._detect_printer_type).start()
    
    def _set_state(self, new_state: PrinterState):
        """상태 변경"""
        if self.state != new_state:
            old_state = self.state
            self.state = new_state
            self.logger.info(f"프린터 상태 변경: {old_state.value} -> {new_state.value}")
            
            # 상태 변경 콜백
            status = self._create_printer_status(new_state)
            self._trigger_callback('on_state_change', status)
    
    def _create_printer_status(self, state: PrinterState) -> PrinterStatus:
        """프린터 상태 객체 생성 (중복 제거)"""
        return PrinterStatus(
            state=state.value,
            timestamp=time.time(),
            flags={
                'operational': state in [PrinterState.OPERATIONAL, PrinterState.PRINTING, PrinterState.PAUSED],
                'printing': state == PrinterState.PRINTING,
                'paused': state == PrinterState.PAUSED,
                'error': state == PrinterState.ERROR,
                'ready': state == PrinterState.OPERATIONAL,
                'connected': state != PrinterState.DISCONNECTED
            }
        )
    
    def _read_worker(self):
        """시리얼 읽기 워커"""
        buffer = ""
        
        while self.running and self.connected:
            try:
                if self.sync_mode:
                    time.sleep(0.01)
                    continue
                if self.serial_conn and self.serial_conn.is_open:
                    # 1) 라인 단위 블로킹 읽기(타임아웃까지 대기)
                    line_bytes = self.serial_conn.readline()  # timeout에 따라 반환
                    if line_bytes:
                        try:
                            data = line_bytes.decode('utf-8', errors='ignore')
                        except Exception:
                            data = ''
                        self.logger.debug(f"[RX_RAW] {repr(data)}")
                        buffer += data.replace('\r\n', '\n').replace('\r', '\n')
                    
                    # 2) 버퍼에 라인이 있으면 처리
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        if line:
                            self.logger.debug(f"[RX_LINE] {line}")
                            self._process_response(line)
                            self.last_response_time = time.time()
                    
                    # 3) 남아있는 바이트가 많다면 추가로 비동기 드레인
                    if self.serial_conn.in_waiting:
                        extra = self.serial_conn.read(self.serial_conn.in_waiting)
                        try:
                            extra_s = extra.decode('utf-8', errors='ignore')
                        except Exception:
                            extra_s = ''
                        if extra_s:
                            self.logger.debug(f"[RX_RAW] {repr(extra_s)}")
                            buffer += extra_s.replace('\r\n', '\n').replace('\r', '\n')
                
                time.sleep(0.01)  # CPU 사용률 조절
                
            except Exception as e:
                self.logger.error(f"시리얼 읽기 오류: {e}")
                time.sleep(1)
    
    def _send_worker(self):
        """시리얼 전송 워커"""
        while self.running and self.connected:
            try:
                # 큐에서 명령 가져오기
                try:
                    command = self.command_queue.get(timeout=1)
                except Empty:
                    continue
                
                if self.serial_conn and self.serial_conn.is_open:
                    # 명령 전송 (LF 사용)
                    command_line = f"{command}\n"
                    self.serial_conn.write(command_line.encode('utf-8'))
                    self.serial_conn.flush()
                    
                    self.logger.debug(f"[TX] {command!r}")
                
                self.command_queue.task_done()
                
            except Exception as e:
                self.logger.error(f"시리얼 전송 오류: {e}")
                time.sleep(1)
    
    def _process_response(self, line: str):
        """응답 처리 - 데이터 취득 모듈 위임"""
        return self.collector.process_response(line)
    
    def _parse_temperature(self, line: str) -> bool:
        """온도 정보 파싱 (Marlin 스타일: ok T:25.2 /0.0 B:24.3 /0.0 @:0 B@:0)"""
        # 우선 간단 패턴으로 빠르게 파싱
        # 노즐(툴0)
        t_match = re.search(r"T:\s*(-?\d+\.?\d*)\s*/\s*(-?\d+\.?\d*)", line)
        # 베드
        b_match = re.search(r"B:\s*(-?\d+\.?\d*)\s*/\s*(-?\d+\.?\d*)", line)

        tools = {}
        bed = None
        chamber = None

        if t_match:
            actual = float(t_match.group(1))
            target = float(t_match.group(2))
            tools["tool0"] = TemperatureData(actual=actual, target=target)

        if b_match:
            actual = float(b_match.group(1))
            target = float(b_match.group(2))
            bed = TemperatureData(actual=actual, target=target)

        # 확장 패턴(여러 툴, 챔버)이 필요한 경우 기존 정규식도 시도
        if not tools and not bed:
            matches = self.temp_pattern.findall(line)
            for match in matches:
                sensor_type, sensor_num, actual, target = match
                actual = float(actual); target = float(target)
                td = TemperatureData(actual=actual, target=target)
                if sensor_type == 'T':
                    tool_name = f"tool{sensor_num}" if sensor_num else "tool0"
                    tools[tool_name] = td
                elif sensor_type == 'B':
                    bed = td
                elif sensor_type == 'C':
                    chamber = td

        if tools or bed or chamber:
            temp_info = TemperatureInfo(tool=tools, bed=bed, chamber=chamber)
            # 캐시 및 콜백
            self._last_temp_info = temp_info
            self._trigger_callback('on_temperature_update', temp_info)
            return True

        return False
    
    def _parse_position(self, line: str) -> bool:
        """위치 정보 파싱"""
        if not line.startswith('X:'):
            return False
        
        matches = self.position_pattern.findall(line)
        if not matches:
            return False
        
        position_data = {}
        for axis, value in matches:
            position_data[axis.lower()] = float(value)
        
        if position_data:
            self.current_position = Position(
                x=position_data.get('x', 0),
                y=position_data.get('y', 0),
                z=position_data.get('z', 0),
                e=position_data.get('e', 0)
            )
            self._trigger_callback('on_position_update', self.current_position)
            return True
        
        return False
    
    def _parse_firmware_info(self, line: str) -> bool:
        """펌웨어 정보 파싱"""
        if line.startswith('FIRMWARE_NAME:'):
            # Marlin 스타일
            parts = line.split()
            for part in parts:
                if part.startswith('FIRMWARE_NAME:'):
                    self.firmware_name = part.split(':')[1]
                elif part.startswith('FIRMWARE_VERSION:'):
                    self.firmware_version = part.split(':')[1]
            return True
        
        if 'Klipper' in line:
            self.firmware_name = "Klipper"
            return True
        
        if 'RepRapFirmware' in line:
            self.firmware_name = "RepRapFirmware"
            return True
        
        return False
    
    def send_command(self, command: str, priority: bool = False):
        """G-code 명령 전송 - 제어 모듈 위임"""
        return self.control.send_command(command, priority)
    
    def _insert_priority_command(self, command: str):
        """우선순위 명령을 큐 앞쪽에 삽입"""
        # 임시로 큐의 모든 항목을 빼고 우선순위 명령을 먼저 넣기
        temp_commands = []
        try:
            while True:
                temp_commands.append(self.command_queue.get_nowait())
        except Empty:
            pass
        
        self.command_queue.put(command)
        
        for cmd in temp_commands:
            self.command_queue.put(cmd)
    
    def send_command_and_wait(self, command: str, timeout: float = 8.0) -> Optional[str]:
        """동기 전송/대기 - 제어 모듈 위임"""
        return self.control.send_command_and_wait(command, timeout)

    def send_gcode(self, command: str, wait: bool = False, timeout: float = 8.0) -> bool:
        """G-code 전송 - 제어 모듈 위임"""
        return self.control.send_gcode(command, wait, timeout)
    
    def get_temperature_info(self) -> TemperatureInfo:
        """현재 온도 정보 반환 - 데이터 취득 모듈 위임"""
        return self.collector.get_temperature_info()
    
    def get_position(self) -> Position:
        """현재 위치 정보 반환 - 데이터 취득 모듈 위임"""
        return self.collector.get_position()
    
    def get_printer_status(self) -> PrinterStatus:
        """프린터 상태 반환"""
        return self._create_printer_status(self.state)
    
    def get_firmware_info(self) -> FirmwareInfo:
        """펌웨어 정보 반환"""
        return FirmwareInfo(
            type=self.firmware_name,
            version=self.firmware_version,
            capabilities=self.capabilities.copy()
        )
    
    def home_axes(self, axes: str = ""):
        """축 홈 이동"""
        if axes:
            self.send_command(f"G28 {axes}")
        else:
            self.send_command("G28")  # 모든 축 홈
    
    def set_temperature(self, tool: int = 0, temp: float = 0):
        """온도 설정"""
        if tool == -1:  # 베드
            self.send_command(f"M140 S{temp}")
        else:  # 툴
            self.send_command(f"M104 T{tool} S{temp}")
    
    def move_axis(self, x: Optional[float] = None, y: Optional[float] = None, 
                  z: Optional[float] = None, e: Optional[float] = None, 
                  feedrate: Optional[float] = None):
        """축 이동"""
        command = "G1"
        
        if x is not None:
            command += f" X{x}"
        if y is not None:
            command += f" Y{y}"
        if z is not None:
            command += f" Z{z}"
        if e is not None:
            command += f" E{e}"
        if feedrate is not None:
            command += f" F{feedrate}"
        
        self.send_command(command)
    
    def emergency_stop(self):
        """비상 정지"""
        # 비동기 브리지에서는 배리어 명령으로 처리
        self.send_command("M112", priority=True)
        self._set_state(PrinterState.ERROR)

    # ===== SD 카드 업로드/인쇄 =====
    def sd_list(self) -> str:
        """SD 카드 파일 목록(M20) 원문 반환"""
        try:
            # 동기 모드에서는 목록 전체를 수집한다(OK 또는 'End file list'까지)
            return self.control.send_command_and_wait("M20", timeout=8.0, collect=True) or ""
        except Exception as e:
            self.logger.error(f"SD 목록 조회 실패: {e}")
            return ""

    def sd_upload_gcode_lines(self, sd_name: str, lines) -> bool:
        """SD 카드에 G-code 파일 쓰기(M28/M29)
        - sd_name: SD에 저장될 파일명(호환성 위해 8.3 대문자 권장)
        - lines: 이터러블 텍스트 라인
        """
        try:
            # 쓰기 시작
            if not self.control.send_command_and_wait(f"M28 {sd_name}", timeout=10.0):
                self.logger.error("SD 쓰기 시작 실패(M28)")
                return False

            sent = 0
            for raw in lines:
                line = (raw or "").strip()
                if not line or line.startswith(";"):
                    continue
                if ";" in line:
                    line = line.split(";", 1)[0].strip()
                    if not line:
                        continue
                # 일반 라인은 대기 없이 전송(동기 모드: 소프트 윈도우로 페이싱)
                ok = self.control.send_gcode(line, wait=False)
                if not ok:
                    self.logger.warning(f"SD 업로드 중 전송 실패: {line}")
                sent += 1
                if sent % 200 == 0:
                    # 버퍼 보호용 가벼운 동기화
                    self.control.send_command_and_wait("M400", timeout=5.0)

            # 종료
            if not self.control.send_command_and_wait("M29", timeout=10.0):
                self.logger.error("SD 쓰기 종료 실패(M29)")
                return False
            self.logger.info(f"SD 업로드 완료: {sd_name}, lines={sent}")
            return True
        except Exception as e:
            self.logger.error(f"SD 업로드 실패: {e}")
            return False

    def sd_upload_and_print(self, sd_name: str, file_path: str, start: bool = True) -> bool:
        """로컬 파일을 SD에 업로드 후 선택(M23)/인쇄(M24)"""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                ok = self.sd_upload_gcode_lines(sd_name, f)
            if not ok:
                return False
            # 파일 선택
            if not self.control.send_command_and_wait(f"M23 {sd_name}", timeout=8.0):
                self.logger.error("SD 파일 선택 실패(M23)")
                return False
            # 인쇄 시작
            if start:
                if not self.control.send_command_and_wait("M24", timeout=8.0):
                    self.logger.error("SD 인쇄 시작 실패(M24)")
                    return False
            return True
        except Exception as e:
            self.logger.error(f"SD 업로드/인쇄 오류: {e}")
            return False

    def sd_print_progress(self) -> str:
        """SD 인쇄 진행률(M27) 원문 반환"""
        try:
            return self.control.send_command_and_wait("M27", timeout=5.0) or ""
        except Exception as e:
            self.logger.error(f"SD 진행률 조회 실패: {e}")
            return ""
    
    def _parse_temperature_response(self, response: str):
        """온도 응답 파싱 (M105) - 통합된 파싱 사용"""
        try:
            # 기존 온도 파싱 로직 사용
            if self._parse_temperature(response):
                self.logger.debug(f"온도 응답 파싱 완료: {self.current_temps}")
        except Exception as e:
            self.logger.error(f"온도 응답 파싱 실패: {e}")
    
    def _parse_position_response(self, response: str):
        """위치 응답 파싱 (M114)"""
        try:
            # M114 응답 예시: "X:0.00 Y:0.00 Z:0.00 E:0.00"
            if "X:" in response and "Y:" in response and "Z:" in response:
                # X축 위치 파싱
                x_match = re.search(r'X:(-?\d+\.?\d*)', response)
                if x_match:
                    self.current_position.x = float(x_match.group(1))
                
                # Y축 위치 파싱
                y_match = re.search(r'Y:(-?\d+\.?\d*)', response)
                if y_match:
                    self.current_position.y = float(y_match.group(1))
                
                # Z축 위치 파싱
                z_match = re.search(r'Z:(-?\d+\.?\d*)', response)
                if z_match:
                    self.current_position.z = float(z_match.group(1))
                
                # E축 위치 파싱
                e_match = re.search(r'E:(-?\d+\.?\d*)', response)
                if e_match:
                    self.current_position.e = float(e_match.group(1))
                
                self.logger.debug(f"위치 파싱 완료: {self.current_position}")
        except Exception as e:
            self.logger.error(f"위치 응답 파싱 실패: {e}")
    
    def _detect_printer_type(self):
        """프린터 타입 감지"""
        try:
            # 펌웨어 타입 감지
            self.firmware_type = self.printer_detector.detect_firmware(self.detection_responses)
            
            # 프린터 타입 감지
            firmware_info = ' '.join(self.detection_responses)
            self.printer_type = self.printer_detector.detect_printer_type(firmware_info, [])
            
            # 프린터 핸들러 생성
            self.printer_handler = PrinterHandlerFactory.create_handler(
                self.printer_type, self.firmware_type
            )
            
            # 확장 데이터 수집기 생성
            self.extended_collector = ExtendedDataCollector(self.printer_handler)
            
            # 프린터별 초기화 명령 실행
            init_commands = self.printer_handler.get_initialization_commands()
            for cmd in init_commands:
                self.send_command(cmd)
            
            self.logger.info(f"프린터 감지 완료: {self.printer_type.value} / {self.firmware_type.value}")
            
        except Exception as e:
            self.logger.error(f"프린터 타입 감지 오류: {e}")
            # 기본 핸들러 사용
            from .printer_types import FDMMarlinHandler
            self.printer_handler = FDMMarlinHandler()

    # ===== 내부 유틸: 배리어 정규식, 비동기 브리지 시작 =====
    @staticmethod
    def _barrier_regex():
        return re.compile(r"^(?:M109|M190|M400|G4|G28|G29|M112)\b", re.IGNORECASE)

    def _start_async_tx_bridge(self):
        """비동기 송신 프로세스 브리지 시작 및 수집기 연결"""
        if not _HAS_SERIAL_ASYNCIO:
            return

        # 하위 프로세스 엔트리포인트
        def _tx_proc_main(port: str, baudrate: int, window_size: int, in_q: 'mp.Queue', out_q: 'mp.Queue'):
            asyncio.run(self._tx_run(port, baudrate, window_size, in_q, out_q))

        # 별도 정적 함수로 분리할 수도 있으나, 한 파일 제한으로 내부에 유지
        # mp context
        ctx = mp.get_context('spawn')
        in_q: 'mp.Queue' = ctx.Queue(maxsize=10000)
        out_q: 'mp.Queue' = ctx.Queue(maxsize=10000)

        # 프로세스 시작
        proc = ctx.Process(target=_tx_proc_main, args=(self.port, self.baudrate, self.window_size, in_q, out_q), daemon=True)
        proc.start()

        # 브리지 객체(간단)
        class _Bridge:
            def __init__(self, in_q, out_q, proc, on_rx: Callable[[str], None], on_error: Callable[[str], None]):
                self.in_q = in_q; self.out_q = out_q; self.proc = proc
                self._seq = 0
                self._acks = {}
                self._lock = threading.Lock()
                self._running = True
                self._collector = threading.Thread(target=self._collect, args=(on_rx, on_error), daemon=True)
                self._collector.start()

            def enqueue(self, line: str, barrier: bool = False) -> int:
                self._seq += 1
                self.in_q.put({'id': self._seq, 'line': line, 'barrier': barrier, 'ts': time.time()})
                return self._seq

            def wait_ack(self, msg_id: int, timeout: float = 8.0) -> bool:
                end = time.time() + timeout
                while time.time() < end:
                    with self._lock:
                        if msg_id in self._acks:
                            self._acks.pop(msg_id, None)
                            return True
                    time.sleep(0.005)
                return False

            def stop(self):
                self._running = False
                try:
                    self.in_q.put(None)
                except Exception:
                    pass

            def _collect(self, on_rx, on_error):
                while self._running:
                    try:
                        evt = self.out_q.get(timeout=0.5)
                    except Exception:
                        continue
                    t = evt.get('type')
                    if t == 'ack':
                        with self._lock:
                            self._acks[evt['id']] = evt
                    elif t == 'rx':
                        try:
                            on_rx(evt.get('line', ''))
                        except Exception:
                            pass
                    elif t == 'error':
                        try:
                            on_error(evt.get('message', ''))
                        except Exception:
                            pass

        # RX 이벤트는 기존 파서 경로로 전달하여 온도/위치/펌웨어 파싱 및 콜백 유지
        def _on_rx(line: str):
            if line:
                try:
                    self._process_response(line)
                except Exception:
                    pass

        def _on_error(msg: str):
            if msg:
                try:
                    self.logger.error(f"프린터 오류: {msg}")
                    self._set_state(PrinterState.ERROR)
                    self._trigger_callback('on_error', msg)
                except Exception:
                    pass

        self.tx_bridge = _Bridge(in_q, out_q, proc, _on_rx, _on_error)

    # 하위 프로세스용 런 루프(정적 메서드로 정의)
    @staticmethod
    async def _tx_run(port: str, baudrate: int, window_size: int, in_q: 'mp.Queue', out_q: 'mp.Queue'):
        reader, writer = await serial_asyncio.open_serial_connection(url=port, baudrate=baudrate)
        loop = asyncio.get_running_loop()
        credit = asyncio.Semaphore(window_size)
        inflight = collections.deque()
        send_q: asyncio.Queue = asyncio.Queue(maxsize=window_size * 4)

        def feeder():
            while True:
                msg = in_q.get()
                if msg is None:
                    loop.call_soon_threadsafe(send_q.put_nowait, None); break
                loop.call_soon_threadsafe(send_q.put_nowait, msg)
        threading.Thread(target=feeder, daemon=True).start()

        async def writer_coro():
            barrier_re = re.compile(r"^(?:M109|M190|M400|G4|G28|G29|M112)\b", re.IGNORECASE)
            while True:
                msg = await send_q.get()
                if msg is None:
                    break
                if msg.get('barrier') or barrier_re.match(msg.get('line') or ''):
                    while inflight:
                        await asyncio.sleep(0.001)
                await credit.acquire()
                line = msg['line']
                writer.write((line + "\n").encode('utf-8'))
                try:
                    await writer.drain()
                except Exception:
                    # 드레인 실패 시 에러 이벤트
                    out_q.put({'type': 'error', 'message': 'writer.drain failed', 'ts': time.time()})
                inflight.append(msg)
                out_q.put({'type': 'tx', 'id': msg['id'], 'line': line, 'ts': time.time()})

        async def reader_coro():
            while True:
                try:
                    data = await reader.readuntil(b'\n')
                except asyncio.IncompleteReadError:
                    await asyncio.sleep(0.005); continue
                s = data.decode('utf-8', errors='ignore').strip()
                if not s:
                    continue
                out_q.put({'type': 'rx', 'line': s, 'ts': time.time()})
                low = s.lower()
                if low.startswith('ok') or s.startswith('start'):
                    if inflight:
                        acked = inflight.popleft()
                        out_q.put({'type': 'ack', 'id': acked['id'], 'line': acked['line'], 'ts': time.time()})
                    credit.release()
                elif low.startswith('error') or '!!' in s or 'alarm' in s:
                    out_q.put({'type': 'error', 'message': s, 'ts': time.time()})

        await asyncio.gather(writer_coro(), reader_coro())
    
    def get_printer_type_info(self) -> Dict[str, str]:
        """프린터 타입 정보 반환"""
        return {
            'printer_type': self.printer_type.value,
            'firmware_type': self.firmware_type.value,
            'firmware_name': self.firmware_name,
            'firmware_version': self.firmware_version
        }
    
    def collect_extended_data(self) -> Dict[str, Any]:
        """확장 데이터 수집"""
        if not self.extended_collector:
            return {}
        
        try:
            extended_data = {}
            
            # 센서 데이터 수집
            sensor_data = self.extended_collector.collect_sensor_data(self.send_command)
            extended_data.update(sensor_data)
            
            # 환경 데이터 수집
            env_data = self.extended_collector.collect_environment_data(self.send_command)
            extended_data.update(env_data)
            
            # 고급 메트릭 수집
            metrics = self.extended_collector.collect_advanced_metrics(self.send_command)
            extended_data.update(metrics)
            
            return extended_data
            
        except Exception as e:
            self.logger.error(f"확장 데이터 수집 오류: {e}")
            return {}
    
    def get_printer_capabilities(self) -> Dict[str, Any]:
        """프린터 기능 정보 반환"""
        if self.printer_handler:
            capabilities = self.printer_handler.capabilities
            return {
                'has_heated_bed': capabilities.has_heated_bed,
                'has_heated_chamber': capabilities.has_heated_chamber,
                'has_auto_leveling': capabilities.has_auto_leveling,
                'has_filament_sensor': capabilities.has_filament_sensor,
                'has_power_recovery': capabilities.has_power_recovery,
                'has_multi_extruder': capabilities.has_multi_extruder,
                'has_uv_led': capabilities.has_uv_led,
                'has_resin_tank': capabilities.has_resin_tank,
                'max_extruders': capabilities.max_extruders,
                'max_temp_hotend': capabilities.max_temp_hotend,
                'max_temp_bed': capabilities.max_temp_bed,
                'build_volume': capabilities.build_volume,
                'layer_height_range': [capabilities.layer_height_min, capabilities.layer_height_max]
            }
        return {} 