# Factor Client 설치 가이드

Factor Client를 라즈베리파이에 설치하는 방법을 안내합니다.

## 📋 준비물

- **라즈베리파이**: 3B+ 이상 (4B 권장)
- **SD카드**: 8GB 이상 (Class 10 권장)
- **네트워크 연결**: WiFi 또는 이더넷
- **3D 프린터**: USB 연결 지원

## 🚀 빠른 설치 (권장)

### 1. 자동 설치 스크립트 사용

```bash
# 라즈베리파이에서 실행
curl -sSL https://raw.githubusercontent.com/your-repo/factor-client-firmware/main/scripts/install.sh | sudo bash
```

### 2. 윈도우에서 SD카드 설정

```powershell
# 관리자 권한으로 PowerShell 실행
cd C:\path\to\factor-client-firmware
.\scripts\windows-sd-setup.ps1 -SDCardDrive D: -EnableSSH
```

## 🔧 수동 설치

### 1. 라즈베리파이 OS 설치

1. **Raspberry Pi Imager 다운로드**
   - https://www.raspberrypi.org/software/ 에서 다운로드

2. **OS 이미지 선택**
   - "Raspberry Pi OS Lite (64-bit)" 권장
   - SSH 활성화
   - WiFi 설정 (선택사항)

3. **SD카드에 굽기**

### 2. 기본 시스템 설정

```bash
# SSH로 라즈베리파이 접속
ssh pi@라즈베리파이IP

# 시스템 업데이트
sudo apt update && sudo apt upgrade -y

# 필수 패키지 설치
sudo apt install -y git python3 python3-pip python3-venv build-essential
```

### 3. Factor Client 설치

```bash
# 소스 코드 다운로드
cd ~
git clone https://github.com/your-repo/factor-client-firmware.git
cd factor-client-firmware

# Python 가상환경 설정
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 시스템 디렉토리 생성
sudo mkdir -p /etc/factor-client /var/log/factor-client
sudo chown -R pi:pi /etc/factor-client /var/log/factor-client

# 설정 파일 복사
cp config/settings.yaml /etc/factor-client/
```

### 4. 3D 프린터 연결 설정

```bash
# USB 시리얼 권한 설정
sudo usermod -a -G dialout pi

# 프린터 포트 확인
ls /dev/ttyUSB* /dev/ttyACM*

# 설정 파일 수정
nano /etc/factor-client/settings.yaml
```

### 5. 서비스 설정

```bash
# systemd 서비스 파일 설치
sudo cp systemd/factor-client.service /etc/systemd/system/

# 서비스 활성화
sudo systemctl daemon-reload
sudo systemctl enable factor-client
sudo systemctl start factor-client

# 상태 확인
sudo systemctl status factor-client
```

## 🌐 웹 인터페이스 접속

설치 완료 후 웹 브라우저에서 접속:

```
http://라즈베리파이IP:8080
```

## 🔧 문제 해결

### 일반적인 문제들

1. **포트 권한 오류**
   ```bash
   sudo usermod -a -G dialout $USER
   sudo reboot
   ```

2. **서비스 시작 실패**
   ```bash
   sudo journalctl -u factor-client -f
   ```

3. **웹 인터페이스 접속 안됨**
   ```bash
   # 방화벽 설정
   sudo ufw allow 8080/tcp
   ```

### 로그 확인

```bash
# 실시간 로그 확인
tail -f /var/log/factor-client/factor-client.log

# 서비스 로그 확인
sudo journalctl -u factor-client -f
```

## 📚 추가 문서

- [핫스팟 설정 가이드](hotspot_setup_guide.md)
- [SD카드 빌드 가이드](sd_card_build_guide.md)

## 🤝 지원

문제가 발생하면 GitHub Issues를 통해 문의해주세요. 