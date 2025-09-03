# 블루투스 설정 가이드

Factor Client의 블루투스 연결을 설정하는 방법을 안내합니다.

## 🔵 블루투스 연결 개요

Factor Client는 이제 핫스팟 대신 **블루투스**를 통해 모바일 앱과 연결됩니다.

### 🎯 블루투스 연결의 장점

- **🔋 낮은 전력 소모**: WiFi 대비 매우 낮은 전력
- **📱 범용성**: 모든 모바일 기기에서 지원
- **🔒 보안**: 페어링 기반 보안
- **🔄 동시 연결**: 여러 Factor Client 장비 동시 연결 가능
- **💰 저비용**: 블루투스 모듈이 저렴

## 🚀 자동 설정

### 설치 스크립트 실행

```bash
# 라즈베리파이에서 실행
sudo bash scripts/install.sh
```

설치 스크립트가 자동으로 다음을 수행합니다:
- 블루투스 패키지 설치 (`bluetooth`, `bluez`, `bluez-tools`)
- 블루투스 서비스 활성화
- Factor Client 서비스와 블루투스 서비스 연결

## 🔧 수동 설정

### 1. 블루투스 패키지 설치

```bash
sudo apt update
sudo apt install -y bluetooth bluez bluez-tools python3-bluez libbluetooth-dev
```

### 2. 블루투스 서비스 활성화

```bash
# 블루투스 서비스 시작 및 활성화
sudo systemctl enable bluetooth.service
sudo systemctl start bluetooth.service

# 상태 확인
sudo systemctl status bluetooth
```

### 3. 블루투스 인터페이스 활성화

```bash
# hci0 인터페이스 활성화
sudo hciconfig hci0 up

# 블루투스 장비 이름 설정
sudo bluetoothctl set-alias "Factor-Client"

# 발견 가능 및 페어링 가능하게 설정
sudo bluetoothctl discoverable on
sudo bluetoothctl pairable on
```

## 📱 모바일 앱 연결

### 1. 블루투스 스캔

모바일 기기에서 블루투스 설정 → 새 장비 추가 → **"Factor-Client"** 검색

### 2. 페어링

- Factor-Client 선택
- 페어링 코드 입력 (기본값: 0000)
- 연결 완료

### 3. 앱에서 연결

Factor Client 모바일 앱에서:
- 블루투스 장비 목록 확인
- Factor-Client 선택
- 연결 및 제어 시작

## 🔍 블루투스 상태 확인

### 명령어로 확인

```bash
# 블루투스 서비스 상태
sudo systemctl status bluetooth

# 블루투스 인터페이스 상태
sudo hciconfig

# 주변 블루투스 장비 스캔
sudo hcitool scan

# 블루투스 관리 도구
sudo bluetoothctl
```

### 웹 인터페이스에서 확인

웹 브라우저에서 `http://[라즈베리파이IP]:8080` 접속:
- **API 엔드포인트**: `/api/bluetooth/status`
- **블루투스 스캔**: `/api/bluetooth/scan`
- **장비 연결**: `/api/bluetooth/connect`

## 🛠️ 문제 해결

### 블루투스가 보이지 않는 경우

```bash
# 블루투스 서비스 재시작
sudo systemctl restart bluetooth

# 인터페이스 재활성화
sudo hciconfig hci0 down
sudo hciconfig hci0 up

# 블루투스 설정 초기화
sudo bluetoothctl
> power off
> power on
> discoverable on
> pairable on
> quit
```

### 연결이 안 되는 경우

```bash
# 블루투스 장비 목록 확인
sudo bluetoothctl devices

# 특정 장비 연결 해제
sudo bluetoothctl disconnect [MAC주소]

# 블루투스 서비스 재시작
sudo systemctl restart bluetooth
```

### 권한 문제

```bash
# factor 사용자를 bluetooth 그룹에 추가
sudo usermod -a -G bluetooth factor

# udev 규칙 적용
sudo udevadm control --reload-rules
sudo udevadm trigger
```

## 📊 블루투스 API 사용법

### 상태 조회

```bash
curl http://localhost:8080/api/bluetooth/status
```

### 장비 스캔

```bash
curl http://localhost:8080/api/bluetooth/scan
```

### 장비 페어링

```bash
curl -X POST http://localhost:8080/api/bluetooth/pair \
  -H "Content-Type: application/json" \
  -d '{"mac_address": "00:11:22:33:44:55"}'
```

### 장비 연결

```bash
curl -X POST http://localhost:8080/api/bluetooth/connect \
  -H "Content-Type: application/json" \
  -d '{"mac_address": "00:11:22:33:44:55"}'
```

## 🔄 자동 연결 설정

Factor Client가 시작될 때 자동으로:
1. 블루투스 서비스 시작
2. 블루투스 인터페이스 활성화
3. 장비 이름을 "Factor-Client"로 설정
4. 발견 가능 및 페어링 가능 상태로 설정
5. 백그라운드에서 주변 장비 스캔

## 📝 설정 파일

`config/settings_rpi.yaml`에서 블루투스 설정을 조정할 수 있습니다:

```yaml
bluetooth:
  enabled: true
  device_name: "Factor-Client"
  discoverable_timeout: 0
  pairable_timeout: 0
  auto_enable: true
  discovery_interval: 60
  max_connections: 10
```

## 🆘 지원

문제가 발생하면:
1. 로그 확인: `sudo journalctl -u factor-client -f`
2. 블루투스 로그: `sudo journalctl -u bluetooth -f`
3. GitHub 이슈 등록

---

**참고**: 이전 핫스팟 설정은 더 이상 지원되지 않습니다. 모든 연결은 블루투스를 통해 이루어집니다.
