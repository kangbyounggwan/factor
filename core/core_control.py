import re
import time
import threading
from enum import Enum
from typing import Optional, Callable, TYPE_CHECKING
if TYPE_CHECKING:
    from .printer_comm import PrinterCommunicator

try:
    _HAS_SERIAL_ASYNCIO = False  # 스트리밍/비동기 경로 제거
except Exception:
    _HAS_SERIAL_ASYNCIO = False

import serial  # Fallback 동기 시리얼 용


class ControlModule:
    """
    프린터 제어(연결/송신) 로직 모듈

    - 역할: 시리얼 연결/해제, G-code 송신(비동기 프로세스 또는 동기 Fallback), ack 대기 제어
    - 예상데이터:
      - 입력: 포트 문자열, 보드레이트(int), G-code 문자열, timeout(float)
      - 출력: 연결 결과(bool), 전송 결과(bool), 동기 전송 결과(str | None)
    - 사용페이지/위치:
      - 웹: `web/app.py` SocketIO `send_gcode`, REST `/api/printer/command`, `/ufp`
      - 코어: `FactorClient.send_gcode`, `PrinterCommunicator` 내부에서 호출
    """

    def __init__(self, pc: "PrinterCommunicator"):
        self.pc = pc
        # 단계 추적기 초기화
        try:
            self.pc.phase_tracker = _PhaseTracker()
        except Exception:
            self.pc.phase_tracker = None

    # ===== 연결/해제 =====
    def connect(self, port: Optional[str] = None, baudrate: Optional[int] = None) -> bool:
        """
        프린터 시리얼 연결을 수행

        - 역할: 포트 자동감지 또는 지정 포트로 연결, 비동기 송신 프로세스(존재 시) 기동
        - 예상데이터:
          - 입력: port(Optional[str]), baudrate(Optional[int])
          - 출력: 성공 여부(bool)
        - 사용페이지/위치: 서버 시작 시 `FactorClient.start()` 경유 또는 최초 API 호출 시 연결
        """
        pc = self.pc
        if pc.connected:
            pc.logger.warning("이미 연결되어 있습니다")
            return True

        if port is not None:
            pc.port = port
        if baudrate is not None:
            pc.baudrate = baudrate

        if not pc.port:
            pc.port = pc._auto_detect_port()
            if not pc.port:
                pc.logger.error("프린터 포트를 찾을 수 없습니다")
                return False

        try:
            pc.logger.info(f"프린터 연결 시도: {pc.port}@{pc.baudrate}")
            pc._set_state(pc.state.__class__.CONNECTING)

            # 비동기 송신 기능 제거됨

            # Fallback: 동기 시리얼
            pc.serial_conn = serial.Serial(
                port=pc.port,
                baudrate=pc.baudrate,
                timeout=0.5,
                write_timeout=1,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                rtscts=False,
                dsrdtr=False
            )
            try:
                pc.logger.info("시리얼 연결 안정화 중...")
                pc.serial_conn.dtr = False
                time.sleep(0.2)
                pc.serial_conn.dtr = True
                pc.serial_conn.reset_input_buffer()
                pc.serial_conn.reset_output_buffer()
                time.sleep(2.0)
                pc.logger.info("시리얼 연결 안정화 완료")
            except Exception as e:
                pc.logger.warning(f"시리얼 안정화 중 오류 (무시됨): {e}")

            pc.running = True
            pc.read_thread = threading.Thread(target=pc._read_worker, daemon=True)
            pc.send_thread = threading.Thread(target=pc._send_worker, daemon=True)
            pc.read_thread.start(); pc.send_thread.start()
            pc.connected = True
            pc._set_state(pc.state.__class__.OPERATIONAL)
            pc._initialize_printer()
            pc.logger.info("프린터 연결 완료")
            # 연결 직후: 온도 오토리포트(M155 S1) 1회 시도 후 ok면 설정에 기록
            try:
                # FactorClient 및 ConfigManager 접근
                fc = getattr(self.pc, 'factor_client', None)
                cm = getattr(fc, 'config', None) if fc else None
                already = False
                try:
                    already = bool(cm.get('data_collection.auto_report', False)) if cm else False
                except Exception:
                    already = False

                if not already:
                    resp = self.send_command_and_wait("M155 S1", timeout=3.0)
                    if resp and 'ok' in str(resp).lower():
                        try:
                            setattr(self.pc, '_last_temp_time', time.time())
                        except Exception:
                            pass
                        if cm:
                            try:
                                cm.mark_auto_report_supported(True)
                            except Exception:
                                pass
                # 지원되는 경우 세션 시작과 함께 온도/위치 오토리포트 활성화
                try:
                    supports = bool(cm.get('data_collection.auto_report', False)) if cm else False
                except Exception:
                    supports = False
                if supports and not bool(getattr(self.pc, '_auto_report_active', False)):
                    try:
                        # 온도 오토리포트 ON
                        self.send_command_and_wait("M155 S1", timeout=2.0)
                    except Exception:
                        pass
                    try:
                        # 위치 오토리포트 ON (펌웨어 지원 시)
                        self.send_command_and_wait("M154 S1", timeout=2.0)
                    except Exception:
                        pass
                    try:
                        # SD 진행률 오토리포트 ON (펌웨어 지원 시)
                        self.send_command_and_wait("M27 S1", timeout=2.0)
                    except Exception:
                        pass
                    try:
                        setattr(self.pc, '_auto_report_active', True)
                    except Exception:
                        pass
            except Exception:
                # 미지원/타임아웃 등은 무시하고 정상 연결만 유지
                pass
            return True

        except Exception as e:
            pc.logger.error(f"프린터 연결 실패: {e}")
            pc._set_state(pc.state.__class__.ERROR)
            return False

    def disconnect(self):
        """
        프린터 시리얼 연결 해제 및 송신 프로세스 종료

        - 역할: 송신 브리지/스레드 종료, 시리얼 자원 반납
        - 예상데이터: 입력/출력 없음
        - 사용페이지/위치: 서버 종료, 오류 회복 루틴 등
        """
        pc = self.pc
        pc.logger.info("프린터 연결 해제 중...")
        pc.running = False

        # 비동기 송신 기능 제거됨

        if pc.read_thread and pc.read_thread.is_alive():
            pc.read_thread.join(timeout=5)
        if pc.send_thread and pc.send_thread.is_alive():
            pc.send_thread.join(timeout=5)

        if pc.serial_conn and pc.serial_conn.is_open:
            pc.serial_conn.close()

        pc.connected = False
        pc._set_state(pc.state.__class__.DISCONNECTED)
        pc.logger.info("프린터 연결 해제 완료")

    # ===== 전송 =====
    def send_command(self, command: str, priority: bool = False) -> bool:
        """
        단일 G-code 명령을 전송(비동기 우선)

        - 역할: 배리어 명령(M109 등)은 플래그를 설정해 송신, 비배리어는 즉시 큐잉
        - 예상데이터:
          - 입력: command(str), priority(bool)
          - 출력: 전송 요청 결과(bool)
        - 사용페이지/위치: REST `/api/printer/command`, SocketIO `send_gcode`
        """
        pc = self.pc
        # 업로드/동기화 중 전면 차단 게이트
        try:
            if getattr(pc, 'tx_inhibit', False):
                pc.logger.debug(f"[TX_INHIBIT] drop: {command}")
                return False
        except Exception:
            pass
        if not pc.connected:
            pc.logger.warning("프린터가 연결되지 않음")
            return False
        # 비동기 송신 기능 제거됨
        if priority:
            pc._insert_priority_command(command)
        else:
            pc.command_queue.put(command)
        return True

    def send_command_and_wait(self, command: str, timeout: float = 8.0):
        """
        동기 전송 후 ack/의미 있는 응답을 대기

        - 역할: 배리어 또는 특정 질의(M105/M114)의 응답을 제한 시간 내 대기
        - 예상데이터:
          - 입력: command(str), timeout(float)
          - 출력: 응답 문자열(str) 또는 None(타임아웃/오류)
        - 사용페이지/위치: 상태 폴링, 위치/온도 즉시 조회
        """
        pc = self.pc
        # 비동기 송신 기능 제거됨

        # Fallback 동기
        if not pc.connected or not (pc.serial_conn and pc.serial_conn.is_open):
            pc.logger.warning("프린터가 연결되지 않음")
            return None
        with pc.serial_lock:
            pc.sync_mode = True
            try:
                pc._last_temp_line = None; pc._last_pos_line = None; pc.last_response = None
                pc.logger.debug(f"[SYNC_TX] {command!r}")
                pc.serial_conn.write(f"{command}\n".encode("utf-8"))
                pc.serial_conn.flush()
                end = time.time() + timeout
                while time.time() < end:
                    line_bytes = pc.serial_conn.readline()
                    if line_bytes:
                        line = line_bytes.decode("utf-8", errors="ignore").strip()
                        if line:
                            pc.logger.debug(f"[SYNC_RX] {line}")
                            try:
                                pc._process_response(line)
                            except Exception:
                                pass
                            if command == "M105" and ("T:" in line or line.lower().startswith("ok")):
                                return line
                            if command == "M114" and ("X:" in line or line.lower().startswith("ok")):
                                return line
                            pc.last_response = line
                    else:
                        if pc.serial_conn.in_waiting:
                            extra = pc.serial_conn.read(pc.serial_conn.in_waiting)
                            try:
                                extra_s = extra.decode("utf-8", errors="ignore")
                            except Exception:
                                extra_s = ""
                            for part in extra_s.replace("\r\n","\n").replace("\r","\n").split("\n"):
                                p = part.strip()
                                if p:
                                    pc.logger.debug(f"[SYNC_RX] {p}")
                                    try:
                                        pc._process_response(p)
                                    except Exception:
                                        pass
                                    if command == "M105" and ("T:" in p or p.lower().startswith("ok")):
                                        return p
                                    if command == "M114" and ("X:" in p or p.lower().startswith("ok")):
                                        return p
                                    pc.last_response = p
                        time.sleep(0.05)
                if pc.last_response:
                    return pc.last_response
                pc.logger.warning(f"명령 '{command}' 응답 타임아웃 ({timeout}초)")
                return None
            except Exception as e:
                pc.logger.error(f"동기 전송/수신 실패: {e}")
                return None
            finally:
                pc.sync_mode = False

    def send_gcode(self, command: str, wait: bool = False, timeout: float = 8.0) -> bool:
        """
        G-code 전송의 통합 엔트리포인트(비동기 우선)

        - 역할: 비배리어는 즉시 반환하여 인쇄 중 전송 공백 제거, 배리어는 ack 대기
        - 예상데이터:
          - 입력: command(str), wait(bool), timeout(float)
          - 출력: 성공 여부(bool)
        - 사용페이지/위치: UFP 스트리밍, 수동 명령 전송
        """
        pc = self.pc
        if not pc.connected:
            pc.logger.warning("프린터가 연결되지 않음")
            return False
        # 비동기 송신 기능 제거됨
        if wait:
            return self.send_command_and_wait(command, timeout=timeout) is not None
        try:
            with pc.serial_lock:
                pc.serial_conn.write(f"{command}\n".encode("utf-8"))
                pc.serial_conn.flush()
                pc.logger.debug(f"[SYNC_TX] {command!r}")
            return True
        except Exception as e:
            pc.logger.error(f"G-code 전송 실패: {e}")
            return False

    # ===== 내부 유틸 =====
    @staticmethod
    def _barrier_regex():
        """
        배리어 명령 정규식 반환

        - 역할: 펌웨어가 온도 대기/동작 완료를 요구하는 명령을 식별
        - 예상데이터: 출력 re.Pattern
        - 사용페이지/위치: 송신 경로에서 배리어 대기 처리
        """
        return re.compile(r"^(?:M109|M190|M400|G4|G28|G29|M112)\b", re.IGNORECASE)

    # 비동기 송신 브리지 제거됨

    # 전송 윈도우 스냅샷 기능 제거됨

    def get_phase_snapshot(self):
        pc = self.pc
        # 프린터 상위 상태가 취소/마무리 단계면 우선 반환
        try:
            st = getattr(pc, 'state', None)
            if st and getattr(st, 'name', '').lower() in ('cancelling', 'finishing'):
                return {'phase': 'finishing', 'since': time.time()}
        except Exception:
            pass
        tracker = getattr(pc, 'phase_tracker', None)
        if not tracker:
            return {'phase': 'unknown', 'since': 0}
        return tracker.snapshot()

    def cancel_print(self):
        """인쇄 취소: 대기 큐 폐기 → 파킹 이동 → 쿨다운"""
        pc = self.pc
        try:
            # 대기열 비우기
            # 동기 경로: 내부 큐 비우기
            try:
                while True:
                    pc.command_queue.get_nowait()
            except Exception:
                pass

            # 안전 파킹 및 쿨다운 시퀀스
            safe_cmds = [
                'G91',            # 상대좌표
                'G1 Z10 F600',    # 노즐 올리기
                'G90',            # 절대좌표 복귀
                'G1 X0 Y200 F6000',
                'M104 S0',        # 노즐 끄기
                'M140 S0',        # 베드 끄기
                'M106 S0'         # 팬 끄기
            ]
            for cmd in safe_cmds:
                # 배리어로 간주하여 순차 실행을 보장
                self.send_gcode(cmd, wait=False)

            # 상태 플래그 갱신(선택)
            try:
                pc._set_state(pc.state.__class__.CANCELLING)
            except Exception:
                pass

            # 쿨링 완료 감시 쓰레드 시작(비차단)
            try:
                t = threading.Thread(target=self._cooling_watchdog, kwargs={
                    'hotend_threshold': 50.0,
                    'bed_threshold': 40.0,
                    'check_interval': 5.0,
                    'timeout_sec': 1800.0,
                }, daemon=True)
                t.start()
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _cooling_watchdog(self, hotend_threshold: float = 50.0, bed_threshold: float = 40.0,
                           check_interval: float = 5.0, timeout_sec: float = 1800.0,
                           stable_seconds: float = 7.0):
        """쿨링 완료 시 로그 출력(비차단 감시).

        완료 조건(둘 다 충족):
          1) 타겟 온도가 0 (노즐/베드)
          2) 현재 온도가 임계치 이하 (노즐 < hotend_threshold, 베드 < bed_threshold)
          3) 위 상태가 stable_seconds 동안 연속 유지

        타임아웃: timeout_sec 후 미도달 시 경고 로그
        """
        pc = self.pc
        start_ts = time.time()
        try:
            pc.logger.info("쿨링 진행 중… (노즐≤%.0f°C, 베드≤%.0f°C)" % (hotend_threshold, bed_threshold))
        except Exception:
            pass
        last_ok_ts = None
        while True:
            try:
                # 온도 정보 갱신
                ti = None
                try:
                    ti = pc.collector.get_temperature_info()
                except Exception:
                    ti = None
                hot = None; hot_t = None; bed = None; bed_t = None
                if ti:
                    # tool dict에서 첫 번째 항목 사용
                    tool_dict = getattr(ti, 'tool', None) or {}
                    if isinstance(tool_dict, dict) and tool_dict:
                        first_key = list(tool_dict.keys())[0]
                        tool0 = tool_dict.get(first_key)
                        hot = getattr(tool0, 'actual', None)
                        hot_t = getattr(tool0, 'target', None)
                    bed_info = getattr(ti, 'bed', None)
                    if bed_info is not None:
                        bed = getattr(bed_info, 'actual', None)
                        bed_t = getattr(bed_info, 'target', None)
                # 판정
                def to_float(v):
                    try:
                        return float(v)
                    except Exception:
                        return None
                hot_ok = ((hot_t is None) or to_float(hot_t) == 0.0) and ((hot is None) or (to_float(hot) is not None and to_float(hot) <= float(hotend_threshold)))
                bed_ok = ((bed_t is None) or to_float(bed_t) == 0.0) and ((bed is None) or (to_float(bed) is not None and to_float(bed) <= float(bed_threshold)))
                if hot_ok and bed_ok:
                    if last_ok_ts is None:
                        last_ok_ts = time.time()
                    if (time.time() - last_ok_ts) >= stable_seconds:
                        try:
                            pc.logger.info("쿨링 완료")
                            # 상태/단계 IDLE로 전환
                            try:
                                pc._set_state(pc.state.__class__.OPERATIONAL)
                            except Exception:
                                pass
                            try:
                                tracker = getattr(pc, 'phase_tracker', None)
                                if tracker:
                                    tracker._set(_PrintPhase.IDLE)
                            except Exception:
                                pass
                        except Exception:
                            pass
                        return
                else:
                    last_ok_ts = None
                if (time.time() - start_ts) > timeout_sec:
                    try:
                        pc.logger.warning("쿨링 완료 타임아웃")
                    except Exception:
                        pass
                    return
            except Exception:
                # 오류 시 잠시 대기 후 재시도
                pass
            time.sleep(max(1.0, float(check_interval)))

    def clear_command_queue(self) -> bool:
        """송신 대기 큐를 즉시 비움(인플라이트는 건드리지 않음)

        - 사용처: 업로드/에러 후 잔여 명령 제거
        """
        pc = self.pc
        try:
            # 내부 큐 비우기
            try:
                while True:
                    pc.command_queue.get_nowait()
            except Exception:
                pass
            # 시리얼 입력 버퍼도 비움(남은 ok/busy 등)
            try:
                if pc.serial_conn and pc.serial_conn.is_open:
                    with pc.serial_lock:
                        pc.serial_conn.reset_input_buffer()
            except Exception:
                pass
            return True
        except Exception:
            return False


