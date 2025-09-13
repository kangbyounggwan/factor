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
from queue import Empty
try:
    from flask import current_app
except ImportError:
    # Flask가 없는 환경에서도 동작하도록 fallback
    current_app = None


# ========== 유틸리티 함수들 ==========

def drain_response_queue(pc, log: bool = False, tag: str = "DRAIN") -> int:
    """
    업로드/핸드셰이크 전에 남아있는 큐를 비우며(선택), 드롭된 라인도 로깅.
    Returns: 드레인한 개수
    """
    n = 0
    while True:
        try:
            item = pc.response_queue.get_nowait()
            n += 1
            if log and getattr(pc, "logger", None):
                s = getattr(item, "line", None)
                if s is None:
                    s = str(item)
                pc.logger.debug(f"[RXQ][{tag}] drop #{n}: {s}")
        except Empty:
            break
    if log and getattr(pc, "logger", None):
        pc.logger.debug(f"[RXQ][{tag}] drained={n} remain={_qsize_safe(pc.response_queue)}")
    return n


def readline_from_worker(pc, timeout: float = 2.0, tag: str = "GEN") -> str:
    """
    워커가 넣어준 response_queue에서 '한 줄' 꺼내고 로그에 남김.
    타임아웃이면 빈 문자열을 반환하고 그 사실도 로그.
    """
    logger = getattr(pc, "logger", None)
    t0 = time.time()
    try:
        item = pc.response_queue.get(timeout=timeout)
    except Empty:
        if logger:
            logger.info(f"[RXQ][{tag}] timeout after {timeout:.2f}s (qsize={_qsize_safe(pc.response_queue)})")
        return ""

    # 라인/타임스탬프 정리
    line = getattr(item, "line", None)
    ts = getattr(item, "ts", None)
    if line is None:
        line = str(item)
    line = (line or "").strip()

    # 보기 좋은 시간 문자열 구성
    if ts:
        ts_str = time.strftime("%H:%M:%S", time.localtime(float(ts)))
    else:
        ts_str = f"+{(time.time() - t0):.3f}s"

    # 로깅 (INFO에 한 줄, DEBUG에 부가 정보)
    if logger:
        logger.info(f"[RXQ][{tag}] {ts_str} <- {line}")
        logger.debug(f"[RXQ][{tag}] qsize={_qsize_safe(pc.response_queue)} last_rx_time={getattr(pc, 'last_response_time', None)}")

    return line


def _send_text_with_retry_using_worker(pc, payload: str, timeout: float = 3.0) -> bool:
    """
    비체크섬 텍스트 G-code 전송 + response_queue 기반 응답 대기.
    ok / "done saving file" → 성공. resend/rs/error → 재시도.
    """
    ser = pc.serial_conn
    MAX_RETRY = 5

    for _ in range(MAX_RETRY):
        # 전송 (TX는 락으로 보호)
        with pc.serial_lock:
            ser.write((payload + "\n").encode("ascii", "ignore"))
            ser.flush()

        # 응답 대기 (큐에서만)
        end = time.time() + timeout
        while time.time() < end:
            resp = readline_from_worker(pc, timeout=0.6)
            if not resp:
                continue
            low = resp.lower()

            # 성공
            if low.startswith("ok") or ("done saving file" in low):
                return True

            # 정보/대기성 라인 → 무시
            if low.startswith(("busy:", "echo:", "t:", "b:")) or low.strip() == "wait":
                continue

            # 재전송/에러 → 바깥 루프 재시도
            if low.startswith(("resend", "rs", "error:")):
                break

        # 타임아웃 → 다음 루프에서 재시도
    return False



# ========== 핵심 업로드 함수 ==========

