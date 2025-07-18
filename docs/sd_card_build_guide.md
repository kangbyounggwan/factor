# Factor 클라이언트 SD카드 이미지 빌드 가이드

라즈베리파이에서 바로 사용할 수 있는 Factor 클라이언트 SD카드 이미지를 빌드하는 방법을 설명합니다.

## 🎯 개요

이 가이드에서는 다음과 같은 방법들을 제공합니다:

1. **완전 자동화된 SD카드 이미지 빌드** - Raspberry Pi OS 기반 커스텀 이미지 생성
2. **Docker 기반 빌드** - 컨테이너 환경에서 간편한 배포
3. **기존 라즈베리파이에 설치** - 이미 설치된 시스템에 추가 설치

## 📋 시스템 요구사항

### 빌드 환경
- **운영체제**: Ubuntu 20.04+ 또는 Debian 11+
- **RAM**: 최소 4GB (8GB 권장)
- **저장공간**: 최소 10GB 여유 공간
- **권한**: root 또는 sudo 권한

### 필수 도구
```bash
# Ubuntu/Debian
sudo apt update
sudo apt install -y \
    wget \
    xz-utils \
    parted \
    kpartx \
    qemu-user-static \
    docker.io \
    git
```

## 🔧 방법 1: 완전 자동화된 SD카드 이미지 빌드

### 1.1 빌드 스크립트 실행

```bash
# 프로젝트 디렉토리로 이동
cd factor-client-firmware

# 빌드 스크립트 실행 (root 권한 필요)
sudo ./scripts/build-sd-image.sh
```

### 1.2 빌드 과정

스크립트는 다음 단계를 자동으로 수행합니다:

1. **의존성 확인** - 필요한 도구들이 설치되어 있는지 확인
2. **Raspberry Pi OS 다운로드** - 최신 Raspberry Pi OS Lite 다운로드
3. **이미지 준비** - 이미지 크기 확장 및 파티션 조정
4. **이미지 마운트** - 루프백 디바이스를 통한 이미지 마운트
5. **Factor 클라이언트 설치** - chroot 환경에서 자동 설치
6. **시스템 설정** - 서비스 등록, 방화벽 설정 등
7. **이미지 압축** - 최종 이미지 압축 및 정리

### 1.3 빌드 결과

```
📁 빌드 결과:
   이미지 파일: build/factor-client-20240101_120000.img.xz
   크기: ~2.5GB (압축됨)
```

### 1.4 SD카드에 굽기

