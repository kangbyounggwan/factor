# Factor Client 핫스팟 모드 설정 가이드

Factor Client의 핫스팟 모드를 사용하면 WiFi 연결이 없는 환경에서도 웹 인터페이스를 통해 초기 설정을 할 수 있습니다.

## 🎯 개요

핫스팟 모드는 다음과 같은 상황에서 자동으로 활성화됩니다:
- WiFi 연결이 없을 때
- 네트워크 설정이 잘못되었을 때
- 사용자가 수동으로 활성화했을 때

핫스팟이 활성화되면 라즈베리파이가 WiFi 액세스 포인트로 동작하며, 사용자는 모바일 기기나 노트북으로 연결하여 웹 인터페이스를 통해 설정할 수 있습니다.

## 📋 시스템 요구사항

- **하드웨어**: 라즈베리파이 3B+ 이상 (내장 WiFi 필요)
- **OS**: Raspberry Pi OS (Lite 또는 Desktop)
- **Python**: 3.7 이상
- **권한**: sudo 권한 필요

## 🔧 설치 방법

### 1단계: 핫스팟 시스템 설정

```bash
# 핫스팟 설정 스크립트 실행
sudo ./scripts/setup-hotspot.sh
```

이 스크립트는 다음 작업을 수행합니다:
- `hostapd`, `dnsmasq` 등 필수 패키지 설치
- 네트워크 인터페이스 설정
- 방화벽 및 라우팅 설정
- 서비스 권한 설정

### 2단계: Factor Client 설치

```bash
# Factor Client 설치
./scripts/install-raspberry-pi.sh
```

### 3단계: 시스템 재부팅

```bash
sudo reboot
```

## 🚀 사용 방법

### 자동 핫스팟 모드

Factor Client는 시작할 때 WiFi 연결 상태를 확인하고, 연결이 없으면 자동으로 핫스팟 모드를 활성화합니다.

1. **라즈베리파이 부팅**
2. **WiFi 연결 확인** (약 30초 대기)
3. **핫스팟 자동 활성화** (WiFi 연결 실패 시)
4. **설정 페이지 접속 가능**

### 핫스팟 연결

핫스팟이 활성화되면 다음 정보로 연결할 수 있습니다:

```
SSID: Factor-Client-Setup
Password: factor123
Gateway IP: 192.168.4.1
```

### 웹 설정 인터페이스

핫스팟에 연결한 후 웹 브라우저에서 다음 주소로 접속:

```
http://192.168.4.1:8080/setup
```

#### 설정 단계

**1단계: WiFi 네트워크 설정**
- WiFi 네트워크 검색
- SSID 및 비밀번호 입력

**2단계: Factor Client 설정**
- OctoPrint 서버 주소
- OctoPrint API 키
- 프린터 포트 설정

**3단계: 설정 완료**
- 설정 적용 및 시스템 재시작

## 🔧 수동 제어

### API를 통한 핫스팟 제어

```bash
# 핫스팟 상태 확인
curl http://localhost:8080/api/hotspot/info

# 핫스팟 활성화
curl -X POST http://localhost:8080/api/hotspot/enable

# 핫스팟 비활성화
curl -X POST http://localhost:8080/api/hotspot/disable

# WiFi 상태 확인
curl http://localhost:8080/api/wifi/status

# WiFi 네트워크 스캔
curl http://localhost:8080/api/wifi/scan
```

### 명령줄 도구

```bash
# 현재 WiFi 연결 확인
iwgetid -r

# 사용 가능한 WiFi 네트워크 스캔
sudo iwlist wlan0 scan | grep ESSID

# hostapd 상태 확인
sudo systemctl status hostapd

# dnsmasq 상태 확인
sudo systemctl status dnsmasq
```

## ⚙️ 설정 커스터마이징

### 핫스팟 설정 변경

핫스팟 설정은 `core/hotspot_manager.py` 파일에서 변경할 수 있습니다:

```python
self.hotspot_config = {
    'ssid': 'Your-Custom-SSID',        # 핫스팟 이름
    'password': 'your-password',       # 비밀번호 (8자 이상)
    'channel': 6,                      # WiFi 채널
    'ip_range': '192.168.4.0/24',     # IP 대역
    'gateway': '192.168.4.1'          # 게이트웨이 IP
}
```

### 자동 핫스팟 비활성화

자동 핫스팟 기능을 비활성화하려면 설정 파일에서:

```yaml
# config/settings.yaml
system:
  network:
    auto_hotspot: false
```

## 🔍 문제 해결

### 일반적인 문제

#### 1. 핫스팟이 활성화되지 않음

