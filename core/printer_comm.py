"""
3D 프린터 직접 통신 모듈
시리얼/USB를 통한 직접 통신
"""

import serial
import os
import glob
import stat
import serial.tools.list_ports as lp
import json
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

# ===== 안정 포트 탐색/초기화 유틸 =====
def _is_char_device(path: str) -> bool:
    try:
        st = os.stat(path, follow_symlinks=False)
        return stat.S_ISCHR(st.st_mode)
    except Exception:
        return False


def _cleanup_dead_nodes(patterns: List[str] = None) -> None:
    patterns = patterns or ["/dev/ttyUSB*", "/dev/ttyACM*"]
    for patt in patterns:
        for p in glob.glob(patt):
            try:
                if not _is_char_device(p):
                    os.remove(p)
            except Exception:
                # 권한 부족/경합 등은 무시
                pass


def _iter_candidate_ports() -> List[str]:
    # 1) udev 심링크 우선
    yield "/dev/printer"
    # 2) 안정 경로 by-id
    for p in sorted(glob.glob("/dev/serial/by-id/*")):
        yield p
    # 3) 일반 경로(USB/ACM)
    for patt in ("/dev/ttyUSB*", "/dev/ttyACM*"):
        for p in sorted(glob.glob(patt)):
            yield p
    # 4) pyserial 열거(후순위)
    for c in lp.comports():
        yield c.device