def sd_upload(pc, remote_name: str, up_stream, total_bytes: Optional[int] = None,
              remove_comments: bool = False) -> Dict[str, Any]:
    """
    SD 카드로 파일 업로드 (M28/M29 프로토콜) - 큐 기반/고속 버전

    - M28, M29 등 핸드셰이크는 비체크섬 텍스트 전송 + response_queue 로 응답 대기
    - M28~M29 본문은 raw 바이트 스트리밍 (빠름)
    - 업로드 중 동시 통신 충돌을 피하려면 상위에서 UploadGuard 사용 권장

    Args:
        pc: 프린터 통신 객체 (serial_conn, serial_lock, response_queue 필요)
        remote_name (str): SD 카드에 저장될 파일명
        up_stream: 업로드할 파일의 바이트 스트림 (파일 객체)
        total_bytes (Optional[int]): 파일 총 크기 (진행률 표시에 사용)
        remove_comments (bool): G-code 주석 제거 여부

    Returns:
        Dict[str, Any]: {'lines': int, 'bytes': int, 'closed': bool}
    """
    ser = pc.serial_conn
    total_lines = 0
    sent_bytes = 0

    # 텍스트 래핑(주석 제거 모드에서만 사용)
    text_stream = io.TextIOWrapper(up_stream, encoding="utf-8", errors="ignore")

    with pc.serial_lock:
        # (선택) 핸드셰이크 전 잔여 응답 비우기
        drain_response_queue(pc)
        try:
            ser.reset_input_buffer()
        except Exception:
            pass

        # 파일 오픈 (비체크섬)
        if not _send_text_with_retry_using_worker(pc, f"M28 {remote_name}", timeout=5.0):
            raise Exception("M28 open failed (no ok)")

        # 본문 전송: raw 바이트 스트리밍
        LOG_BYTES = 512 * 1024  # 512KB 마다 진행률 로그
        CHUNK = 16 * 1024       # 16KB write
        FLUSH_EVERY = 64 * 1024 # 64KB flush
        acc = 0
        flush_acc = 0
        last_log = time.time()

        if remove_comments:
            # 한 줄씩 읽어 주석 제거 후 누적 → 바이트로 변환하여 청크 송신
            processed: list[str] = []
            for raw in text_stream:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                semi = line.find(";")
                if semi >= 0:
                    line = line[:semi].rstrip()
                if not line:
                    continue
                processed.append(line)
            content_bytes = ("\n".join(processed)).encode("utf-8", "ignore")
            total_lines = len(processed)

            for i in range(0, len(content_bytes), CHUNK):
                chunk = content_bytes[i:i+CHUNK]
                ser.write(chunk)
                sent_bytes += len(chunk)
                acc += len(chunk)
                flush_acc += len(chunk)

                if flush_acc >= FLUSH_EVERY:
                    ser.flush()
                    flush_acc = 0

                if (acc >= LOG_BYTES) or (time.time() - last_log >= 1.0):
                    if total_bytes:
                        pct = (sent_bytes / float(total_bytes)) * 100.0
                        if current_app and hasattr(current_app, "logger"):
                            current_app.logger.info(
                                f"SD 업로드 진행: {sent_bytes}/{total_bytes} bytes ({pct:.1f}%)"
                            )
                    else:
                        if current_app and hasattr(current_app, "logger"):
                            current_app.logger.info(f"SD 업로드 진행: {sent_bytes} bytes")
                    acc = 0
                    last_log = time.time()

            ser.flush()

        else:
            # 주석 제거 없이 라인 그대로 전송 (버퍼링/청크/주기적 flush)
            buf = bytearray()
            for raw in text_stream:
                line_bytes = raw.encode("utf-8", "ignore")
                buf.extend(line_bytes)
                sent_bytes += len(line_bytes)
                total_lines += 1
                acc += len(line_bytes)
                flush_acc += len(line_bytes)

                if len(buf) >= CHUNK:
                    ser.write(buf)
                    buf.clear()

                if flush_acc >= FLUSH_EVERY:
                    ser.flush()
                    flush_acc = 0

                if (acc >= LOG_BYTES) or (time.time() - last_log >= 1.0):
                    if total_bytes:
                        pct = (sent_bytes / float(total_bytes)) * 100.0
                        if current_app and hasattr(current_app, "logger"):
                            current_app.logger.info(
                                f"SD 업로드 진행: {sent_bytes}/{total_bytes} bytes ({pct:.1f}%)"
                            )
                    else:
                        if current_app and hasattr(current_app, "logger"):
                            current_app.logger.info(f"SD 업로드 진행: {sent_bytes} bytes")
                    acc = 0
                    last_log = time.time()

            if buf:
                ser.write(buf)
            ser.flush()

        # 파일 닫기(M29) + ok 대기 (큐에서만)
        if not _send_text_with_retry_using_worker(pc, "M29", timeout=6.0):
            raise Exception("M29 close failed (no ok)")

        # (선택) 끝에 M20 등 보조 명령을 날리려면 여기서 사용
        # _send_text_with_retry_using_worker(pc, "M20", timeout=3.0)

    return {"lines": total_lines, "bytes": sent_bytes, "closed": True}


