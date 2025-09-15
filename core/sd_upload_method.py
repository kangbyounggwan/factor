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

def _xor(s: str) -> int:
    """
    문자열의 XOR 체크섬 계산
    
    Marlin 펌웨어에서 사용하는 체크섬 알고리즘으로,
    각 문자의 ASCII 값을 XOR 연산하여 단일 바이트 체크섬을 생성합니다.
    
    Args:
        s (str): 체크섬을 계산할 문자열
        
    Returns:
        int: 계산된 체크섬 값 (0-255)
        
    Example:
        >>> _xor("G28")
        123
    """
    v = 0
    for ch in s:
        v ^= ord(ch)
    return v


def _nline(n: int, payload: str) -> bytes:
    """
    라인 번호와 체크섬이 포함된 G-code 명령 생성
    
    Marlin의 라인 번호링 시스템에 따라 "N{번호} {명령}*{체크섬}" 형식으로
    명령을 생성합니다. 이를 통해 프린터가 명령의 순서와 무결성을 확인할 수 있습니다.
    
    Args:
        n (int): 라인 번호 (0부터 시작)
        payload (str): 실제 G-code 명령
        
    Returns:
        bytes: 인코딩된 명령 (ASCII)
        
    Example:
        >>> _nline(1, "G28")
        b'N1 G28*123\\n'
    """
    line = f"N{n} {payload}"
    return f"{line}*{_xor(line)}\n".encode("ascii", "ignore")

def _wait_ok_or_keywords(ser, timeout=5.0):
    """
    ok / Writing to file / open / done saving file 같은 키워드가 나오면 True
    타임아웃이면 False
    """
    end = time.time() + timeout
    while time.time() < end:
        line = _readline(ser, timeout=0.5)
        if not line:
            continue
        low = line.lower()
        if low.startswith("ok") or "writing" in low or "open" in low or "done saving file" in low:
            # 디버깅에 도움되는 로그
            print(f"wait_ok_or_keywords: {line}")
            return True
        # echo:, busy: 등은 무시
    return False



def _readline(ser, timeout: float = 2.0) -> str:
    """
    타임아웃이 있는 시리얼 라인 읽기
    
    지정된 시간 동안 시리얼 포트에서 한 줄을 읽어옵니다.
    타임아웃이 발생하면 빈 문자열을 반환합니다.
    
    Args:
        ser: 시리얼 연결 객체
        timeout (float): 타임아웃 시간 (초)
        
    Returns:
        str: 읽어온 라인 (공백 제거됨) 또는 빈 문자열
        
    Note:
        UTF-8 디코딩 시 오류가 발생하면 무시하고 계속 진행합니다.
    """
    end = time.time() + timeout
    while time.time() < end:
        raw = ser.readline()
        if raw:
            return raw.decode("utf-8", "ignore").strip()
        time.sleep(0.005)
    return ""


def _send_with_retry(ser, n: int, payload: str, timeout: float = 3.0) -> int:
    """
    순차적 명령 전송 및 재전송 처리
    
    라인 번호가 포함된 명령을 전송하고, 프린터의 응답을 확인합니다.
    "ok" 또는 "done saving file" 응답을 받으면 다음 라인 번호를 반환하고,
    "resend" 또는 "rs" 응답을 받으면 같은 라인을 재전송합니다.
    
    Args:
        ser: 시리얼 연결 객체
        n (int): 현재 라인 번호
        payload (str): 전송할 G-code 명령
        timeout (float): 응답 대기 타임아웃 (초)
        
    Returns:
        int: 다음 라인 번호 (성공 시 n+1)
        
    Note:
        순차적 모드에서는 한 번에 하나의 명령만 전송하여
        프린터의 처리 능력에 맞춰 안정성을 확보합니다.
    """
    while True:
        # 명령 전송
        ser.write(_nline(n, payload))
        ser.flush()
        
        # 응답 대기
        end = time.time() + timeout
        while time.time() < end:
            resp = _readline(ser, timeout=0.5).lower()
            if not resp:
                continue
                
            # 성공 응답 확인
            if resp.startswith("ok") or ("done saving file" in resp):
                return n + 1
                
            # 재전송 요청 확인
            if resp.startswith("resend") or resp.startswith("rs"):
                # 현재 라인을 재전송하기 위해 루프 계속
                break
                
            # 기타 응답 (echo:, t:, busy:, wait 등)은 무시하고 계속 대기
        # 재전송을 위해 루프 계속


# ========== 핵심 업로드 함수 ==========

