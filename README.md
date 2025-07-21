# Factor Client

라즈베리파이용 3D 프린터 서버 클라이언트입니다.

## 🚀 주요 특징

- **🔌 안전한 설계**: 전원 차단에도 안전한 구조
- **⚡ 빠른 부팅**: 최적화된 시스템으로 빠른 시작
- **📊 실시간 모니터링**: 프린터 상태 실시간 추적
- **🌐 웹 인터페이스**: 직관적인 웹 기반 제어 인터페이스
- **🔧 자동 복구**: 연결 오류 시 자동 재연결
- **📱 모바일 친화적**: 반응형 웹 디자인
- **🔄 실시간 통신**: WebSocket 기반 실시간 데이터 전송

## 📋 시스템 요구사항

### 하드웨어
- Raspberry Pi 3B+ 이상 (권장: Pi 4)
- 8GB 이상 SD 카드 (Class 10 권장)
- 네트워크 연결 (WiFi 또는 이더넷)

### 소프트웨어
- Raspberry Pi OS (Bullseye 이상)
- Python 3.9+
- systemd

## 🛠️ 빠른 설치

### 자동 설치 (권장)

```bash
# 라즈베리파이에서 실행
curl -sSL https://raw.githubusercontent.com/your-repo/factor-client-firmware/main/scripts/install.sh | sudo bash
```

### 윈도우에서 SD카드 설정

```powershell
# 관리자 권한으로 PowerShell 실행
cd C:\path\to\factor-client-firmware
.\scripts\windows-sd-setup.ps1 -SDCardDrive D: -EnableSSH
```

## 📚 상세 설치 가이드

[설치 가이드](docs/installation_guide.md)를 참조하세요.

## 🌐 사용법

### 웹 인터페이스

설치 후 웹 브라우저에서 접속:

```
http://라즈베리파이IP:8080
```

### 주요 기능

- **대시보드**: 프린터 상태 및 시스템 정보
- **설정**: 프린터 연결 및 시스템 설정
- **데이터 취득**: 프린터 데이터 수집 관리
- **데이터 로그**: 실시간 데이터 그래프 및 분석

### API 엔드포인트

- `GET /api/status` - 전체 상태 정보
- `GET /api/printer/status` - 프린터 상태
- `GET /api/printer/temperature` - 온도 정보
- `GET /api/printer/position` - 위치 정보
- `GET /api/printer/progress` - 프린트 진행률
- `POST /api/printer/command` - G-code 명령 전송
- `GET /api/system/info` - 시스템 정보
- `GET /api/health` - 헬스 체크

### 서비스 관리

```bash
# 상태 확인
sudo systemctl status factor-client

# 재시작
sudo systemctl restart factor-client

# 로그 확인
sudo journalctl -u factor-client -f
```

### 업데이트

```bash
# 자동 업데이트 (Git pull + 의존성 업데이트 + 서비스 재시작)
./scripts/update.sh

# 특정 브랜치에서 업데이트
./scripts/update.sh -b main

# 의존성 업데이트 건너뛰기
./scripts/update.sh --skip-deps

# 설정 파일 복사 건너뛰기
./scripts/update.sh --skip-config

# 도움말 보기
./scripts/update.sh --help
```

## 🔧 개발

### 로컬 개발 환경

```bash
# 저장소 클론
git clone https://github.com/your-repo/factor-client-firmware.git
cd factor-client-firmware

# 가상환경 생성
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 의존성 설치
pip install -r requirements.txt

# 개발 모드 실행
python main.py
```

### 개발 도구 설치

```bash
pip install -r requirements-dev.txt
```

## 📁 프로젝트 구조

```
factor-client-firmware/
├── core/                 # 핵심 모듈
│   ├── client.py        # 메인 클라이언트
│   ├── config_manager.py # 설정 관리
│   ├── printer_comm.py  # 프린터 통신
│   └── ...
├── web/                 # 웹 인터페이스
│   ├── app.py          # Flask 앱
│   ├── api.py          # API 엔드포인트
│   └── templates/      # HTML 템플릿
├── config/             # 설정 파일
├── scripts/            # 설치 스크립트
├── docs/              # 문서
└── systemd/           # 서비스 파일
```

## 📚 문서

- [설치 가이드](docs/installation_guide.md)
- [핫스팟 설정](docs/hotspot_setup_guide.md)
- [SD카드 빌드](docs/sd_card_build_guide.md)

## 🤝 기여

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📄 라이선스

이 프로젝트는 MIT 라이선스 하에 배포됩니다.

## 🆘 지원

문제가 발생하면 GitHub Issues를 통해 문의해주세요. 