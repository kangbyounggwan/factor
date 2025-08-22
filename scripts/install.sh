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
        python3-rpi.gpio
    
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
    
    # 필요한 그룹에 추가
    usermod -a -G i2c,spi,gpio,dialout factor 2>/dev/null || true
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
    
    # 현재 디렉토리의 파일들을 복사
    cp -r . /opt/factor-client/
    
    # Python 가상환경 생성
    cd /opt/factor-client
    python3 -m venv venv
    source venv/bin/activate
    
    # Python 패키지 설치
    pip install --upgrade pip
    pip install -r requirements.txt
    
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
    systemctl disable bluetooth.service 2>/dev/null || true
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
    setup_service
    setup_firewall
    installation_complete
}

# 스크립트 실행
main "$@" 