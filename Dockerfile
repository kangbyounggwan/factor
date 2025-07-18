# Factor OctoPrint Client Firmware
# 라즈베리파이용 최적화된 Docker 이미지

FROM python:3.9-slim-bullseye

# 메타데이터
LABEL maintainer="Factor Client Team"
LABEL description="Factor OctoPrint Client Firmware for Raspberry Pi"
LABEL version="1.0.0"

# 환경 변수
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV FACTOR_LOG_LEVEL=INFO
ENV FACTOR_CONFIG_PATH=/app/config/settings.yaml

# 시스템 패키지 설치
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    curl \
    i2c-tools \
    && rm -rf /var/lib/apt/lists/*

# 작업 디렉토리 설정
WORKDIR /app

# Python 의존성 복사 및 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드 복사
COPY . .

# 권한 설정
RUN chmod +x main.py

# 사용자 생성
RUN useradd -r -s /bin/false -d /app factor
RUN chown -R factor:factor /app

# 볼륨 마운트 포인트
VOLUME ["/app/config", "/app/logs"]

# 포트 노출
EXPOSE 8080

# 헬스 체크
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# 사용자 변경
USER factor

# 실행 명령
CMD ["python", "main.py"] 