# ========== 업로드 보호 메커니즘 ==========
class UploadGuard:
    """
    업로드 중 시스템 보호 컨텍스트 매니저

    - 자동리포트 지원 시: M155/M154 끄기
    - 미지원 시: 폴링 간격을 매우 크게 설정
    - M27 폴링 간격도 크게 조정
    - 외부 send_command 차단, tx_inhibit 설정
    - (주의) RX 워커는 계속 동작해야 하므로 pause하지 않음
    """
    def __init__(self, fc, pc):
        self.fc = fc
        self.pc = pc

        self.temp_auto_supported = getattr(fc, "M155_auto_supported", None)
        self.pos_auto_supported  = getattr(fc, "M154_auto_supported", None)

        self.ti = getattr(fc, "temp_poll_interval", None)
        self.pi = getattr(fc, "position_poll_interval", None)
        self.mi = getattr(fc, "m27_poll_interval", None)

        self.orig_send = getattr(getattr(pc, "control", None), "send_command", None)

    def __enter__(self):
        try:
            # 자동리포트/폴링 일시정지
            if self.temp_auto_supported is True:
                try: self.pc.send_command("M155 S0")
                except Exception: pass
            elif self.temp_auto_supported is False and self.ti is not None:
                self.fc.temp_poll_interval = 1e9

            if self.pos_auto_supported is True:
                try: self.pc.send_command("M154 S0")
                except Exception: pass
            elif self.pos_auto_supported is False and self.pi is not None:
                self.fc.position_poll_interval = 1e9

            if self.mi is not None:
                self.fc.m27_poll_interval = 1e9

            # 플래그/차단
            setattr(self.fc, "_upload_guard_active", True)
            setattr(self.pc, "tx_inhibit", True)

            if self.orig_send:
                self.pc.control.send_command = lambda *_a, **_k: False

            if current_app and hasattr(current_app, "logger"):
                current_app.logger.info("업로드 보호: 자동리포트/폴링 일시정지")
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        try:
            # 원복
            if self.temp_auto_supported is True:
                try: self.pc.send_command("M155 S1")
                except Exception: pass
            elif self.temp_auto_supported is False and self.ti is not None:
                self.fc.temp_poll_interval = self.ti

            if self.pos_auto_supported is True:
                try: self.pc.send_command("M154 S1")
                except Exception: pass
            elif self.pos_auto_supported is False and self.pi is not None:
                self.fc.position_poll_interval = self.pi

            if self.mi is not None:
                self.fc.m27_poll_interval = self.mi

            setattr(self.fc, "_upload_guard_active", False)
            setattr(self.pc, "tx_inhibit", False)

            if self.orig_send:
                self.pc.control.send_command = self.orig_send

            if current_app and hasattr(current_app, "logger"):
                current_app.logger.info("업로드 보호: 자동리포트/폴링 재개")
        except Exception:
            pass


# ========== 파일 처리 유틸리티 ==========

def validate_upload_request(request) -> Tuple[bool, str, Optional[str]]:
    """
    업로드 요청 검증 → (성공여부, 파일명, 오류메시지)
    """
    if "file" not in request.files:
        return False, "", "file field missing"

    upfile = request.files["file"]
    if not upfile.filename:
        return False, "", "no filename"

    name_override = (request.form.get("name") or "").strip()
    remote_name = name_override or upfile.filename
    remote_name = re.sub(r"[^A-Za-z0-9._/\-]+", "_", remote_name).lstrip("/")
    if not remote_name:
        return False, "", "invalid remote name"

    return True, remote_name, None


def prepare_upload_stream(upfile) -> Tuple[Any, Optional[int], Optional[str]]:
    """
    업로드 스트림 준비 및 크기 확인 → (스트림, 크기, 임시파일경로)
    """
    up_stream = upfile.stream
    total_bytes = None
    tmp_path = None

    # 헤더에서 크기
    try:
        if getattr(upfile, "content_length", None):
            total_bytes = int(upfile.content_length)
    except Exception:
        pass

    # 스트림에서 크기
    if total_bytes is None:
        try:
            cur = up_stream.tell()
            up_stream.seek(0, os.SEEK_END)
            total_bytes = up_stream.tell()
            up_stream.seek(cur, os.SEEK_SET)
        except Exception:
            # 임시 파일 저장 후 크기 확인
            tmp = tempfile.NamedTemporaryFile(delete=False)
            tmp_path = tmp.name
            tmp.close()
            upfile.save(tmp_path)
            total_bytes = os.path.getsize(tmp_path)
            up_stream = open(tmp_path, "rb")

    return up_stream, total_bytes, tmp_path


def cleanup_temp_file(tmp_path: Optional[str], up_stream: Any) -> None:
    """임시 파일/스트림 정리"""
    if tmp_path:
        try:
            up_stream.close()
        except Exception:
            pass
        try:
            os.remove(tmp_path)
        except Exception:
            pass