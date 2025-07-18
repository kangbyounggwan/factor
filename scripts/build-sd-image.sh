#!/bin/bash

# Factor 클라이언트 SD카드 이미지 빌드 스크립트
# 사용법: ./scripts/build-sd-image.sh

set -e

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 설정 변수
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$PROJECT_DIR/build"
IMAGE_NAME="factor-client-$(date +%Y%m%d_%H%M%S).img"
MOUNT_DIR="/tmp/factor-build-mount"

# 기본 설정
PI_OS_IMAGE_URL="https://downloads.raspberrypi.org/raspios_lite_arm64/images/raspios_lite_arm64-2023-12-11/2023-12-11-raspios-bookworm-arm64-lite.img.xz"
PI_OS_IMAGE_FILE="raspios-lite.img.xz"
PI_OS_IMAGE_EXTRACTED="raspios-lite.img"

# 로그 함수
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 에러 핸들링
error_exit() {
    log_error "$1"
    cleanup
    exit 1
}

# 정리 함수
cleanup() {
    log_info "정리 중..."
    
    # 마운트 해제
    if mountpoint -q "$MOUNT_DIR" 2>/dev/null; then
        sudo umount "$MOUNT_DIR" || true
    fi
    
    # 루프 디바이스 해제
    if [[ -n "$LOOP_DEVICE" ]]; then
        sudo losetup -d "$LOOP_DEVICE" || true
    fi
    
    # 임시 디렉토리 삭제
    if [[ -d "$MOUNT_DIR" ]]; then
        sudo rm -rf "$MOUNT_DIR" || true
    fi
}

# 시그널 핸들러
trap cleanup EXIT
trap 'error_exit "사용자에 의해 중단됨"' INT TERM

# 사용자 확인
confirm() {
    read -p "$1 (y/N): " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]]
}

