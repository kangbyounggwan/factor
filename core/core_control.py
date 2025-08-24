import re
import time
import threading
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


