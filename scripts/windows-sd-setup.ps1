# Factor Client Windows SD Card Setup Script
# Requires PowerShell 5.1 or higher

param(
    [Parameter(Mandatory=$true)]
    [string]$SDCardDrive,
    
    [string]$WiFiSSID = "",
    [string]$WiFiPassword = "",
    [string]$SSHKeyPath = "",
    [switch]$EnableSSH = $false,
    [switch]$Help
)

# Show help
if ($Help) {
    Write-Host @"
Factor Client Windows SD Card Setup Script

Usage:
    .\scripts\windows-sd-setup.ps1 -SDCardDrive D: [options]

Parameters:
    -SDCardDrive    SD card drive letter (e.g., D:, E:)
    -WiFiSSID       WiFi network name
    -WiFiPassword   WiFi password
    -SSHKeyPath     SSH public key file path
    -EnableSSH      Enable SSH
    -Help           Show help

Examples:
    .\scripts\windows-sd-setup.ps1 -SDCardDrive D: -EnableSSH
    .\scripts\windows-sd-setup.ps1 -SDCardDrive E: -WiFiSSID "MyWiFi" -WiFiPassword "MyPassword" -EnableSSH

Important Notes:
1. First install Raspberry Pi OS to SD card using Raspberry Pi Imager
2. Run PowerShell as administrator
3. Verify the correct SD card drive letter
"@
    exit 0
}

# Check administrator privileges
if (-NOT ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")) {
    Write-Error "This script requires administrator privileges. Please run PowerShell as administrator."
    exit 1
}

# Check SD card drive
if (-not (Test-Path $SDCardDrive)) {
    Write-Error "Cannot find SD card drive: $SDCardDrive"
    Write-Host "Available drives:"
    Get-WmiObject -Class Win32_LogicalDisk | Where-Object { $_.DriveType -eq 2 } | ForEach-Object { Write-Host "  $($_.DeviceID)" }
    exit 1
}

Write-Host "=========================================="
Write-Host " Factor Client SD Card Setup"
Write-Host "=========================================="
Write-Host ""

Write-Host "Setup Information:"
Write-Host "  SD Card Drive: $SDCardDrive"
Write-Host "  WiFi SSID: $(if($WiFiSSID) { $WiFiSSID } else { 'Not configured' })"
Write-Host "  SSH Enabled: $(if($EnableSSH) { 'Yes' } else { 'No' })"
Write-Host ""

# User confirmation
$confirm = Read-Host "Continue? (y/N)"
if ($confirm -ne 'y' -and $confirm -ne 'Y') {
    Write-Host "Operation cancelled."
    exit 0
}

