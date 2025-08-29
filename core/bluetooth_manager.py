"""
블루투스 연결 관리자
Factor Client의 블루투스 연결을 관리
"""

import os
import subprocess
import logging
import time
import json
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, List
import threading


class BluetoothManager:
    """블루투스 연결 관리자"""
    # BLE 고정 UUID (펌웨어/앱과 사전 합의된 값)
    SERVICE_UUID = "12345678-1234-1234-1234-123456789abc"
    CHAR_CMD_UUID = "87654321-4321-4321-4321-cba987654321"
    # 단순화된 기능 분리(권장 명칭): Wi-Fi 등록용/설비 설정용
    WIFI_REGISTER_CHAR_UUID = "87654321-4321-4321-4321-cba987654321"
    EQUIPMENT_SETTINGS_CHAR_UUID = "87654321-4321-4321-4321-cba987654322"
    
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

            # BLE 전원/광고 설정 (BLE 스캐너에서 검색 가능하도록)
            try:
                subprocess.run(['sudo', 'bluetoothctl', 'power', 'on'], check=True)
            except Exception:
                pass
            # 광고 재설정: off 후 on (상태 불명확 시 안전)
            try:
                subprocess.run(['sudo', 'bluetoothctl', 'advertise', 'off'], check=False)
            except Exception:
                pass
            try:
                subprocess.run(['sudo', 'bluetoothctl', 'advertise', 'on'], check=True)
                self.logger.info("BLE 광고(advertise on) 활성화")
            except Exception as e:
                self.logger.warning(f"BLE 광고 활성화 실패: {e}")
            
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
                
                # 연결 상태확인 로그 추가
                self._log_connection_status(mac_address)
                
                return True
            else:
                self.logger.error(f"장비 연결 실패: {mac_address}")
                return False
                
        except Exception as e:
            self.logger.error(f"장비 연결 중 오류: {e}")
            return False

    def _log_connection_status(self, mac_address: str) -> None:
        """bluetoothctl info 결과를 요약해 연결 상태를 로깅"""
        try:
            result = subprocess.run(
                ['bluetoothctl', 'info', mac_address],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                self.logger.warning(f"연결 상태 확인 실패({mac_address}): 반환코드 {result.returncode}")
                return

            summary = {}
            for raw in (result.stdout or "").splitlines():
                line = (raw or "").strip()
                if not line:
                    continue
                if line.startswith('Name:'):
                    summary['name'] = line.split('Name:', 1)[1].strip()
                elif line.startswith('Alias:'):
                    summary['alias'] = line.split('Alias:', 1)[1].strip()
                elif line.startswith('Paired:'):
                    summary['paired'] = line.split(':', 1)[1].strip()
                elif line.startswith('Trusted:'):
                    summary['trusted'] = line.split(':', 1)[1].strip()
                elif line.startswith('Blocked:'):
                    summary['blocked'] = line.split(':', 1)[1].strip()
                elif line.startswith('Connected:'):
                    summary['connected'] = line.split(':', 1)[1].strip()

            self.logger.info(f"블루투스 연결 상태({mac_address}): {summary}")
        except Exception as e:
            self.logger.warning(f"연결 상태 확인 중 예외({mac_address}): {e}")

    def log_received_data(self, mac_address: str, data: bytes) -> None:
        """수신 데이터 로깅(길이/텍스트미리보기/hex미리보기), 과도한 로그 방지"""
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
                f"[BT RX] mac={mac_address} bytes={total_len} "
                f"text_preview={preview_text!r} hex_preview={preview_hex}"
            )
        except Exception as e:
            self.logger.error(f"수신 데이터 로깅 실패({mac_address}): {e}")
    
    # 일반 데이터 송수신 로깅은 사용하지 않음(기능 축소)
    
    def new_trace_id(self) -> str:
        """작업 단위 추적용 trace_id 발급"""
        return uuid.uuid4().hex

    def B_pair_device(self, mac_address: str, trace_id: Optional[str] = None) -> bool:
        """BLE 단위 - 페어링(추적 ID 포함)"""
        trace = trace_id or self.new_trace_id()
        self.logger.info(
            f"[B_pair_device][trace={trace}] 시작: mac={mac_address} "
            f"svc={self.SERVICE_UUID} chr={self.CHAR_CMD_UUID}"
        )
        try:
            ok = self.pair_device(mac_address)
            if ok:
                self.logger.info(f"[B_pair_device][trace={trace}] 성공: mac={mac_address}")
            else:
                self.logger.error(f"[B_pair_device][trace={trace}] 실패: mac={mac_address}")
            return ok
        except Exception as e:
            self.logger.error(f"[B_pair_device][trace={trace}] 예외: {e}")
            return False

    def B_connect_device(self, mac_address: str, trace_id: Optional[str] = None) -> bool:
        """BLE 단위 - 연결(추적 ID 포함)"""
        trace = trace_id or self.new_trace_id()
        self.logger.info(
            f"[B_connect_device][trace={trace}] 시작: mac={mac_address} "
            f"svc={self.SERVICE_UUID} chr={self.CHAR_CMD_UUID}"
        )
        try:
            ok = self.connect_device(mac_address)
            if ok:
                self.logger.info(f"[B_connect_device][trace={trace}] 성공: mac={mac_address}")
            else:
                self.logger.error(f"[B_connect_device][trace={trace}] 실패: mac={mac_address}")
            return ok
        except Exception as e:
            self.logger.error(f"[B_connect_device][trace={trace}] 예외: {e}")
            return False

    def B_disconnect_device(self, mac_address: str, trace_id: Optional[str] = None) -> bool:
        """BLE 단위 - 연결 해제(추적 ID 포함)"""
        trace = trace_id or self.new_trace_id()
        self.logger.info(
            f"[B_disconnect_device][trace={trace}] 시작: mac={mac_address} "
            f"svc={self.SERVICE_UUID} chr={self.CHAR_CMD_UUID}"
        )
        try:
            ok = self.disconnect_device(mac_address)
            if ok:
                self.logger.info(f"[B_disconnect_device][trace={trace}] 성공: mac={mac_address}")
            else:
                self.logger.error(f"[B_disconnect_device][trace={trace}] 실패: mac={mac_address}")
            return ok
        except Exception as e:
            self.logger.error(f"[B_disconnect_device][trace={trace}] 예외: {e}")
            return False

    def B_scan_devices(self, trace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """BLE 모듈 단위 - 스캔(추적 ID 포함)"""
        trace = trace_id or self.new_trace_id()
        self.logger.info(f"[B_scan_devices][trace={trace}] 시작")
        try:
            devices = self.scan_devices()
            self.logger.info(f"[B_scan_devices][trace={trace}] 완료 - 발견 {len(devices)}개")
            return devices
        except Exception as e:
            self.logger.error(f"[B_scan_devices][trace={trace}] 예외: {e}")
            return []

    def B_wifi_info_received(
        self,
        mac_address: str,
        data: bytes,
        trace_id: Optional[str] = None
    ) -> Optional[bytes]:
        """BLE 단위 - Wi-Fi 등록 캐릭터리스틱 수신 로깅 및 처리

        - 입력: 앱이 WIFI_REGISTER_CHAR_UUID로 보낸 JSON 바이트
          {"type":"wifi_scan","data":{},"timestamp":...}
        - 출력: 처리 결과를 JSON 바이트로 반환(상위 BLE 레이어에서 notify/write로 응답 전송)
        """
        trace = trace_id or self.new_trace_id()
        self.logger.info(
            f"[B_wifi_info_received][trace={trace}] mac={mac_address} "
            f"svc={self.SERVICE_UUID} chr={self.WIFI_REGISTER_CHAR_UUID}"
        )
        self.log_received_data(mac_address, data)

        try:
            payload = json.loads((data or b"{}").decode("utf-8", errors="ignore"))
        except Exception as e:
            self.logger.error(f"[B_wifi_info_received][trace={trace}] JSON 파싱 실패: {e}")
            return self._ble_make_response_bytes(
                event_type="wifi_scan_result",
                data={"success": False, "error": "invalid_json"},
                trace_id=trace
            )

        msg_type = str(payload.get("type", "")).strip().lower()
        if msg_type == "wifi_scan":
            networks = self._scan_wifi_networks_ble()
            return self._ble_make_response_bytes(
                event_type="wifi_scan_result",
                data=networks,
                trace_id=trace
            )

        # 알 수 없는 타입은 무시하고 에러 반환
        return self._ble_make_response_bytes(
            event_type="wifi_error",
            data={"success": False, "error": "unknown_type", "type": msg_type},
            trace_id=trace
        )

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

    # ===== BLE Wi-Fi 스캔 처리 유틸 =====
    def _ble_make_response_bytes(self, event_type: str, data: Any, trace_id: str) -> bytes:
        """BLE 응답 JSON을 바이트로 생성"""
        resp = {
            "type": event_type,
            "data": data,
            "trace_id": trace_id,
            "timestamp": int(time.time())
        }
        try:
            out = json.dumps(resp, ensure_ascii=False).encode("utf-8", errors="ignore")
        except Exception:
            out = b"{}"
        return out

    def _scan_wifi_networks_ble(self) -> List[Dict[str, Any]]:
        """iwlist를 사용하여 주변 Wi-Fi 네트워크 스캔(ssid, rssi, security)"""
        networks: List[Dict[str, Any]] = []
        try:
            result = subprocess.run(
                ['sudo', 'iwlist', 'wlan0', 'scan'],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                self.logger.error("BLE Wi-Fi 스캔 실패(iwlist 오류)")
                return networks

            # iwlist 출력 파싱
            current: Dict[str, Any] = {}
            for raw in result.stdout.split('\n'):
                line = (raw or '').strip()
                if not line:
                    continue
                # 새로운 셀 시작
                if line.startswith('Cell '):
                    if current.get('ssid'):
                        networks.append(current)
                    current = {}
                    continue
                # SSID
                if 'ESSID:' in line:
                    try:
                        ssid = line.split('ESSID:', 1)[1].strip().strip('"')
                        current['ssid'] = ssid
                    except Exception:
                        pass
                    continue
                # RSSI(dBm)
                if 'Signal level=' in line:
                    try:
                        # 예: Signal level=-40 dBm 또는 Signal level=-40/70
                        import re as _re
                        m = _re.search(r'Signal level=\s*(-?\d+)', line)
                        if m:
                            current['rssi'] = int(m.group(1))
                    except Exception:
                        pass
                    continue
                # 보안 방식
                if 'Encryption key:' in line:
                    enc_on = 'on' in line.lower()
                    current.setdefault('_enc', enc_on)
                    continue
                if ('WPA2' in line) or ('IEEE 802.11i' in line) or ('RSN' in line):
                    current['security'] = 'WPA2'
                    continue
                if 'WPA' in line and 'WPA2' not in line:
                    current.setdefault('security', 'WPA')
                    continue
                if 'WEP' in line:
                    current.setdefault('security', 'WEP')
                    continue

            # 마지막 네트워크 추가
            if current.get('ssid'):
                networks.append(current)

            # 기본 security 결정 및 정리
            for n in networks:
                if 'security' not in n:
                    if n.get('_enc'):
                        n['security'] = 'Protected'
                    else:
                        n['security'] = 'Open'
                # 누락된 rssi 기본값
                if 'rssi' not in n:
                    n['rssi'] = -100
                # 내부 플래그 제거
                n.pop('_enc', None)

            return networks

        except Exception as e:
            self.logger.error(f"BLE Wi-Fi 스캔 처리 오류: {e}")
            return networks
    
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