# ====== 단계 추적기 구현 ======
class _PrintPhase(Enum):
    INITIALIZING = "initializing"
    HOMING = "homing"
    HEATING = "heating"
    LEVELING = "leveling"
    PRIMING = "priming"
    FIRST_LAYER = "first_layer"
    PRINTING = "printing"
    IDLE = "idle"


class _PhaseTracker:
    G28_RE = re.compile(r"^\s*G28\b", re.I)
    G29_RE = re.compile(r"^\s*G29\b", re.I)
    HEAT_RE = re.compile(r"^\s*M10(?:4|9)\b|^\s*M19(?:0|4)\b", re.I)  # M104/M109/M190/M140
    PRIME_HINT = re.compile(r"prime|purge", re.I)
    MOVE_EXTRUDE_RE = re.compile(r"^\s*G1\b.*\bE(-?\d+\.?\d*)", re.I)
    Z_RE = re.compile(r"\bZ(-?\d+\.?\d*)", re.I)

    def __init__(self, first_layer_height: float = 0.2):
        self.phase = _PrintPhase.INITIALIZING
        self.first_layer_height = first_layer_height
        self._heating_pending = False
        self.last_tx = None
        self.last_change = time.time()

    def on_tx(self, line: str):
        self.last_tx = line or ''
        if self.G28_RE.search(line):
            self._set(_PrintPhase.HOMING)
            return
        if self.G29_RE.search(line):
            self._set(_PrintPhase.LEVELING)
            return
        if self.HEAT_RE.search(line):
            self._heating_pending = True
            self._set(_PrintPhase.HEATING)
            return
        if self.PRIME_HINT.search(line):
            self._set(_PrintPhase.PRIMING)
            return
        if self._looks_first_layer(line):
            self._set(_PrintPhase.FIRST_LAYER)
            return
        if self.MOVE_EXTRUDE_RE.search(line):
            if self.phase not in (_PrintPhase.FIRST_LAYER, _PrintPhase.PRINTING):
                self._set(_PrintPhase.PRINTING)

    def on_ack(self, ok_line: str):
        if self._heating_pending and ok_line.lower().startswith('ok'):
            # 최종 판정은 on_temp에서, 여기서는 pending만 클리어 가능
            self._heating_pending = False

    def on_temp(self, tool_actual: Optional[float], tool_target: Optional[float], bed_actual: Optional[float], bed_target: Optional[float]):
        try:
            # Heating 단계일 때 목표 근접 시 다음 단계로 이동 준비
            if self.phase == _PrintPhase.HEATING:
                close = lambda a, t: (a is not None and t is not None and abs(float(a) - float(t)) <= 1.0 and float(t) > 0)
                if close(tool_actual, tool_target) or close(bed_actual, bed_target):
                    # 다음 전송 라인에서 자연스럽게 다른 단계로 넘어감
                    pass
        except Exception:
            pass

    def _looks_first_layer(self, line: str) -> bool:
        mE = self.MOVE_EXTRUDE_RE.search(line or '')
        if not mE:
            return False
        mZ = self.Z_RE.search(line or '')
        if not mZ:
            return False
        try:
            z = float(mZ.group(1))
            return 0 <= z <= (self.first_layer_height + 0.05)
        except Exception:
            return False

    def _set(self, p: _PrintPhase):
        if p != self.phase:
            self.phase = p
            self.last_change = time.time()

    def snapshot(self):
        return {"phase": self.phase.value, "since": self.last_change}

    # 비동기 하위 프로세스 송신 루틴 제거됨