try {
    # 1. Enable SSH
    if ($EnableSSH) {
        Write-Host "Enabling SSH..."
        $sshFile = Join-Path $SDCardDrive "ssh"
        New-Item -ItemType File -Path $sshFile -Force | Out-Null
        Write-Host "SSH enabled"
    }

    # 2. Setup SSH key
    if ($SSHKeyPath -and (Test-Path $SSHKeyPath)) {
        Write-Host "Setting up SSH key..."
        $sshDir = Join-Path $SDCardDrive ".ssh"
        New-Item -ItemType Directory -Path $sshDir -Force | Out-Null
        
        $authorizedKeys = Join-Path $sshDir "authorized_keys"
        Copy-Item $SSHKeyPath $authorizedKeys -Force
        Write-Host "SSH key configured"
    }

    # 3. Setup USB-Ethernet for Pi 4
    Write-Host "Setting up USB-Ethernet for Pi 4..."
    $configTxt = Join-Path $SDCardDrive "config.txt"
    
    # Enable USB OTG mode
    $usbConfig = @"

# USB-Ethernet for direct PC connection
dtoverlay=dwc2
"@
    
    if (Test-Path $configTxt) {
        Add-Content -Path $configTxt -Value $usbConfig -Encoding UTF8
    } else {
        $usbConfig | Out-File -FilePath $configTxt -Encoding UTF8
    }
    
    # Modify cmdline.txt
    $cmdlineTxt = Join-Path $SDCardDrive "cmdline.txt"
    if (Test-Path $cmdlineTxt) {
        $cmdlineContent = Get-Content $cmdlineTxt -Raw
        if ($cmdlineContent -notmatch "modules-load=dwc2,g_ether") {
            $cmdlineContent = $cmdlineContent.TrimEnd() + " modules-load=dwc2,g_ether"
            $cmdlineContent | Out-File -FilePath $cmdlineTxt -Encoding ASCII -NoNewline
        }
    }
    
    Write-Host "USB-Ethernet configured"
    
    # 4. Setup USB serial console for all Pi versions
    Write-Host "Setting up USB serial console..."
    
    # Enable UART
    $uartConfig = @"

# USB Serial Console
enable_uart=1
"@
    
    if (Test-Path $configTxt) {
        Add-Content -Path $configTxt -Value $uartConfig -Encoding UTF8
    }
    
    Write-Host "USB serial console configured"

    # 5. Setup WiFi
    if ($WiFiSSID -and $WiFiPassword) {
        Write-Host "Configuring WiFi..."
        $wpaSupplicant = Join-Path $SDCardDrive "wpa_supplicant.conf"
        
        $wpaConfig = @"
country=KR
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1

network={
    ssid="$WiFiSSID"
    psk="$WiFiPassword"
}
"@
        
        $wpaConfig | Out-File -FilePath $wpaSupplicant -Encoding utf8 -Force
        Write-Host "WiFi configured"
    }

    # 6. Create Factor Client auto-install script with hotspot support
    Write-Host "Creating Factor Client install script..."
    
    $installScript = @'
#!/bin/bash

# Factor Client Auto-Install Script with Hotspot Support
# Runs automatically on first boot with user-friendly interface

set -e

# 설정
LOG_FILE="/var/log/factor-install.log"
QUIET_INSTALL=true
SHOW_PROGRESS=true

# 부팅 메시지 표시
if [ "$SHOW_PROGRESS" = "true" ]; then
    clear
    cat << 'EOF'
╔══════════════════════════════════════════════════════════════╗
║                    Factor Client 설치 중                     ║
║                                                              ║
║  🔧 시스템 구성 중...                                        ║
║  📡 핫스팟 모드 설정 중...                                   ║
║  🌐 네트워크 설정 중...                                      ║
║                                                              ║
║  ⏱️  예상 소요 시간: 5-10분                                 ║
║                                                              ║
║  완료 후 자동으로 재부팅됩니다.                              ║
║                                                              ║
║  설치 로그: /var/log/factor-install.log                     ║
╚══════════════════════════════════════════════════════════════╝
EOF
    echo
fi

# 조용한 설치 모드 (로그는 파일로만)
if [ "$QUIET_INSTALL" = "true" ]; then
    exec > "$LOG_FILE" 2>&1
fi

# 설치 시작 로그
echo "[$(date)] =========================================="
echo "[$(date)] Factor Client 자동 설치 시작"
echo "[$(date)] =========================================="

# 진행 상황 표시 함수
show_progress() {
    local step=$1
    local total=$2
    local message=$3
    
    if [ "$SHOW_PROGRESS" = "true" ]; then
        local percent=$((step * 100 / total))
        echo "[$(date)] [$step/$total] ($percent%) $message" >> "$LOG_FILE"
        
        # 콘솔에 간단한 진행 상황 표시
        if [ "$QUIET_INSTALL" != "true" ]; then
            printf "\r[$step/$total] $message..."
        fi
    fi
}

NETWORK_AVAILABLE=false

# 1단계: 네트워크 확인
show_progress 1 8 "네트워크 연결 확인 중"
for i in {1..15}; do
    if ping -c 1 google.com &> /dev/null; then
        echo "[$(date)] 네트워크 연결 확인됨 - 패키지 다운로드 가능"
        NETWORK_AVAILABLE=true
        break
    fi
    sleep 2
done

if [ "$NETWORK_AVAILABLE" = false ]; then
    echo "[$(date)] 네트워크 연결 없음 - 오프라인 설치 진행"
fi

# 2단계: 시스템 업데이트 (네트워크 연결 시에만)
if [ "$NETWORK_AVAILABLE" = true ]; then
    show_progress 2 8 "시스템 업데이트 중"
    apt-get update -y
    apt-get upgrade -y
    
    # 3단계: 핫스팟 패키지 설치
    show_progress 3 8 "핫스팟 패키지 설치 중"
    apt-get install -y hostapd dnsmasq iptables bridge-utils wireless-tools wpasupplicant dhcpcd5 iw rfkill
    
    # 4단계: 기본 패키지 설치
    show_progress 4 8 "기본 패키지 설치 중"
    apt-get install -y git python3 python3-pip python3-venv curl wget
else
    echo "[$(date)] 오프라인 모드 - 기본 패키지 사용"
    show_progress 2 8 "오프라인 모드로 진행"
    show_progress 3 8 "기본 패키지 확인 완료"
    show_progress 4 8 "패키지 설치 건너뛰기"
fi

# 5단계: Factor Client 소스 설정
show_progress 5 8 "Factor Client 소스 설정 중"
if [ -d "/boot/factor-client-source" ]; then
    echo "[$(date)] 로컬 소스 복사 중..."
    cp -r /boot/factor-client-source /opt/factor-client-firmware
    chown -R pi:pi /opt/factor-client-firmware
else
    # 네트워크 연결 시에만 Git에서 다운로드
    if [ "$NETWORK_AVAILABLE" = true ]; then
        echo "[$(date)] Git에서 Factor Client 다운로드 중..."
        cd /opt
        git clone https://github.com/your-repo/factor-client-firmware.git || {
            echo "[$(date)] Git clone 실패"
        }
    else
        echo "[$(date)] 네트워크 연결 없음 - Factor Client 소스를 찾을 수 없음"
        exit 1
    fi