def _probe_serial_port(device_path: str, baudrate: int = 115200) -> bool:
    try:
        with serial.Serial(device_path, baudrate, timeout=1, write_timeout=1, rtscts=False, dsrdtr=False) as s:
            try:
                s.dtr = False
                time.sleep(0.2)
                s.dtr = True
            except Exception:
                pass
            try:
                s.reset_input_buffer()
                s.reset_output_buffer()
            except Exception:
                pass
        return True
    except Exception:
        return False


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
        self.window_size = 32
        
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
    

    def _auto_detect_port(self) -> Optional[str]:
        """프린터 포트 자동 감지(안정 경로 우선 + 죽은 노드 정리 + ttyACM 포함)"""
        self.logger.info("프린터 포트 자동 감지 중...")
        # 0) 죽은 노드 정리
        try:
            _cleanup_dead_nodes()
        except Exception:
            pass

        # 1) 후보 순회
        for dev in _iter_candidate_ports():
            try:
                if not os.path.exists(dev):
                    continue
                if not os.path.islink(dev) and not _is_char_device(dev):
                    continue
                if _probe_serial_port(dev, self.baudrate):
                    self.logger.info(f"프린터 포트 감지: {dev}")
                    return dev
            except Exception:
                continue

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
                # 업로드 핸드셰이크 등에서 RX를 일시 정지할 수 있도록 플래그 확인
                try:
                    if getattr(self, 'rx_paused', False):
                        time.sleep(0.01)
                        continue
                except Exception:
                    pass
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
                        # INFO 레벨일 때 raw 데이터도 표기
                        try:
                            if self.logger.getEffectiveLevel() == logging.INFO and data:
                                snippet = data.replace('\r', '').replace('\n', '\\n')
                                if snippet:
                                    self.logger.info(f"[RX_RAW] {snippet[:300]}")
                        except Exception:
                            pass
                        buffer += data.replace('\r\n', '\n').replace('\r', '\n')
                    
                    # 2) 버퍼에 라인이 있으면 처리
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        if line:
                            self.logger.debug(f"[RX_LINE] {line}")
                            self._process_response(line)
                            # INFO 레벨일 때 변환(파싱) 데이터 표기
                            try:
                                if self.logger.getEffectiveLevel() == logging.INFO:
                                    low = line.lower()
                                    # 온도 라인일 가능성
                                    if ('t:' in low) or (self.temp_pattern.search(line) is not None):
                                        ti = getattr(self, '_last_temp_info', None)
                                        if ti:
                                            try:
                                                td = ti.to_dict()
                                                # 소수점 2자리로 라운딩
                                                for k, v in list((td.get('tool') or {}).items()):
                                                    try:
                                                        v['actual'] = round(float(v.get('actual', 0.0)), 2)
                                                        v['target'] = round(float(v.get('target', 0.0)), 2)
                                                        v['offset'] = round(float(v.get('offset', 0.0)), 2)
                                                    except Exception:
                                                        pass
                                                if td.get('bed'):
                                                    try:
                                                        td['bed']['actual'] = round(float(td['bed'].get('actual', 0.0)), 2)
                                                        td['bed']['target'] = round(float(td['bed'].get('target', 0.0)), 2)
                                                        td['bed']['offset'] = round(float(td['bed'].get('offset', 0.0)), 2)
                                                    except Exception:
                                                        pass
                                                self.logger.info(f"[PARSED_TEMP] {json.dumps(td, ensure_ascii=False)}")
                                            except Exception:
                                                pass
                                    # 위치 라인일 가능성
                                    if low.startswith('x:') or (' x:' in low):
                                        pos = getattr(self, 'current_position', None)
                                        if pos:
                                            try:
                                                pd = {
                                                    'x': round(float(getattr(pos, 'x', 0.0)), 2),
                                                    'y': round(float(getattr(pos, 'y', 0.0)), 2),
                                                    'z': round(float(getattr(pos, 'z', 0.0)), 2),
                                                    'e': round(float(getattr(pos, 'e', 0.0)), 2),
                                                }
                                                self.logger.info(f"[PARSED_POS] {json.dumps(pd, ensure_ascii=False)}")
                                            except Exception:
                                                pass
                            except Exception:
                                pass
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
                            # INFO 레벨일 때 raw 데이터도 표기
                            try:
                                if self.logger.getEffectiveLevel() == logging.INFO:
                                    snippet2 = extra_s.replace('\r', '').replace('\n', '\\n')
                                    if snippet2:
                                        self.logger.info(f"[RX_RAW] {snippet2[:300]}")
                            except Exception:
                                pass
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
                    # 명령 전송 (LF 사용) – 업로드 등 동기 작업과 충돌 방지 위해 시리얼 락 사용
                    with self.serial_lock:
                        command_line = f"{command}\n"
                        self.serial_conn.write(command_line.encode('utf-8'))
                        self.serial_conn.flush()
                        self.logger.debug(f"[TX] {command!r}")
                
                self.command_queue.task_done()
                
            except Exception as e:
                self.logger.error(f"시리얼 전송 오류: {e}")
                time.sleep(1)
    
    
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
    
    
    def get_printer_status(self) -> PrinterStatus:
        """프린터 상태 반환"""
        return self._create_printer_status(self.state)
    
    def get_firmware_info(self) -> FirmwareInfo:
        """펌웨어/타입 통합 정보 반환"""
        return FirmwareInfo(
            type=self.firmware_name,  # backward-compatible
            version=self.firmware_version,
            capabilities=self.capabilities.copy(),
            firmware_name=self.firmware_name,
            firmware_type=self.firmware_type.value,
            printer_type=self.printer_type.value,
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

    def clear_command_queue(self) -> bool:
        """송신 대기 큐 비우기 래퍼"""
        try:
            return self.control.clear_command_queue()
        except Exception:
            return False
    
    
    
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
        """프린터 타입 정보 반환 (통합 모델에서 파생)"""
        fw = self.get_firmware_info()
        return {
            'printer_type': fw.printer_type or '',
            'firmware_type': fw.firmware_type or '',
            'firmware_name': fw.firmware_name or '',
            'firmware_version': fw.version or '',
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





# =====================위임 모듈 정리=====================

    def _process_response(self, line: str):
        """응답 처리 - 데이터 취득 모듈 위임"""
        return self.collector.process_response(line)

    def get_temperature_info(self) -> TemperatureInfo:
        """현재 온도 정보 반환 - 데이터 취득 모듈 위임"""
        return self.collector.get_temperature_info()
    
    def get_position(self) -> Position:
        """현재 위치 정보 반환 - 데이터 취득 모듈 위임"""
        return self.collector.get_position()    




    def connect(self, port: Optional[str] = None, baudrate: Optional[int] = None) -> bool:
        """프린터 연결 - 제어 모듈 위임"""
        return self.control.connect(port, baudrate)
    
    def disconnect(self):
        """프린터 연결 해제 - 제어 모듈 위임"""
        return self.control.disconnect()

    def send_gcode(self, command: str, wait: bool = False, timeout: float = 8.0) -> bool:
        """G-code 전송 - 제어 모듈 위임"""
        return self.control.send_gcode(command, wait, timeout)
    
    def send_command(self, command: str, priority: bool = False):
        """G-code 명령 전송 - 제어 모듈 위임"""
        return self.control.send_command(command, priority)

    def send_command_and_wait(self, command: str, timeout: float = 8.0) -> Optional[str]:
        """동기 전송/대기 - 제어 모듈 위임"""
        return self.control.send_command_and_wait(command, timeout)