"""
핫스팟 모드 관리자
사용자가 WiFi 설정을 할 수 있도록 AP 모드를 제공
"""

import os
import subprocess
import logging
import time
from pathlib import Path
from typing import Dict, Any, Optional
import yaml


class HotspotManager:
    """핫스팟 모드 관리자"""
    
    def __init__(self, config_manager=None):
        self.config_manager = config_manager
        self.logger = logging.getLogger(__name__)
        self.is_hotspot_active = False
        self.hotspot_config = {
            'ssid': 'Factor-Client-Setup',
            'password': 'factor123',
            'channel': 6,
            'ip_range': '192.168.4.0/24',
            'gateway': '192.168.4.1'
        }
        
    def check_wifi_connection(self) -> bool:
        """WiFi 연결 상태 확인"""
        try:
            result = subprocess.run(
                ['iwgetid', '-r'], 
                capture_output=True, 
                text=True, 
                timeout=5
            )
            return result.returncode == 0 and result.stdout.strip()
        except Exception as e:
            self.logger.error(f"WiFi 연결 상태 확인 실패: {e}")
            return False
    
    def enable_hotspot(self) -> bool:
        """핫스팟 모드 활성화"""
        if self.is_hotspot_active:
            self.logger.info("핫스팟이 이미 활성화되어 있습니다")
            return True
            
        try:
            self.logger.info("핫스팟 모드 활성화 중...")
            
            # 1. hostapd 설정 파일 생성
            self._create_hostapd_config()
            
            # 2. dnsmasq 설정 파일 생성
            self._create_dnsmasq_config()
            
            # 3. 네트워크 인터페이스 설정
            self._configure_network_interface()
            
            # 4. 서비스 시작
            self._start_services()
            
            self.is_hotspot_active = True
            self.logger.info("핫스팟 모드가 활성화되었습니다")
            return True
            
        except Exception as e:
            self.logger.error(f"핫스팟 활성화 실패: {e}")
            return False
    
    def disable_hotspot(self) -> bool:
        """핫스팟 모드 비활성화"""
        if not self.is_hotspot_active:
            self.logger.info("핫스팟이 이미 비활성화되어 있습니다")
            return True
            
        try:
            self.logger.info("핫스팟 모드 비활성화 중...")
            
            # 서비스 중지
            self._stop_services()
            
            # 네트워크 인터페이스 재설정
            self._reset_network_interface()
            
            self.is_hotspot_active = False
            self.logger.info("핫스팟 모드가 비활성화되었습니다")
            return True
            
        except Exception as e:
            self.logger.error(f"핫스팟 비활성화 실패: {e}")
            return False
    
    def _create_hostapd_config(self):
        """hostapd 설정 파일 생성"""
        config_content = f"""
        interface=wlan0
        driver=nl80211
        ssid={self.hotspot_config['ssid']}
        hw_mode=g
        channel={self.hotspot_config['channel']}
        wmm_enabled=0
        macaddr_acl=0
        auth_algs=1
        ignore_broadcast_ssid=0
        wpa=2
        wpa_passphrase={self.hotspot_config['password']}
        wpa_key_mgmt=WPA-PSK
        wpa_pairwise=TKIP
        rsn_pairwise=CCMP
        """
        
        with open('/etc/hostapd/hostapd.conf', 'w') as f:
            f.write(config_content.strip())
    
    def _create_dnsmasq_config(self):
        """dnsmasq 설정 파일 생성"""
        config_content = f"""
        interface=wlan0
        dhcp-range=192.168.4.2,192.168.4.20,255.255.255.0,24h
        """
        
        with open('/etc/dnsmasq.conf', 'w') as f:
            f.write(config_content.strip())
    
    def _configure_network_interface(self):
        """네트워크 인터페이스 설정"""
        # wlan0 인터페이스에 고정 IP 할당
        subprocess.run(['sudo', 'ip', 'addr', 'add', '192.168.4.1/24', 'dev', 'wlan0'], check=True)
        subprocess.run(['sudo', 'ip', 'link', 'set', 'wlan0', 'up'], check=True)
    
    def _reset_network_interface(self):
        """네트워크 인터페이스 재설정"""
        subprocess.run(['sudo', 'ip', 'addr', 'flush', 'dev', 'wlan0'], check=False)
        subprocess.run(['sudo', 'systemctl', 'restart', 'dhcpcd'], check=False)
    
    def _start_services(self):
        """핫스팟 서비스 시작"""
        subprocess.run(['sudo', 'systemctl', 'start', 'hostapd'], check=True)
        subprocess.run(['sudo', 'systemctl', 'start', 'dnsmasq'], check=True)
    
    def _stop_services(self):
        """핫스팟 서비스 중지"""
        subprocess.run(['sudo', 'systemctl', 'stop', 'hostapd'], check=False)
        subprocess.run(['sudo', 'systemctl', 'stop', 'dnsmasq'], check=False)
    
    def get_hotspot_info(self) -> Dict[str, Any]:
        """핫스팟 정보 반환"""
        return {
            'active': self.is_hotspot_active,
            'ssid': self.hotspot_config['ssid'],
            'password': self.hotspot_config['password'],
            'gateway': self.hotspot_config['gateway'],
            'setup_url': f"http://{self.hotspot_config['gateway']}:8080/setup"
        }
    
    def auto_manage_hotspot(self):
        """자동 핫스팟 관리 - WiFi 연결 실패 시 핫스팟 모드 활성화"""
        if not self.check_wifi_connection():
            self.logger.info("WiFi 연결이 없습니다. 핫스팟 모드를 활성화합니다.")
            self.enable_hotspot()
        elif self.is_hotspot_active:
            self.logger.info("WiFi 연결이 복구되었습니다. 핫스팟 모드를 비활성화합니다.")
            self.disable_hotspot()
    
    def apply_wifi_config(self, wifi_config: Dict[str, str]) -> bool:
        """WiFi 설정 적용"""
        try:
            ssid = wifi_config.get('ssid', '')
            password = wifi_config.get('password', '')
            
            if not ssid:
                raise ValueError("SSID가 필요합니다")
            
            # wpa_supplicant.conf 파일 생성
            wpa_config = f"""
                country=KR
                ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
                update_config=1

                network={{
                    ssid="{ssid}"
                    psk="{password}"
                }}
            """
            
            with open('/etc/wpa_supplicant/wpa_supplicant.conf', 'w') as f:
                f.write(wpa_config.strip())
            
            # 네트워크 재시작
            subprocess.run(['sudo', 'systemctl', 'restart', 'dhcpcd'], check=True)
            subprocess.run(['sudo', 'wpa_cli', '-i', 'wlan0', 'reconfigure'], check=True)
            
            self.logger.info(f"WiFi 설정이 적용되었습니다: {ssid}")
            return True
            
        except Exception as e:
            self.logger.error(f"WiFi 설정 적용 실패: {e}")
            return False 