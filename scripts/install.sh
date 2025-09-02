#!/bin/bash
# Factor OctoPrint Client Firmware 설치 스크립트
# 라즈베리파이 최적화

set -e

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 로그 함수
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "${BLUE}[STEP]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

# 루트 권한 확인
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "이 스크립트는 root 권한으로 실행해야 합니다."
        exit 1
    fi
}

# 라즈베리파이 확인
check_raspberry_pi() {
    if ! grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
        log_warn "라즈베리파이가 아닌 시스템에서 실행 중입니다."
        read -p "계속 진행하시겠습니까? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
}

# 이전 설치 정리
cleanup_previous_install() {
    log_step "이전 설치 정리 중..."

    # 서비스가 존재하면 중지 시도
    if systemctl list-unit-files | grep -q '^factor-client.service'; then
        systemctl stop factor-client.service || true
    fi

    # 과거 설치 디렉토리 제거(충돌 방지)
    if [ -d /opt/factor-client ]; then
        rm -rf /opt/factor-client
        log_info "/opt/factor-client 제거 완료"
    fi

    log_info "이전 설치 정리 완료"
}

# 시스템 업데이트
update_system() {
    log_step "시스템 업데이트 중..."
    apt update && apt upgrade -y
    log_info "시스템 업데이트 완료"
}

# 의존성 패키지 설치
install_dependencies() {
    log_step "의존성 패키지 설치 중..."
    
    apt install -y \
        python3 \
        python3-pip \
        python3-venv \
        python3-dev \
        build-essential \
        git \
        curl \
        wget \
        nginx \
        supervisor \
        logrotate \
        rsyslog \
        systemd-journal-remote \
        i2c-tools \
        python3-rpi.gpio \
        bluetooth \
        bluez \
        bluez-tools \
        python3-bluez \
        libbluetooth-dev
    
    log_info "의존성 패키지 설치 완료"
}

# 사용자 및 그룹 생성
create_user() {
    log_step "factor 사용자 생성 중..."
    
    if ! id "factor" &>/dev/null; then
        useradd -r -s /bin/false -d /opt/factor-client factor
        log_info "factor 사용자 생성 완료"
    else
        log_info "factor 사용자가 이미 존재합니다"
    fi
    
    # 필요한 그룹에 추가 (bluetooth 포함)
    usermod -a -G i2c,spi,gpio,dialout,bluetooth factor 2>/dev/null || true
}

# 디렉토리 생성
create_directories() {
    log_step "디렉토리 생성 중..."
    
    mkdir -p /opt/factor-client
    mkdir -p /var/log/factor-client
    mkdir -p /var/lib/factor-client
    mkdir -p /etc/factor-client
    
    # 권한 설정
    chown -R factor:factor /opt/factor-client
    chown -R factor:factor /var/log/factor-client
    chown -R factor:factor /var/lib/factor-client
    chown -R factor:factor /etc/factor-client
    
    log_info "디렉토리 생성 완료"
}

# 애플리케이션 설치
install_application() {
    log_step "Factor 클라이언트 설치 중..."

    # 최신 소스 배치: REPO_URL이 지정되면 git clone, 아니면 현재 디렉토리 복사
    if [ -n "$REPO_URL" ]; then
        BRANCH=${BRANCH:-master}
        log_info "Git 저장소에서 소스 클론: $REPO_URL (브랜치: $BRANCH)"
        git clone --depth 1 -b "$BRANCH" "$REPO_URL" /opt/factor-client
    else
        log_info "현재 디렉토리 소스를 /opt/factor-client 로 복사"
        mkdir -p /opt/factor-client
        cp -r . /opt/factor-client/
    fi
    
    # Python 가상환경 생성
    cd /opt/factor-client
    python3 -m venv venv
    source venv/bin/activate
    
    # Python 패키지 설치
    pip install --upgrade pip
    pip install -r requirements.txt
    # 비동기 시리얼 송신을 위한 추가 의존성
    pip install "pyserial-asyncio>=0.6"
    
    # 권한 설정
    chown -R factor:factor /opt/factor-client
    chmod +x /opt/factor-client/main.py
    
    log_info "Factor 클라이언트 설치 완료"
}

# 설정 파일 복사
install_config() {
    log_step "설정 파일 설치 중..."
    
    # 기본 설정 파일 복사
    cp /opt/factor-client/config/settings.yaml /etc/factor-client/
    
    # systemd 서비스 파일 설치
    cp /opt/factor-client/systemd/factor-client.service /etc/systemd/system/
    
    # 권한 설정
    chown factor:factor /etc/factor-client/settings.yaml
    chmod 644 /etc/factor-client/settings.yaml
    
    log_info "설정 파일 설치 완료"
}

