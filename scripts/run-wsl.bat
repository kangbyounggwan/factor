@echo off
REM Windows에서 WSL 환경으로 Factor Client Firmware 실행 스크립트

echo === Factor Client Firmware - WSL 실행 ===
echo 환경: WSL (Windows Subsystem for Linux)
echo 설정 파일: config/settings_wsl.yaml
echo.

REM WSL에서 실행
echo WSL 환경으로 실행 중...
wsl bash -c "cd /mnt/c/Users/USER/factor-client-firmware && python main.py --environment wsl"

echo 실행 완료
pause

