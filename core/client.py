"""
Factor 3D 프린터 직접 클라이언트
3D 프린터와 직접 시리얼 통신을 통해 실시간 데이터 수집
"""

import json
import time
import threading
import signal
import sys
from typing import Dict, Any, Optional, Callable, List
from datetime import datetime
import logging
from queue import Queue, Empty
import psutil
import os

from .data_models import *
from .config_manager import ConfigManager
from .logger import get_logger, PerformanceLogger
from .printer_comm import PrinterCommunicator, PrinterState


class FactorClient:
    """Factor 3D 프린터 직접 클라이언트"""
    
    def __init__(self, config_manager: ConfigManager):
        self.config = config_manager
        self.logger = logging.getLogger('factor-client')
        
        # 프린터 연결 정보
        printer_config = self.config.get('printer', {})
        self.printer_port = printer_config.get('port')
        self.printer_baudrate = printer_config.get('baudrate', 115200)
        self.auto_detect = printer_config.get('auto_detect', True)
        self.firmware_type = printer_config.get('firmware_type', 'auto')
        self.simulation_mode = printer_config.get('simulation_mode', False)
        
        # 폴링 간격
        self.temp_poll_interval = printer_config.get('temp_poll_interval', 5.0)
        self.position_poll_interval = printer_config.get('position_poll_interval', 10.0)
        
        # 시뮬레이션 모드이거나 포트가 비어있으면 프린터 통신 없이 진행
        if self.simulation_mode or not self.printer_port:
            self.logger.info("시뮬레이션 모드로 실행합니다.")
            self.printer_comm: Optional[PrinterCommunicator] = None
        else:
            # 프린터 통신 객체
            port = self.printer_port if not self.auto_detect else ""
            self.printer_comm = PrinterCommunicator(
                port=port,
                baudrate=self.printer_baudrate
            )
        
        # 데이터 저장소
        self.current_data = {}
        self.temperature_history = []
        self.position_data = Position(0, 0, 0, 0)
        self.firmware_info = FirmwareInfo()
        self.camera_info = CameraInfo()
        self.system_info = SystemInfo()
        self.printer_status = PrinterStatus(
            state="disconnected", 
            timestamp=time.time()
        )
        
        # 상태 관리
        self.running = False
        self.error_count = 0
        self.last_heartbeat = time.time()
        self.connected = False
        
        # 스레드 및 큐
        self.data_queue = Queue()
        self.worker_threads = []
        self.polling_threads = []
        
        # 콜백 함수들
        self.callbacks = {
            'on_printer_state_change': [],
            'on_temperature_update': [],
            'on_progress_update': [],
            'on_position_update': [],
            'on_message': [],
            'on_connect': [],
            'on_disconnect': [],
            'on_error': [],
            'on_gcode_response': []
        }
        
        # 프린터 콜백 등록 (시뮬레이션 모드가 아닐 때만)
        if self.printer_comm:
            self.printer_comm.add_callback('on_state_change', self._on_printer_state_change)
            self.printer_comm.add_callback('on_temperature_update', self._on_temperature_update)
            self.printer_comm.add_callback('on_position_update', self._on_position_update)
            self.printer_comm.add_callback('on_response', self._on_gcode_response)
            self.printer_comm.add_callback('on_error', self._on_printer_error)
        
        # 시그널 핸들러 등록
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        
        # 워치독 설정
        self.watchdog_enabled = self.config.is_watchdog_enabled()
        self.watchdog_thread = None
        
        self.logger.info("Factor 클라이언트 초기화 완료")
    
    def add_callback(self, event_type: str, callback: Callable):
        """콜백 함수 추가"""
        if event_type in self.callbacks:
            self.callbacks[event_type].append(callback)
            self.logger.debug(f"콜백 추가: {event_type}")
    
    def remove_callback(self, event_type: str, callback: Callable):
        """콜백 함수 제거"""
        if event_type in self.callbacks and callback in self.callbacks[event_type]:
            self.callbacks[event_type].remove(callback)
            self.logger.debug(f"콜백 제거: {event_type}")
    
    def _trigger_callback(self, event_type: str, data: Any):
        """콜백 함수 실행"""
        for callback in self.callbacks.get(event_type, []):
            try:
                callback(data)
            except Exception as e:
                self.logger.error(f"콜백 실행 오류 ({event_type}): {e}")
    
    def start(self):
        """클라이언트 시작"""
        if self.running:
            self.logger.warning("클라이언트가 이미 실행 중입니다")
            return True  # 이미 실행 중이면 True 반환
        
        self.logger.info("Factor 클라이언트 시작")
        self.running = True
        
        # 설정 유효성 검사
        if not self.config.validate_config():
            self.logger.error("설정이 유효하지 않습니다")
            return False
        
        # 워커 스레드 시작
        self._start_worker_threads()
        
        # 워치독 시작
        if self.watchdog_enabled:
            self._start_watchdog()
        
        # 프린터 연결 시도 (실패해도 계속 실행)
        if self._connect_to_printer():
            self.connected = True
            self._start_polling_threads()
            self._trigger_callback('on_connect', None)
            self.logger.info("3D 프린터 연결 성공")
        else:
            self.connected = False
            self.logger.warning("프린터 연결 실패 - 연결 대기 모드로 실행")
            # 연결 재시도 스레드 시작
            self._start_connection_retry_thread()
        
        return True  # 연결 실패해도 True 반환
    
    def stop(self):
        """클라이언트 중지"""
        self.logger.info("Factor 클라이언트 중지 중...")
        self.running = False
        
        # 프린터 연결 해제
        if self.printer_comm:
            self.printer_comm.disconnect()
        
        # 워커 스레드 종료
        for thread in self.worker_threads + self.polling_threads:
            if thread.is_alive():
                thread.join(timeout=5)
        
        # 워치독 중지
        if self.watchdog_thread and self.watchdog_thread.is_alive():
            self.watchdog_thread.join(timeout=5)
        
        self.connected = False
        self._trigger_callback('on_disconnect', None)
        self.logger.info("Factor 클라이언트 중지 완료")
    
    def _signal_handler(self, signum, frame):
        """시그널 핸들러"""
        self.logger.info(f"시그널 수신: {signum}")
        self.stop()
        sys.exit(0)
    
    def _connect_to_printer(self) -> bool:
        """프린터 연결"""
        # 시뮬레이션 모드일 때는 연결하지 않음
        if not self.printer_comm:
            self.logger.info("시뮬레이션 모드 - 프린터 연결 건너뜀")
            return True
            
        self.logger.info("3D 프린터 연결 시도")
        
        try:
            port = self.printer_port if not self.auto_detect else ""
            success = self.printer_comm.connect(
                port=port,
                baudrate=self.printer_baudrate
            )
            
            if success:
                self.logger.info("3D 프린터 연결 성공")
                self.error_count = 0
                return True
            else:
                self.logger.error("3D 프린터 연결 실패")
                return False
                
        except Exception as e:
            self.logger.error(f"프린터 연결 오류: {e}")
            return False
    
    def _start_worker_threads(self):
        """워커 스레드 시작"""
        # 데이터 처리 스레드
        data_thread = threading.Thread(target=self._data_worker, daemon=True)
        data_thread.start()
        self.worker_threads.append(data_thread)
        
        # 시스템 모니터링 스레드
        monitor_thread = threading.Thread(target=self._system_monitor_worker, daemon=True)
        monitor_thread.start()
        self.worker_threads.append(monitor_thread)
        
        self.logger.info("워커 스레드 시작 완료")
    
    def _start_polling_threads(self):
        """폴링 스레드 시작"""
        # 온도 폴링 스레드
        temp_thread = threading.Thread(target=self._temperature_polling_worker, daemon=True)
        temp_thread.start()
        self.polling_threads.append(temp_thread)
        
        # 위치 폴링 스레드
        pos_thread = threading.Thread(target=self._position_polling_worker, daemon=True)
        pos_thread.start()
        self.polling_threads.append(pos_thread)
        
        self.logger.info("폴링 스레드 시작 완료")
    
    def _temperature_polling_worker(self):
        """온도 폴링 워커"""
        while self.running and self.connected:
            try:
                # 온도 정보 요청
                if self.printer_comm:
                    self.printer_comm.send_command("M105")
                time.sleep(self.temp_poll_interval)
                
            except Exception as e:
                self.logger.error(f"온도 폴링 오류: {e}")
                time.sleep(self.temp_poll_interval)
    
    def _position_polling_worker(self):
        """위치 폴링 워커"""
        while self.running and self.connected:
            try:
                # 위치 정보 요청
                if self.printer_comm:
                    self.printer_comm.send_command("M114")
                time.sleep(self.position_poll_interval)
                
            except Exception as e:
                self.logger.error(f"위치 폴링 오류: {e}")
                time.sleep(self.position_poll_interval)
    
    def _start_watchdog(self):
        """워치독 스레드 시작"""
        def watchdog_worker():
            while self.running:
                try:
                    current_time = time.time()
                    time_since_heartbeat = current_time - self.last_heartbeat
                    
                    # 하트비트 상태 상세 로깅
                    self.logger.debug(f"워치독 체크 - 마지막 하트비트: {time_since_heartbeat:.1f}초 전")
                    
                    # 하트비트 타임아웃 체크
                    if time_since_heartbeat > 60:  # 60초 타임아웃
                        self.logger.error(f"하트비트 타임아웃 발생!")
                        self.logger.error(f"  - 마지막 하트비트: {time_since_heartbeat:.1f}초 전")
                        self.logger.error(f"  - 현재 시간: {datetime.fromtimestamp(current_time)}")
                        self.logger.error(f"  - 마지막 하트비트 시간: {datetime.fromtimestamp(self.last_heartbeat)}")
                        
                        # 프린터 연결 상태 상세 정보
                        if self.printer_comm:
                            self.logger.error(f"  - 프린터 통신 상태: {self.printer_comm.connected}")
                            self.logger.error(f"  - 프린터 포트: {self.printer_comm.port}")
                            self.logger.error(f"  - 프린터 상태: {self.printer_comm.state}")
                        else:
                            self.logger.error("  - 프린터 통신 객체가 없음")
                        
                        # 시스템 상태 정보
                        self.logger.error(f"  - 클라이언트 연결 상태: {self.connected}")
                        self.logger.error(f"  - 클라이언트 실행 상태: {self.running}")
                        
                        # 스레드 상태 확인
                        active_threads = [t for t in self.polling_threads if t.is_alive()]
                        self.logger.error(f"  - 활성 폴링 스레드: {len(active_threads)}/{len(self.polling_threads)}")
                        
                        self._handle_error("heartbeat_timeout")
                    
                    # 메모리 사용량 확인
                    memory_percent = psutil.virtual_memory().percent
                    if memory_percent > 90:
                        self.logger.warning(f"메모리 사용량 높음: {memory_percent}%")
                    
                    time.sleep(30)  # 30초마다 확인
                    
                except Exception as e:
                    self.logger.error(f"워치독 오류: {e}", exc_info=True)
                    time.sleep(30)
        
        self.watchdog_thread = threading.Thread(target=watchdog_worker, daemon=True)
        self.watchdog_thread.start()
        self.logger.info("워치독 시작")
    
    def _handle_error(self, error_type: str):
        """오류 처리"""
        self.error_count += 1
        max_errors = self.config.get('system.power_management.max_error_count', 5)
        
        # 오류 발생 시 상세 정보 로깅
        self.logger.error(f"=== 오류 발생 상세 정보 ===")
        self.logger.error(f"오류 타입: {error_type}")
        self.logger.error(f"오류 횟수: {self.error_count}/{max_errors}")
        self.logger.error(f"발생 시간: {datetime.now()}")
        
        # 시스템 상태 상세 정보
        self.logger.error(f"시스템 상태:")
        self.logger.error(f"  - 클라이언트 실행 중: {self.running}")
        self.logger.error(f"  - 프린터 연결됨: {self.connected}")
        self.logger.error(f"  - 마지막 하트비트: {datetime.fromtimestamp(self.last_heartbeat)}")
        
        if self.printer_comm:
            self.logger.error(f"  - 프린터 통신 상태: {self.printer_comm.connected}")
            self.logger.error(f"  - 프린터 포트: {self.printer_comm.port}")
            self.logger.error(f"  - 프린터 상태: {self.printer_comm.state}")
        
        # 스레드 상태
        active_worker_threads = [t for t in self.worker_threads if t.is_alive()]
        active_polling_threads = [t for t in self.polling_threads if t.is_alive()]
        
        self.logger.error(f"스레드 상태:")
        self.logger.error(f"  - 워커 스레드: {len(active_worker_threads)}/{len(self.worker_threads)} 활성")
        self.logger.error(f"  - 폴링 스레드: {len(active_polling_threads)}/{len(self.polling_threads)} 활성")
        
        # 메모리 및 CPU 상태
        try:
            memory = psutil.virtual_memory()
            cpu_percent = psutil.cpu_percent()
            self.logger.error(f"시스템 리소스:")
            self.logger.error(f"  - 메모리 사용률: {memory.percent}%")
            self.logger.error(f"  - CPU 사용률: {cpu_percent}%")
        except Exception as e:
            self.logger.error(f"시스템 리소스 정보 수집 실패: {e}")
        
        self.logger.error(f"================================")
        
        if self.error_count >= max_errors:
            if self.config.get('system.power_management.auto_reboot_on_error', False):
                self.logger.critical("최대 오류 횟수 초과 - 시스템 재부팅")
                os.system('sudo reboot')
            else:
                self.logger.critical("최대 오류 횟수 초과 - 프로그램 종료")
                self.stop()
    
    def _data_worker(self):
        """데이터 처리 워커"""
        while self.running:
            try:
                # 큐에서 데이터 가져오기
                try:
                    data_type, data = self.data_queue.get(timeout=1)
                except Empty:
                    continue
                
                # 데이터 타입별 처리
                if data_type == 'printer_data':
                    self._process_printer_data(data)
                
                self.data_queue.task_done()
                
            except Exception as e:
                self.logger.error(f"데이터 처리 워커 오류: {e}")
    
    def _system_monitor_worker(self):
        """시스템 모니터링 워커"""
        while self.running:
            try:
                # 시스템 정보 수집
                cpu_percent = psutil.cpu_percent(interval=1)
                memory = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                
                # 라즈베리파이 온도 (가능한 경우)
                cpu_temp = 0
                try:
                    with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                        cpu_temp = int(f.read()) / 1000.0
                except:
                    pass
                
                # 시스템 정보 업데이트
                self.system_info = SystemInfo(
                    cpu_usage=cpu_percent,
                    memory_usage=memory.percent,
                    disk_usage=disk.percent,
                    temperature=cpu_temp,
                    uptime=int(time.time() - psutil.boot_time())
                )
                
                # 임계값 확인
                if cpu_percent > 80:
                    self.logger.warning(f"CPU 사용률 높음: {cpu_percent}%")
                if memory.percent > 85:
                    self.logger.warning(f"메모리 사용률 높음: {memory.percent}%")
                if cpu_temp > 70:
                    self.logger.warning(f"CPU 온도 높음: {cpu_temp}°C")
                
                time.sleep(30)  # 30초마다 모니터링
                
            except Exception as e:
                self.logger.error(f"시스템 모니터링 오류: {e}")
                time.sleep(30)
    
    def _process_printer_data(self, data: Dict):
        """프린터 데이터 처리"""
        try:
            # 여기서 추가적인 데이터 처리 로직 구현
            pass
        except Exception as e:
            self.logger.error(f"프린터 데이터 처리 오류: {e}")
    
    # 프린터 콜백 함수들
    def _on_printer_state_change(self, status: PrinterStatus):
        """프린터 상태 변경 콜백"""
        old_state = getattr(self, 'printer_status', None)
        old_state_name = old_state.state if old_state else "None"
        
        self.printer_status = status
        self.last_heartbeat = time.time()
        
        # 하트비트 업데이트 상세 로깅
        self.logger.debug(f"하트비트 업데이트 - 프린터 상태 변경: {old_state_name} → {status.state}")
        self.logger.debug(f"  - 하트비트 시간: {datetime.fromtimestamp(self.last_heartbeat)}")
        self.logger.debug(f"  - 상태 플래그: {status.flags}")
        
        self._trigger_callback('on_printer_state_change', status)
        self.logger.info(f"프린터 상태 변경: {status.state}")
    
    def _on_temperature_update(self, temp_info: TemperatureInfo):
        """온도 업데이트 콜백"""
        # 온도 업데이트 시에도 하트비트 업데이트
        self.last_heartbeat = time.time()
        self.logger.debug(f"하트비트 업데이트 - 온도 업데이트")
        
        self._trigger_callback('on_temperature_update', temp_info)
        self.logger.debug(f"온도 업데이트: {temp_info}")
    
    def _on_position_update(self, position: Position):
        """위치 업데이트 콜백"""
        # 위치 업데이트 시에도 하트비트 업데이트
        self.last_heartbeat = time.time()
        self.logger.debug(f"하트비트 업데이트 - 위치 업데이트")
        
        self.position_data = position
        self._trigger_callback('on_position_update', position)
        self.logger.debug(f"위치 업데이트: {position}")
    
    def _on_gcode_response(self, response):
        """G-code 응답 콜백"""
        # G-code 응답 시에도 하트비트 업데이트
        self.last_heartbeat = time.time()
        self.logger.debug(f"하트비트 업데이트 - G-code 응답")
        
        self._trigger_callback('on_gcode_response', response)
        self._trigger_callback('on_message', response.response)
        self.logger.debug(f"G-code 응답: {response.response}")
    
    def _on_printer_error(self, error_msg: str):
        """프린터 오류 콜백"""
        self._trigger_callback('on_error', error_msg)
        self._handle_error("printer_error")
    
    # 외부 인터페이스 메서드들
    def get_printer_status(self) -> PrinterStatus:
        """프린터 상태 반환"""
        if self.connected and self.printer_comm:
            return self.printer_comm.get_printer_status()
        else:
            return PrinterStatus(
                state="disconnected",
                timestamp=time.time(),
                flags={'connected': False}
            )
    
    def get_temperature_info(self) -> TemperatureInfo:
        """온도 정보 반환"""
        if self.connected and self.printer_comm:
            return self.printer_comm.get_temperature_info()
        else:
            return TemperatureInfo(tool={})
    
    def get_position(self) -> Position:
        """위치 정보 반환"""
        if self.connected and self.printer_comm:
            return self.printer_comm.get_position()
        else:
            return Position(0, 0, 0, 0)
    
    def get_print_progress(self) -> PrintProgress:
        """프린트 진행률 반환"""
        # 3D 프린터에서 직접 진행률 정보를 가져오기 어려움
        # SD 카드 프린팅의 경우 M27 명령으로 가능
        return PrintProgress(
            completion=0,
            file_position=0,
            file_size=0,
            print_time=0,
            print_time_left=0,
            filament_used=0
        )
    
    def get_firmware_info(self) -> FirmwareInfo:
        """펌웨어 정보 반환"""
        if self.connected and self.printer_comm:
            return self.printer_comm.get_firmware_info()
        else:
            return FirmwareInfo()
    
    def get_system_info(self) -> SystemInfo:
        """시스템 정보 반환"""
        return self.system_info
    
    def get_camera_info(self) -> CameraInfo:
        """카메라 정보 반환"""
        # 카메라 정보는 별도 구현 필요
        return self.camera_info
    
    def is_connected(self) -> bool:
        """연결 상태 확인"""
        return bool(self.connected and self.printer_comm and self.printer_comm.connected)
    
    def send_gcode(self, command: str) -> bool:
        """G-code 명령 전송"""
        if not self.connected or not self.printer_comm:
            self.logger.warning("프린터가 연결되지 않음")
            return False
        
        try:
            return self.printer_comm.send_gcode(command) or False
        except Exception as e:
            self.logger.error(f"G-code 전송 오류: {e}")
            return False
    
    def home_axes(self, axes: str = ""):
        """축 홈 이동"""
        if self.connected and self.printer_comm:
            self.printer_comm.home_axes(axes)
    
    def set_temperature(self, tool: int = 0, temp: float = 0):
        """온도 설정"""
        if self.connected and self.printer_comm:
            self.printer_comm.set_temperature(tool, temp)
    
    def move_axis(self, x: Optional[float] = None, y: Optional[float] = None, 
                  z: Optional[float] = None, e: Optional[float] = None, 
                  feedrate: Optional[float] = None):
        """축 이동"""
        if self.connected and self.printer_comm:
            # None 값들을 기본값으로 변환
            x_val = x if x is not None else 0.0
            y_val = y if y is not None else 0.0
            z_val = z if z is not None else 0.0
            e_val = e if e is not None else 0.0
            feedrate_val = feedrate if feedrate is not None else 1000.0
            
            self.printer_comm.move_axis(x_val, y_val, z_val, e_val, feedrate_val)
    
    def emergency_stop(self):
        """비상 정지"""
        if self.connected and self.printer_comm:
            self.printer_comm.emergency_stop()
    
    def get_all_data(self) -> Dict[str, Any]:
        """모든 데이터 반환"""
        return {
            'printer_status': self.get_printer_status(),
            'temperature': self.get_temperature_info(),
            'position': self.get_position(),
            'progress': self.get_print_progress(),
            'firmware': self.get_firmware_info(),
            'system': self.get_system_info(),
            'camera': self.get_camera_info(),
            'connected': self.is_connected(),
            'timestamp': time.time()
        }
    
    def _start_connection_retry_thread(self):
        """연결 재시도 스레드 시작"""
        retry_thread = threading.Thread(target=self._connection_retry_worker, daemon=True)
        retry_thread.start()
        self.worker_threads.append(retry_thread)

    def _connection_retry_worker(self):
        """연결 재시도 워커"""
        while self.running:
            try:
                time.sleep(30)  # 30초마다 재시도
                
                if not self.connected and self.printer_comm:
                    self.logger.info("프린터 연결 재시도 중...")
                    if self._connect_to_printer():
                        self.connected = True
                        self._start_polling_threads()
                        self._trigger_callback('on_connect', None)
                        self.logger.info("프린터 재연결 성공")
                        break
                        
            except Exception as e:
                self.logger.error(f"연결 재시도 오류: {e}")
                time.sleep(5)  # 오류 발생 시 5초 대기 