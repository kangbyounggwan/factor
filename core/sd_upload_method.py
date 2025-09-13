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
import os
import re
import time
import tempfile
from typing import Optional, Tuple, Dict, Any
try:
    from flask import current_app
except ImportError:
    # Flask가 없는 환경에서도 동작하도록 fallback
    current_app = None


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

def sd_upload(pc, remote_name: str, up_stream, total_bytes: Optional[int] = None, remove_comments: bool = False) -> Dict[str, Any]:
    """
    SD 카드로 파일 업로드 (M28/M29 프로토콜) - 고속 버전
    
    Marlin 펌웨어의 M28/M29 프로토콜을 사용하여 파일을 SD 카드에 업로드합니다.
    M28~M29 사이의 본문은 raw 바이트 스트리밍으로 전송하여 고속 업로드를 구현합니다.
    라인 번호링과 체크섬은 핸드셰이크에만 사용하고, 본문은 직접 스트리밍합니다.
    
    Args:
        pc: 프린터 통신 객체 (serial_conn, serial_lock 속성 필요)
        remote_name (str): SD 카드에 저장될 파일명
        up_stream: 업로드할 파일의 스트림 객체
        total_bytes (Optional[int]): 파일의 총 크기 (진행률 표시용)
        remove_comments (bool): 주석 제거 여부 (기본값: False)
        
    Returns:
        Dict[str, Any]: 업로드 결과 정보
            - lines (int): 전송된 라인 수
            - bytes (int): 전송된 바이트 수
            - closed (bool): 업로드 완료 여부
            
    Raises:
        Exception: 시리얼 통신 오류, 파일 처리 오류 등
        
    Note:
        이 함수는 시리얼 락을 사용하여 동시 접근을 방지합니다.
        업로드 중에는 다른 시리얼 통신이 차단됩니다.
    """
    ser = pc.serial_conn
    total_lines = 0
    sent_bytes = 0

    # 텍스트 스트림으로 변환 (유효하지 않은 바이트는 무시)
    text_stream = io.TextIOWrapper(up_stream, encoding="utf-8", errors="ignore")

    with pc.serial_lock:
        # 입력 버퍼 정리 (이전 응답 잔여물 제거)
        try:
            ser.reset_input_buffer()
        except Exception:
            pass

        n = 0
        # 라인 번호 동기화 후 파일 열기
        try:
            n = _send_with_retry(ser, n, "M110 N0", timeout=2.0)
        except Exception:
            pass
        n = _send_with_retry(ser, n, f"M28 {remote_name}", timeout=5.0)

        # 파일 내용 raw 바이트 스트리밍 (고속 전송)
        LOG_BYTES = 512 * 1024  # 512KB마다 진행률 로그
        acc = 0
        last_log = time.time()
        
        # 주석 제거가 필요한 경우 텍스트 모드로 처리
        if remove_comments:
            processed_content = []
            for raw in text_stream:
                payload = raw.rstrip("\r\n")
                if payload:
                    comment_pos = payload.find(';')
                    if comment_pos >= 0:
                        payload = payload[:comment_pos].rstrip()
                if payload:  # 빈 라인 제외
                    processed_content.append(payload)
            
            # 처리된 내용을 바이트로 변환하여 스트리밍
            content_bytes = '\n'.join(processed_content).encode('utf-8', 'ignore')
            total_lines = len(processed_content)
            
            # 청크 단위로 raw 바이트 전송
            chunk_size = 1024  # 1KB 청크
            for i in range(0, len(content_bytes), chunk_size):
                chunk = content_bytes[i:i + chunk_size]
                ser.write(chunk)
                ser.flush()
                
                sent_bytes += len(chunk)
                acc += len(chunk)
                
                # 진행률 로깅
                if (acc >= LOG_BYTES) or (time.time() - last_log >= 1.0):
                    if total_bytes:
                        pct = (sent_bytes / total_bytes) * 100.0
                        if current_app and hasattr(current_app, 'logger'):
                            current_app.logger.info(f"SD 업로드 진행: {sent_bytes}/{total_bytes} bytes ({pct:.1f}%)")
                    else:
                        if current_app and hasattr(current_app, 'logger'):
                            current_app.logger.info(f"SD 업로드 진행: {sent_bytes} bytes")
                    acc = 0
                    last_log = time.time()
        else:
            # 주석 제거 없이 raw 바이트 스트리밍
            for raw in text_stream:
                line_bytes = raw.encode('utf-8', 'ignore')
                ser.write(line_bytes)
                ser.flush()
                
                sent_bytes += len(line_bytes)
                total_lines += 1
                acc += len(line_bytes)
                
                # 진행률 로깅
                if (acc >= LOG_BYTES) or (time.time() - last_log >= 1.0):
                    if total_bytes:
                        pct = (sent_bytes / total_bytes) * 100.0
                        if current_app and hasattr(current_app, 'logger'):
                            current_app.logger.info(f"SD 업로드 진행: {sent_bytes}/{total_bytes} bytes ({pct:.1f}%)")
                    else:
                        if current_app and hasattr(current_app, 'logger'):
                            current_app.logger.info(f"SD 업로드 진행: {sent_bytes} bytes")
                    acc = 0
                    last_log = time.time()

        # 파일 닫기 및 완료 확인
        ser.write(b"\nM29\n")
        ser.flush()
        _ = _readline(ser, timeout=5.0)  # "ok" 또는 "done saving file" 응답 대기
        
        # 라인 번호 리셋
        try:
            ser.write(b"\nM110 N0\n")
            ser.flush()
        except Exception:
            pass

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
            if current_app and hasattr(current_app, 'logger'):
                current_app.logger.info("업로드 보호: 자동리포트/폴링 일시정지")
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
                    # RX 가디언 재활성화
            # if hasattr(self.fc, '_rx_guard_running'):
            #     self.fc._rx_guard_running = True
            #     self.fc._start_rx_guardian()

            if current_app and hasattr(current_app, 'logger'):
                current_app.logger.info("업로드 보호: 자동리포트/폴링 재개")
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
