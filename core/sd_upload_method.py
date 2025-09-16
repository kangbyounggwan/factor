"""
SD 카드 업로드 모듈 (Marlin M28/M29 호환)

Ultra-lite SD upload via M28/M29 (Marlin compatible)
- Flask 라우트를 최소화하여 유지보수성 향상
- 작은 헬퍼 함수들로 모듈화 (체크섬 + 라인 번호)
- 순차적 전송 (한 번에 하나씩)으로 단순성 확보, Resend 요청에 강건함
- 향후 WINDOW>1 확장 용이
- 모든 함수에 상세한 한글 주석 제공

가정: current_app.factor_client.printer_comm (pc)가 serial_conn, serial_lock을 제공
"""
from __future__ import annotations
import io
import json
import os
import re
import time
import tempfile
from typing import Optional, Tuple, Dict, Any


try:
    from core.mqtt_service.bridge import MQTTService
    # 전역 MQTT 서비스 인스턴스 참조를 위한 변수
    _mqtt_service_instance = None
except ImportError:
    MQTTService = None
    _mqtt_service_instance = None

def set_mqtt_service(mqtt_service):
    """MQTT 서비스 인스턴스를 설정하는 함수"""
    global _mqtt_service_instance
    _mqtt_service_instance = mqtt_service


# ========== 유틸리티 함수들 ==========
_RE_LEADING_N = re.compile(r"^\s*N\d+\s+")
_RE_TRAILING_CS = re.compile(r"\s*\*[0-9]+\s*$")

def _normalize_gcode_line(line: str, force_strip_comments: bool = True) -> str:
    """
    - 기존 N라인/체크섬 제거
    - 주석(;) 제거 (force_strip_comments=True면 항상 제거)
    - 앞뒤 공백 제거
    - 결과가 빈 줄이면 '' 반환
    """
    s = line.strip("\r\n")
    if not s:
        return ""
    s = _RE_LEADING_N.sub("", s)
    s = _RE_TRAILING_CS.sub("", s)
    if force_strip_comments:
        c = s.find(";")
        if c >= 0:
            s = s[:c].rstrip()
    return s



def _xor(s: str) -> int:
    """Marlin XOR 체크섬 (문자열 전체의 ASCII XOR)"""
    v = 0
    for ch in s:
        v ^= ord(ch)
    return v & 0xFF


def _nline(n: int, payload: str) -> bytes:
    """
    'N{n} {payload}*{cs}\\r\\n' 프레임 생성
    * 체크섬은 'N{n} {payload}' 문자열 전체에 대해 XOR
    """
    body = f"N{n} {payload}"
    cs = _xor(body)
    return f"{body}*{cs}\r\n".encode("ascii", "ignore")


def _read_until_ok_or_resend(ser, timeout: float = 2.0):
    """
    FW 응답을 읽어 ok / Resend:n / Error / timeout 판정
    return: ("ok", None) | ("resend", n) | ("error", msg) | ("timeout", None)
    """
    import re
    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline:
        data = ser.read(ser.in_waiting or 1)
        if data:
            buf += data
            lines = buf.split(b"\n")
            buf = lines[-1]
            for raw in lines[:-1]:
                line_raw = raw.decode(errors="ignore").strip()
                line = line_raw.lower()
                if not line:
                    continue
                if line == "ok" or line.endswith(" ok"):
                    return ("ok", None)
                m = re.search(r"resend:\s*(\d+)", line)
                if m:
                    try:
                        return ("resend", int(m.group(1)))
                    except Exception:
                        pass
                if line.startswith("error"):
                    return ("error", line_raw)
        else:
            time.sleep(0.01)
    return ("timeout", None)


def _wait_ok_or_keywords(ser, timeout=5.0) -> bool:
    """
    ok / 'writing' / 'open' / 'done saving file' 키워드가 나오면 True
    타임아웃이면 False
    """
    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline:
        data = ser.read(ser.in_waiting or 1)
        if data:
            buf += data
            lines = buf.split(b"\n")
            buf = lines[-1]
            for raw in lines[:-1]:
                line = raw.decode("utf-8", "ignore").strip()
                low = line.lower()
                if not low:
                    continue
                if low.startswith("ok") or ("writing" in low) or ("open" in low) or ("done saving file" in low):
                    # 디버깅 로그
                    print(f"[PRINTER] {line}")
                    return True
        else:
            time.sleep(0.01)
    return False


def _send_numbered_line(ser, n: int, payload: str, timeout: float = 2.0) -> int:
    """
    번호/체크섬 프레임 전송 + ok/Resend 처리. 성공 시 다음 N 반환.
    """
    while True:
        ser.write(_nline(n, payload))
        ser.flush()
        status, val = _read_until_ok_or_resend(ser, timeout=timeout)
        if status == "ok":
            return n + 1
        elif status == "resend":
            # FW가 요구한 줄번호로 재전송
            n = val
            continue
        elif status == "timeout":
            # 같은 N 재시도 (현장 FW가 ok를 늦게 주는 경우 방지)
            continue
        else:
            raise RuntimeError(f"Printer error on N{n}: {val}")


