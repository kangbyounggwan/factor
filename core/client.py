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
import glob
import subprocess
import os
import random

from .data_models import *
from .config_manager import ConfigManager
from .logger import get_logger, PerformanceLogger
from .printer_comm import PrinterCommunicator, PrinterState
from .eta_estimator import EtaEstimator


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
        self.temp_poll_interval = printer_config.get('temp_poll_interval', 1.0)
        self.position_poll_interval = printer_config.get('position_poll_interval', 1.0)
        
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
        
        # 워치독 설정(비활성화) 및 RX 가디언 초기화
        self.watchdog_enabled = False
        self.watchdog_thread = None
        self._rx_guard_running = False
        self._rx_guard_thread = None
        
        # 자동리포트 모니터 상태
        self._arm_running = False
        self._arm_thread = None
        self._last_toggle_ts_temp = 0.0
        self._last_toggle_ts_pos = 0.0
        self._last_temp_rounded = None
        self._last_pos_rounded = None
        self._last_temp_update_ts = 0.0
        self._last_pos_update_ts = 0.0

        # 오토리포트 지원/폴링 스레드 상태 (개별 코드별로 관리)
        self.M155_auto_supported: Optional[bool] = None  # 온도 자동리포트
        self.M154_auto_supported: Optional[bool] = None  # 위치 자동리포트
        self.M27_auto_supported: Optional[bool] = None   # 진행률 자동리포트
        self._temp_poll_thread: Optional[threading.Thread] = None
        self._pos_poll_thread: Optional[threading.Thread] = None
        self._m27_poll_thread: Optional[threading.Thread] = None
        # M27 ETA 추정기
        try:
            self.m27_eta = EtaEstimator(half_life_s=20.0)
        except Exception:
            self.m27_eta = None

        # sudo 비밀번호(재부팅 시도에 사용; 환경변수로 주입 권장)
        try:
            self.sudo_password = os.environ.get('SUDO_PASSWORD')
        except Exception:
            self.sudo_password = None

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
        
        # RX 가디언 시작(워커 무정지 보장)
        try:
            self._start_rx_guardian()
        except Exception:
            pass
        
        # 프린터 연결 시도 (실패해도 계속 실행)
        if self._connect_to_printer():
            self.connected = True
            # 시작 즉시 자동리포트 시도(S1). 미지원이면 폴링으로 전환
            try:
                if self.printer_comm and self.printer_comm.connected:
                    # 통합 함수로 설정
                    self._setup_reporting_modes()
            except Exception:
                pass

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
        # RX 가디언 중지
        try:
            self._stop_rx_guardian()
        except Exception:
            pass
        
        # 프린터 연결 해제
        if self.printer_comm:
            self.printer_comm.disconnect()
        
        # 워커 스레드 종료
        for thread in self.worker_threads + self.polling_threads:
            if thread.is_alive():
                thread.join(timeout=5)
        
        # 워치독 사용 안 함
        
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

    def _start_rx_guardian(self):
        """RX 가디언: 읽기 워커가 멈추지 않도록 강제 유지"""
        if self._rx_guard_running:
            return
        self._rx_guard_running = True
        def _guard():
            while self._rx_guard_running:
                try:
                    pc = getattr(self, 'printer_comm', None)
                    if not pc:
                        time.sleep(0.1)
                        continue
                    # 절대 멈춤 방지: rx_paused/sync_mode 해제
                    try:
                        setattr(pc, 'rx_paused', False)
                        pc.sync_mode = False
                    except Exception:
                        pass
                    # read_thread 재기동
                    try:
                        if pc.connected and pc.serial_conn and pc.serial_conn.is_open:
                            if not (pc.read_thread and pc.read_thread.is_alive()):
                                pc.read_thread = threading.Thread(target=pc._read_worker, daemon=True)
                                pc.read_thread.start()
                    except Exception:
                        pass
                except Exception:
                    pass
                time.sleep(0.05)
        self._rx_guard_thread = threading.Thread(target=_guard, daemon=True)
        self._rx_guard_thread.start()

    def _stop_rx_guardian(self):
        self._rx_guard_running = False
        try:
            if self._rx_guard_thread and self._rx_guard_thread.is_alive():
                self._rx_guard_thread.join(timeout=0.2)
        except Exception:
            pass
        self._rx_guard_thread = None

    def _start_autoreport_monitor(self):
        if self._arm_running:
            return
        self._arm_running = True
        def _worker():
            STALE_SEC_TEMP = 4.0
            STALE_SEC_POS  = 6.0
            TOGGLE_COOLDOWN = 5.0
            CHECK_INTERVAL = 0.5
            while self._arm_running and self.connected:
                now = time.time()
                try:
                    pc = getattr(self, 'printer_comm', None)
                    if not (pc and pc.connected):
                        time.sleep(CHECK_INTERVAL); continue

                    # 온도: 자동리포트 지원 시에만 토글, 미지원이면 폴링 스레드가 담당
                    if bool(self.M155_auto_supported) and ((now - self._last_temp_update_ts) > STALE_SEC_TEMP) and ((now - self._last_toggle_ts_temp) > TOGGLE_COOLDOWN):
                        try:
                            pc.send_command("M155 S0"); time.sleep(0.1)
                            pc.send_command("M155 S1")
                        except Exception:
                            pass
                        self._last_toggle_ts_temp = now

                    # 위치: 자동리포트 지원 시에만 토글, 미지원이면 폴링 스레드가 담당
                    if bool(self.M154_auto_supported) and ((now - self._last_pos_update_ts) > STALE_SEC_POS) and ((now - self._last_toggle_ts_pos) > TOGGLE_COOLDOWN):
                        try:
                            pc.send_command("M154 S0"); time.sleep(0.1)
                            pc.send_command("M154 S1")
                        except Exception:
                            pass
                        self._last_toggle_ts_pos = now

                except Exception:
                    pass
                time.sleep(CHECK_INTERVAL)
        self._arm_thread = threading.Thread(target=_worker, daemon=True)
        self._arm_thread.start()

    def _stop_autoreport_monitor(self):
        self._arm_running = False
        try:
            if self._arm_thread and self._arm_thread.is_alive():
                self._arm_thread.join(timeout=0.2)
        except Exception:
            pass
        self._arm_thread = None

    # ===== Fallback polling workers (자동리포트 미지원 시만 사용) =====
    def _fallback_temp_poll_worker(self):
        """온도 폴링(자동리포트가 미지원인 펌웨어용)"""
        interval = float(getattr(self, "temp_poll_interval", 1.0))
        next_ts = time.monotonic()
        while self.running and self.connected and (self.M155_auto_supported is False):
            try:
                if self.printer_comm and self.printer_comm.connected:
                    # 동기 조회로 확실히 응답을 받고 파싱 반영
                    ti = None
                    try:
                        ti = self.printer_comm.collector.get_temperature_info()
                    except Exception:
                        ti = None
                    if ti is not None:
                        try:
                            self.logger.info(f"[TEMP_POLL] {ti.to_dict()}")
                        except Exception:
                            pass
                    else:
                        try:
                            self.logger.info("[TEMP_POLL] no response")
                        except Exception:
                            pass
            except Exception:
                pass
            next_ts += interval
            sleep_for = max(0.0, next_ts - time.monotonic())
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _fallback_pos_poll_worker(self):
        """위치 폴링(자동리포트가 미지원인 펌웨어용)"""
        interval = float(getattr(self, "position_poll_interval", 1.0))
        next_ts = time.monotonic()
        while self.running and self.connected and (self.M154_auto_supported is False):
            try:
                if self.printer_comm and self.printer_comm.connected:
                    # 동기 조회로 확실히 응답을 받고 파싱 반영
                    p = None
                    try:
                        p = self.printer_comm.collector.get_position()
                    except Exception:
                        p = None
                    # 폴링 로그 제거 (기능만 유지)
                    _ = p is not None
            except Exception:
                pass
            next_ts += interval
            sleep_for = max(0.0, next_ts - time.monotonic())
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _fallback_m27_poll_worker(self):
        """M27 진행률 폴링(오토리포트 미사용, 동기 조회)"""
        interval = 3.0  # 3초 주기
        next_ts = time.monotonic()
        while self.running and self.connected:
            try:
                if self.printer_comm and self.printer_comm.connected:
                    try:
                        r = self.printer_comm.send_command_and_wait("M27", timeout=2.0)
                        if r:
                            # 폴링 로그 제거 (기능만 유지)
                            _ = r
                            # ETA 추정기 업데이트 및 진행률 캐시에 반영
                            try:
                                eta = getattr(self, 'm27_eta', None)
                                if eta:
                                    from .eta_estimator import parse_m27
                                    parsed = parse_m27(r)
                                    if parsed:
                                        res = eta.update_bytes(*parsed)
                                        cache = getattr(self, '_sd_progress_cache', {}) or {}
                                        cache.update({
                                            'active': True,
                                            'completion': res.progress,
                                            'printed_bytes': parsed[0],
                                            'total_bytes': parsed[1],
                                            'eta_sec': res.remaining_s,
                                            'last_update': time.time(),
                                            'source': 'sd'
                                        })
                                        setattr(self, '_sd_progress_cache', cache)
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass
            next_ts += interval
            sleep_for = max(0.0, next_ts - time.monotonic())
            if sleep_for > 0:
                time.sleep(sleep_for)

    def _setup_reporting_modes(self) -> None:
        """연결 직후 자동리포트 지원 여부를 확인하고, 모니터/폴링을 설정한다."""
        try:
            if not (self.printer_comm and self.printer_comm.connected):
                return
            # 온도 자동리포트 (M155)
            self.M155_auto_supported = None
            try:
                r = self.printer_comm.send_command_and_wait("M155 S1", timeout=2.0)
                rl = ((r or "").strip().lower())
                # 명시적으로 ok가 있을 때만 지원으로 간주
                self.M155_auto_supported = ("unknown command" not in rl) and ("ok" in rl)
                self.logger.info(f"M155 S1 support={self.M155_auto_supported} resp={r!r}")
            except Exception as e:
                self.M155_auto_supported = False
                self.logger.info(f"M155 S1 지원 안 함/오류: {e}")

            # 위치 자동리포트 (M154)
            self.M154_auto_supported = None
            try:
                r2 = self.printer_comm.send_command_and_wait("M154 S1", timeout=2.0)
                r2l = ((r2 or "").strip().lower())
                # 위치도 명시적으로 ok가 있을 때만 지원으로 간주
                self.M154_auto_supported = ("unknown command" not in r2l) and ("ok" in r2l)
                self.logger.info(f"M154 S1 support={self.M154_auto_supported} resp={r2!r}")
            except Exception as e:
                self.M154_auto_supported = False
                self.logger.info(f"M154 S1 지원 안 함/오류: {e}")

            # SD 진행률 자동리포트(M27 S5)는 사용하지 않음. 동기 조회로 대체.
            self.M27_auto_supported = False

            # 미지원 항목 폴링 스레드 시작
            if self.M155_auto_supported is False and self._temp_poll_thread is None:
                try:
                    self._temp_poll_thread = threading.Thread(target=self._fallback_temp_poll_worker, daemon=True)
                    self._temp_poll_thread.start()
                except Exception:
                    pass
            if self.M154_auto_supported is False and self._pos_poll_thread is None:
                try:
                    self._pos_poll_thread = threading.Thread(target=self._fallback_pos_poll_worker, daemon=True)
                    self._pos_poll_thread.start()
                except Exception:
                    pass
            # M27은 오토리포트 사용하지 않고 항상 1초 주기 동기 폴링
            if self._m27_poll_thread is None:
                try:
                    self._m27_poll_thread = threading.Thread(target=self._fallback_m27_poll_worker, daemon=True)
                    self._m27_poll_thread.start()
                except Exception:
                    pass
            # 요약 로그
            try:
                self.logger.info(
                    f"[AUTOREPORT_SUMMARY] M155={self.M155_auto_supported} M154={self.M154_auto_supported} M27={self.M27_auto_supported} "
                    f"poll_temp={'on' if (self.M155_auto_supported is False) else 'off'} "
                    f"poll_pos={'on' if (self.M154_auto_supported is False) else 'off'}"
                )
            except Exception:
                pass
        except Exception as e:
            self.logger.debug(f"_setup_reporting_modes 오류: {e}")
    
    
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
        # 하트비트 의존 제거
        
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
            # 재부팅 로직 일시 비활성화
            # if self.config.get('system.power_management.auto_reboot_on_error', False):
            #     self.logger.critical("최대 오류 횟수 초과 - 시스템 재부팅")
            #     os.system('sudo reboot')
            # else:
            self.logger.critical("최대 오류 횟수 초과 - 프로그램 종료")
            self.stop()
    
    # _attempt_heartbeat_recovery 제거됨 (하트비트 복구 미사용)
    
    def _reconnect_printer(self):
        """프린터 재연결 시도"""
        try:
            self.logger.info("프린터 재연결 시도")
            
            # 기존 연결 종료
            if self.printer_comm:
                self.printer_comm.disconnect()
                time.sleep(1)
            
            # 기본 경로 시도 없이: 먼저 /dev/ttyUSB* 스캔 후 결과로만 연결 시도
            ok = False
            self.logger.info("포트 스캔 시작: /dev/ttyUSB*")
            # ls -l /dev/ttyUSB* 로그 덤프(가능 시)
            try:
                cmd = ["bash", "-lc", "ls -l /dev/ttyUSB* 2>/dev/null || true"]
                self.logger.info(f"[PORT_SCAN_CMD] {' '.join(cmd)}")
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                self.logger.info(f"[PORT_SCAN_RC] {proc.returncode}")
                if proc.stdout:
                    self.logger.info(f"[PORT_SCAN_STDOUT]\n{proc.stdout.rstrip()}\n[PORT_SCAN_END]")
                else:
                    self.logger.info("[PORT_SCAN_STDOUT] <empty>")
                if proc.stderr:
                    self.logger.info(f"[PORT_SCAN_STDERR]\n{proc.stderr.rstrip()}\n[PORT_SCAN_END]")
                else:
                    self.logger.info("[PORT_SCAN_STDERR] <empty>")
            except Exception as e:
                self.logger.debug(f"ls -l 실행 불가/무시: {e}")

            # 파이썬 glob으로 후보 수집(ttyUSB만)
            candidates = []
            try:
                candidates.extend(sorted(glob.glob("/dev/ttyUSB*")))
            except Exception:
                candidates = []

            tried = set()
            for dev in candidates:
                if dev in tried:
                    continue
                tried.add(dev)
                try:
                    self.logger.info(f"포트 재연결 시도: {dev}@{self.printer_baudrate}")
                    ok = self.printer_comm.connect(port=dev, baudrate=self.printer_baudrate)
                    if ok:
                        self.logger.info(f"프린터 재연결 성공: {dev}")
                        break
                except Exception as e:
                    self.logger.warning(f"포트 {dev} 연결 실패: {e}")

            if ok and self.printer_comm and self.printer_comm.connected:
                # 성공 후 자동리포트/폴링 모드 설정
                try:
                    self._setup_reporting_modes()
                except Exception:
                    pass

                self.connected = True
                self.error_count = 0
                self.logger.info("프린터 재연결 완료 및 에러 카운트 리셋")
            else:
                self.connected = False
                self.logger.error("프린터 재연결 실패 → 5회 재시도 후 재부팅 예정")
                # 5회 재시도(총 25초) 후 실패 시 리부팅
                try:
                    total_ok = False
                    for attempt in range(1, 6):
                        self.logger.info(f"[RECONNECT] 추가 시도 {attempt}/5")
                        # 짧은 대기
                        try:
                            time.sleep(5)
                        except Exception:
                            pass
                        # 재스캔 및 연결 시도
                        ok2 = False
                        try:
                            cmd = ["bash", "-lc", "ls -l /dev/ttyUSB* 2>/dev/null || true"]
                            self.logger.info(f"[PORT_SCAN_CMD] {' '.join(cmd)}")
                            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                            self.logger.info(f"[PORT_SCAN_RC] {proc.returncode}")
                            if proc.stdout:
                                self.logger.info(f"[PORT_SCAN_STDOUT]\n{proc.stdout.rstrip()}\n[PORT_SCAN_END]")
                            else:
                                self.logger.info("[PORT_SCAN_STDOUT] <empty>")
                            if proc.stderr:
                                self.logger.info(f"[PORT_SCAN_STDERR]\n{proc.stderr.rstrip()}\n[PORT_SCAN_END]")
                            else:
                                self.logger.info("[PORT_SCAN_STDERR] <empty>")
                        except Exception as e:
                            self.logger.debug(f"ls -l 실행 불가/무시: {e}")

                        candidates2 = []
                        try:
                            candidates2.extend(sorted(glob.glob("/dev/ttyUSB*")))
                        except Exception:
                            candidates2 = []
                        tried2 = set()
                        for dev in candidates2:
                            if dev in tried2:
                                continue
                            tried2.add(dev)
                            try:
                                self.logger.info(f"포트 재연결 시도: {dev}@{self.printer_baudrate}")
                                ok2 = self.printer_comm.connect(port=dev, baudrate=self.printer_baudrate)
                                if ok2:
                                    self.logger.info(f"프린터 재연결 성공: {dev}")
                                    total_ok = True
                                    break
                            except Exception as e:
                                self.logger.warning(f"포트 {dev} 연결 실패: {e}")
                                
                        if total_ok and self.printer_comm and self.printer_comm.connected:
                            self.connected = True
                            # 자동리포트/폴링 모드 설정
                            try:
                                self._setup_reporting_modes()
                            except Exception:
                                pass
                            self.error_count = 0
                            self.logger.info("프린터 재연결 완료 및 에러 카운트 리셋")
                            return

                    # 여기까지 오면 실패 → 재부팅 로직 주석 처리
                    self.logger.error("[RECONNECT] 5회 재연결 실패 (재부팅 로직 비활성화)")
                except Exception as e:
                    self.logger.error(f"추가 재시도 루프 오류: {e}")
                
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
        # 하트비트 의존 제거
        
        self._trigger_callback('on_printer_state_change', status)
        self.logger.info(f"프린터 상태 변경: {status.state}")
    
    def _on_temperature_update(self, temp_info: TemperatureInfo):
        """온도 업데이트 콜백"""
        # 소수점 둘째 자리 정규화 + 최근값/시각 기록(모니터 참고용)
        try:
            tool0 = None
            if isinstance(temp_info.tool, dict) and temp_info.tool:
                first_key = list(temp_info.tool.keys())[0]
                t = temp_info.tool[first_key]
                tool0 = (
                    round(float(getattr(t, 'actual', 0.0)), 2),
                    round(float(getattr(t, 'target', 0.0)), 2),
                )
            self._last_temp_rounded = tool0
        except Exception:
            pass
        self._last_temp_update_ts = time.time()

        self._trigger_callback('on_temperature_update', temp_info)
        self.logger.debug(f"온도 업데이트: {temp_info}")
    
    def _on_position_update(self, position: Position):
        """위치 업데이트 콜백"""
        # 소수점 둘째 자리 정규화 + 최근값/시각 기록(모니터 참고용)
        try:
            pr = (
                round(float(position.x), 2),
                round(float(position.y), 2),
                round(float(position.z), 2),
                round(float(position.e), 2),
            )
            self._last_pos_rounded = pr
        except Exception:
            pass
        self._last_pos_update_ts = time.time()

        self.position_data = position
        self._trigger_callback('on_position_update', position)
        self.logger.debug(f"위치 업데이트: {position}")
    
    def _on_gcode_response(self, response):
        """G-code 응답 콜백"""
        # 하트비트 의존 제거
        
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
            ti = self.printer_comm.get_temperature_info()
            # 소수점 둘째 자리 정규화
            try:
                rounded_tools = {}
                for k, v in (ti.tool or {}).items():
                    rounded_tools[k] = TemperatureData(
                        actual=round(float(getattr(v, 'actual', 0.0)), 2),
                        target=round(float(getattr(v, 'target', 0.0)), 2),
                        offset=round(float(getattr(v, 'offset', 0.0)), 2),
                    )
                bed = ti.bed
                bed_r = TemperatureData(
                    actual=round(float(bed.actual), 2),
                    target=round(float(bed.target), 2),
                    offset=round(float(getattr(bed, 'offset', 0.0)), 2),
                ) if bed else None
                chamber = ti.chamber
                chamber_r = TemperatureData(
                    actual=round(float(chamber.actual), 2),
                    target=round(float(chamber.target), 2),
                    offset=round(float(getattr(chamber, 'offset', 0.0)), 2),
                ) if chamber else None
                return TemperatureInfo(tool=rounded_tools, bed=bed_r, chamber=chamber_r)
            except Exception:
                return ti
        else:
            return TemperatureInfo(tool={})
    
    def get_position(self) -> Position:
        """위치 정보 반환"""
        if self.connected and self.printer_comm:
            p = self.printer_comm.get_position()
            try:
                return Position(
                    x=round(float(p.x), 2),
                    y=round(float(p.y), 2),
                    z=round(float(p.z), 2),
                    e=round(float(p.e), 2),
                )
            except Exception:
                return p
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
                    # 새 재연결 로직 사용: ls -l /dev/ttyUSB* 스캔 후 순차 연결 시도
                    try:
                        self._reconnect_printer()
                    except Exception as e:
                        self.logger.error(f"재연결 로직 오류: {e}")
                    if self.connected:
                        try:
                            self._trigger_callback('on_connect', None)
                        except Exception:
                            pass
                        self.logger.info("프린터 재연결 성공")
                        break
                        
            except Exception as e:
                self.logger.error(f"연결 재시도 오류: {e}")
                time.sleep(5)  # 오류 발생 시 5초 대기 