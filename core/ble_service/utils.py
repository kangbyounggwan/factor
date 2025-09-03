import time
import json
import logging
from typing import Any, Dict


def json_bytes(obj: Dict[str, Any]) -> bytes:
    """주어진 딕셔너리를 UTF-8 JSON 바이트로 직렬화.

    - 입력: 파이썬 딕셔너리
    - 성공: JSON 바이트(bytes)
    - 실패: 예외를 로깅하고 빈 JSON(b'{}') 반환
    """
    try:
        return json.dumps(obj, ensure_ascii=False).encode('utf-8', errors='ignore')
    except Exception:
        logging.getLogger('ble-gatt').exception("json dumps 실패")
        return b'{}'


def now_ts() -> int:
    """현재 UNIX 타임스탬프(초)를 정수로 반환."""
    return int(time.time())


def now_ms() -> int:
    """현재 UNIX 타임스탬프(밀리초)를 정수로 반환."""
    return int(time.time() * 1000)


