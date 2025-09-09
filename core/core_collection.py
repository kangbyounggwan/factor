import time
import re
from typing import Optional, TYPE_CHECKING, Dict, List
from .data_models import TemperatureData, TemperatureInfo, Position, GCodeResponse
if TYPE_CHECKING:
    from .printer_comm import PrinterCommunicator


class DataCollectionModule:
    """
    프린터 데이터 취득/파싱/동기 조회 로직 모듈

    - 역할: 수신 라인 파싱, 콜백 트리거, 동기 조회(M105/M114) 보조
    - 예상데이터:
      - 입력: 프린터 응답 문자열, 조회 명령
      - 출력: 파싱된 데이터 모델(TemperatureInfo/Position), 콜백 알림
    - 사용페이지/위치:
      - 웹: `/status` API, SocketIO 초기 데이터 전송
      - 코어: `FactorClient` 폴링 워커, `PrinterCommunicator` 수신 처리
    """

    def __init__(self, pc: "PrinterCommunicator"):
        self.pc = pc
        # 정규식/상수 사전 컴파일
        self.re_sd_list_begin = re.compile(r'^\s*begin file list', re.I)
        self.re_sd_list_end   = re.compile(r'^\s*end file list', re.I)
        self.re_sd_printing   = re.compile(r'sd\s+printing\s+byte\s+(\d+)\s*/\s*(\d+)', re.I)
        self.re_temp_TB       = re.compile(r'([TBCP])(\d*):(-?\d+\.?\d*)\s*/(-?\d+\.?\d*)', re.I)
        self.re_pos_XYZE      = re.compile(r'([XYZE]):(-?\d+\.?\d*)')
        self.re_fw_name       = re.compile(r'\bFIRMWARE_NAME:|Klipper|RepRapFirmware', re.I)
        # SD 목록 수집 상태
        self._sd_list_capturing = False
        self._sd_list_buffer: List[str] = []
        # M27 상태기(선택)
        self._m27: Dict[str, object] = {"last_update": 0.0, "consec_not": 0, "active": False, "printed": 0, "total": 0, "eq_ts": None}

    # ===== 파싱 및 응답 처리 =====
    def process_response(self, line: str):
        pc = self.pc
        if not line:
            return
        now = time.time()
        pc.last_response = pc.last_rx_line = line
        pc.last_rx_time = now
        llow = (line or '').strip().lower()

        # 1) SD 목록
        if self._handle_sd_list(line, llow, now):
            return

        # 2) 온도/위치 파싱
        #    - 온도: 핸들러 우선 → 실패 시 폴백
        #    - 위치: 항상 폴백(_parse_position) 사용하여 "Count" 이후 좌표 무시 로직 일원화
        if pc.printer_handler:
            if self._handle_temp_via_handler(line):
                return
            if self._handle_temp_fallback(line):
                return
            if self._handle_pos_fallback(line):
                return
        else:
            if self._handle_temp_fallback(line):
                return
            if self._handle_pos_fallback(line):
                return

        # 3) 펌웨어 정보
        if self._handle_firmware(line):
            return

        # 4) 오류/ok
        if self._handle_error_or_ok(line, llow):
            return

        # 5) 일반 응답 이벤트
        self._emit_response_event(line, now)

        # 6) M27 진행률
        self._handle_m27(line, llow, now)

    # ---------- 각각의 핸들러 ----------
    def _handle_sd_list(self, line: str, llow: str, now: float) -> bool:
        if self.re_sd_list_begin.match(llow):
            self._sd_list_capturing = True
            self._sd_list_buffer = []
            return True
        if self.re_sd_list_end.match(llow):
            self._sd_list_capturing = False
            self._finalize_sd_list(now)
            return True
        if self._sd_list_capturing:
            self._sd_list_buffer.append(line)
            return True
        return False

    def _finalize_sd_list(self, now: float) -> None:
        files: List[Dict[str, Optional[int]]] = []
        buf = self._sd_list_buffer
        i = 0
        def _extract_size(s: str):
            m = re.search(r"(\d+)\s*(?:bytes?)?\s*$", s, re.I) or re.search(r"\bsize\s*[:=]\s*(\d+)", s, re.I)
            if m:
                return int(m.group(1))
            ints = re.findall(r"(\d+)", s)
            return int(ints[-1]) if ints else None
        while i < len(buf):
            s = (buf[i] or '').strip(); i += 1
            if not s or s.lower() == 'ok':
                continue
            if s.endswith('/') or s.endswith('\\'):
                continue
            if s.lower().startswith('long filename:'):
                continue
            size = _extract_size(s)
            short_name = s
            if size is not None:
                try:
                    short_name = s[:s.lower().rfind(str(size))].strip()
                except Exception:
                    pass
            long_name = None
            if i < len(buf):
                nxt = (buf[i] or '').strip()
                if nxt.lower().startswith('long filename:'):
                    long_name = nxt.split(':', 1)[1].strip().strip('"')
                    i += 1
            files.append({'name': long_name or short_name, 'size': size})
        self.pc.sd_card_info = {'files': files, 'last_update': now}
        self._sd_list_buffer = []

    def _handle_temp_via_handler(self, line: str) -> bool:
        ti = self.pc.printer_handler.parse_temperature(line)
        if ti:
            self.pc._trigger_callback('on_temperature_update', ti)
            self.pc._last_temp_line = line
            return True
        return False

    def _handle_pos_via_handler(self, line: str) -> bool:
        pos = self.pc.printer_handler.parse_position(line)
        if pos:
            self.pc.current_position = pos
            self.pc._trigger_callback('on_position_update', pos)
            self.pc._last_pos_line = line
            return True
        return False

    def _handle_temp_fallback(self, line: str) -> bool:
        if self._parse_temperature(line):
            self.pc._last_temp_line = line
            return True
        return False

    def _handle_pos_fallback(self, line: str) -> bool:
        if self._parse_position(line):
            self.pc._last_pos_line = line
            return True
        return False

    def _handle_firmware(self, line: str) -> bool:
        if self.re_fw_name.search(line):
            return self._parse_firmware_info(line)
        return False

    def _handle_error_or_ok(self, line: str, llow: str) -> bool:
        pc = self.pc
        if pc.error_pattern.search(line):
            pc.logger.error(f"프린터 오류: {line}")
            pc._set_state(pc.PrinterState.ERROR)
            pc._trigger_callback('on_error', line)
            return True
        if pc.ok_pattern.match(line):
            return False
        return False

    def _emit_response_event(self, line: str, now: float) -> None:
        resp = GCodeResponse(
            command="",
            response=line,
            timestamp=now,
            success=not self.pc.error_pattern.search(line),
            error_message=None
        )
        self.pc._trigger_callback('on_response', resp)

    # ---- M27 : SD 진행률 + 상태기 적용 ----
    def _handle_m27(self, line: str, llow: str, now: float) -> None:
        st = self._m27
        if 'not sd printing' in llow or 'sd printing byte 0/0' in llow:
            st["consec_not"] += 1
            st["last_update"] = now
            if st["consec_not"] >= 2:
                self._set_sd_progress(active=False, printed=0, total=0, now=now)
            return
        m = self.re_sd_printing.search(llow)
        if not m:
            return
        printed, total = int(m.group(1)), int(m.group(2))
        st["printed"], st["total"], st["last_update"] = printed, total, now
        st["consec_not"] = 0
        if total > 0 and 0 < printed < total:
            st["active"] = True; st["eq_ts"] = None
        elif total > 0 and printed == total:
            st["eq_ts"] = now
            st["active"] = True
        completion = (printed / total * 100.0) if total > 0 else 0.0
        self._set_sd_progress(active=st["active"], printed=printed, total=total, now=now, completion=completion)

    def _set_sd_progress(self, active: bool, printed: int, total: int, now: float, completion: float = 0.0) -> None:
        try:
            fc = getattr(self.pc, 'factor_client', None)
            if not fc:
                return
            fc._sd_progress_cache = {
                'active': active,
                'completion': completion,
                'printed_bytes': printed,
                'total_bytes': total,
                'eta_sec': None,
                'last_update': now,
                'source': 'sd'
            }
            try:
                self.pc._set_state(self.pc.PrinterState.PRINTING if active else self.pc.PrinterState.OPERATIONAL)
            except Exception:
                pass
        except Exception:
            pass




    # ===== M115(KV) 파싱 유틸 =====
    @staticmethod
    def parse_m115_kv_line(line: str) -> Dict[str, str]:
        """M115의 KEY:VALUE 묶음 한 줄을 dict로 파싱

        예: "FIRMWARE_NAME:Marlin 2.1.2 MACHINE_TYPE:Ender UUID:..."
        """
        try:
            pattern = re.compile(r'([A-Z_]+):\s*(.*?)(?=\s+[A-Z_]+:|$)')
            return {m.group(1): m.group(2).strip() for m in pattern.finditer((line or '').strip())}
        except Exception:
            return {}

    @staticmethod
    def extract_m115_kv_from_lines(lines: List[str]) -> Dict[str, str]:
        """여러 응답 라인에서 KEY:VALUE가 2개 이상 포함된 대표 라인을 찾아 파싱"""
        if not lines:
            return {}
        try:
            for ln in lines:
                if not ln:
                    continue
                # KEY 토큰이 2개 이상 포함된 라인 우선 선택
                if len(re.findall(r'[A-Z_]+:', ln)) >= 2:
                    return DataCollectionModule.parse_m115_kv_line(ln)
        except Exception:
            pass
        # fallback: 첫 줄 시도
        try:
            return DataCollectionModule.parse_m115_kv_line(lines[0] or '')
        except Exception:
            return {}

    def _parse_temperature(self, line: str) -> bool:
        """
        온도 응답 라인 파싱(M105 등)

        - 역할: T/B/C 센서 값 추출하여 TemperatureInfo 생성/콜백
        - 예상데이터:
          - 입력: line(str)
          - 출력: 파싱 성공 여부(bool)
        - 사용페이지/위치: process_response, _parse_temperature_response
        """
        pc = self.pc
        t_match = re.search(r"T:\s*(-?\d+\.?\d*)\s*/\s*(-?\d+\.?\d*)", line)
        b_match = re.search(r"B:\s*(-?\d+\.?\d*)\s*/\s*(-?\d+\.?\d*)", line)
        tools = {}; bed = None; chamber = None
        if t_match:
            actual = float(t_match.group(1)); target = float(t_match.group(2))
            tools["tool0"] = TemperatureData(actual=actual, target=target)
        if b_match:
            actual = float(b_match.group(1)); target = float(b_match.group(2))
            bed = TemperatureData(actual=actual, target=target)
        if not tools and not bed:
            matches = pc.temp_pattern.findall(line)
            for sensor_type, sensor_num, actual, target in matches:
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
            pc._last_temp_info = temp_info
            try:
                pc._last_temp_time = time.time()
            except Exception:
                pass
            pc._trigger_callback('on_temperature_update', temp_info)
            # 단계 추적에 온도 업데이트 전달
            try:
                tool0 = tools.get('tool0') if tools else None
                pc.phase_tracker.on_temp(
                    tool0.actual if tool0 else None,
                    tool0.target if tool0 else None,
                    bed.actual if bed else None,
                    bed.target if bed else None,
                )
            except Exception:
                pass
            return True
        return False


    def _parse_firmware_info(self, line: str) -> bool:
        """
        펌웨어 정보 파싱(M115 응답 등)

        - 역할: 펌웨어 명/버전 추출
        - 예상데이터:
          - 입력: line(str)
          - 출력: 파싱 성공 여부(bool)
        - 사용페이지/위치: process_response 초기 구간
        """
        pc = self.pc
        if line.startswith('FIRMWARE_NAME:'):
            parts = line.split()
            for part in parts:
                if part.startswith('FIRMWARE_NAME:'):
                    pc.firmware_name = part.split(':')[1]
                elif part.startswith('FIRMWARE_VERSION:'):
                    pc.firmware_version = part.split(':')[1]
            return True
        if 'Klipper' in line:
            pc.firmware_name = "Klipper"; return True
        if 'RepRapFirmware' in line:
            pc.firmware_name = "RepRapFirmware"; return True
        return False

    def get_temperature_info(self):
        """
        현재 온도 정보 동기 조회

        - 역할: M105 전송 후 응답 파싱 결과 반환
        - 예상데이터:
          - 입력: 없음
          - 출력: TemperatureInfo
        - 사용페이지/위치: `/status` API, SocketIO 초기 데이터 전송
        """
        pc = self.pc
        try:
            # 동기 전송 중이면 즉시 캐시 반환하여 잠금 경쟁 회피
            if getattr(pc, 'sync_mode', False):
                return getattr(pc, '_last_temp_info', TemperatureInfo(tool={}))
            # 오토리포트 활성 시(최근 수신이 있으면) 캐시 사용하여 M105 회피
            try:
                if getattr(pc, '_last_temp_info', None) is not None:
                    last_ts = float(getattr(pc, '_last_temp_time', 0.0) or 0.0)
                    if (time.time() - last_ts) <= 2.0:
                        return pc._last_temp_info
            except Exception:
                pass
            response = pc.send_command_and_wait("M105", timeout=5.0)
            if response:
                self._parse_temperature(response)
                if pc._last_temp_info:
                    return pc._last_temp_info
            return TemperatureInfo(tool={})
        except Exception as e:
            pc.logger.error(f"온도 정보 수집 실패: {e}")
            return TemperatureInfo(tool={})


    def get_position(self):
        """
        현재 위치 정보 동기 조회

        - 역할: M114 전송 후 응답 파싱 결과 반환
        - 예상데이터:
          - 입력: 없음
          - 출력: Position
        - 사용페이지/위치: `/status` API, SocketIO 초기 데이터 전송
        """
        pc = self.pc
        try:
            # 동기 전송 중이면 즉시 캐시 반환하여 잠금 경쟁 회피
            if getattr(pc, 'sync_mode', False):
                return pc.current_position
            # 오토리포트 활성 시(최근 수신이 있으면) 캐시 사용하여 M114 회피
            try:
                last_ts = float(getattr(pc, '_last_pos_time', 0.0) or 0.0)
                if (time.time() - last_ts) <= 2.0:
                    return pc.current_position
            except Exception:
                pass
            response = pc.send_command_and_wait("M114", timeout=5.0)
            if response:
                self._parse_position(response)
            return pc.current_position
        except Exception as e:
            pc.logger.error(f"위치 정보 수집 실패: {e}")
            return pc.current_position


    def _parse_position(self, line: str) -> bool:
        """
        위치 응답 라인 파싱(M114)

        - 역할: X/Y/Z/E 좌표 추출 및 Position 갱신/콜백
        - 예상데이터:
          - 입력: line(str)
          - 출력: 파싱 성공 여부(bool)
        - 사용페이지/위치: process_response
        """
        pc = self.pc
        if not line.startswith('X:'):
            return False
        # Count 이후 좌표는 스텝 카운트이므로 무시하고 앞부분만 파싱
        line_to_parse = re.split(r'\bCount\b', line, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        matches = pc.position_pattern.findall(line_to_parse)
        if not matches:
            return False
        position_data = {}
        for axis, value in matches:
            position_data[axis.lower()] = float(value)
        if position_data:
            pc.current_position = Position(
                x=position_data.get('x', 0),
                y=position_data.get('y', 0),
                z=position_data.get('z', 0),
                e=position_data.get('e', 0)
            )
            try:
                pc._last_pos_time = time.time()
            except Exception:
                pass
            pc._trigger_callback('on_position_update', pc.current_position)
            return True
        return False