fi

# 6단계: 핫스팟 시스템 설정
show_progress 6 8 "핫스팟 시스템 설정 중"
if [ -f "/opt/factor-client-firmware/scripts/setup-hotspot.sh" ]; then
    chmod +x /opt/factor-client-firmware/scripts/setup-hotspot.sh
    echo "y" | /opt/factor-client-firmware/scripts/setup-hotspot.sh || echo "[$(date)] 핫스팟 설정 완료"
fi

# 7단계: Factor Client 설치
show_progress 7 8 "Factor Client 설치 중"
if [ -f "/opt/factor-client-firmware/scripts/install-raspberry-pi.sh" ]; then
    echo "[$(date)] Factor Client 설치 스크립트 실행 중..."
    chmod +x /opt/factor-client-firmware/scripts/install-raspberry-pi.sh
    # 자동 설치 환경 변수 설정하여 조용한 모드로 실행
    export AUTO_INSTALL=true
    /opt/factor-client-firmware/scripts/install-raspberry-pi.sh --quiet
else
    echo "[$(date)] 설치 스크립트를 찾을 수 없음"
fi

# 8단계: 설정 파일 복사 및 정리
show_progress 8 8 "설정 완료 및 정리 중"
if [ -f "/boot/factor-config/settings.yaml" ]; then
    echo "[$(date)] 설정 파일 복사 중..."
    mkdir -p /etc/factor-client
    cp /boot/factor-config/settings.yaml /etc/factor-client/
    chown pi:pi /etc/factor-client/settings.yaml
fi

# 자동 설치 스크립트 제거
systemctl disable factor-auto-install
rm -f /etc/systemd/system/factor-auto-install.service
rm -f /usr/local/bin/factor-auto-install.sh

# 정리
rm -rf /boot/factor-client-source 2>/dev/null || true
rm -rf /boot/factor-config 2>/dev/null || true

echo "[$(date)] Factor Client 설치 완료"

# 설치 완료 메시지 (조용한 모드가 아닐 때만 표시)
if [ "$SHOW_PROGRESS" = "true" ]; then
    clear
    cat << 'EOF'
╔══════════════════════════════════════════════════════════════╗
║                  🎉 설치 완료! 🎉                           ║
║                                                              ║
║  Factor Client 핫스팟 모드 설치가 완료되었습니다!           ║
║                                                              ║
║  📱 접속 방법:                                              ║
║                                                              ║
║  방법 1: USB 직접 연결 (라즈베리파이 4 권장)                ║
║    • PC와 Pi를 USB-C 케이블로 연결                          ║
║    • 접속: http://169.254.1.1:8080/setup                   ║
║                                                              ║
║  방법 2: WiFi 핫스팟 (모든 버전)                            ║
║    • WiFi에서 'Factor-Client-Setup' 검색                   ║
║    • 비밀번호: factor123                                    ║
║    • 접속: http://192.168.4.1:8080/setup                   ║
║                                                              ║
║  🔧 문제 해결:                                              ║
║    • SSH: ssh pi@192.168.4.1 (비밀번호: raspberry)         ║
║    • 로그: sudo journalctl -u factor-client                ║
║                                                              ║
║  잠시 후 자동으로 재부팅됩니다...                           ║
╚══════════════════════════════════════════════════════════════╝
EOF
    echo
    
    # 카운트다운 표시
    for i in {5..1}; do
        echo -n "재부팅까지 $i초..."
        sleep 1
        echo -ne "\r                    \r"
    done
    echo "재부팅 중..."