# ---------- 핵심 업로드 ----------

def sd_upload(pc, remote_name: str, up_stream, total_bytes: Optional[int] = None,
              remove_comments: bool = False, upload_id: Optional[str] = None) -> Dict[str, Any]:
    """
    SD 카드 업로드 (M28/M29) — 번호/체크섬 기반 + Resend 대응

    순서:
      N0: M110 N0         → 라인 넘버 리셋
      N1: M28 <name>      → 파일 오픈
      N2..: payload lines → 본문(줄 단위, N/체크섬)
      N?: M29             → 파일 닫기
    """
    ser = pc.serial_conn
    total_lines = 0
    sent_bytes = 0

    # 텍스트 래핑 보장 (이후 줄 단위로 읽음)
    if hasattr(up_stream, "read") and not isinstance(up_stream, (io.BufferedReader, io.BytesIO)):
        try:
            up_stream = up_stream.buffer
        except Exception:
            data = up_stream.read()
            if isinstance(data, str):
                data = data.encode("utf-8", "ignore")
            import io as _io
            up_stream = _io.BytesIO(data)
            total_bytes = len(data)

    # 총 크기 추정(선택)
    if total_bytes is None:
        try:
            cur = up_stream.tell()
            up_stream.seek(0, 2)  # SEEK_END
            total_bytes = up_stream.tell()
            up_stream.seek(cur, 0)
        except Exception:
            pass

    LOG_BYTES = 512 * 1024
    acc = 0
    last_log = time.time()

    # (옵션) MQTT 진행률
    def _pub_progress(final: bool = False):
        try:
            if not upload_id or not _mqtt_service_instance:
                return
            msg = {
                "upload_id": upload_id,
                "stage": "to_printer",
                "name": remote_name,
                "sent_bytes": sent_bytes,
                "total_bytes": total_bytes,
                "percent": round((sent_bytes / total_bytes) * 100.0, 1) if total_bytes else None,
            }
            if final:
                msg["done"] = True
            _mqtt_service_instance._publish_ctrl_result(
                "sd_upload_progress", True, json.dumps(msg, ensure_ascii=False)
            )
        except Exception:
            pass

    with pc.serial_lock:
        # 0) 포트 정리 + 간섭 억제
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        try:
            # 자동 온도/좌표 리포트 및 전원복구 기능 끄기 (가능한 경우)
            for cmd in (b"M155 S0\r\n", b"M154 S0\r\n", b"M413 S0\r\n"):
                ser.write(cmd); ser.flush()
                _read_until_ok_or_resend(ser, 1.0)
        except Exception:
            pass
        print("@@@@@@@@@@@@@@@@@오토리프트 끄기기@@@@@@@@@@@@@@@@@")
        # 1) 라인번호 리셋 (N0)
        n_cur = _send_numbered_line(ser, 0, "M110 N0", timeout=2.0)
        print("@@@@@@@@@@@@@@@@@라인번호 리셋@@@@@@@@@@@@@@@@@")
        # 2) 파일 열기 (N1)
        n_cur = _send_numbered_line(ser, 1, f"M28 {remote_name}", timeout=7.0)
        _wait_ok_or_keywords(ser, timeout=3.0)  # 'Writing to file' 등의 상태 메시지 대기
        print("@@@@@@@@@@@@@@@@@SD 업로드 준비@@@@@@@@@@@@@@@@@")

        time.sleep(2)
        print("@@@@@@@@@@@@@@@@@폴링 상태 없음음@@@@@@@@@@@@@@@@@")
        # 3) 본문 전송 (줄 단위 + N/체크섬)
        text = io.TextIOWrapper(up_stream, encoding="utf-8", errors="ignore", newline=None)
        for raw in text:
            # ※ 번호/체크섬 모드에선 주석 줄을 전송하면 안 됨 → 항상 정규화
            line = _normalize_gcode_line(raw, force_strip_comments=True)
            if not line:
                continue

            # (안전) 비정상적으로 긴 라인은 분절 — 보통 필요 없음
            parts = [line] if len(line) <= 200 else line.split(" ")

            for part in parts:
                if not part:
                    continue

                print(f"[TX] N{n_cur}: {part}")
                prev = sent_bytes
                n_cur = _send_numbered_line(ser, n_cur, part, timeout=2.0)

                # 진행률(파일에 기록될 payload 기준, \r\n 2바이트 가정)
                sent_bytes += len(part) + 2
                total_lines += 1
                acc += (sent_bytes - prev)

                # 1초마다 또는 512KB마다 진행률 표시
                if (acc >= LOG_BYTES) or (time.time() - last_log >= 1.0):
                    if total_bytes:
                        print(f"SD 업로드 진행: {sent_bytes}/{total_bytes} bytes "
                              f"({(sent_bytes/total_bytes)*100:.1f}%)")
                    else:
                        print(f"SD 업로드 진행: {sent_bytes} bytes")
                    _pub_progress()
                    acc = 0
                    last_log = time.time()

        # 4) 파일 닫기 (N/체크섬 M29)
        _ = _send_numbered_line(ser, n_cur, "M29", timeout=5.0)

        # 5) 완료 키워드/ok 대기 + 최종 보고
        _wait_ok_or_keywords(ser, timeout=10.0)
        _pub_progress(final=True)

    return {"lines": total_lines, "bytes": sent_bytes, "closed": True}


