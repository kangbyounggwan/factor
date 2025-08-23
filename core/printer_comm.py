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

from .data_models import *
from .printer_types import (
    PrinterType, FirmwareType, PrinterDetector, 
    PrinterHandlerFactory, ExtendedDataCollector
)


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
        
        # 스레드 관리
        self.running = False
        self.read_thread = None
        self.send_thread = None
        
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
        
        # 프린터 타입 감지 및 핸들러
        self.printer_detector = PrinterDetector()
        self.printer_type = PrinterType.UNKNOWN
        self.firmware_type = FirmwareType.UNKNOWN
        self.printer_handler = None
        self.extended_collector = None
        self.detection_responses = []  # 감지용 응답 저장
        
        self.logger.info("프린터 통신 모듈 초기화 완료")
    
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
        """프린터 연결"""
        if self.connected:
            self.logger.warning("이미 연결되어 있습니다")
            return True
        
        if port is not None:
            self.port = port
        if baudrate is not None:
            self.baudrate = baudrate
        
        if not self.port:
            self.port = self._auto_detect_port()
            if not self.port:
                self.logger.error("프린터 포트를 찾을 수 없습니다")
                return False
        
        try:
            self.logger.info(f"프린터 연결 시도: {self.port}@{self.baudrate}")
            self._set_state(PrinterState.CONNECTING)
            
            # 시리얼 연결
            self.serial_conn = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=0.5,
                write_timeout=1,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                rtscts=False,
                dsrdtr=False
            )
            
            # 리셋/버퍼 클리어 및 안정화
            try:
                self.logger.info("시리얼 연결 안정화 중...")
                
                # DTR 리셋으로 보드 리셋
                self.serial_conn.dtr = False
                time.sleep(0.2)
                self.serial_conn.dtr = True
                
                # 입력/출력 버퍼 클리어
                self.serial_conn.reset_input_buffer()
                self.serial_conn.reset_output_buffer()
                
                # 안정화 대기
                time.sleep(2.0)
                
                self.logger.info("시리얼 연결 안정화 완료")
                
            except Exception as e:
                self.logger.warning(f"시리얼 안정화 중 오류 (무시됨): {e}")
                pass
            
            # 스레드 시작
            self.running = True
            self.read_thread = threading.Thread(target=self._read_worker, daemon=True)
            self.send_thread = threading.Thread(target=self._send_worker, daemon=True)
            
            self.read_thread.start()
            self.send_thread.start()
            
            self.connected = True
            self._set_state(PrinterState.OPERATIONAL)
            
            # 초기 설정 명령
            self._initialize_printer()
            
            self.logger.info("프린터 연결 완료")
            return True
            
        except Exception as e:
            self.logger.error(f"프린터 연결 실패: {e}")
            self._set_state(PrinterState.ERROR)
            return False
    
    def disconnect(self):
        """프린터 연결 해제"""
        self.logger.info("프린터 연결 해제 중...")
        self.running = False
        
        # 스레드 종료 대기
        if self.read_thread and self.read_thread.is_alive():
            self.read_thread.join(timeout=5)
        if self.send_thread and self.send_thread.is_alive():
            self.send_thread.join(timeout=5)
        
        # 시리얼 연결 종료
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
        
        self.connected = False
        self._set_state(PrinterState.DISCONNECTED)
        self.logger.info("프린터 연결 해제 완료")
    
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
        """응답 처리"""
        self.logger.debug(f"응답 수신: {line}")
        
        # 응답을 last_response에 저장 (send_command_and_wait용)
        self.last_response = line
        
        # 감지용 응답 저장
        self.detection_responses.append(line)
        
        # 프린터 핸들러가 있으면 특화 파싱 사용
        if self.printer_handler:
            # 온도 정보 파싱
            temp_info = self.printer_handler.parse_temperature(line)
            if temp_info:
                self._trigger_callback('on_temperature_update', temp_info)
                self._last_temp_line = line
                return
            
            # 위치 정보 파싱
            position = self.printer_handler.parse_position(line)
            if position:
                self.current_position = position
                self._trigger_callback('on_position_update', position)
                self._last_pos_line = line
                return
        else:
            # 기본 파싱 방식
            if self._parse_temperature(line):
                self._last_temp_line = line
                return
            
            if self._parse_position(line):
                self._last_pos_line = line
                return
        
        # 펌웨어 정보 파싱
        if self._parse_firmware_info(line):
            return
        
        # 오류 확인
        if self.error_pattern.search(line):
            self.logger.error(f"프린터 오류: {line}")
            self._set_state(PrinterState.ERROR)
            self._trigger_callback('on_error', line)
        
        # OK 응답
        if self.ok_pattern.match(line):
            pass  # 정상 응답
        
        # 응답 콜백
        response = GCodeResponse(
            command="",
            response=line,
            timestamp=time.time(),
            success=not self.error_pattern.search(line)
        )
        self._trigger_callback('on_response', response)
    
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
        """G-code 명령 전송"""
        if not self.connected:
            self.logger.warning("프린터가 연결되지 않음")
            return False
        
        # 우선순위 명령은 큐 앞쪽에 추가
        if priority:
            self._insert_priority_command(command)
        else:
            self.command_queue.put(command)
        
        return True
    
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
        """동기식으로 바로 전송하고 같은 함수에서 읽어서 반환"""
        if not self.connected or not (self.serial_conn and self.serial_conn.is_open):
            self.logger.warning("프린터가 연결되지 않음")
            return None

        with self.serial_lock:
            self.sync_mode = True
            try:
                # 준비: 내부 상태 초기화(의미있는 라인 캐시)
                self._last_temp_line = None
                self._last_pos_line = None
                self.last_response = None

                # 전송(LF)
                self.logger.debug(f"[SYNC_TX] {command!r}")
                self.serial_conn.write(f"{command}\n".encode("utf-8"))
                self.serial_conn.flush()

                # 동기 수신: readline 반복 + 추가 드레인
                end = time.time() + timeout
                while time.time() < end:
                    line_bytes = self.serial_conn.readline()
                    if line_bytes:
                        line = line_bytes.decode("utf-8", errors="ignore").strip()
                        if line:
                            self.logger.debug(f"[SYNC_RX] {line}")
                            # 의미있는 응답이면 즉시 반환
                            if command == "M105" and ("T:" in line or line.lower().startswith("ok")):
                                return line
                            if command == "M114" and ("X:" in line or line.lower().startswith("ok")):
                                return line
                            # 다른 라인이라도 마지막 응답으로 킵
                            self.last_response = line
                    else:
                        if self.serial_conn.in_waiting:
                            extra = self.serial_conn.read(self.serial_conn.in_waiting)
                            try:
                                extra_s = extra.decode("utf-8", errors="ignore")
                            except Exception:
                                extra_s = ""
                            for part in extra_s.replace("\r\n","\n").replace("\r","\n").split("\n"):
                                p = part.strip()
                                if p:
                                    self.logger.debug(f"[SYNC_RX] {p}")
                                    if command == "M105" and ("T:" in p or p.lower().startswith("ok")):
                                        return p
                                    if command == "M114" and ("X:" in p or p.lower().startswith("ok")):
                                        return p
                                    self.last_response = p
                        time.sleep(0.05)

                # fallback: 마지막 응답이라도 반환
                if self.last_response:
                    return self.last_response
                self.logger.warning(f"명령 '{command}' 응답 타임아웃 ({timeout}초)")
                return None
            except Exception as e:
                self.logger.error(f"동기 전송/수신 실패: {e}")
                return None
            finally:
                self.sync_mode = False

    def send_gcode(self, command: str, wait: bool = False, timeout: float = 8.0) -> bool:
        """G-code 전송 유틸리티.
        - wait=False: 즉시 LF로 전송 후 True 반환(에러 시 False)
        - wait=True: send_command_and_wait로 동기 응답 확인 후 True/False
        """
        if not self.connected or not (self.serial_conn and self.serial_conn.is_open):
            self.logger.warning("프린터가 연결되지 않음")
            return False

        if wait:
            return self.send_command_and_wait(command, timeout=timeout) is not None

        try:
            with self.serial_lock:
                self.serial_conn.write(f"{command}\n".encode("utf-8"))
                self.serial_conn.flush()
                self.logger.debug(f"[SYNC_TX] {command!r}")
            return True
        except Exception as e:
            self.logger.error(f"G-code 전송 실패: {e}")
            return False
    

    
    def get_temperature_info(self) -> TemperatureInfo:
        """현재 온도 정보 반환 (동기 요청/파싱 결과 우선)"""
        try:
            response = self.send_command_and_wait("M105", timeout=5.0)
            if response:
                # 파싱 시도 → 내부 캐시에 저장됨
                self._parse_temperature_response(response)
                if self._last_temp_info:
                    return self._last_temp_info
            # 파싱 실패 시 빈 구조 반환
            return TemperatureInfo(tool={})
        except Exception as e:
            self.logger.error(f"온도 정보 수집 실패: {e}")
            return TemperatureInfo(tool={})
    
    def get_position(self) -> Position:
        """현재 위치 정보 반환"""
        try:
            # 최신 위치 요청 및 응답 대기
            response = self.send_command_and_wait("M114", timeout=5.0)
            if response:
                # 응답 파싱하여 위치 정보 업데이트
                self._parse_position_response(response)
            
            return self.current_position
        except Exception as e:
            self.logger.error(f"위치 정보 수집 실패: {e}")
            return self.current_position
    
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
        self.send_command("M112", priority=True)
        self._set_state(PrinterState.ERROR)
    
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