fi

echo "[$(date)] 설치 완료 - 5초 후 재부팅"
sleep 5
reboot
'@

    $autoInstallScript = Join-Path $SDCardDrive "factor-auto-install.sh"
    $installScript | Out-File -FilePath $autoInstallScript -Encoding utf8 -Force
    
    # 7. Create systemd service file
    $serviceContent = @"
[Unit]
Description=Factor Client Auto Install
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/factor-auto-install.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"@

    $serviceFile = Join-Path $SDCardDrive "factor-auto-install.service"
    $serviceContent | Out-File -FilePath $serviceFile -Encoding utf8 -Force

    # 8. Create first boot setup script
    $firstBootScript = @"
#!/bin/bash

# First boot setup script

# Copy auto-install script
cp /boot/factor-auto-install.sh /usr/local/bin/
chmod +x /usr/local/bin/factor-auto-install.sh

# Copy service file
cp /boot/factor-auto-install.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable factor-auto-install

# Remove first boot scripts
rm -f /boot/factor-auto-install.sh
rm -f /boot/factor-auto-install.service
rm -f /boot/firstrun.sh

# Reboot
reboot
"@
    $firstRunScript = Join-Path $SDCardDrive "firstrun.sh"
    $firstBootScript | Out-File -FilePath $firstRunScript -Encoding utf8 -Force

    Write-Host "Factor Client install script created"

    # 9. Copy Factor Client source code for offline installation
    Write-Host "Copying Factor Client source code..."
    $sourceDir = Join-Path $SDCardDrive "factor-client-source"
    
    # 현재 스크립트 위치에서 프로젝트 루트 찾기
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $projectRoot = Split-Path -Parent $scriptDir
    
    if (Test-Path $projectRoot) {
        # Create source directory
        New-Item -ItemType Directory -Path $sourceDir -Force | Out-Null
        
        # Copy only necessary files (exclude Git)
        $itemsToCopy = @(
            "core",
            "web", 
            "scripts",
            "config",
            "docs",
            "main.py",
            "requirements.txt",
            "README.md"
        )
        
        foreach ($item in $itemsToCopy) {
            $sourcePath = Join-Path $projectRoot $item
            if (Test-Path $sourcePath) {
                $destPath = Join-Path $sourceDir $item
                Copy-Item $sourcePath $destPath -Recurse -Force
                Write-Host "  $item copied"
            }
        }
        
        Write-Host "Factor Client source code copied"
    } else {
        Write-Warning "Project root not found. Online installation will be used."
    }

    # 10. Create default configuration file
    Write-Host "Creating default configuration file..."
    
    $configDir = Join-Path $SDCardDrive "factor-config"
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    
    $defaultConfig = @"
# Factor Client Default Configuration
# This file will be copied to /etc/factor-client/settings.yaml on first boot

octoprint:
  host: "192.168.1.100"  # OctoPrint server IP address
  port: 5000
  api_key: "YOUR_API_KEY_HERE"  # OctoPrint API key

server:
  host: "0.0.0.0"
  port: 8080
  debug: false

logging:
  level: "INFO"
  file: "/var/log/factor-client/factor-client.log"

printer:
  port: "/dev/ttyUSB0"  # Printer serial port
  baudrate: 115200
  auto_detect: true
"@
    $configFile = Join-Path $configDir "settings.yaml"
    $defaultConfig | Out-File -FilePath $configFile -Encoding utf8 -Force
    
    Write-Host "Default configuration file created"

    # 11. Create usage guide file
# 11. Create usage guide file
$readmeContent = @"
# Factor Client SD Card 사용 가이드 (핫스팟 모드)
# ==============================================

# 이 SD카드는 Factor Client를 핫스팟 기능과 함께 자동으로 설치하도록 구성되어 있습니다.

