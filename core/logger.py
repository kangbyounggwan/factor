"""
로깅 시스템
전원 차단에 안전한 로깅 및 RAM 디스크 지원
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any
import colorlog


class RAMLogHandler(logging.handlers.MemoryHandler):
    """RAM 기반 로그 핸들러 (전원 차단 시 로그 손실 방지)"""
    
    def __init__(self, capacity: int = 1000, target_file: Optional[str] = None):
        # 메모리 핸들러 초기화
        super().__init__(capacity)
        
        self.target_file = target_file
        self.file_handler = None
        
        if target_file:
            # 파일 핸들러 설정
            log_dir = Path(target_file).parent
            log_dir.mkdir(parents=True, exist_ok=True)
            
            self.file_handler = logging.handlers.RotatingFileHandler(
                target_file,
                maxBytes=10*1024*1024,  # 10MB
                backupCount=5
            )
            
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            self.file_handler.setFormatter(formatter)
            self.setTarget(self.file_handler)
    
    def shouldFlush(self, record):
        """로그 플러시 조건"""
        # 에러 레벨 이상이면 즉시 플러시
        return record.levelno >= logging.ERROR or len(self.buffer) >= self.capacity


class SystemdJournalHandler(logging.Handler):
    """systemd journal 핸들러"""
    
    def __init__(self):
        super().__init__()
        self.available = self._check_systemd_available()
    
    def _check_systemd_available(self) -> bool:
        """systemd 사용 가능 여부 확인"""
        try:
            import systemd.journal
            return True
        except ImportError:
            return False
    
    def emit(self, record):
        """로그 레코드 전송"""
        if not self.available:
            return
        
        try:
            import systemd.journal
            
            # 로그 레벨 매핑
            level_map = {
                logging.DEBUG: systemd.journal.LOG_DEBUG,
                logging.INFO: systemd.journal.LOG_INFO,
                logging.WARNING: systemd.journal.LOG_WARNING,
                logging.ERROR: systemd.journal.LOG_ERR,
                logging.CRITICAL: systemd.journal.LOG_CRIT
            }
            
            priority = level_map.get(record.levelno, systemd.journal.LOG_INFO)
            message = self.format(record)
            
            systemd.journal.send(
                message,
                PRIORITY=priority,
                LOGGER_NAME=record.name,
                CODE_FILE=record.pathname,
                CODE_LINE=record.lineno,
                CODE_FUNC=record.funcName
            )
            
        except Exception:
            # systemd journal 실패 시 무시
            pass


def setup_logger(config: Dict[str, Any], name: str = None) -> logging.Logger:
    """로거 설정"""
    
    # 로거 생성
    logger_name = name or 'factor-client'
    logger = logging.getLogger(logger_name)
    root_logger = logging.getLogger()
    
    # 기존 핸들러 제거
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # 로그 레벨 설정
    log_level = config.get('level', 'INFO').upper()
    level_value = getattr(logging, log_level, logging.INFO)
    logger.setLevel(level_value)
    root_logger.setLevel(level_value)
    
    # 콘솔 핸들러 (컬러 로깅)
    console_handler = colorlog.StreamHandler()
    console_formatter = colorlog.ColoredFormatter(
        '%(log_color)s%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red,bg_white',
        }
    )
    console_handler.setFormatter(console_formatter)
    # 모든 서브 로거 로그가 동일하게 파일/콘솔로 가도록 루트 로거에 연결
    root_logger.addHandler(console_handler)
    
    # 파일 핸들러 설정
    log_file = config.get('file')
    if log_file:
        # RAM 로그 핸들러 사용 (전원 차단 보호)
        if config.get('log_to_ram', False):
            ram_handler = RAMLogHandler(
                capacity=1000,
                target_file=log_file
            )
            root_logger.addHandler(ram_handler)
        else:
            # 일반 로테이팅 파일 핸들러
            log_dir = Path(log_file).parent
            log_dir.mkdir(parents=True, exist_ok=True)
            
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=_parse_size(config.get('max_size', '10MB')),
                backupCount=config.get('backup_count', 5)
            )
            
            file_formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            file_handler.setFormatter(file_formatter)
            root_logger.addHandler(file_handler)
    
    # systemd journal 핸들러 (systemd 환경에서)
    if _is_systemd_environment():
        journal_handler = SystemdJournalHandler()
        if journal_handler.available:
            journal_formatter = logging.Formatter(
                '%(name)s - %(levelname)s - %(message)s'
            )
            journal_handler.setFormatter(journal_formatter)
            root_logger.addHandler(journal_handler)
    
    # 예외 로깅 설정
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        
        logger.critical(
            "처리되지 않은 예외 발생",
            exc_info=(exc_type, exc_value, exc_traceback)
        )
    
    sys.excepthook = handle_exception
    
    # 중복 기록 방지: 개별 명명 로거는 상위로 전파만 하고 자체 핸들러는 두지 않음
    logger.propagate = True
    logger.info(f"로거 설정 완료: {logger_name}")
    return logger


def _parse_size(size_str: str) -> int:
    """크기 문자열을 바이트로 변환"""
    size_str = size_str.upper().strip()
    
    if size_str.endswith('KB'):
        return int(size_str[:-2]) * 1024
    elif size_str.endswith('MB'):
        return int(size_str[:-2]) * 1024 * 1024
    elif size_str.endswith('GB'):
        return int(size_str[:-2]) * 1024 * 1024 * 1024
    else:
        return int(size_str)


def _is_systemd_environment() -> bool:
    """systemd 환경 여부 확인"""
    return (
        os.path.exists('/run/systemd/system') or
        os.getenv('SYSTEMD_EXEC_PID') is not None
    )


class PerformanceLogger:
    """성능 측정 로거"""
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
    
    def __enter__(self):
        import time
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        import time
        elapsed = time.time() - self.start_time
        self.logger.debug(f"실행 시간: {elapsed:.3f}초")


def get_logger(name: str = None) -> logging.Logger:
    """로거 인스턴스 반환"""
    logger_name = name or 'factor-client'
    return logging.getLogger(logger_name)


def log_system_info(logger: logging.Logger):
    """시스템 정보 로깅"""
    try:
        import platform
        import psutil
        
        logger.info("=== 시스템 정보 ===")
        logger.info(f"OS: {platform.system()} {platform.release()}")
        logger.info(f"Python: {platform.python_version()}")
        logger.info(f"CPU 코어: {psutil.cpu_count()}")
        logger.info(f"메모리: {psutil.virtual_memory().total // (1024**3)}GB")
        
        # 라즈베리파이 정보
        try:
            with open('/proc/cpuinfo', 'r') as f:
                cpuinfo = f.read()
                if 'Raspberry Pi' in cpuinfo:
                    for line in cpuinfo.split('\n'):
                        if 'Model' in line:
                            logger.info(f"하드웨어: {line.split(':')[1].strip()}")
                            break
        except:
            pass
        
        logger.info("==================")
        
    except Exception as e:
        logger.warning(f"시스템 정보 수집 실패: {e}") 