# 로그 로테이션 설정
setup_logrotate() {
    log_step "로그 로테이션 설정 중..."
    
    cat > /etc/logrotate.d/factor-client << 'EOF'
/var/log/factor-client/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 644 factor factor
    postrotate
        systemctl reload factor-client || true
    endscript
}
EOF
    
    log_info "로그 로테이션 설정 완료"
}

# 시스템 최적화
optimize_system() {
    log_step "시스템 최적화 중..."
    
    # GPU 메모리 분할 최적화 (라즈베리파이)
    if grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
        echo "gpu_mem=16" >> /boot/config.txt
        
        # I2C, SPI 활성화
        echo "dtparam=i2c_arm=on" >> /boot/config.txt
        echo "dtparam=spi=on" >> /boot/config.txt
    fi
    
    # 스왑 파일 크기 조정
    if [ -f /etc/dphys-swapfile ]; then
        sed -i 's/CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
        systemctl restart dphys-swapfile
    fi
    
    # 시스템 서비스 최적화
    systemctl disable cups.service 2>/dev/null || true
    
    log_info "시스템 최적화 완료"
}

# 읽기 전용 파일시스템 설정 (선택사항)
setup_readonly_root() {
    log_step "읽기 전용 루트 파일시스템 설정..."
    
    read -p "읽기 전용 루트 파일시스템을 설정하시겠습니까? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        # /etc/fstab 수정
        cp /etc/fstab /etc/fstab.backup
        
        # tmpfs 마운트 추가
        cat >> /etc/fstab << 'EOF'
tmpfs /tmp tmpfs defaults,noatime,nosuid,size=100m 0 0
tmpfs /var/tmp tmpfs defaults,noatime,nosuid,size=30m 0 0
tmpfs /var/log tmpfs defaults,noatime,nosuid,mode=0755,size=100m 0 0
tmpfs /var/run tmpfs defaults,noatime,nosuid,mode=0755,size=2m 0 0
tmpfs /var/spool/mqueue tmpfs defaults,noatime,nosuid,mode=0700,gid=12,size=30m 0 0
EOF
        
        log_info "읽기 전용 루트 파일시스템 설정 완료"
        log_warn "재부팅 후 적용됩니다"
    fi
}

# 서비스 등록 및 시작
setup_service() {
    log_step "서비스 등록 및 시작..."
    
    # systemd 데몬 리로드
    systemctl daemon-reload
    
    # 서비스 활성화
    systemctl enable factor-client.service
    
    # 서비스 시작
    systemctl start factor-client.service
    
    # 상태 확인
    sleep 3
    if systemctl is-active --quiet factor-client.service; then
        log_info "Factor 클라이언트 서비스가 성공적으로 시작되었습니다"
    else
        log_error "Factor 클라이언트 서비스 시작 실패"
        systemctl status factor-client.service
        exit 1
    fi
}

# USB 장치 권한 설정
setup_usb_permissions() {
    log_step "USB 장치 권한 설정 중..."
    
    # udev 규칙 생성 - CH340 USB-to-Serial 변환기
    cat > /etc/udev/rules.d/99-factor-client.rules << 'EOF'
# Factor Client 3D 프린터 USB 장치 권한 설정
# CH340 USB-to-Serial 변환기
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", MODE="0660", OWNER="factor", GROUP="dialout"

# 일반적인 3D 프린터 USB 장치들
SUBSYSTEM=="tty", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5740", MODE="0660", OWNER="factor", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0660", OWNER="factor", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="067b", ATTRS{idProduct}=="2303", MODE="0660", OWNER="factor", GROUP="dialout"

# FTDI USB-to-Serial 변환기
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", MODE="0660", OWNER="factor", GROUP="dialout"
EOF
    
    # udev 규칙 적용
    udevadm control --reload-rules
    udevadm trigger
    
    log_info "USB 장치 권한 설정 완료"
}

