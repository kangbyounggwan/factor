import time
import re
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING
from .data_models import TemperatureData, TemperatureInfo, Position
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

    # ===== 파싱 및 응답 처리 =====
    def process_response(self, line: str):
        """
        단일 수신 라인을 처리하고 내부 상태/콜백을 갱신

        - 역할: 온도/위치/펌웨어/오류 파싱 및 이벤트 발생
        - 예상데이터:
          - 입력: line(str)
          - 출력: 없음(부수효과: 상태/콜백)
        - 사용페이지/위치: 수신 워커/비동기 브리지의 RX 이벤트에서 호출
        """
        pc = self.pc
        pc.logger.debug(f"응답 수신: {line}")
        pc.last_response = line
        pc.last_rx_line = line
        pc.last_rx_time = time.time()

        pc.detection_responses.append(line)

        if pc.printer_handler:
            temp_info = pc.printer_handler.parse_temperature(line)
            if temp_info:
                pc._trigger_callback('on_temperature_update', temp_info)
                pc._last_temp_line = line
                return
            position = pc.printer_handler.parse_position(line)
            if position:
                pc.current_position = position
                pc._trigger_callback('on_position_update', position)
                pc._last_pos_line = line
                return
        else:
            if self._parse_temperature(line):
                pc._last_temp_line = line
                return
            if self._parse_position(line):
                pc._last_pos_line = line
                return

        if self._parse_firmware_info(line):
            return

        if pc.error_pattern.search(line):
            pc.logger.error(f"프린터 오류: {line}")
            pc._set_state(pc.PrinterState.ERROR)
            pc._trigger_callback('on_error', line)

        if pc.ok_pattern.match(line):
            # 동기 모드 소프트 윈도우 카운터 감소(연속 전송 페이싱)
            try:
                if hasattr(pc, 'control') and pc.control and hasattr(pc.control, '_outstanding'):
                    with pc.control._lock:
                        if pc.control._outstanding > 0:
                            pc.control._outstanding -= 1
            except Exception:
                pass
            # 계속 진행

        # 로컬 응답 객체(shim)로 콜백 전달 (순환참조 방지)
        @dataclass
        class GCodeResponseShim:
            command: str
            response: str
            timestamp: float
            success: bool
            error_message: Optional[str] = None

        response = GCodeResponseShim(
            command="",
            response=line,
            timestamp=time.time(),
            success=not pc.error_pattern.search(line)
        )
        pc._trigger_callback('on_response', response)

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

    def _parse_position(self, line: str) -> bool:
        """
        위치 응답 라인 파싱(M114)

        - 역할: X/Y/Z/E 좌표 추출 및 Position 갱신/콜백
        - 예상데이터:
          - 입력: line(str)
          - 출력: 파싱 성공 여부(bool)
        - 사용페이지/위치: process_response, _parse_position_response
        """
        pc = self.pc
        if not line.startswith('X:'):
            return False
        matches = pc.position_pattern.findall(line)
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
            pc._trigger_callback('on_position_update', pc.current_position)
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

    # ===== 동기 조회 =====
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
            response = pc.send_command_and_wait("M105", timeout=5.0)
            if response:
                self._parse_temperature_response(response)
                if pc._last_temp_info:
                    return pc._last_temp_info
            return TemperatureInfo(tool={})
        except Exception as e:
            pc.logger.error(f"온도 정보 수집 실패: {e}")
            return TemperatureInfo(tool={})

    def _parse_temperature_response(self, response: str):
        """
        온도 응답 문자열 파싱 도우미

        - 역할: 단일 문자열에 대해 _parse_temperature 호출
        - 예상데이터: 입력 response(str), 출력 없음
        - 사용페이지/위치: get_temperature_info
        """
        pc = self.pc
        try:
            if self._parse_temperature(response):
                pc.logger.debug(f"온도 응답 파싱 완료: {pc.current_temps}")
        except Exception as e:
            pc.logger.error(f"온도 응답 파싱 실패: {e}")

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
            response = pc.send_command_and_wait("M114", timeout=5.0)
            if response:
                self._parse_position_response(response)
            return pc.current_position
        except Exception as e:
            pc.logger.error(f"위치 정보 수집 실패: {e}")
            return pc.current_position

    def _parse_position_response(self, response: str):
        """
        위치 응답 문자열 파싱 도우미

        - 역할: 단일 문자열에 대해 _parse_position 호출 및 내부 상태 갱신
        - 예상데이터: 입력 response(str), 출력 없음
        - 사용페이지/위치: get_position
        """
        pc = self.pc
        try:
            if "X:" in response and "Y:" in response and "Z:" in response:
                x_match = re.search(r'X:(-?\d+\.?\d*)', response)
                if x_match:
                    pc.current_position.x = float(x_match.group(1))
                y_match = re.search(r'Y:(-?\d+\.?\d*)', response)
                if y_match:
                    pc.current_position.y = float(y_match.group(1))
                z_match = re.search(r'Z:(-?\d+\.?\d*)', response)
                if z_match:
                    pc.current_position.z = float(z_match.group(1))
                e_match = re.search(r'E:(-?\d+\.?\d*)', response)
                if e_match:
                    pc.current_position.e = float(e_match.group(1))
                pc.logger.debug(f"위치 파싱 완료: {pc.current_position}")
        except Exception as e:
            pc.logger.error(f"위치 응답 파싱 실패: {e}")


