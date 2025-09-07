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
import random

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
        
        # 폴링 간격
        self.temp_poll_interval = printer_config.get('temp_poll_interval', 5.0)
        self.position_poll_interval = printer_config.get('position_poll_interval', 10.0)
        
        # 프린터 통신 객체 (항상 실제 연결 시도)
        port = self.printer_port if not self.auto_detect else ""
        self.printer_comm = PrinterCommunicator(
            port=port,
            baudrate=self.printer_baudrate
        )
        try:
            # M27 진행률 캐시 공유를 위한 역참조 연결
            setattr(self.printer_comm, 'factor_client', self)
        except Exception:
            pass
        
        # 데이터 저장소(콜백 중심; 큐 제거)
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
        
        # 각 명령별 마지막 응답 시간 추적
        self.last_temperature_response = time.time()  # M105 응답
        self.last_position_response = time.time()    # M114 응답
        self.last_gcode_response = time.time()       # G-code 응답
        self.last_state_response = time.time()       # 상태 변경
        
        # 스레드(큐 삭제)
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
        
        # 프린터 콜백 등록
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
        """
        온도 폴링 워커
        - 오토리포트 우선(M155 S1). 끊기면 재암시 → M105 1회 폴백 → 폴링 전환
        - 정확한 주기 유지(monotonic + 처리시간 보정)
        - 약간의 지터로 경합 완화
        """
        interval = float(getattr(self, "temp_poll_interval", 1.0))
        fresh_thresh = max(2.0, 3.0 * interval)   # 최근 응답 허용 시간
        reassert_gap = max(3.0, 2.0 * interval)   # 재암시 간격
        last_reassert = 0.0
        next_ts = time.monotonic()

        while self.running and self.connected:
            now = time.time()
            try:
                auto_report = bool(self.config.get('data_collection.auto_report', False))
                # 최근 온도 응답이 신선한가?
                last_rx = float(getattr(self, "last_temperature_response", 0.0) or 0.0)
                fresh = (now - last_rx) <= fresh_thresh

                if auto_report:
                    if not fresh:
                        # 오토리포트가 켜져있다고 믿는데 데이터가 끊김 → 재암시 시도
                        if (now - last_reassert) > reassert_gap and self.printer_comm and self.printer_comm.connected:
                            self.logger.debug("[temp] auto-report stale → M155 S1 re-assert")
                            try:
                                self.printer_comm.send_command("M155 S1")
                            except Exception:
                                pass
                            last_reassert = now

                        # 빠른 복구를 위해 1회 폴백 질의
                        if self.printer_comm and self.printer_comm.connected:
                            self.printer_comm.send_command("M105")
                        # 연속으로 오래 끊기면 폴링 모드로 전환
                        if (now - last_rx) > (2.0 * fresh_thresh):
                            self.logger.warning("[temp] auto-report fallback → polling mode")
                            try:
                                self.config.set("data_collection.auto_report", False)
                            except Exception:
                                pass
                    # fresh면 아무 것도 안 보냄 (오토리포트 수신 대기)
                else:
                    # 폴링 모드
                    if self.printer_comm and self.printer_comm.connected:
                        self.printer_comm.send_command("M105")

            except Exception as e:
                self.logger.warning(f"[temp-poll] error: {e}", exc_info=True)

            # 다음 틱 예약(정확 주기 + 지터)
            next_ts += interval
            sleep_for = max(0.0, next_ts - time.monotonic()) + random.uniform(0, min(0.05, 0.2 * interval))
            # 루프 종료 신호/상태 확인
            if sleep_for > 0:
                time.sleep(sleep_for)
            if not (self.running and self.connected):
                break

    
    def _position_polling_worker(self):
        """
        위치 폴링 워커
        - 오토리포트가 True면 우선 스킵.
        * 끊기면 M154 S1(지원 시) 재암시 → M114 1회 폴백 → 계속 끊기면 폴링 모드 유지
        - 정확한 주기 유지(monotonic + 처리시간 보정)
        """
        interval = float(getattr(self, "position_poll_interval", 2.0))
        fresh_thresh = max(2.0, 3.0 * interval)
        reassert_gap = max(3.0, 2.0 * interval)
        last_reassert = 0.0
        next_ts = time.monotonic()

        while self.running and self.connected:
            now = time.time()
            try:
                auto_report = bool(self.config.get('data_collection.auto_report', False))
                last_rx = float(getattr(self, "last_position_response", 0.0) or 0.0)
                fresh = (now - last_rx) <= fresh_thresh

                if auto_report:
                    if not fresh:
                        # 위치 오토리포트 재암시 (펌웨어가 지원할 때만; 미지원이어도 무해)
                        if (now - last_reassert) > reassert_gap and self.printer_comm and self.printer_comm.connected:
                            self.logger.debug("[pos] auto-report stale → try M154 S1 (if supported)")
                            try:
                                self.printer_comm.send_command("M154 S1")
                            except Exception:
                                pass
                            last_reassert = now

                        # 1회 폴백 질의
                        if self.printer_comm and self.printer_comm.connected:
                            self.printer_comm.send_command("M114")

                        # 온도와 달리 위치는 상시 오토리포트 미보장 → 굳이 auto_report False로 내리지 않음
                        # (온도 auto_report는 유지하면서 위치만 폴링으로 커버)
                else:
                    if self.printer_comm and self.printer_comm.connected:
                        self.printer_comm.send_command("M114")

            except Exception as e:
                self.logger.error(f"[pos-poll] error: {e}", exc_info=True)

            # 다음 틱 예약(정확 주기 + 지터)
            next_ts += interval
            sleep_for = max(0.0, next_ts - time.monotonic()) + random.uniform(0, min(0.05, 0.2 * interval))
            if sleep_for > 0:
                time.sleep(sleep_for)
            if not (self.running and self.connected):
                break
    
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
                        
                        # 각 명령별 응답 시간 분석
                        temp_time_since = current_time - self.last_temperature_response
                        pos_time_since = current_time - self.last_position_response
                        gcode_time_since = current_time - self.last_gcode_response
                        state_time_since = current_time - self.last_state_response
                        
                        self.logger.error(f"  - 명령별 응답 상태:")
                        self.logger.error(f"    * M105 (온도): {temp_time_since:.1f}초 전")
                        self.logger.error(f"    * M114 (위치): {pos_time_since:.1f}초 전")
                        self.logger.error(f"    * G-code: {gcode_time_since:.1f}초 전")
                        self.logger.error(f"    * 상태 변경: {state_time_since:.1f}초 전")
                        
                        # 가장 오래된 응답 찾기
                        oldest_response = min(temp_time_since, pos_time_since, gcode_time_since, state_time_since)
                        if oldest_response == temp_time_since:
                            self.logger.error(f"  - 하트비트 타임아웃 원인: M105 (온도) 명령에 응답하지 않음")
                        elif oldest_response == pos_time_since:
                            self.logger.error(f"  - 하트비트 타임아웃 원인: M114 (위치) 명령에 응답하지 않음")
                        elif oldest_response == gcode_time_since:
                            self.logger.error(f"  - 하트비트 타임아웃 원인: G-code 명령에 응답하지 않음")
                        else:
                            self.logger.error(f"  - 하트비트 타임아웃 원인: 프린터 상태 변경에 응답하지 않음")
                        
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
                        
                        # 하트비트 타임아웃 복구 시도
                        self._attempt_heartbeat_recovery()
                        
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
    
    def _attempt_heartbeat_recovery(self):
        """하트비트 타임아웃 복구 시도"""
        try:
            self.logger.info("하트비트 복구 시도 시작")
            
            # 1. 프린터 통신 상태 확인
            if not self.printer_comm or not self.printer_comm.connected:
                self.logger.warning("프린터 통신이 끊어짐 - 재연결 시도")
                self._reconnect_printer()
                return
            
            # 2. 프린터 상태 확인 명령 전송
            self.logger.info("프린터 상태 확인 명령 전송 (M115)")
            self.printer_comm.send_command("M115")
            
            # 3. 온도 확인 명령 전송
            self.logger.info("온도 확인 명령 전송 (M105)")
            self.printer_comm.send_command("M105")
            
            # 4. 위치 확인 명령 전송
            self.logger.info("위치 확인 명령 전송 (M114)")
            self.printer_comm.send_command("M114")
            
            # 5. 짧은 대기 후 응답 확인
            time.sleep(2)
            
            # 6. 응답이 있었는지 확인
            current_time = time.time()
            temp_time_since = current_time - self.last_temperature_response
            pos_time_since = current_time - self.last_position_response
            gcode_time_since = current_time - self.last_gcode_response
            
            if temp_time_since < 5 or pos_time_since < 5 or gcode_time_since < 5:
                self.logger.info("하트비트 복구 성공 - 프린터 응답 확인됨")
                # 하트비트 시간 업데이트
                self.last_heartbeat = current_time
                self.last_temperature_response = current_time
                self.last_position_response = current_time
                self.last_gcode_response = current_time
                self.last_state_response = current_time
                # 에러 카운트 리셋
                self.error_count = 0
                self.logger.info("하트비트 시간 초기화 완료 및 에러 카운트 리셋")
            else:
                self.logger.warning("하트비트 복구 실패 - 프린터 응답 없음")
                
        except Exception as e:
            self.logger.error(f"하트비트 복구 시도 중 오류: {e}")
    
    def _reconnect_printer(self):
        """프린터 재연결 시도"""
        try:
            self.logger.info("프린터 재연결 시도")
            
            # 기존 연결 종료
            if self.printer_comm:
                self.printer_comm.disconnect()
                time.sleep(1)
            
            # 새 연결 시도
            self.printer_comm = PrinterCommunicator(self.config)
            self.printer_comm.connect()
            
            if self.printer_comm.connected:
                self.logger.info("프린터 재연결 성공")
                # 하트비트 시간 초기화
                current_time = time.time()
                self.last_heartbeat = current_time
                self.last_temperature_response = current_time
                self.last_position_response = current_time
                self.last_gcode_response = current_time
                self.last_state_response = current_time
                # 에러 카운트 리셋
                self.error_count = 0
                self.logger.info("프린터 재연결 완료 및 에러 카운트 리셋")
            else:
                self.logger.error("프린터 재연결 실패")
                
        except Exception as e:
            self.logger.error(f"프린터 재연결 중 오류: {e}")
    
    
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
                
                # 설정에서 임계값 가져오기
                cpu_threshold = self.config.get('monitoring.cpu_threshold', 80)
                memory_threshold = self.config.get('monitoring.memory_threshold', 85)
                temp_threshold = self.config.get('monitoring.temperature_threshold', 70)
                
                # 임계값 확인
                if cpu_percent > cpu_threshold:
                    self.logger.warning(f"CPU 사용률 높음: {cpu_percent}% (임계값: {cpu_threshold}%)")
                if memory.percent > memory_threshold:
                    self.logger.warning(f"메모리 사용률 높음: {memory.percent}% (임계값: {memory_threshold}%)")
                if cpu_temp > temp_threshold:
                    self.logger.warning(f"CPU 온도 높음: {cpu_temp}°C (임계값: {temp_threshold}°C)")
                
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
        self.last_state_response = time.time()  # 상태 변경 응답 시간 업데이트
        
        # 하트비트 업데이트 상세 로깅 (DEBUG 레벨로 변경)
        self.logger.debug(f"하트비트 업데이트 - 프린터 상태 변경: {old_state_name} → {status.state}")
        self.logger.debug(f"  - 하트비트 시간: {datetime.fromtimestamp(self.last_heartbeat)}")
        self.logger.debug(f"  - 상태 플래그: {status.flags}")
        self.logger.debug(f"  - 트리거: 프린터 상태 변경 콜백")
        
        self._trigger_callback('on_printer_state_change', status)
        self.logger.info(f"프린터 상태 변경: {status.state}")
    
    def _on_temperature_update(self, temp_info: TemperatureInfo):
        """온도 업데이트 콜백"""
        # 온도 업데이트 시에도 하트비트 업데이트
        self.last_heartbeat = time.time()
        self.last_temperature_response = time.time()  # M105 응답 시간 업데이트
        
        # 하트비트 업데이트 상세 로깅 (DEBUG 레벨로 변경)
        self.logger.debug(f"하트비트 업데이트 - 온도 업데이트")
        self.logger.debug(f"  - 하트비트 시간: {datetime.fromtimestamp(self.last_heartbeat)}")
        self.logger.debug(f"  - 온도 정보: {temp_info}")
        self.logger.debug(f"  - 트리거: 온도 업데이트 콜백 (M105 응답)")
        
        self._trigger_callback('on_temperature_update', temp_info)
        self.logger.debug(f"온도 업데이트: {temp_info}")
    
    def _on_position_update(self, position: Position):
        """위치 업데이트 콜백"""
        # 위치 업데이트 시에도 하트비트 업데이트
        self.last_heartbeat = time.time()
        self.last_position_response = time.time()  # M114 응답 시간 업데이트
        
        # 하트비트 업데이트 상세 로깅 (DEBUG 레벨로 변경)
        self.logger.debug(f"하트비트 업데이트 - 위치 업데이트")
        self.logger.debug(f"  - 하트비트 시간: {datetime.fromtimestamp(self.last_heartbeat)}")
        self.logger.debug(f"  - 위치 정보: {position}")
        self.logger.debug(f"  - 트리거: 위치 업데이트 콜백 (M114 응답)")
        
        self.position_data = position
        self._trigger_callback('on_position_update', position)
        self.logger.debug(f"위치 업데이트: {position}")
    
    def _on_gcode_response(self, response):
        """G-code 응답 콜백"""
        # G-code 응답 시에도 하트비트 업데이트
        self.last_heartbeat = time.time()
        self.last_gcode_response = time.time()  # G-code 응답 시간 업데이트
        
        # 하트비트 업데이트 상세 로깅 (DEBUG 레벨로 변경)
        self.logger.debug(f"하트비트 업데이트 - G-code 응답")
        self.logger.debug(f"  - 하트비트 시간: {datetime.fromtimestamp(self.last_heartbeat)}")
        self.logger.debug(f"  - G-code 응답: {response.response}")
        self.logger.debug(f"  - 트리거: G-code 응답 콜백")
        
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
        try:
            if self.connected and self.printer_comm:
                cache = getattr(self, '_sd_progress_cache', None)
                if cache:
                    printed = int(cache.get('printed_bytes') or 0)
                    total = int(cache.get('total_bytes') or 0)
                    completion_pct = float(cache.get('completion') or 0.0)
                    completion_ratio = max(0.0, min(1.0, completion_pct / 100.0))
                    return PrintProgress(
                        active=bool(cache.get('active')),
                        completion=completion_ratio,
                        file_position=printed,
                        file_size=total,
                        print_time=None,
                        print_time_left=cache.get('eta_sec'),
                        filament_used=None
                    )
                # 캐시가 없으면 1회 조회 트리거
                try:
                    self.printer_comm.send_command("M27")
                except Exception:
                    pass
        except Exception:
            pass
        # 기본값
        return PrintProgress(active=False, completion=0.0, file_position=0, file_size=0)
    
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