# 블루투스 설정
setup_bluetooth() {
    log_step "블루투스 설정 중..."
    
    # 블루투스 서비스 활성화 및 시작
    log_step "블루투스 서비스 활성화 중..."
    systemctl enable bluetooth.service
    systemctl start bluetooth.service
    
    # 블루투스 서비스 상태 확인
    if systemctl is-active --quiet bluetooth.service; then
        log_success "블루투스 서비스가 성공적으로 시작되었습니다."
    else
        log_error "블루투스 서비스 시작에 실패했습니다."
        return 1
    fi
    
    # 블루투스 인터페이스 활성화
    log_step "블루투스 인터페이스 활성화 중..."
    hciconfig hci0 up 2>/dev/null || true
    
    # 블루투스 인터페이스 상태 확인
    if hciconfig hci0 >/dev/null 2>&1; then
        log_success "블루투스 인터페이스가 활성화되었습니다."
    else
        log_warning "블루투스 인터페이스 활성화에 실패했습니다. (재부팅 후 자동 활성화)"
    fi
    
    # 블루투스 설정 파일 생성
    cat > /etc/bluetooth/main.conf << 'EOF'
[General]
Name = Factor-Client
Class = 0x000000
DeviceID = 0x0000000000000000
DiscoverableTimeout = 0
PairableTimeout = 0
Privacy = off
Key = 00000000000000000000000000000000
ControllerMode = le

[Policy]
AutoEnable=true
EOF

    # 블루투스 네트워크 설정
    cat > /etc/bluetooth/network.conf << 'EOF'
[GATT]
Key=00000000000000000000000000000000

[GAP]
Key=00000000000000000000000000000000
EOF

    # 블루투스 권한 설정 (중복 허용)
    usermod -a -G bluetooth factor 2>/dev/null || true
    
    # 블루투스 보안 정책 설정
    cat > /etc/bluetooth/input.conf << 'EOF'
[General]
ClassicBondedOnly=false
LEBondedOnly=false
EOF

    # 블루투스 서비스 재시작
    systemctl restart bluetooth.service
    
    log_info "블루투스 설정 완료"
}
# 블루투스 권한(Polkit) 완화 설정
setup_bluetooth_permissions() {
    log_step "블루투스 접근 권한(Polkit) 설정 중..."

    # org.bluez 정책: bluetooth 그룹 사용자에게 허용
    mkdir -p /etc/polkit-1/rules.d
    cat > /etc/polkit-1/rules.d/90-factor-bluetooth.rules << 'EOF'
polkit.addRule(function(action, subject) {
  if (action && action.id && action.id.indexOf('org.bluez') === 0 && subject && subject.isInGroup('bluetooth')) {
    return polkit.Result.YES;
  }
});
EOF

    # polkit 데몬이 rule을 자동 감지하므로 재시작은 선택
    if systemctl is-active --quiet polkit 2>/dev/null; then
        systemctl reload polkit 2>/dev/null || true
    fi

    log_info "Polkit 규칙 적용 완료: bluetooth 그룹에 org.bluez 액션 허용"
}

# (제거됨) 블루투스 자동 설정 oneshot 서비스 설치
# 중복 광고를 유발할 수 있는 bluetoothctl advertise on 자동 구성을 삭제했습니다.

# iwlist sudo 허용 및 bluetoothd experimental 활성화
setup_ble_permissions_and_experimental() {
    log_step "BLE 권한 및 bluetoothd experimental 옵션 설정 중..."

    # 1) iwlist 절대 경로 확인
    IWLIST_PATH=$(command -v iwlist || true)
    if [ -z "$IWLIST_PATH" ]; then
        # 대부분 /usr/sbin/iwlist
        if [ -x /usr/sbin/iwlist ]; then
            IWLIST_PATH=/usr/sbin/iwlist
        elif [ -x /sbin/iwlist ]; then
            IWLIST_PATH=/sbin/iwlist
        fi
    fi

    if [ -n "$IWLIST_PATH" ]; then
        log_info "iwlist 경로: $IWLIST_PATH"
        # factor 사용자가 비밀번호 없이 iwlist 실행 가능하도록 sudoers 규칙 추가
        cat > /etc/sudoers.d/factor-iwlist << EOF
factor ALL=(root) NOPASSWD: $IWLIST_PATH
Defaults!$IWLIST_PATH !requiretty
EOF
        chmod 440 /etc/sudoers.d/factor-iwlist
        log_info "sudoers에 iwlist 허용 규칙 추가"
    else
        log_warning "iwlist 바이너리를 찾을 수 없습니다. 무선 스캔 응답이 실패할 수 있습니다."
    fi

    # 2) bluetoothd -E (experimental) 활성화 (LEAdvertisingManager1 안정 제공)
    mkdir -p /etc/systemd/system/bluetooth.service.d
    BLUETOOTHD_BIN=""
    if [ -x /usr/libexec/bluetooth/bluetoothd ]; then
        BLUETOOTHD_BIN=/usr/libexec/bluetooth/bluetoothd
    elif [ -x /usr/lib/bluetooth/bluetoothd ]; then
        BLUETOOTHD_BIN=/usr/lib/bluetooth/bluetoothd
    elif command -v bluetoothd >/dev/null 2>&1; then
        BLUETOOTHD_BIN=$(command -v bluetoothd)
    fi
    if [ -n "$BLUETOOTHD_BIN" ]; then
        cat > /etc/systemd/system/bluetooth.service.d/override.conf << EOF
[Service]
ExecStart=
ExecStart=$BLUETOOTHD_BIN -E
EOF
        systemctl daemon-reload
        systemctl restart bluetooth.service || true
        log_info "bluetoothd에 experimental 옵션(-E) 적용"
    else
        log_warning "bluetoothd 실행 파일을 찾지 못했습니다. experimental 적용 생략"
    fi
}