# ========== 업로드 보호 메커니즘 ==========

class UploadGuard:
    """
    업로드 중 시스템 보호를 위한 컨텍스트 매니저
    
    파일 업로드 중에 발생할 수 있는 시스템 간섭을 방지하기 위해
    폴링 간격을 조정하고 명령 전송을 차단합니다.
    
    사용법:
        with UploadGuard(fc, pc):
            # 업로드 작업 수행
            result = sd_upload(pc, filename, stream)
    """
    
    def __init__(self, fc, pc):
        """
        업로드 보호 객체 초기화
        
        Args:
            fc: Factor client 객체 (폴링 간격 설정용)
            pc: 프린터 통신 객체 (명령 전송 차단용)
        """
        self.fc = fc
        self.pc = pc
        
        # 현재 활성화된 방식 확인 및 원본 상태 저장
        self.temp_auto_supported = getattr(fc, 'M155_auto_supported', None)
        self.pos_auto_supported = getattr(fc, 'M154_auto_supported', None)
        
        # 원본 폴링 간격 저장 (폴링 사용 시에만 의미 있음)
        self.ti = getattr(fc, 'temp_poll_interval', None)
        self.pi = getattr(fc, 'position_poll_interval', None)
        self.mi = getattr(fc, 'm27_poll_interval', None)
        
        # 원본 명령 전송 함수 저장
        self.orig_send = getattr(getattr(pc, 'control', None), 'send_command', None)
    
    def __enter__(self):
        """
        업로드 보호 활성화
        
        - 자동리포트 지원 시: 자동리포트 중지 (M155 S0, M154 S0)
        - 자동리포트 미지원 시: 폴링 간격을 매우 크게 설정하여 일시정지
        - M27 폴링: 항상 간격 조정으로 일시정지
        - 업로드 보호 플래그 설정
        - 명령 전송 함수를 차단 함수로 교체
        - TX inhibit 플래그 설정
        """
        try:
            # 온도 자동리포트/폴링 중지
            # if hasattr(self.fc, '_rx_guard_running'):
            #     self.fc._rx_guard_running = False
            if self.temp_auto_supported is True:
                # 자동리포트 지원 시: 자동리포트 중지
                try:
                    self.pc.send_command("M155 S0")
                except Exception:
                    pass
            elif self.temp_auto_supported is False:
                # 자동리포트 미지원 시: 폴링 간격 조정
                if self.ti is not None:
                    self.fc.temp_poll_interval = 1e9
            
            # 위치 자동리포트/폴링 중지
            if self.pos_auto_supported is True:
                # 자동리포트 지원 시: 자동리포트 중지
                try:
                    self.pc.send_command("M154 S0")
                except Exception:
                    pass
            elif self.pos_auto_supported is False:
                # 자동리포트 미지원 시: 폴링 간격 조정
                if self.pi is not None:
                    self.fc.position_poll_interval = 1e9
            
            # M27 폴링 중지 (항상 폴링 사용)
            if self.mi is not None:
                self.fc.m27_poll_interval = 1e9
                
            # 업로드 보호 플래그 설정
            setattr(self.fc, '_upload_guard_active', True)
            
            # 명령 전송 차단
            if self.orig_send:
                self.pc.control.send_command = lambda *_a, **_k: False
                
            # TX inhibit 설정
            setattr(self.pc, 'tx_inhibit', True)
            
            setattr(self.pc, 'rx_paused', True)
            self.pc.sync_mode = True

            time.sleep(0.1)
            print("업로드 보호: 자동리포트/폴링 일시정지")
        except Exception:
            pass
        return self
    
    def __exit__(self, *exc):
        """
        업로드 보호 해제
        
        - 자동리포트 지원 시: 자동리포트 재활성화 (M155 S1, M154 S1)
        - 자동리포트 미지원 시: 원본 폴링 간격 복원
        - M27 폴링: 원본 간격 복원
        - 업로드 보호 플래그 해제
        - 원본 명령 전송 함수 복원
        - TX inhibit 플래그 해제
        """
        try:
            # 온도 자동리포트/폴링 복원
            if self.temp_auto_supported is True:
                # 자동리포트 지원 시: 자동리포트 재활성화
                try:
                    self.pc.send_command("M155 S1")
                except Exception:
                    pass
            elif self.temp_auto_supported is False:
                # 자동리포트 미지원 시: 폴링 간격 복원
                if self.ti is not None:
                    self.fc.temp_poll_interval = self.ti
            
            # 위치 자동리포트/폴링 복원
            if self.pos_auto_supported is True:
                # 자동리포트 지원 시: 자동리포트 재활성화
                try:
                    self.pc.send_command("M154 S1")
                except Exception:
                    pass
            elif self.pos_auto_supported is False:
                # 자동리포트 미지원 시: 폴링 간격 복원
                if self.pi is not None:
                    self.fc.position_poll_interval = self.pi
            
            # M27 폴링 복원 (항상 폴링 사용)
            if self.mi is not None:
                self.fc.m27_poll_interval = self.mi
                
            # 업로드 보호 플래그 해제
            setattr(self.fc, '_upload_guard_active', False)
            
            # 원본 명령 전송 함수 복원
            if self.orig_send:
                self.pc.control.send_command = self.orig_send
                
            # TX inhibit 해제
            setattr(self.pc, 'tx_inhibit', False)

            setattr(self.pc, 'rx_paused', False)
            self.pc.sync_mode = False

            time.sleep(0.1)
            try:
                self.pc._ensure_read_thread()   # 업로드 끝났으면 리더 워커 다시 보장
            except Exception:
                pass

            print("업로드 보호: 자동리포트/폴링 재개")
        except Exception:
            pass


