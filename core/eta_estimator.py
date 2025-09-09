import re
import time
from dataclasses import dataclass
from typing import Optional, Tuple

M27_RE = re.compile(r"\bSD\s+printing\s+byte\s+(\d+)\s*/\s*(\d+)", re.I)


def parse_m27(line: str) -> Optional[Tuple[int, int]]:
    """
    'SD printing byte 286601/8227542' -> (286601, 8227542)
    못 찾으면 None
    """
    m = M27_RE.search(line or "")
    if not m:
        return None
    done = int(m.group(1))
    total = int(m.group(2))
    # total==0 보호
    if total <= 0:
        return None
    return done, total


def fmt_hms(seconds: float) -> str:
    """
    3661.2 -> '1:01:01'
    음수/None 보호
    """
    if seconds is None or seconds != seconds:  # NaN check
        return "--:--:--"
    s = max(0, int(round(seconds)))
    h = s // 3600
    m = (s % 3600) // 60
    s = s % 60
    return f"{h}:{m:02d}:{s:02d}"


@dataclass
class ETAResult:
    progress: float           # 0.0~100.0
    rate_bps: float           # bytes per second (EWMA)
    elapsed_s: float          # seconds
    remaining_s: Optional[float]  # seconds (None if unknown)
    eta_str: str              # formatted remaining
    elapsed_str: str          # formatted elapsed


class EtaEstimator:
    """
    M27 기반 ETA 추정기.
    - update_line(line)로 M27 라인 전달.
    - 또는 update_bytes(done,total)로 직접 전달.
    - EWMA로 속도 추정(half-life 설정 가능).
    """
    def __init__(self, half_life_s: float = 20.0):
        """
        half_life_s: 속도 EWMA 반감기(초). 작을수록 반응 빠름, 클수록 안정적.
        """
        self.half_life_s = max(1.0, float(half_life_s))
        self._alpha_cache = {}  # dt별 alpha 캐시
        self._t0 = None         # 시작 시각
        self._last_t = None     # 마지막 샘플 시각
        self._last_done = None  # 마지막 누적 바이트
        self._total = None
        self._rate_ewma = None  # bytes/sec
        self._started = False

    def reset(self):
        self._t0 = None
        self._last_t = None
        self._last_done = None
        self._total = None
        self._rate_ewma = None
        self._started = False

    def _alpha(self, dt: float) -> float:
        # half-life => alpha = 1 - 0.5**(dt/half_life)
        dt = max(0.0, float(dt))
        key = round(dt, 3)
        a = self._alpha_cache.get(key)
        if a is not None:
            return a
        a = 1.0 - pow(0.5, dt / self.half_life_s) if dt > 0 else 1.0
        self._alpha_cache[key] = a
        return a

    def update_line(self, line: str) -> Optional[ETAResult]:
        parsed = parse_m27(line)
        if not parsed:
            return None
        return self.update_bytes(*parsed)

    def update_bytes(self, done: int, total: int) -> ETAResult:
        now = time.time()
        if total <= 0:
            # total 모르면 진행률/ETA 계산 불가
            total = 0

        # 새 작업 시작/재시작 감지: done이 크게 줄었거나 total이 바뀜
        if (
            self._total is not None and (
                total != self._total or
                (self._last_done is not None and done + 1024 < self._last_done)  # 1KB 이상 역행하면 리셋
            )
        ):
            self.reset()

        if not self._started:
            self._t0 = now
            self._started = True

        # 속도 샘플 (instantaneous)
        if self._last_t is not None and done >= (self._last_done or 0):
            dt = max(1e-6, now - self._last_t)
            inst_rate = (done - (self._last_done or 0)) / dt  # B/s
            # EWMA 업데이트
            a = self._alpha(dt)
            if self._rate_ewma is None:
                self._rate_ewma = inst_rate
            else:
                self._rate_ewma = (1 - a) * self._rate_ewma + a * inst_rate

        # 상태 갱신
        self._last_t = now
        self._last_done = done
        self._total = total

        # 결과 계산
        progress = (100.0 * done / total) if total > 0 else 0.0
        elapsed = (now - self._t0) if self._t0 else 0.0

        remaining_s = None
        if self._rate_ewma and self._rate_ewma > 1e-6 and total > 0:
            remain_bytes = max(0, total - done)
            remaining_s = remain_bytes / self._rate_ewma

        return ETAResult(
            progress=progress,
            rate_bps=self._rate_ewma or 0.0,
            elapsed_s=elapsed,
            remaining_s=remaining_s,
            eta_str=fmt_hms(remaining_s),
            elapsed_str=fmt_hms(elapsed),
        )


