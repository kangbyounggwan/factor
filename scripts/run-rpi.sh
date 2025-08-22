#!/bin/bash
# 라즈베리파이 환경에서 Factor Client Firmware 실행 스크립트

echo "=== Factor Client Firmware - Raspberry Pi 실행 ==="
echo "환경: Raspberry Pi"
echo "설정 파일: config/settings_rpi.yaml"
echo ""

# 프로젝트 루트 디렉토리로 이동
cd "$(dirname "$0")/.."

# Python 가상환경 활성화 (있는 경우)
if [ -d "venv" ]; then
    echo "가상환경 활성화 중..."
    source venv/bin/activate
fi

# 필요한 패키지 설치 확인
echo "필요한 패키지 확인 중..."
pip install -r requirements.txt

# 라즈베리파이 환경 설정으로 실행
echo "라즈베리파이 환경으로 실행 중..."
python main.py --environment rpi

echo "실행 완료"