# (제거됨) Headless BLE agent + advertiser (NoInputNoOutput)
# ble-headless 서비스/스크립트 설치 코드를 삭제했습니다.

# 방화벽 설정
setup_firewall() {
    log_step "방화벽 설정 중..."
    
    if command -v ufw &> /dev/null; then
        ufw allow 8080/tcp comment "Factor Client Web Interface"
        ufw allow 22/tcp comment "SSH"
        log_info "방화벽 설정 완료"
    else
        log_warn "ufw가 설치되지 않았습니다. 방화벽 설정을 건너뜁니다."
    fi
}

# 설치 완료 메시지
installation_complete() {
    log_info "==============================================="
    log_info "Factor OctoPrint Client Firmware 설치 완료!"
    log_info "==============================================="
    echo
    log_info "웹 인터페이스: http://$(hostname -I | awk '{print $1}'):8080"
    log_info "설정 파일: /etc/factor-client/settings.yaml"
    log_info "로그 파일: /var/log/factor-client/"
    echo
    log_info "블루투스 정보:"
    echo "  서비스: bluetooth.service"
    echo "  장비 이름: Factor-Client"
    echo "  관리 도구: bluetoothctl"
    echo "  스캔 명령: bluetoothctl --timeout 5 scan on"
    echo
    log_info "블루투스 서비스 관리:"
    echo "  sudo systemctl status bluetooth     # 블루투스 상태 확인"
    echo "  sudo systemctl restart bluetooth    # 블루투스 재시작"
    echo "  sudo bluetoothctl                   # 블루투스 관리 도구"
    echo
    log_info "블루투스 상태 확인:"
    echo "  hciconfig                           # 블루투스 인터페이스 상태"
    echo "  bluetoothctl devices                # 주변 블루투스 장비 목록"
    echo "  sudo systemctl is-active bluetooth  # 블루투스 서비스 실행 상태"
    echo
    log_info "서비스 관리 명령어:"
    echo "  systemctl status factor-client    # 상태 확인"
    echo "  systemctl restart factor-client   # 재시작"
    echo "  systemctl stop factor-client      # 중지"
    echo "  systemctl start factor-client     # 시작"
    echo
    log_warn "설정 파일을 수정한 후 서비스를 재시작하세요:"
    echo "  sudo nano /etc/factor-client/settings.yaml"
    echo "  sudo systemctl restart factor-client"
}

# 메인 설치 함수
main() {
    log_info "Factor OctoPrint Client Firmware 설치를 시작합니다..."
    
    check_root
    check_raspberry_pi
    update_system
    install_dependencies
    create_user
    create_directories
    install_application
    install_config
    setup_logrotate
    optimize_system
    setup_readonly_root
    setup_usb_permissions
    setup_bluetooth
    setup_ble_permissions_and_experimental
    setup_service
    setup_firewall
    
    # 블루투스 상태 최종 확인
    log_step "블루투스 상태 최종 확인 중..."
    if systemctl is-active --quiet bluetooth.service; then
        log_success "블루투스 서비스가 정상적으로 실행 중입니다."
        
        # 블루투스 인터페이스 상태 확인
        if hciconfig hci0 >/dev/null 2>&1; then
            log_success "블루투스 인터페이스가 활성화되었습니다."
        else
            log_warning "블루투스 인터페이스가 비활성화되어 있습니다. 재부팅 후 자동 활성화됩니다."
        fi
        # 상태 확인 힌트 출력(BLE-only)
        log_info "참고: bluetoothctl show 에서 Discoverable: no, ActiveInstances > 0 이어야 BLE 광고 중입니다."
    else
        log_error "블루투스 서비스가 실행되지 않고 있습니다."
        log_info "다음 명령어로 수동으로 시작할 수 있습니다:"
        echo "  sudo systemctl start bluetooth"
        echo "  sudo systemctl enable bluetooth"
    fi
    
    echo
    installation_complete
}

# 스크립트 실행
main "$@" 