# 필수 도구 확인
check_dependencies() {
    log_info "필수 도구 확인 중..."
    
    local missing_tools=()
    
    for tool in wget xz-utils parted kpartx qemu-user-static; do
        if ! command -v "$tool" &> /dev/null; then
            missing_tools+=("$tool")
        fi
    done
    
    if [[ ${#missing_tools[@]} -gt 0 ]]; then
        log_error "다음 도구들이 필요합니다: ${missing_tools[*]}"
        log_info "설치 명령어: sudo apt install -y ${missing_tools[*]}"
        exit 1
    fi
    
    # root 권한 확인
    if [[ $EUID -ne 0 ]]; then
        log_error "이 스크립트는 root 권한이 필요합니다."
        log_info "다음 명령어로 실행하세요: sudo $0"
        exit 1
    fi
    
    log_success "필수 도구 확인 완료"
}

# 빌드 디렉토리 준비
prepare_build_dir() {
    log_info "빌드 디렉토리 준비 중..."
    
    # 빌드 디렉토리 생성
    mkdir -p "$BUILD_DIR"
    cd "$BUILD_DIR"
    
    log_success "빌드 디렉토리 준비 완료: $BUILD_DIR"
}

# Raspberry Pi OS 이미지 다운로드
download_pi_os() {
    log_info "Raspberry Pi OS 이미지 다운로드 중..."
    
    if [[ ! -f "$PI_OS_IMAGE_FILE" ]]; then
        log_info "다운로드 중: $PI_OS_IMAGE_URL"
        wget -O "$PI_OS_IMAGE_FILE" "$PI_OS_IMAGE_URL"
    else
        log_info "이미 다운로드됨: $PI_OS_IMAGE_FILE"
    fi
    
    # 압축 해제
    if [[ ! -f "$PI_OS_IMAGE_EXTRACTED" ]]; then
        log_info "압축 해제 중..."
        xz -d -k "$PI_OS_IMAGE_FILE"
    else
        log_info "이미 압축 해제됨: $PI_OS_IMAGE_EXTRACTED"
    fi
    
    log_success "Raspberry Pi OS 이미지 준비 완료"
}

# 이미지 복사 및 확장
prepare_image() {
    log_info "이미지 복사 및 확장 중..."
    
    # 이미지 복사
    cp "$PI_OS_IMAGE_EXTRACTED" "$IMAGE_NAME"
    
    # 이미지 크기 확장 (2GB 추가)
    log_info "이미지 크기 확장 중..."
    dd if=/dev/zero bs=1M count=2048 >> "$IMAGE_NAME"
    
    # 파티션 테이블 수정
    log_info "파티션 크기 조정 중..."
    parted "$IMAGE_NAME" --script resizepart 2 100%
    
    log_success "이미지 준비 완료: $IMAGE_NAME"
}

# 이미지 마운트
mount_image() {
    log_info "이미지 마운트 중..."
    
    # 루프 디바이스 설정
    LOOP_DEVICE=$(losetup -f --show "$IMAGE_NAME")
    log_info "루프 디바이스: $LOOP_DEVICE"
    
    # 파티션 매핑
    kpartx -av "$LOOP_DEVICE"
    
    # 잠시 대기
    sleep 2
    
    # 루트 파티션 확인
    ROOT_PARTITION="/dev/mapper/$(basename "$LOOP_DEVICE")p2"
    
    if [[ ! -b "$ROOT_PARTITION" ]]; then
        error_exit "루트 파티션을 찾을 수 없습니다: $ROOT_PARTITION"
    fi
    
    # 파일시스템 체크 및 리사이즈
    e2fsck -f "$ROOT_PARTITION" || true
    resize2fs "$ROOT_PARTITION"
    
    # 마운트 디렉토리 생성
    mkdir -p "$MOUNT_DIR"
    
    # 마운트
    mount "$ROOT_PARTITION" "$MOUNT_DIR"
    
    log_success "이미지 마운트 완료: $MOUNT_DIR"
}

# Factor 클라이언트 설치
install_factor_client() {
    log_info "Factor 클라이언트 설치 중..."
    
    # chroot 환경 준비
    mount -t proc proc "$MOUNT_DIR/proc"
    mount -t sysfs sysfs "$MOUNT_DIR/sys"
    mount -o bind /dev "$MOUNT_DIR/dev"
    mount -o bind /dev/pts "$MOUNT_DIR/dev/pts"
    
    # QEMU 설정
    cp /usr/bin/qemu-aarch64-static "$MOUNT_DIR/usr/bin/"
    
    # 프로젝트 파일 복사
    log_info "프로젝트 파일 복사 중..."
    mkdir -p "$MOUNT_DIR/opt/factor-client"
    cp -r "$PROJECT_DIR"/* "$MOUNT_DIR/opt/factor-client/"
    
    # 설치 스크립트 실행
    log_info "Factor 클라이언트 설치 실행 중..."
    
    # chroot 환경에서 설치 스크립트 실행
    cat > "$MOUNT_DIR/tmp/install-factor.sh" << 'EOF'
#!/bin/bash
set -e

# 시스템 업데이트
apt-get update
apt-get upgrade -y

# 필수 패키지 설치
apt-get install -y \
    git \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    curl \
    wget \
    nano \
    htop \
    ufw \
    logrotate \
    bc \
    systemd

# Factor 클라이언트 설치
cd /opt/factor-client

# Python 가상환경 생성
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 시스템 디렉토리 생성
mkdir -p /etc/factor-client
mkdir -p /var/log/factor-client

# 설정 파일 복사
cp config/settings.yaml /etc/factor-client/

# 사용자 생성
useradd -r -s /bin/false -d /opt/factor-client factor || true

# 권한 설정
chown -R factor:factor /opt/factor-client
chown -R factor:factor /etc/factor-client
chown -R factor:factor /var/log/factor-client

# dialout 그룹에 pi 사용자 추가
usermod -a -G dialout pi

# systemd 서비스 설정
cp systemd/factor-client.service /etc/systemd/system/
sed -i 's|/home/pi|/opt|g' /etc/systemd/system/factor-client.service
sed -i 's|User=pi|User=factor|g' /etc/systemd/system/factor-client.service
sed -i 's|Group=pi|Group=factor|g' /etc/systemd/system/factor-client.service

# 서비스 활성화
systemctl daemon-reload
systemctl enable factor-client

# SSH 활성화
systemctl enable ssh

# 로그 로테이션 설정
cat > /etc/logrotate.d/factor-client << 'LOGROTATE_EOF'
/var/log/factor-client/*.log {
    daily
    missingok
    rotate 7
    compress
    delaycompress
    notifempty
    copytruncate
}
LOGROTATE_EOF

# 성능 최적화
echo "gpu_mem=16" >> /boot/config.txt

# 첫 부팅 시 자동 설정 스크립트
cat > /etc/systemd/system/factor-first-boot.service << 'FIRSTBOOT_EOF'
[Unit]
Description=Factor Client First Boot Setup
After=network.target

[Service]
Type=oneshot
ExecStart=/opt/factor-client/scripts/first-boot-setup.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
FIRSTBOOT_EOF

systemctl enable factor-first-boot

echo "Factor 클라이언트 설치 완료"
EOF

    chmod +x "$MOUNT_DIR/tmp/install-factor.sh"
    chroot "$MOUNT_DIR" /tmp/install-factor.sh
    
    log_success "Factor 클라이언트 설치 완료"
}

# 첫 부팅 설정 스크립트 생성
create_first_boot_script() {
    log_info "첫 부팅 설정 스크립트 생성 중..."
    
    cat > "$MOUNT_DIR/opt/factor-client/scripts/first-boot-setup.sh" << 'EOF'
#!/bin/bash

# Factor 클라이언트 첫 부팅 설정 스크립트

set -e

LOG_FILE="/var/log/factor-client/first-boot.log"
mkdir -p "$(dirname "$LOG_FILE")"

log_info() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] $1" | tee -a "$LOG_FILE"
}

log_info "Factor 클라이언트 첫 부팅 설정 시작"

# 네트워크 대기
log_info "네트워크 연결 대기 중..."
for i in {1..30}; do
    if ping -c 1 google.com &> /dev/null; then
        log_info "네트워크 연결 확인됨"
        break
    fi
    sleep 2
done

# 시스템 시간 동기화
log_info "시스템 시간 동기화 중..."
timedatectl set-ntp true

# 프린터 포트 자동 감지
log_info "프린터 포트 자동 감지 중..."
PORTS=$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true)
if [[ -n "$PORTS" ]]; then
    FIRST_PORT=$(echo "$PORTS" | head -n1)
    log_info "발견된 포트: $FIRST_PORT"
    
    # 설정 파일 업데이트
    sed -i "s|port: \"/dev/ttyUSB0\"|port: \"$FIRST_PORT\"|" /etc/factor-client/settings.yaml
fi

# Factor 클라이언트 서비스 시작
log_info "Factor 클라이언트 서비스 시작 중..."
systemctl start factor-client

# 서비스 상태 확인
sleep 5
if systemctl is-active --quiet factor-client; then
    log_info "Factor 클라이언트 서비스 시작 성공"
else
    log_info "Factor 클라이언트 서비스 시작 실패"
fi

# 첫 부팅 서비스 비활성화
systemctl disable factor-first-boot

log_info "첫 부팅 설정 완료"
EOF

    chmod +x "$MOUNT_DIR/opt/factor-client/scripts/first-boot-setup.sh"
    
    log_success "첫 부팅 설정 스크립트 생성 완료"
}

# 이미지 정리 및 언마운트
cleanup_image() {
    log_info "이미지 정리 및 언마운트 중..."
    
    # 임시 파일 정리
    rm -f "$MOUNT_DIR/tmp/install-factor.sh"
    rm -f "$MOUNT_DIR/usr/bin/qemu-aarch64-static"
    
    # chroot 환경 정리
    umount "$MOUNT_DIR/dev/pts" || true
    umount "$MOUNT_DIR/dev" || true
    umount "$MOUNT_DIR/sys" || true
    umount "$MOUNT_DIR/proc" || true
    
    # 루트 파티션 언마운트
    umount "$MOUNT_DIR"
    
    # 파티션 매핑 해제
    kpartx -dv "$LOOP_DEVICE"
    
    # 루프 디바이스 해제
    losetup -d "$LOOP_DEVICE"
    
    # 마운트 디렉토리 삭제
    rm -rf "$MOUNT_DIR"
    
    log_success "이미지 정리 완료"
}

# 이미지 압축
compress_image() {
    log_info "이미지 압축 중..."
    
    # 이미지 압축
    log_info "이미지를 압축합니다. 시간이 오래 걸릴 수 있습니다..."
    xz -z -9 "$IMAGE_NAME"
    
    COMPRESSED_IMAGE="$IMAGE_NAME.xz"
    
    log_success "이미지 압축 완료: $COMPRESSED_IMAGE"
    
    # 파일 크기 확인
    ORIGINAL_SIZE=$(stat -c%s "$PI_OS_IMAGE_EXTRACTED")
    COMPRESSED_SIZE=$(stat -c%s "$COMPRESSED_IMAGE")
    
    log_info "원본 크기: $(numfmt --to=iec "$ORIGINAL_SIZE")"
    log_info "압축 크기: $(numfmt --to=iec "$COMPRESSED_SIZE")"
}

# 완료 메시지
show_completion_message() {
    echo
    echo "=========================================="
    log_success "Factor 클라이언트 SD카드 이미지 빌드 완료!"
    echo "=========================================="
    echo
    
    echo "📁 빌드 결과:"
    echo "   이미지 파일: $BUILD_DIR/$IMAGE_NAME.xz"
    echo "   크기: $(stat -c%s "$BUILD_DIR/$IMAGE_NAME.xz" | numfmt --to=iec)"
    echo
    
    echo "💾 SD카드 굽기 방법:"
    echo "   1. Raspberry Pi Imager 사용:"
    echo "      - 'Use custom' 선택"
    echo "      - 빌드된 이미지 파일 선택"
    echo
    echo "   2. dd 명령어 사용 (Linux/macOS):"
    echo "      sudo dd if=$BUILD_DIR/$IMAGE_NAME.xz of=/dev/sdX bs=4M status=progress"
    echo "      (sdX는 실제 SD카드 디바이스)"
    echo
    
    echo "🚀 사용 방법:"
    echo "   1. SD카드를 라즈베리파이에 삽입"
    echo "   2. 전원 연결 (첫 부팅은 시간이 걸릴 수 있음)"
    echo "   3. 웹 브라우저에서 http://라즈베리파이IP:8080 접속"
    echo
    
    echo "⚙️ 기본 설정:"
    echo "   - SSH 활성화됨 (사용자: pi, 기본 비밀번호 변경 필요)"
    echo "   - Factor 클라이언트 자동 시작"
    echo "   - 웹 서버 포트: 8080"
    echo
}

# 메인 함수
main() {
    echo "=========================================="
    echo " Factor 클라이언트 SD카드 이미지 빌드"
    echo "=========================================="
    echo
    
    log_info "SD카드 이미지 빌드를 시작합니다..."
    
    if ! confirm "계속 진행하시겠습니까?"; then
        echo "빌드가 취소되었습니다."
        exit 0
    fi
    
    echo
    
    # 빌드 단계 실행
    check_dependencies
    prepare_build_dir
    download_pi_os
    prepare_image
    mount_image
    install_factor_client
    create_first_boot_script
    cleanup_image
    compress_image
    show_completion_message
}

# 스크립트 실행
main "$@" 