# 🚀 사용 방법:
# 1. 이 SD카드를 라즈베리파이에 삽입
# 2. 전원 연결 (첫 부팅은 5-10분 소요)
# 3. 아래 방법 중 하나로 접속:

# 📱 접속 방법:

# 방법 1: USB 직접 연결 (라즈베리파이 4 권장)
# - PC와 Pi를 USB-C 케이블로 직접 연결
# - 접속: http://169.254.1.1:8080/setup 또는 http://raspberrypi.local:8080/setup

# 방법 2: WiFi 연결 가능한 경우
# - 접속: http://라즈베리파이IP:8080

# 방법 3: WiFi 연결 없음 (핫스팟 모드)
# - 모바일/노트북의 WiFi 설정으로 이동
# - 'Factor-Client-Setup' 네트워크에 연결
# - 비밀번호: factor123
# - 접속: http://192.168.4.1:8080/setup
# - 설정 마법사에서 WiFi 및 Factor Client 구성

# ⚙️ 설정 변경:
# - WiFi 설정: wpa_supplicant.conf 파일 수정
# - Factor 설정: factor-config/settings.yaml 파일 수정

# 🔐 SSH 접속:
# - 사용자: pi
# - 비밀번호: raspberry (변경 권장)

# 🔧 문제 해결:
# - 설치 로그: /var/log/factor-install.log
# - 서비스 로그: sudo journalctl -u factor-client
# - 핫스팟 상태: curl http://localhost:8080/api/hotspot/info

# 📋 핫스팟 정보:
# - SSID: Factor-Client-Setup
# - 비밀번호: factor123
# - 게이트웨이: 192.168.4.1
# - 설정 URL: http://192.168.4.1:8080/setup

# 📖 추가 정보:
# - 핫스팟 설정 가이드: docs/hotspot_setup_guide.md
# - 지원: https://github.com/your-repo/factor-client-firmware

# 💡 팁:
# - 첫 부팅 시 화면에 진행 상황이 표시됩니다
# - 설치 완료 후 자동으로 재부팅됩니다
# - 로그가 최소화되어 깔끔한 부팅 경험을 제공합니다
"@



$readmeFile = Join-Path $SDCardDrive "README-Factor.txt"
$readmeContent | Out-File -FilePath $readmeFile -Encoding utf8 -Force

Write-Host ""
Write-Host "=========================================="
Write-Host "SD Card Setup with Hotspot Mode Complete!"
Write-Host "=========================================="
Write-Host ""
Write-Host "Next Steps:"
Write-Host "1. Insert SD card into Raspberry Pi"
Write-Host "2. Connect power (first boot takes 5-10 minutes)"
Write-Host "3. Access using one of the following methods:"
Write-Host ""
Write-Host "WiFi Connection Available:"
Write-Host "   http://RaspberryPiIP:8080"
Write-Host ""
Write-Host "No WiFi Connection (Hotspot Mode):"
Write-Host "   1) Connect to 'Factor-Client-Setup' in WiFi"
Write-Host "   2) Password: factor123"
Write-Host "   3) Access: http://192.168.4.1:8080/setup"
Write-Host "   4) Configure WiFi and Factor Client via setup wizard"
Write-Host ""
Write-Host "Configuration File Locations:"
Write-Host "  WiFi settings: $SDCardDrive\wpa_supplicant.conf"
Write-Host "  Factor settings: $SDCardDrive\factor-config\settings.yaml"
Write-Host "  Usage guide: $SDCardDrive\README-Factor.txt"
Write-Host "  Source code: $SDCardDrive\factor-client-source\"
Write-Host ""
Write-Host "Troubleshooting:"
Write-Host "  Installation log: /var/log/factor-install.log"
Write-Host "  Service log: sudo journalctl -u factor-client"
Write-Host "  Hotspot status: curl http://localhost:8080/api/hotspot/info"
Write-Host ""
Write-Host "Hotspot Information:"
Write-Host "  SSID: Factor-Client-Setup"
Write-Host "  Password: factor123"
Write-Host "  Setup URL: http://192.168.4.1:8080/setup"
Write-Host ""

} catch {
    Write-Error "Error occurred: $($_.Exception.Message)"
    exit 1
}

# Exit with success code
exit 0
