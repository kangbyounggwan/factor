#!/bin/bash

# Factor Client 업데이트 스크립트
# 라즈베리파이에서 Git pull 후 자동으로 서비스를 재시작합니다.

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

# 사용법 출력
usage() {
    echo "사용법: $0 [OPTIONS]"
    echo ""
    echo "옵션:"
    echo "  -b, --branch BRANCH    Git 브랜치 (기본값: master)"
    echo "  -r, --remote REMOTE    Git 원격 저장소 (기본값: origin)"
    echo "  -s, --skip-deps        의존성 업데이트 건너뛰기"
    echo "  -c, --skip-config      설정 파일 복사 건너뛰기"
    echo "  -h, --help             도움말 표시"
    echo ""
    echo "예시:"
    echo "  $0 -b main -r origin"
    echo "  $0 --skip-deps"
}

# 옵션 파싱
BRANCH="master"
REMOTE="origin"
SKIP_DEPS=false
SKIP_CONFIG=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -b|--branch)
            BRANCH="$2"
            shift 2
            ;;
        -r|--remote)
            REMOTE="$2"
            shift 2
            ;;
        -s|--skip-deps)
            SKIP_DEPS=true
            shift
            ;;
        -c|--skip-config)
            SKIP_CONFIG=true
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

# 프로젝트 디렉토리 확인
check_project_dir() {
    log_info "프로젝트 디렉토리 확인 중..."
    
    if [[ ! -d "$PROJECT_DIR" ]]; then
        log_error "프로젝트 디렉토리를 찾을 수 없습니다: $PROJECT_DIR"
        exit 1
    fi
    
    if [[ ! -f "$PROJECT_DIR/main.py" ]]; then
        log_error "Factor Client 프로젝트가 아닙니다: main.py 파일이 없습니다"
        exit 1
    fi
    
    log_success "프로젝트 디렉토리 확인 완료"
}

# Git 상태 확인
check_git_status() {
    log_info "Git 상태 확인 중..."
    
    cd "$PROJECT_DIR"
    
    if [[ ! -d ".git" ]]; then
        log_error "Git 저장소가 아닙니다"
        exit 1
    fi
    
    # 로컬 변경사항 확인
    if [[ -n "$(git status --porcelain)" ]]; then
        log_warning "로컬 변경사항이 있습니다"
        echo "변경사항을 stash하시겠습니까? (y/N)"
        read -r response
        if [[ "$response" =~ ^[Yy]$ ]]; then
            git stash
            log_info "변경사항을 stash했습니다"
        else
            log_error "업데이트를 중단합니다"
            exit 1
        fi
    fi
    
    log_success "Git 상태 확인 완료"
}

# Git pull 실행
git_pull() {
    log_info "Git pull 실행 중..."
    
    cd "$PROJECT_DIR"
    
    # 원격 저장소 확인
    if ! git remote get-url "$REMOTE" > /dev/null 2>&1; then
        log_error "원격 저장소 '$REMOTE'를 찾을 수 없습니다"
        exit 1
    fi
    
    # 브랜치 확인
    if ! git show-ref --verify --quiet "refs/remotes/$REMOTE/$BRANCH"; then
        log_error "브랜치 '$BRANCH'를 찾을 수 없습니다"
        exit 1
    fi
    
    # Pull 실행
    if git pull "$REMOTE" "$BRANCH"; then
        log_success "Git pull 완료"
    else
        log_error "Git pull 실패"
        exit 1
    fi
}

# 의존성 업데이트
update_dependencies() {
    if [[ "$SKIP_DEPS" == "true" ]]; then
        log_info "의존성 업데이트 건너뛰기"
        return
    fi
    
    log_info "의존성 업데이트 중..."
    
    cd "$PROJECT_DIR"
    
    # 가상환경 확인
    if [[ ! -d "venv" ]]; then
        log_error "가상환경을 찾을 수 없습니다. 먼저 설치를 실행하세요"
        exit 1
    fi
    
    # 가상환경 활성화
    source venv/bin/activate
    
    # pip 업그레이드
    pip install --upgrade pip
    
    # 의존성 설치
    if pip install -r requirements.txt; then
        log_success "의존성 업데이트 완료"
    else
        log_error "의존성 업데이트 실패"
        exit 1
    fi
}

# 설정 파일 동기화
sync_config() {
    if [[ "$SKIP_CONFIG" == "true" ]]; then
        log_info "설정 파일 동기화 건너뛰기"
        return
    fi
    
    log_info "설정 파일 동기화 중..."
    
    # 설정 디렉토리 생성
    sudo mkdir -p /opt/factor-client/config
    sudo mkdir -p /opt/factor-client/logs
    
    # 설정 파일 복사
    if sudo cp "$PROJECT_DIR/config/settings.yaml" /opt/factor-client/config/; then
        log_success "설정 파일 복사 완료"
    else
        log_error "설정 파일 복사 실패"
        exit 1
    fi
    
    # 권한 설정
    sudo chown -R pi:pi /opt/factor-client
    log_success "권한 설정 완료"
}

# 서비스 재시작
restart_service() {
    log_info "서비스 재시작 중..."
    
    # systemd 재로드
    if sudo systemctl daemon-reload; then
        log_success "systemd 재로드 완료"
    else
        log_error "systemd 재로드 실패"
        exit 1
    fi
    
    # 서비스 재시작
    if sudo systemctl restart factor-client; then
        log_success "서비스 재시작 완료"
    else
        log_error "서비스 재시작 실패"
        exit 1
    fi
}

# 상태 확인
check_status() {
    log_info "서비스 상태 확인 중..."
    
    if sudo systemctl is-active --quiet factor-client; then
        log_success "Factor Client 서비스가 정상 실행 중입니다"
    else
        log_error "Factor Client 서비스가 실행되지 않았습니다"
        echo "로그를 확인하세요: sudo journalctl -u factor-client -n 50"
        exit 1
    fi
    
    # 상태 정보 출력
    echo
    echo "📊 서비스 상태:"
    sudo systemctl status factor-client --no-pager
}

# 메인 함수
main() {
    echo "🚀 Factor Client 업데이트 시작..."
    echo "프로젝트 디렉토리: $PROJECT_DIR"
    echo "브랜치: $BRANCH"
    echo "원격 저장소: $REMOTE"
    echo
    
    check_project_dir
    check_git_status
    git_pull
    update_dependencies
    sync_config
    restart_service
    check_status
    
    echo
    log_success "업데이트가 성공적으로 완료되었습니다!"
    echo
    echo "📋 유용한 명령어:"
    echo "  로그 확인: sudo journalctl -u factor-client -f"
    echo "  서비스 상태: sudo systemctl status factor-client"
    echo "  서비스 중지: sudo systemctl stop factor-client"
    echo "  서비스 시작: sudo systemctl start factor-client"
}

# 스크립트 실행
main "$@" 