def sd_upload(pc, remote_name: str, up_stream, total_bytes: Optional[int] = None,
              remove_comments: bool = False, upload_id: Optional[str] = None) -> Dict[str, Any]:
    """
    SD 카드로 파일 업로드 (M28/M29 프로토콜) - 고속 버전

    흐름:
      1) N코드로 M110 N0 → 라인 넘버 리셋
      2) N코드로 M28 <name> → 파일 오픈
      3) (중요) M28~M29 사이 본문은 raw 바이트 스트리밍
      4) 평문 M29 → 파일 닫기
      5) 완료 응답 대기 → MQTT 최종 100%
    """
    ser = pc.serial_conn
    total_lines = 0
    sent_bytes = 0

    # 업스트림 위치/크기 파악 (필요 시 임시파일 사용)
    # 이미 호출측에서 prepare_upload_stream()을 쓰는 경우 그대로 넘어온다고 가정
    # 여기서는 up_stream이 바이너리 모드라고 가정하고, 모르면 바이너리로 감쌉니다.
    if hasattr(up_stream, "read") and not isinstance(up_stream, (io.BufferedReader, io.BytesIO)):
        # 텍스트 스트림으로 넘어왔으면 바이너리 래핑
        try:
            up_stream = up_stream.buffer  # 텍스트 IO의 buffer
        except Exception:
            # 마지막 보루: 전체를 바이트로 읽어 BytesIO에 담음
            data = up_stream.read()
            if isinstance(data, str):
                data = data.encode("utf-8", "ignore")
            up_stream = io.BytesIO(data)
            total_bytes = len(data)

    with pc.serial_lock:
        # 잔여 응답 정리
        try:
            ser.reset_input_buffer()
        except Exception:
            pass

        # 라인 번호 동기화 + 파일 열기 (N코드 + 체크섬)
        n = 0
        try:
            n = _send_with_retry(ser, n, "M110 N0", timeout=2.0)  # => N0 M110 N0*cs
        except Exception:
            # 일부 FW는 이미 동기화된 상태일 수 있으므로 무시 가능
            pass

        n = _send_with_retry(ser, n, f"M28 {remote_name}", timeout=7.0)  # => N1 M28 name*cs
        # 'Now fresh file' / 'Writing to file' 응답이 오도록 잠시 대기
        _wait_ok_or_keywords(ser, timeout=3.0)

        # MQTT 진행률 발행 함수
        def _pub_progress(final: bool = False):
            try:
                if not _mqtt_service_instance:
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

        # === 본문 스트리밍 ===
        LOG_BYTES = 512 * 1024  # 512KB마다 로그
        acc = 0
        last_log = time.time()

        if remove_comments:
            # 텍스트로 한 줄씩 읽어 주석 제거 → 바이트로 변환 후 청크 전송
            text = io.TextIOWrapper(up_stream, encoding="utf-8", errors="ignore", newline=None)
            processed = []
            for raw in text:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                # 세미콜론 주석 제거
                cpos = line.find(";")
                if cpos >= 0:
                    line = line[:cpos].rstrip()
                if line:
                    processed.append(line)
            content = ("\n".join(processed) + "\n").encode("utf-8", "ignore")
            total_lines = len(processed)

            # 청크 전송
            CHUNK = 1024
            for i in range(0, len(content), CHUNK):
                chunk = content[i:i+CHUNK]
                ser.write(chunk)
                ser.flush()

                sent_bytes += len(chunk)
                acc += len(chunk)

                # 진행률 출력 & MQTT
                if (acc >= LOG_BYTES) or (time.time() - last_log >= 1.0):
                    if total_bytes:
                        print(f"SD 업로드 진행: {sent_bytes}/{total_bytes} bytes ({(sent_bytes/total_bytes)*100:.1f}%)")
                    else:
                        print(f"SD 업로드 진행: {sent_bytes} bytes")
                    _pub_progress()
                    acc = 0
                    last_log = time.time()
        else:
            # **원본 그대로 raw 바이트 스트리밍** (가장 호환성 높음)
            # 가능하면 총 크기 추정
            if total_bytes is None:
                try:
                    cur = up_stream.tell()
                    up_stream.seek(0, os.SEEK_END)
                    total_bytes = up_stream.tell()
                    up_stream.seek(cur, os.SEEK_SET)
                except Exception:
                    pass

            CHUNK = 1024
            while True:
                chunk = up_stream.read(CHUNK)
                if not chunk:
                    break
                # EOL 정규화가 필요하면 여기서 처리 가능 (보통 그대로가 안전)
                # chunk = chunk.replace(b"\r\n", b"\n")  # 필요시

                ser.write(chunk)
                ser.flush()

                sent_bytes += len(chunk)
                acc += len(chunk)
                total_lines += 1  # 대략적인 라인 카운트(엄밀하진 않음)

                if (acc >= LOG_BYTES) or (time.time() - last_log >= 1.0):
                    if total_bytes:
                        print(f"SD 업로드 진행: {sent_bytes}/{total_bytes} bytes ({(sent_bytes/total_bytes)*100:.1f}%)")
                    else:
                        print(f"SD 업로드 진행: {sent_bytes} bytes")
                    _pub_progress()
                    acc = 0
                    last_log = time.time()

        # 파일 닫기: 평문 M29 (대부분 FW에서 안전)
        ser.write(b"M29\n")
        ser.flush()

        # 완료 키워드/ok 대기
        _wait_ok_or_keywords(ser, timeout=10.0)

        # 최종 100% 발행
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
