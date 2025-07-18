#!/bin/bash

# Factor 클라이언트 Docker 이미지 빌드 스크립트
# 라즈베리파이용 Docker 이미지를 빌드합니다.

set -e

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

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

# 스크립트 디렉토리
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# 이미지 정보
IMAGE_NAME="factor-client"
IMAGE_TAG="latest"
REGISTRY="your-registry.com"  # 실제 레지스트리 주소로 변경

# 사용법 출력
usage() {
    echo "사용법: $0 [OPTIONS]"
    echo ""
    echo "옵션:"
    echo "  -t, --tag TAG        이미지 태그 (기본값: latest)"
    echo "  -r, --registry REG   Docker 레지스트리 주소"
    echo "  -p, --push           빌드 후 레지스트리에 푸시"
    echo "  -h, --help           도움말 표시"
    echo ""
    echo "예시:"
    echo "  $0 -t v1.0.0 -r your-registry.com -p"
}

# 옵션 파싱
PUSH_IMAGE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -t|--tag)
            IMAGE_TAG="$2"
            shift 2
            ;;
        -r|--registry)
            REGISTRY="$2"
            shift 2
            ;;
        -p|--push)
            PUSH_IMAGE=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            log_error "알 수 없는 옵션: $1"
            usage
            exit 1
            ;;
    esac
done

# Docker 확인
check_docker() {
    log_info "Docker 환경 확인 중..."
    
    if ! command -v docker &> /dev/null; then
        log_error "Docker가 설치되지 않았습니다."
        exit 1
    fi
    
    if ! docker info &> /dev/null; then
        log_error "Docker 데몬이 실행되지 않았습니다."
        exit 1
    fi
    
    log_success "Docker 환경 확인 완료"
}

# 멀티 플랫폼 빌드 설정
setup_buildx() {
    log_info "Docker Buildx 설정 중..."
    
    # buildx 사용 가능 확인
    if ! docker buildx version &> /dev/null; then
        log_error "Docker Buildx가 지원되지 않습니다."
        exit 1
    fi
    
    # 빌더 생성 (이미 존재하면 무시)
    docker buildx create --name factor-builder --use 2>/dev/null || true
    docker buildx inspect --bootstrap
    
    log_success "Docker Buildx 설정 완료"
}

# 이미지 빌드
build_image() {
    log_info "Docker 이미지 빌드 중..."
    
    cd "$PROJECT_DIR"
    
    # 빌드 정보
    BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    VERSION=$(cat VERSION 2>/dev/null || echo "dev")
    
    # 빌드 아규먼트
    BUILD_ARGS=(
        --build-arg "BUILD_DATE=$BUILD_DATE"
        --build-arg "GIT_COMMIT=$GIT_COMMIT"
        --build-arg "VERSION=$VERSION"
    )
    
    # 플랫폼 설정 (ARM64 for Raspberry Pi)
    PLATFORMS="linux/arm64,linux/amd64"
    
    # 이미지 태그
    FULL_IMAGE_NAME="$REGISTRY/$IMAGE_NAME:$IMAGE_TAG"
    
    log_info "빌드 정보:"
    log_info "  이미지: $FULL_IMAGE_NAME"
    log_info "  플랫폼: $PLATFORMS"
    log_info "  버전: $VERSION"
    log_info "  커밋: $GIT_COMMIT"
    log_info "  빌드 날짜: $BUILD_DATE"
    
    # 빌드 실행
    if [[ "$PUSH_IMAGE" == "true" ]]; then
        log_info "빌드 및 푸시 중..."
        docker buildx build \
            --platform "$PLATFORMS" \
            "${BUILD_ARGS[@]}" \
            -t "$FULL_IMAGE_NAME" \
            -t "$REGISTRY/$IMAGE_NAME:latest" \
            --push \
            .
    else
        log_info "로컬 빌드 중..."
        docker buildx build \
            --platform "linux/arm64" \
            "${BUILD_ARGS[@]}" \
            -t "$FULL_IMAGE_NAME" \
            --load \
            .
    fi
    
    log_success "Docker 이미지 빌드 완료"
}