```bash
# 필수 패키지 확인
dpkg -l | grep -E "(hostapd|dnsmasq)"

# 서비스 상태 확인
sudo systemctl status hostapd
sudo systemctl status dnsmasq

# 로그 확인
sudo journalctl -u hostapd -f
sudo journalctl -u dnsmasq -f
```

#### 2. WiFi 인터페이스 문제

```bash
# WiFi 인터페이스 확인
ip link show wlan0

# RF kill 상태 확인
rfkill list

# WiFi 인터페이스 활성화
sudo ip link set wlan0 up
```

#### 3. 권한 문제

```bash
# sudoers 설정 확인
sudo visudo -f /etc/sudoers.d/factor-client

# 사용자 그룹 확인
groups pi
```

#### 4. 네트워크 설정 문제

```bash
# dhcpcd 설정 확인
cat /etc/dhcpcd.conf | grep wlan0

# IP 포워딩 확인
cat /proc/sys/net/ipv4/ip_forward

# iptables 규칙 확인
sudo iptables -L -n
```

### 로그 확인

```bash
# Factor Client 로그
sudo journalctl -u factor-client -f

# 핫스팟 관련 로그
sudo journalctl | grep -E "(hostapd|dnsmasq|wlan0)"

# 시스템 로그
sudo dmesg | grep wlan
```

### 설정 초기화

핫스팟 설정을 초기화하려면:

```bash
# 서비스 중지
sudo systemctl stop hostapd
sudo systemctl stop dnsmasq

# 설정 파일 삭제
sudo rm -f /etc/hostapd/hostapd.conf
sudo rm -f /etc/dnsmasq.conf

# 핫스팟 설정 스크립트 재실행
sudo ./scripts/setup-hotspot.sh
```

## 🛡️ 보안 고려사항

### 기본 보안 설정

- **WPA2 암호화**: 핫스팟은 WPA2-PSK로 암호화됩니다
- **강력한 비밀번호**: 기본 비밀번호를 변경하는 것을 권장합니다
- **방화벽**: 불필요한 포트는 차단됩니다
- **임시 사용**: 설정 완료 후 자동으로 비활성화됩니다

### 보안 강화 방법

1. **비밀번호 변경**
   ```python
   # core/hotspot_manager.py에서 변경
   'password': 'strong-password-here'
   ```

2. **SSID 숨김**
   ```python
   # hostapd 설정에 추가
   ignore_broadcast_ssid=1
   ```

3. **MAC 주소 필터링**
   ```python
   # hostapd 설정에 추가
   macaddr_acl=1
   accept_mac_file=/etc/hostapd/hostapd.accept
   ```

4. **접속 시간 제한**
   - 설정 완료 후 자동으로 핫스팟 비활성화
   - 일정 시간 후 자동 종료

## 📱 모바일 설정 가이드

### iOS 기기

1. **설정** → **Wi-Fi** 이동
2. **Factor-Client-Setup** 네트워크 선택
3. 비밀번호 **factor123** 입력
4. Safari에서 **http://192.168.4.1:8080/setup** 접속

### Android 기기

1. **설정** → **네트워크 및 인터넷** → **Wi-Fi** 이동
2. **Factor-Client-Setup** 네트워크 선택
3. 비밀번호 **factor123** 입력
4. Chrome에서 **http://192.168.4.1:8080/setup** 접속

### Windows/macOS

1. WiFi 설정에서 **Factor-Client-Setup** 선택
2. 비밀번호 **factor123** 입력
3. 웹 브라우저에서 **http://192.168.4.1:8080/setup** 접속

## 🔄 업데이트 및 유지보수

### 핫스팟 기능 업데이트

```bash
# Factor Client 업데이트
cd factor-client-firmware
git pull

# 핫스팟 설정 재적용
sudo ./scripts/setup-hotspot.sh
```

### 정기 점검

```bash
# 월간 점검 스크립트
#!/bin/bash

# 핫스팟 기능 테스트
curl -s http://localhost:8080/api/hotspot/info

# WiFi 기능 테스트
iwgetid -r

# 로그 정리
sudo journalctl --vacuum-time=30d
```

## 📚 추가 자료

- [라즈베리파이 WiFi 설정 가이드](raspberry_pi_setup.md)
- [Factor Client API 문서](api_documentation.md)
- [문제 해결 가이드](troubleshooting.md)
- [보안 설정 가이드](security_guide.md)

## 🤝 지원 및 문의

문제가 발생하거나 도움이 필요한 경우:

- **GitHub Issues**: [프로젝트 이슈 페이지](https://github.com/your-repo/factor-client-firmware/issues)
- **문서**: [온라인 문서](https://your-docs-site.com)
- **커뮤니티**: [디스커션 포럼](https://github.com/your-repo/factor-client-firmware/discussions) 