# ========== 파일 처리 유틸리티 ==========

def validate_upload_request(request) -> Tuple[bool, str, Optional[str]]:
    """
    업로드 요청 검증
    
    Flask 요청 객체를 검증하여 업로드 가능 여부와 파일명을 확인합니다.
    
    Args:
        request: Flask 요청 객체
        
    Returns:
        Tuple[bool, str, Optional[str]]: (성공여부, 파일명, 오류메시지)
    """
    # 파일 필드 확인
    if 'file' not in request.files:
        return False, "", "file field missing"
    
    upfile = request.files['file']
    if not upfile.filename:
        return False, "", "no filename"
    
    # 원격 파일명 생성
    name_override = (request.form.get('name') or '').strip()
    remote_name = name_override or upfile.filename
    
    # 파일명 정규화 (안전한 문자만 허용)
    remote_name = re.sub(r'[^A-Za-z0-9._/\-]+', '_', remote_name).lstrip('/')
    if not remote_name:
        return False, "", "invalid remote name"
    
    return True, remote_name, None


def prepare_upload_stream(upfile) -> Tuple[Any, Optional[int], Optional[str]]:
    """
    업로드 스트림 준비 및 크기 확인
    
    업로드 파일의 스트림을 준비하고 총 크기를 확인합니다.
    크기를 확인할 수 없는 경우 임시 파일을 생성합니다.
    
    Args:
        upfile: Flask 파일 객체
        
    Returns:
        Tuple[Any, Optional[int], Optional[str]]: (스트림, 크기, 임시파일경로)
    """
    up_stream = upfile.stream
    total_bytes = None
    tmp_path = None
    
    # Content-Length 헤더에서 크기 확인
    try:
        if getattr(upfile, 'content_length', None):
            total_bytes = int(upfile.content_length)
    except Exception:
        pass
    
    # 스트림에서 크기 확인
    if total_bytes is None:
        try:
            cur = up_stream.tell()
            up_stream.seek(0, os.SEEK_END)
            total_bytes = up_stream.tell()
            up_stream.seek(cur, os.SEEK_SET)
        except Exception:
            # 임시 파일로 저장 후 크기 확인
            tmp = tempfile.NamedTemporaryFile(delete=False)
            tmp_path = tmp.name
            tmp.close()
            upfile.save(tmp_path)
            total_bytes = os.path.getsize(tmp_path)
            up_stream = open(tmp_path, 'rb')
    
    return up_stream, total_bytes, tmp_path


def cleanup_temp_file(tmp_path: Optional[str], up_stream: Any) -> None:
    """
    임시 파일 정리
    
    업로드 완료 후 임시 파일과 스트림을 정리합니다.
    
    Args:
        tmp_path (Optional[str]): 임시 파일 경로
        up_stream: 스트림 객체
    """
    if tmp_path:
        try:
            up_stream.close()
        except Exception:
            pass
        try:
            os.remove(tmp_path)
        except Exception:
            pass
