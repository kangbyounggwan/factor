"""
블루투스 연결 관리자
Factor Client의 블루투스 연결을 관리
"""

import os
import subprocess
import logging
import time
import json
from pathlib import Path
from typing import Dict, Any, Optional, List
import threading


class BluetoothManager:
    """블루투스 연결 관리자"""
    
    def __init__(self, config_manager=None):
        self.config_manager = config_manager
        self.logger = logging.getLogger(__name__)
        self.is_bluetooth_active = False
        self.connected_devices = {}
        self.discovered_devices = {}
        
        # 블루투스 설정
        self.bluetooth_config = {
            'device_name': 'Factor-Client',
            'discoverable_timeout': 0,
            'pairable_timeout': 0,
            'auto_enable': True
        }
        
        # 설정 파일에서 블루투스 설정 로드
        self._load_bluetooth_config()
        
        # 블루투스 상태 확인 및 초기화
        self._init_bluetooth()
    
    def _load_bluetooth_config(self):
        """설정 파일에서 블루투스 설정 로드"""
        try:
            if self.config_manager and hasattr(self.config_manager, 'get_config'):
                config = self.config_manager.get_config()
                if config and 'bluetooth' in config:
                    bluetooth_config = config['bluetooth']
                    
                    if 'device_name' in bluetooth_config:
                        self.bluetooth_config['device_name'] = bluetooth_config['device_name']
                    
                    self.logger.info(f"블루투스 설정 로드됨: {self.bluetooth_config['device_name']}")
                    
        except Exception as e:
            self.logger.warning(f"블루투스 설정 로드 실패, 기본값 사용: {e}")
    
    def _init_bluetooth(self):
        """블루투스 초기화"""
        try:
            # 블루투스 서비스 상태 확인
            result = subprocess.run(
                ['systemctl', 'is-active', 'bluetooth'], 
                capture_output=True, 
                text=True, 
                timeout=5
            )
            
            if result.returncode == 0 and result.stdout.strip() == 'active':
                self.is_bluetooth_active = True
                self.logger.info("블루투스 서비스가 활성화되어 있습니다")
                
                # 블루투스 인터페이스 활성화
                self._enable_bluetooth_interface()
                
            else:
                self.logger.warning("블루투스 서비스가 비활성화되어 있습니다")
                self._start_bluetooth_service()
                
        except Exception as e:
            self.logger.error(f"블루투스 초기화 실패: {e}")
    
    def _start_bluetooth_service(self):
        """블루투스 서비스 시작"""
        try:
            subprocess.run(['sudo', 'systemctl', 'start', 'bluetooth'], check=True)
            subprocess.run(['sudo', 'systemctl', 'enable', 'bluetooth'], check=True)
            
            # 잠시 대기 후 인터페이스 활성화
            time.sleep(3)
            self._enable_bluetooth_interface()
            
            self.is_bluetooth_active = True
            self.logger.info("블루투스 서비스가 시작되었습니다")
            
        except Exception as e:
            self.logger.error(f"블루투스 서비스 시작 실패: {e}")
    
    def _enable_bluetooth_interface(self):
        """블루투스 인터페이스 활성화"""
        try:
            # hci0 인터페이스 활성화
            subprocess.run(['sudo', 'hciconfig', 'hci0', 'up'], check=True)
            
            # 블루투스 장비 이름 설정
            subprocess.run([
                'sudo', 'bluetoothctl', 'set-alias', self.bluetooth_config['device_name']
            ], check=True)
            
            # 블루투스 장비를 발견 가능하게 설정
            subprocess.run(['sudo', 'bluetoothctl', 'discoverable', 'on'], check=True)
            
            # 블루투스 장비를 페어링 가능하게 설정
            subprocess.run(['sudo', 'bluetoothctl', 'pairable', 'on'], check=True)
            
            self.logger.info("블루투스 인터페이스가 활성화되었습니다")
            
        except Exception as e:
            self.logger.error(f"블루투스 인터페이스 활성화 실패: {e}")
    
    def scan_devices(self) -> List[Dict[str, Any]]:
        """주변 블루투스 장비 스캔"""
        try:
            if not self.is_bluetooth_active:
                self.logger.warning("블루투스가 비활성화되어 있습니다")
                return []
            
            self.logger.info("블루투스 장비 스캔 중...")
            
            # 블루투스 스캔 실행
            result = subprocess.run(
                ['sudo', 'hcitool', 'scan'], 
                capture_output=True, 
                text=True, 
                timeout=30
            )
            
            if result.returncode == 0:
                devices = []
                lines = result.stdout.strip().split('\n')
                
                for line in lines[1:]:  # 첫 번째 줄은 헤더
                    if line.strip():
                        parts = line.strip().split('\t')
                        if len(parts) >= 2:
                            mac_address = parts[0].strip()
                            device_name = parts[1].strip()
                            
                            device_info = {
                                'mac_address': mac_address,
                                'name': device_name,
                                'type': 'unknown',
                                'rssi': 0
                            }
                            
                            # Factor Client 장비인지 확인
                            if 'factor' in device_name.lower() or 'client' in device_name.lower():
                                device_info['type'] = 'factor_client'
                            
                            devices.append(device_info)
                            self.discovered_devices[mac_address] = device_info
                
                self.logger.info(f"{len(devices)}개의 블루투스 장비를 발견했습니다")
                return devices
                
            else:
                self.logger.error("블루투스 스캔 실패")
                return []
                
        except Exception as e:
            self.logger.error(f"블루투스 장비 스캔 실패: {e}")
            return []
    
    def pair_device(self, mac_address: str) -> bool:
        """블루투스 장비 페어링"""
        try:
            if not self.is_bluetooth_active:
                return False
            
            self.logger.info(f"장비 페어링 중: {mac_address}")
            
            # 페어링 명령 실행
            result = subprocess.run([
                'sudo', 'bluetoothctl', 'pair', mac_address
            ], capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0 and 'successful' in result.stdout.lower():
                self.logger.info(f"장비 페어링 성공: {mac_address}")
                return True
            else:
                self.logger.error(f"장비 페어링 실패: {mac_address}")
                return False
                
        except Exception as e:
            self.logger.error(f"장비 페어링 중 오류: {e}")
            return False
    
    def connect_device(self, mac_address: str) -> bool:
        """블루투스 장비 연결"""
        try:
            if not self.is_bluetooth_active:
                return False
            
            self.logger.info(f"장비 연결 중: {mac_address}")
            
            # 연결 명령 실행
            result = subprocess.run([
                'sudo', 'bluetoothctl', 'connect', mac_address
            ], capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0 and 'successful' in result.stdout.lower():
                self.logger.info(f"장비 연결 성공: {mac_address}")
                
                # 연결된 장비 정보 저장
                if mac_address in self.discovered_devices:
                    self.connected_devices[mac_address] = self.discovered_devices[mac_address]
                    self.connected_devices[mac_address]['connected_at'] = time.time()
                
                return True
            else:
                self.logger.error(f"장비 연결 실패: {mac_address}")
                return False
                
        except Exception as e:
            self.logger.error(f"장비 연결 중 오류: {e}")
            return False
    
    def disconnect_device(self, mac_address: str) -> bool:
        """블루투스 장비 연결 해제"""
        try:
            if not self.is_bluetooth_active:
                return False
            
            self.logger.info(f"장비 연결 해제 중: {mac_address}")
            
            # 연결 해제 명령 실행
            result = subprocess.run([
                'sudo', 'bluetoothctl', 'disconnect', mac_address
            ], capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                self.logger.info(f"장비 연결 해제 성공: {mac_address}")
                
                # 연결된 장비 목록에서 제거
                if mac_address in self.connected_devices:
                    del self.connected_devices[mac_address]
                
                return True
            else:
                self.logger.error(f"장비 연결 해제 실패: {mac_address}")
                return False
                
        except Exception as e:
            self.logger.error(f"장비 연결 해제 중 오류: {e}")
            return False
    
    def get_connected_devices(self) -> List[Dict[str, Any]]:
        """연결된 블루투스 장비 목록 반환"""
        return list(self.connected_devices.values())
    
    def get_discovered_devices(self) -> List[Dict[str, Any]]:
        """발견된 블루투스 장비 목록 반환"""
        return list(self.discovered_devices.values())
    
    def get_bluetooth_status(self) -> Dict[str, Any]:
        """블루투스 상태 정보 반환"""
        return {
            'active': self.is_bluetooth_active,
            'device_name': self.bluetooth_config['device_name'],
            'connected_count': len(self.connected_devices),
            'discovered_count': len(self.discovered_devices),
            'connected_devices': self.get_connected_devices(),
            'discovered_devices': self.get_discovered_devices()
        }
    
    def start_discovery_service(self):
        """블루투스 발견 서비스 시작 (백그라운드)"""
        def discovery_worker():
            while self.is_bluetooth_active:
                try:
                    self.scan_devices()
                    time.sleep(60)  # 1분마다 스캔
                except Exception as e:
                    self.logger.error(f"발견 서비스 오류: {e}")
                    time.sleep(30)
        
        discovery_thread = threading.Thread(target=discovery_worker, daemon=True)
        discovery_thread.start()
        self.logger.info("블루투스 발견 서비스가 시작되었습니다")
    
    def stop_bluetooth(self):
        """블루투스 서비스 중지"""
        try:
            # 연결된 모든 장비 연결 해제
            for mac_address in list(self.connected_devices.keys()):
                self.disconnect_device(mac_address)
            
            # 블루투스 서비스 중지
            subprocess.run(['sudo', 'systemctl', 'stop', 'bluetooth'], check=False)
            
            self.is_bluetooth_active = False
            self.logger.info("블루투스 서비스가 중지되었습니다")
            
        except Exception as e:
            self.logger.error(f"블루투스 서비스 중지 실패: {e}")