# 이미지 정보 출력
show_image_info() {
    log_info "빌드된 이미지 정보:"
    
    FULL_IMAGE_NAME="$REGISTRY/$IMAGE_NAME:$IMAGE_TAG"
    
    # 이미지 크기 확인
    if docker image inspect "$FULL_IMAGE_NAME" &> /dev/null; then
        SIZE=$(docker image inspect "$FULL_IMAGE_NAME" --format='{{.Size}}' | numfmt --to=iec)
        log_info "  이미지 크기: $SIZE"
    fi
    
    echo
    echo "🐳 Docker 실행 명령어:"
    echo "  docker run -d --name factor-client \\"
    echo "    -p 8080:8080 \\"
    echo "    -v /dev:/dev \\"
    echo "    -v factor-config:/app/config \\"
    echo "    -v factor-logs:/app/logs \\"
    echo "    --privileged \\"
    echo "    $FULL_IMAGE_NAME"
    echo
    
    echo "🏗️ Docker Compose 예시:"
    echo "  services:"
    echo "    factor-client:"
    echo "      image: $FULL_IMAGE_NAME"
    echo "      ports:"
    echo "        - \"8080:8080\""
    echo "      volumes:"
    echo "        - /dev:/dev"
    echo "        - factor-config:/app/config"
    echo "        - factor-logs:/app/logs"
    echo "      privileged: true"
    echo "      restart: unless-stopped"
    echo
}

# 라즈베리파이 배포 스크립트 생성
create_deploy_script() {
    log_info "라즈베리파이 배포 스크립트 생성 중..."
    
    DEPLOY_SCRIPT="$PROJECT_DIR/deploy-to-pi.sh"
    
    cat > "$DEPLOY_SCRIPT" << EOF
#!/bin/bash

# Factor 클라이언트 라즈베리파이 배포 스크립트
# 사용법: ./deploy-to-pi.sh [PI_HOST] [PI_USER]

set -e

PI_HOST="\${1:-raspberrypi.local}"
PI_USER="\${2:-pi}"
IMAGE_NAME="$REGISTRY/$IMAGE_NAME:$IMAGE_TAG"

echo "🍓 라즈베리파이에 Factor 클라이언트 배포"
echo "  대상: \$PI_USER@\$PI_HOST"
echo "  이미지: \$IMAGE_NAME"
echo

# SSH 연결 테스트
echo "SSH 연결 테스트 중..."
if ! ssh -o ConnectTimeout=5 "\$PI_USER@\$PI_HOST" "echo 'SSH 연결 성공'"; then
    echo "❌ SSH 연결 실패"
    exit 1
fi

# Docker 설치 확인
echo "Docker 설치 확인 중..."
ssh "\$PI_USER@\$PI_HOST" "
    if ! command -v docker &> /dev/null; then
        echo 'Docker 설치 중...'
        curl -fsSL https://get.docker.com | sh
        sudo usermod -aG docker \$USER
        echo 'Docker 설치 완료. 재로그인 후 사용하세요.'
    else
        echo 'Docker가 이미 설치되어 있습니다.'
    fi
"

# 이미지 풀
echo "Docker 이미지 풀 중..."
ssh "\$PI_USER@\$PI_HOST" "docker pull \$IMAGE_NAME"

# 기존 컨테이너 중지 및 제거
echo "기존 컨테이너 정리 중..."
ssh "\$PI_USER@\$PI_HOST" "
    docker stop factor-client 2>/dev/null || true
    docker rm factor-client 2>/dev/null || true
"

# 새 컨테이너 시작
echo "새 컨테이너 시작 중..."
ssh "\$PI_USER@\$PI_HOST" "
    docker run -d --name factor-client \\
        -p 8080:8080 \\
        -v /dev:/dev \\
        -v factor-config:/app/config \\
        -v factor-logs:/app/logs \\
        --privileged \\
        --restart unless-stopped \\
        \$IMAGE_NAME
"

# 배포 확인
echo "배포 확인 중..."
sleep 5
if ssh "\$PI_USER@\$PI_HOST" "docker ps | grep factor-client"; then
    echo "✅ Factor 클라이언트 배포 성공!"
    echo "   접속 주소: http://\$PI_HOST:8080"
else
    echo "❌ 배포 실패"
    exit 1
fi
EOF

    chmod +x "$DEPLOY_SCRIPT"
    
    log_success "배포 스크립트 생성 완료: $DEPLOY_SCRIPT"
}

# 메인 함수
main() {
    echo "=========================================="
    echo "🐳 Factor 클라이언트 Docker 이미지 빌드"
    echo "=========================================="
    echo
    
    check_docker
    setup_buildx
    build_image
    show_image_info
    create_deploy_script
    
    echo
    log_success "모든 작업이 완료되었습니다!"
    
    if [[ "$PUSH_IMAGE" == "true" ]]; then
        log_info "이미지가 레지스트리에 푸시되었습니다."
    else
        log_info "로컬에서 이미지를 확인할 수 있습니다:"
        echo "  docker images | grep $IMAGE_NAME"
    fi
}

# 스크립트 실행
main "$@" 