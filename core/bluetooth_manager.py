"""
블루투스 연결 관리자
Factor Client의 블루투스 연결을 관리
"""

import os
import subprocess
import logging
import time
from typing import Dict, Any


class BluetoothManager:
    """블루투스 연결 관리자"""
    # BLE 고정 UUID (펌웨어/앱과 사전 합의된 값)
    SERVICE_UUID = "12345678-1234-1234-1234-123456789abc"
    CHAR_CMD_UUID = "87654321-4321-4321-4321-cba987654321"
    
    def __init__(self, config_manager=None):
        self.config_manager = config_manager
        self.logger = logging.getLogger(__name__)
        self.is_bluetooth_active = False
        # 내부 상태
        # (연결/스캔 관리는 BLE GATT 서버 및 앱이 담당)
        
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
                # 루트일 때만 서비스 시작을 시도, 아닐 경우 가이드만 출력
                try:
                    if hasattr(os, 'geteuid') and os.geteuid() == 0:
                        self._start_bluetooth_service()
                    else:
                        self.logger.warning(
                            "루트 권한이 아니므로 서비스 시작을 건너뜁니다. "
                            "관리자 권한에서 'sudo systemctl enable --now bluetooth'를 실행하세요."
                        )
                except Exception as e:
                    self.logger.warning(f"블루투스 서비스 시작 시도 중 예외: {e}")
                
        except Exception as e:
            self.logger.error(f"블루투스 초기화 실패: {e}")
    
    def _start_bluetooth_service(self):
        """블루투스 서비스 시작"""
        try:
            subprocess.run(['systemctl', 'start', 'bluetooth'], check=True)
            subprocess.run(['systemctl', 'enable', 'bluetooth'], check=True)
            
            # 잠시 대기 후 인터페이스 활성화
            time.sleep(3)
            self._enable_bluetooth_interface()
            
            self.is_bluetooth_active = True
            self.logger.info("블루투스 서비스가 시작되었습니다")
            
        except Exception as e:
            self.logger.error(f"블루투스 서비스 시작 실패(권한 필요): {e}")
    
    def _enable_bluetooth_interface(self):
        """블루투스 인터페이스 활성화"""
        try:
            # 블루투스 장비 이름 설정
            subprocess.run([
                'bluetoothctl', 'set-alias', self.bluetooth_config['device_name']
            ], check=True)
            
            # 블루투스 장비를 발견 가능하게 설정
            subprocess.run(['bluetoothctl', 'discoverable', 'on'], check=True)
            
            # 블루투스 장비를 페어링 가능하게 설정
            subprocess.run(['bluetoothctl', 'pairable', 'on'], check=True)

            # BLE 전원/광고 설정 (BLE 스캐너에서 검색 가능하도록)
            try:
                subprocess.run(['bluetoothctl', 'power', 'on'], check=True)
            except Exception:
                pass
            # 광고 재설정: off 후 on (상태 불명확 시 안전)
            try:
                subprocess.run(['bluetoothctl', 'advertise', 'off'], check=False)
            except Exception:
                pass
            try:
                subprocess.run(['bluetoothctl', 'advertise', 'on'], check=True)
                self.logger.info("BLE 광고(advertise on) 활성화")
            except Exception as e:
                self.logger.warning(f"BLE 광고 활성화 실패: {e}")
            
            self.logger.info("블루투스 인터페이스가 활성화되었습니다")
            
        except Exception as e:
            self.logger.error(f"블루투스 인터페이스 활성화 실패: {e}")
    
    # BLE GATT 처리는 core/ble_gatt_server.py에서 담당

    def B_equipment_info_sent(
        self,
        mac_address: str,
        data: bytes,
        trace_id: Optional[str] = None
    ) -> None:
        """BLE 단위 - 설비 설정 캐릭터리스틱 발신 로깅"""
        trace = trace_id or self.new_trace_id()
        self.logger.info(
            f"[B_equipment_info_sent][trace={trace}] mac={mac_address} "
            f"svc={self.SERVICE_UUID} chr={self.EQUIPMENT_SETTINGS_CHAR_UUID}"
        )
        # 기능 단순화: 송신 로그만 기록(데이터 송신 자체는 BLE 상위 레이어에서 처리)
        try:
            total_len = len(data) if data is not None else 0
            preview_len = min(total_len, 128)
            preview_bytes = (data or b'')[:preview_len]
            try:
                preview_text = preview_bytes.decode('utf-8', errors='replace')
            except Exception:
                preview_text = repr(preview_bytes)
            preview_hex = preview_bytes.hex()
            self.logger.info(
                f"[BT TX] mac={mac_address} bytes={total_len} "
                f"text_preview={preview_text!r} hex_preview={preview_hex}"
            )
        except Exception as e:
            self.logger.error(f"송신 데이터 미리보기 로깅 실패({mac_address}): {e}")

    def B_on_ble_connected(self, mac_address: str, trace_id: Optional[str] = None) -> None:
        """BLE 단위 - 연결 이벤트 로깅"""
        trace = trace_id or self.new_trace_id()
        self.logger.info(
            f"[B_on_ble_connected][trace={trace}] mac={mac_address} "
            f"svc={self.SERVICE_UUID} wifi_chr={self.WIFI_REGISTER_CHAR_UUID} equip_chr={self.EQUIPMENT_SETTINGS_CHAR_UUID}"
        )

    def B_on_ble_disconnected(self, mac_address: str, trace_id: Optional[str] = None) -> None:
        """BLE 단위 - 해제 이벤트 로깅"""
        trace = trace_id or self.new_trace_id()
        self.logger.info(
            f"[B_on_ble_disconnected][trace={trace}] mac={mac_address}"
        )

    def get_bluetooth_status(self) -> Dict[str, Any]:
        """블루투스 상태 정보 반환"""
        return {
            'active': self.is_bluetooth_active,
            'device_name': self.bluetooth_config['device_name']
        }
    
    def stop_bluetooth(self):
        """블루투스 서비스 중지"""
        try:
            # 블루투스 서비스 중지
            subprocess.run(['systemctl', 'stop', 'bluetooth'], check=False)
            
            self.is_bluetooth_active = False
            self.logger.info("블루투스 서비스가 중지되었습니다")
            
        except Exception as e:
            self.logger.error(f"블루투스 서비스 중지 실패: {e}")