#### Raspberry Pi Imager 사용 (권장)
1. [Raspberry Pi Imager](https://www.raspberrypi.org/software/) 다운로드
2. "Use custom" 선택
3. 빌드된 `.img.xz` 파일 선택
4. SD카드 선택 후 굽기

#### 명령줄 사용
```bash
# Linux/macOS
sudo dd if=build/factor-client-20240101_120000.img.xz of=/dev/sdX bs=4M status=progress

# Windows (WSL)
# 먼저 압축 해제
xz -d factor-client-20240101_120000.img.xz
# 그 후 Win32DiskImager 사용
```

## 🐳 방법 2: Docker 기반 빌드

### 2.1 Docker 이미지 빌드

```bash
# 프로젝트 디렉토리로 이동
cd factor-client-firmware

# Docker 이미지 빌드
./scripts/build-docker-image.sh -t v1.0.0
```

### 2.2 라즈베리파이에 배포

```bash
# 자동 생성된 배포 스크립트 사용
./deploy-to-pi.sh raspberrypi.local pi
```

### 2.3 수동 Docker 실행

```bash
# 라즈베리파이에서 실행
docker run -d --name factor-client \
    -p 8080:8080 \
    -v /dev:/dev \
    -v factor-config:/app/config \
    -v factor-logs:/app/logs \
    --privileged \
    --restart unless-stopped \
    your-registry.com/factor-client:v1.0.0
```

## 🛠️ 방법 3: 기존 라즈베리파이에 설치

### 3.1 자동 설치 스크립트

```bash
# 라즈베리파이에서 실행
curl -sSL https://raw.githubusercontent.com/your-repo/factor-client-firmware/main/scripts/install-raspberry-pi.sh | bash
```

### 3.2 수동 설치

```bash
# 1. 시스템 업데이트
sudo apt update && sudo apt upgrade -y

# 2. 필수 패키지 설치
sudo apt install -y git python3 python3-pip python3-venv

# 3. 프로젝트 클론
git clone https://github.com/your-repo/factor-client-firmware.git
cd factor-client-firmware

# 4. 설치 스크립트 실행
sudo ./scripts/install-raspberry-pi.sh
```

## ⚙️ 설정 및 커스터마이징

### 기본 설정 수정

빌드 전에 다음 파일들을 수정하여 커스터마이징할 수 있습니다:

#### `config/settings.yaml`
```yaml
# OctoPrint 연결 설정
octoprint:
  host: "192.168.1.100"
  port: 5000
  api_key: "YOUR_API_KEY"

# 웹 서버 설정
server:
  host: "0.0.0.0"
  port: 8080
  debug: false

# 로깅 설정
logging:
  level: "INFO"
  file: "/var/log/factor-client/factor-client.log"
```

#### `scripts/build-sd-image.sh` 수정
```bash
# Raspberry Pi OS 이미지 URL 변경
PI_OS_IMAGE_URL="https://downloads.raspberrypi.org/raspios_lite_arm64/images/..."

# 추가 패키지 설치
apt-get install -y your-additional-packages
```

### 네트워크 설정

#### WiFi 자동 연결 설정
```bash
# /boot/wpa_supplicant.conf 생성
country=KR
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1

network={
    ssid="Your_WiFi_Name"
    psk="Your_WiFi_Password"
}
```

#### 고정 IP 설정
```bash
# /etc/dhcpcd.conf 수정
interface wlan0
static ip_address=192.168.1.100/24
static routers=192.168.1.1
static domain_name_servers=8.8.8.8
```

## 🚀 사용 방법

### 1. SD카드 삽입 및 부팅
1. 빌드된 이미지를 SD카드에 굽기
2. 라즈베리파이에 SD카드 삽입
3. 네트워크 케이블 연결 (또는 WiFi 설정)
4. 전원 연결

### 2. 첫 부팅 (약 2-3분 소요)
- 시스템 초기화
- 네트워크 설정
- Factor 클라이언트 자동 시작

### 3. 웹 인터페이스 접속
```
http://라즈베리파이IP:8080
```

### 4. SSH 접속 (필요시)
```bash
ssh pi@라즈베리파이IP
# 기본 비밀번호: raspberry (변경 권장)
```

## 📊 성능 최적화

### 라즈베리파이 설정

#### `/boot/config.txt`
```ini
# GPU 메모리 최소화
gpu_mem=16

# CPU 성능 최적화
arm_freq=1500
over_voltage=2

# USB 전력 증가
max_usb_current=1
```

#### 스왑 파일 설정
```bash
# 스왑 크기 증가
sudo dphys-swapfile swapoff
sudo nano /etc/dphys-swapfile
# CONF_SWAPSIZE=1024
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```

### 불필요한 서비스 비활성화
```bash
sudo systemctl disable bluetooth
sudo systemctl disable cups
sudo systemctl disable avahi-daemon
```

## 🔍 문제 해결

### 빌드 관련 문제

#### 권한 오류
```bash
# 스크립트에 실행 권한 부여
chmod +x scripts/build-sd-image.sh

# root 권한으로 실행
sudo ./scripts/build-sd-image.sh
```

#### 디스크 공간 부족
```bash
# 사용 가능한 공간 확인
df -h

# 임시 파일 정리
sudo rm -rf /tmp/factor-build-*
```

#### 네트워크 오류
```bash
# DNS 설정 확인
echo "nameserver 8.8.8.8" | sudo tee -a /etc/resolv.conf

# 프록시 설정 (필요시)
export http_proxy=http://proxy:port
export https_proxy=http://proxy:port
```

### 런타임 문제

#### 서비스 시작 실패
```bash
# 서비스 상태 확인
sudo systemctl status factor-client

# 로그 확인
sudo journalctl -u factor-client -f

# 수동 시작
sudo systemctl start factor-client
```

#### 포트 연결 문제
```bash
# 시리얼 포트 확인
ls -la /dev/ttyUSB* /dev/ttyACM*

# 권한 확인
groups pi
sudo usermod -a -G dialout pi
```

#### 네트워크 연결 문제
```bash
# IP 주소 확인
ip addr show

# 방화벽 상태 확인
sudo ufw status

# 포트 8080 열기
sudo ufw allow 8080/tcp
```

## 📚 추가 자료

### 관련 문서
- [라즈베리파이 설정 가이드](raspberry_pi_setup.md)
- [Factor 클라이언트 API 문서](api_documentation.md)
- [문제 해결 가이드](troubleshooting.md)

### 커뮤니티
- [GitHub Issues](https://github.com/your-repo/factor-client-firmware/issues)
- [Discussions](https://github.com/your-repo/factor-client-firmware/discussions)

### 기여하기
- [개발 가이드](development_guide.md)
- [코드 스타일 가이드](code_style_guide.md)

## 🆘 지원

문제가 발생하면 다음 정보와 함께 이슈를 등록해주세요:

1. **시스템 정보**
   ```bash
   cat /proc/version
   free -h
   df -h
   ```

2. **서비스 로그**
   ```bash
   sudo journalctl -u factor-client --since "1 hour ago"
   ```

3. **네트워크 상태**
   ```bash
   ip addr show
   sudo netstat -tlnp | grep 8080
   ```

---

**Factor OctoPrint Client Firmware** - 라즈베리파이에서 바로 사용할 수 있는 완전한 솔루션 