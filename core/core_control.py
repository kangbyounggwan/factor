import re
import time
import threading
from enum import Enum
from typing import Optional, Callable, TYPE_CHECKING
if TYPE_CHECKING:
    from .printer_comm import PrinterCommunicator

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
        # 동기 모드 소프트 크레딧 윈도우(ack 기반 페이싱)
        # 비동기 브리지 미사용 시에도 연속 전송을 가능하게 함
        self._sync_window = 20  # 기본 동시 인플라이트 허용 수
        self._outstanding = 0
        self._lock = threading.Lock()

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

            if _HAS_SERIAL_ASYNCIO:
                pc.logger.info("비동기 송신 프로세스 모드 사용 (pyserial-asyncio)")
                self._start_async_tx_bridge()
                pc.connected = True
                pc._set_state(pc.state.__class__.OPERATIONAL)
                pc._initialize_printer()
                pc.logger.info("프린터 연결 완료(Async TX)")
                return True

            # Fallback: 동기 시리얼
            pc.logger.info("pyserial-asyncio 미설치 → 동기 시리얼 모드로 동작")
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

        if pc.tx_bridge:
            try:
                pc.tx_bridge.stop()
            except Exception:
                pass
            pc.tx_bridge = None

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
        if not pc.connected:
            pc.logger.warning("프린터가 연결되지 않음")
            return False
        if pc.tx_bridge:
            line = f"{command}".strip()
            if not line:
                return True
            is_barrier = bool(self._barrier_regex().match(line))
            pc.tx_bridge.enqueue(line, barrier=is_barrier)
            return True
        if priority:
            pc._insert_priority_command(command)
        else:
            pc.command_queue.put(command)
        return True

    def send_command_and_wait(self, command: str, timeout: float = 8.0, collect: bool = False, flush_before: bool = False):
        """
        동기 전송 후 ack/의미 있는 응답을 대기

        - 역할: 배리어 또는 특정 질의(M105/M114)의 응답을 제한 시간 내 대기
        - 예상데이터:
          - 입력: command(str), timeout(float)
          - 출력: 응답 문자열(str) 또는 None(타임아웃/오류)
        - 사용페이지/위치: 상태 폴링, 위치/온도 즉시 조회
        """
        pc = self.pc
        if pc.tx_bridge:
            try:
                pc._last_temp_line = None; pc._last_pos_line = None; pc.last_response = None
                line = f"{command}".strip()
                if not line:
                    return ""
                is_barrier = bool(self._barrier_regex().match(line))
                msg_id = pc.tx_bridge.enqueue(line, barrier=is_barrier)
                waited = pc.tx_bridge.wait_ack(msg_id, timeout=timeout)
                if waited:
                    if command == "M105" and pc._last_temp_line:
                        return pc._last_temp_line
                    if command == "M114" and pc._last_pos_line:
                        return pc._last_pos_line
                    # 비동기 브리지에서는 라인 수집을 직접 지원하지 않음
                    return pc.last_response if pc.last_response else "ok"
                pc.logger.warning(f"명령 '{command}' 응답 타임아웃 ({timeout}초)")
                return None
            except Exception as e:
                pc.logger.error(f"동기 전송/수신 실패(Async TX): {e}")
                return None

        # Fallback 동기
        if not pc.connected or not (pc.serial_conn and pc.serial_conn.is_open):
            pc.logger.warning("프린터가 연결되지 않음")
            return None
        with pc.serial_lock:
            pc.sync_mode = True
            try:
                pc._last_temp_line = None; pc._last_pos_line = None; pc.last_response = None
                # 필요 시 입력 버퍼 비우기(이전 폴링 응답 제거)
                if flush_before:
                    try:
                        if hasattr(pc.serial_conn, 'reset_input_buffer'):
                            pc.serial_conn.reset_input_buffer()
                        else:
                            if pc.serial_conn.in_waiting:
                                pc.serial_conn.read(pc.serial_conn.in_waiting)
                    except Exception:
                        pass

                pc.logger.debug(f"[SYNC_TX] {command!r}")
                pc.serial_conn.write(f"{command}\n".encode("utf-8"))
                pc.serial_conn.flush()
                end = time.time() + timeout
                collected: list[str] = [] if collect else None  # type: ignore[assignment]
                def _maybe_collect(s: str):
                    if collect and s:
                        collected.append(s)  # type: ignore[union-attr]
                while time.time() < end:
                    line_bytes = pc.serial_conn.readline()
                    if line_bytes:
                        line = line_bytes.decode("utf-8", errors="ignore").strip()
                        if line:
                            pc.logger.debug(f"[SYNC_RX] {line}")
                            _maybe_collect(line)
                            try:
                                pc._process_response(line)
                            except Exception:
                                pass
                            low = line.lower()
                            if collect and (low.startswith('ok') or 'end file list' in low):
                                break
                            if not collect:
                                if command == "M105" and ("T:" in line or low.startswith("ok")):
                                    return line
                                if command == "M114" and ("X:" in line or low.startswith("ok")):
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
                                    _maybe_collect(p)
                                    try:
                                        pc._process_response(p)
                                    except Exception:
                                        pass
                                    lowp = p.lower()
                                    if collect and (lowp.startswith('ok') or 'end file list' in lowp):
                                        break
                                    if not collect:
                                        if command == "M105" and ("T:" in p or lowp.startswith("ok")):
                                            return p
                                        if command == "M114" and ("X:" in p or lowp.startswith("ok")):
                                            return p
                                    pc.last_response = p
                        time.sleep(0.05)
                if collect and collected is not None and len(collected) > 0:
                    return "\n".join(collected)
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
        if pc.tx_bridge:
            try:
                line = f"{command}".strip()
                if not line:
                    return True
                is_barrier = bool(self._barrier_regex().match(line))
                msg_id = pc.tx_bridge.enqueue(line, barrier=is_barrier)
                if is_barrier:
                    return pc.tx_bridge.wait_ack(msg_id, timeout=timeout)
                return True
            except Exception as e:
                pc.logger.error(f"G-code 전송 실패(Async TX): {e}")
                return False
        # 동기 경로: 소프트 크레딧 윈도우 적용
        # 1) 배리어 명령은 항상 동기 대기 수행
        if self._barrier_regex().match(command or ""):
            return self.send_command_and_wait(command, timeout=timeout) is not None
        # 2) 호출자가 명시 wait=True인 경우에도 동기 대기 수행
        if wait:
            return self.send_command_and_wait(command, timeout=timeout) is not None
        try:
            # outstanding < window 될 때까지 잠시 양보
            while True:
                with self._lock:
                    if self._outstanding < self._sync_window:
                        self._outstanding += 1
                        break
                time.sleep(0.001)

            with pc.serial_lock:
                pc.serial_conn.write(f"{command}\n".encode("utf-8"))
                pc.serial_conn.flush()
                pc.logger.debug(f"[SYNC_TX] {command!r}")
            return True
        except Exception as e:
            pc.logger.error(f"G-code 전송 실패: {e}")
            with self._lock:
                if self._outstanding > 0:
                    self._outstanding -= 1
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

    def _start_async_tx_bridge(self):
        """
        비동기 송신 브리지(mp.Process + pyserial-asyncio) 시작

        - 역할: 별도 프로세스에서 윈도우/크레딧 기반 송신 및 응답 수집
        - 예상데이터: 내부 큐(in_q/out_q), ack 테이블 초기화
        - 사용페이지/위치: 연결 시 1회 시작
        """
        if not _HAS_SERIAL_ASYNCIO:
            return

        pc = self.pc

        def _tx_proc_main(port: str, baudrate: int, window_size: int, in_q: 'mp.Queue', out_q: 'mp.Queue'):
            asyncio.run(self._tx_run(port, baudrate, window_size, in_q, out_q))

        ctx = mp.get_context('spawn')
        in_q: 'mp.Queue' = ctx.Queue(maxsize=10000)
        out_q: 'mp.Queue' = ctx.Queue(maxsize=10000)
        proc = ctx.Process(target=_tx_proc_main, args=(pc.port, pc.baudrate, pc.window_size, in_q, out_q), daemon=True)
        proc.start()

        class _Bridge:
            def __init__(self, in_q, out_q, proc, on_rx: Callable[[str], None], on_error: Callable[[str], None]):
                self.in_q = in_q; self.out_q = out_q; self.proc = proc
                self._seq = 0
                self._acks = {}
                self._lock = threading.Lock()
                self._running = True
                # 전송 윈도우 상태(부모 프로세스 미러)
                self._pending_map = {}
                self._pending_order = collections.deque()
                self._inflight_map = {}
                self._inflight_order = collections.deque()
                self._collector = threading.Thread(target=self._collect, args=(on_rx, on_error), daemon=True)
                self._collector.start()

            def enqueue(self, line: str, barrier: bool = False) -> int:
                self._seq += 1
                msg = {'id': self._seq, 'line': line, 'barrier': barrier, 'ts': time.time()}
                # 보류 큐에 등록
                with self._lock:
                    self._pending_map[self._seq] = line
                    self._pending_order.append(self._seq)
                self.in_q.put(msg)
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
                            # inflight 제거
                            _id = evt['id']
                            if _id in self._inflight_map:
                                self._inflight_map.pop(_id, None)
                                try:
                                    self._inflight_order.remove(_id)
                                except ValueError:
                                    pass
                        # 단계 추적: OK 수신
                        try:
                            tracker = getattr(self, '_tracker_ref', None) or getattr(self, 'tracker_ref', None)
                            if tracker is None:
                                tracker = getattr(self_parent(), 'phase_tracker', None)  # may raise
                            ok_line = evt.get('line', '')
                            if tracker:
                                tracker.on_ack(ok_line)
                        except Exception:
                            pass
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
                    elif t == 'tx':
                        # 보류 → 인플라이트 이동
                        _id = evt.get('id')
                        if _id is not None:
                            with self._lock:
                                line = self._pending_map.pop(_id, None)
                                if _id in self._pending_order:
                                    try:
                                        self._pending_order.remove(_id)
                                    except ValueError:
                                        pass
                                if line is None:
                                    line = evt.get('line', '')
                                self._inflight_map[_id] = line
                                self._inflight_order.append(_id)
                        # 단계 추적: 전송 라인
                        try:
                            tracker = getattr(self, '_tracker_ref', None) or getattr(self, 'tracker_ref', None)
                            if tracker is None:
                                tracker = getattr(self_parent(), 'phase_tracker', None)
                            if tracker:
                                tracker.on_tx(evt.get('line', ''))
                        except Exception:
                            pass

            def snapshot(self, window_size: int):
                """현재 전송 윈도우 상태 스냅샷 반환"""
                with self._lock:
                    inflight = [
                        {'id': _id, 'line': self._inflight_map.get(_id, '')}
                        for _id in list(self._inflight_order)
                    ]
                    pending_ids = list(self._pending_order)[:window_size]
                    pending = [
                        {'id': _id, 'line': self._pending_map.get(_id, '')}
                        for _id in pending_ids
                    ]
                return {'inflight': inflight, 'pending_next': pending}

            def purge(self):
                """대기 중인 전송을 모두 폐기(인플라이트는 유지될 수 있음)"""
                # in_q 비우기
                try:
                    while True:
                        self.in_q.get_nowait()
                except Exception:
                    pass
                # 보류 큐 초기화
                with self._lock:
                    self._pending_map.clear()
                    self._pending_order.clear()

        def _on_rx(line: str):
            if line:
                try:
                    self.pc._process_response(line)
                except Exception:
                    pass

        def _on_error(msg: str):
            if msg:
                try:
                    self.pc.logger.error(f"프린터 오류: {msg}")
                    self.pc._set_state(self.pc.state.__class__.ERROR)
                    self.pc._trigger_callback('on_error', msg)
                except Exception:
                    pass

        pc.tx_bridge = _Bridge(in_q, out_q, proc, _on_rx, _on_error)

    # ===== 자동 복구: 체크섬 모드 리셋 =====
    def _auto_reset_line_number_mode(self):
        """프린터가 'No Checksum with line number'를 보고할 때 체크섬 포함 M110을 전송"""
        try:
            # 라인 번호 + 체크섬 구성
            line = "N0 M110"
            cs = 0
            for ch in line:
                cs ^= ord(ch)
            cmd = f"{line}*{cs & 0xFF}"
            # 동기 경로로 즉시 전송
            pc = self.pc
            if pc.tx_bridge:
                # 브리지 경로에서는 우선순위 송신 후 ack 대기
                try:
                    msg_id = pc.tx_bridge.enqueue(cmd, barrier=True)
                    pc.tx_bridge.wait_ack(msg_id, timeout=2.0)
                except Exception:
                    pass
            else:
                try:
                    with pc.serial_lock:
                        pc.serial_conn.write(f"{cmd}\n".encode("utf-8"))
                        pc.serial_conn.flush()
                except Exception:
                    pass
        except Exception:
            pass

    def get_tx_window_snapshot(self):
        """전송 윈도우 스냅샷 반환(API용)"""
        pc = self.pc
        if not pc.tx_bridge:
            return {'window_size': pc.window_size, 'inflight': [], 'pending_next': []}
        snap = pc.tx_bridge.snapshot(pc.window_size)
        snap['window_size'] = pc.window_size
        return snap

    def get_phase_snapshot(self):
        pc = self.pc
        tracker = getattr(pc, 'phase_tracker', None)
        if not tracker:
            return {'phase': 'unknown', 'since': 0}
        return tracker.snapshot()

    def cancel_print(self):
        """인쇄 취소: 대기 큐 폐기 → 파킹 이동 → 쿨다운"""
        pc = self.pc
        try:
            # 대기열 비우기
            if pc.tx_bridge and hasattr(pc.tx_bridge, 'purge'):
                pc.tx_bridge.purge()
            else:
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

    @staticmethod
    async def _tx_run(port: str, baudrate: int, window_size: int, in_q: 'mp.Queue', out_q: 'mp.Queue'):
        """
        하위 프로세스 비동기 루프(송신/수신)

        - 역할: send_q/credit/inflight 관리, ok 응답으로 크레딧 회복, 에러 이벤트 방출
        - 예상데이터:
          - 입력: 시리얼 포트/보드레이트/윈도우 크기, mp 큐 핸들
          - 출력: 없음(이벤트는 out_q로 전달)
        - 사용페이지/위치: 내부 전용
        """
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


