"""
설정 관리자
YAML 파일 기반 설정 관리 및 환경 변수 지원
"""

import os
import yaml
import logging
from typing import Dict, Any, Optional
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class ConfigFileHandler(FileSystemEventHandler):
    """설정 파일 변경 감지 핸들러"""
    
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.logger = logging.getLogger(__name__)
    
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith('.yaml'):
            self.logger.info(f"설정 파일 변경 감지: {event.src_path}")
            self.config_manager.reload_config()


class ConfigManager:
    """설정 관리자 클래스"""
    
    def __init__(self, config_path: str = "config/settings_rpi.yaml"):
        self.config_path = Path(config_path)
        self.config_data = {}
        self.logger = logging.getLogger(__name__)
        self.observer = None
        
        # 기본 설정값
        self.defaults = {
            'server': {
                'host': '0.0.0.0',
                'port': 8080,
                'debug': False
            },
            'printer': {
                'auto_detect': True,
                'port': '',
                'baudrate': 115200,
                'firmware_type': 'auto',
                'temp_poll_interval': 2.0,
                'position_poll_interval': 5.0,
                'timeout': 5
            },
            'logging': {
                'level': 'INFO',
                'file': '/var/log/factor-client.log',
                'max_size': '10MB',
                'backup_count': 5
            },
            'system': {
                'hostname': 'factor-client',
                'timezone': 'UTC',
                'power_management': {
                    'enable_watchdog': True,
                    'auto_reboot_on_error': True,
                    'max_error_count': 5
                },
                'network': {
                    'wifi_country': 'US',
                    'enable_ssh': True,
                    'enable_vnc': False
                },
                'storage': {
                    'enable_readonly_root': True,
                    'log_to_ram': True
                }
            },
            'display': {
                'enable': False,
                'type': 'ssd1306',
                'width': 128,
                'height': 64
            },
            'notifications': {
                'enable': False,
                'methods': []
            },
            'camera': {
                'enable': False,
                'stream_url': '',
                'snapshot_url': ''
            }
        }
        
        self.load_config()
        self.start_watching()
    
    def load_config(self):
        """설정 파일 로드"""
        try:
            if self.config_path.exists():
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    file_config = yaml.safe_load(f) or {}
                
                # 기본값과 파일 설정 병합
                self.config_data = self._merge_configs(self.defaults, file_config)
                
                # 환경 변수로 덮어쓰기
                self._apply_env_overrides()
                
                self.logger.info(f"설정 파일 로드 완료: {self.config_path}")
            else:
                self.logger.warning(f"설정 파일이 없습니다. 기본값 사용: {self.config_path}")
                self.config_data = self.defaults.copy()
                self._apply_env_overrides()
                
        except Exception as e:
            self.logger.error(f"설정 파일 로드 실패: {e}")
            self.config_data = self.defaults.copy()
            self._apply_env_overrides()
    
    def reload_config(self):
        """설정 파일 다시 로드"""
        self.logger.info("설정 파일 다시 로드 중...")
        self.load_config()
    
    def _merge_configs(self, default: Dict, override: Dict) -> Dict:
        """설정 딕셔너리 병합"""
        result = default.copy()
        
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_configs(result[key], value)
            else:
                result[key] = value
        
        return result
    
    def _apply_env_overrides(self):
        """환경 변수로 설정 덮어쓰기"""
        env_mappings = {
            'FACTOR_PRINTER_PORT': ['printer', 'port'],
            'FACTOR_PRINTER_BAUDRATE': ['printer', 'baudrate'],
            'FACTOR_SERVER_HOST': ['server', 'host'],
            'FACTOR_SERVER_PORT': ['server', 'port'],
            'FACTOR_LOG_LEVEL': ['logging', 'level'],
            'FACTOR_DEBUG': ['server', 'debug']
        }
        
        for env_var, config_path in env_mappings.items():
            value = os.getenv(env_var)
            if value is not None:
                self._set_nested_value(config_path, self._convert_env_value(value))
    
    def _convert_env_value(self, value: str) -> Any:
        """환경 변수 값 타입 변환"""
        # 불린 값 처리
        if value.lower() in ('true', 'yes', '1', 'on'):
            return True
        elif value.lower() in ('false', 'no', '0', 'off'):
            return False
        
        # 숫자 값 처리
        try:
            if '.' in value:
                return float(value)
            else:
                return int(value)
        except ValueError:
            pass
        
        # 문자열 그대로 반환
        return value
    
    def _set_nested_value(self, path: list, value: Any):
        """중첩된 딕셔너리에 값 설정"""
        current = self.config_data
        for key in path[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]
        current[path[-1]] = value
    
    def get(self, path: str, default: Any = None) -> Any:
        """설정값 가져오기 (점 표기법 지원)"""
        keys = path.split('.')
        current = self.config_data
        
        try:
            for key in keys:
                current = current[key]
            return current
        except (KeyError, TypeError):
            return default
    
    def set(self, path: str, value: Any):
        """설정값 설정하기 (점 표기법 지원)"""
        keys = path.split('.')
        current = self.config_data
        
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]
        
        current[keys[-1]] = value
    
    def get_printer_config(self) -> Dict[str, Any]:
        """프린터 관련 설정 반환"""
        return self.get('printer', {})
    
    def get_server_config(self) -> Dict[str, Any]:
        """서버 관련 설정 반환"""
        return self.get('server', {})
    
    def get_logging_config(self) -> Dict[str, Any]:
        """로깅 관련 설정 반환"""
        return self.get('logging', {})
    
    def get_system_config(self) -> Dict[str, Any]:
        """시스템 관련 설정 반환"""
        return self.get('system', {})
    
    def is_debug_enabled(self) -> bool:
        """디버그 모드 활성화 여부"""
        return self.get('server.debug', False)
    
    def is_readonly_root_enabled(self) -> bool:
        """읽기 전용 루트 파일시스템 활성화 여부"""
        return self.get('system.storage.enable_readonly_root', False)
    
    def is_watchdog_enabled(self) -> bool:
        """워치독 활성화 여부"""
        return self.get('system.power_management.enable_watchdog', True)
    
    def save_config(self, path: Optional[str] = None):
        """설정을 파일로 저장"""
        save_path = Path(path) if path else self.config_path
        
        try:
            # 디렉토리가 없으면 생성
            save_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(save_path, 'w', encoding='utf-8') as f:
                yaml.dump(self.config_data, f, default_flow_style=False, 
                         allow_unicode=True, sort_keys=False)
            
            self.logger.info(f"설정 파일 저장 완료: {save_path}")
            
        except Exception as e:
            self.logger.error(f"설정 파일 저장 실패: {e}")
            raise

    # ===== 프로젝트 전역에서 사용하는 설정 조작 헬퍼 =====
    def mark_auto_report_supported(self, enabled: bool = True) -> None:
        """오토리포트 지원 여부를 설정 파일에 반영하고 저장"""
        try:
            self.set('data_collection.auto_report', bool(enabled))
            self.save_config()
            try:
                self.logger.info(f"data_collection.auto_report={bool(enabled)} 저장 완료")
            except Exception:
                pass
        except Exception as e:
            self.logger.error(f"오토리포트 설정 저장 실패: {e}")

    def update_equipment_uuid(self, new_uuid: Optional[str]) -> bool:
        """설비 UUID를 갱신하고 변경 시 저장. 변경되었으면 True 반환"""
        try:
            if not new_uuid:
                return False
            old_uuid = self.get('equipment.uuid', None)
            if new_uuid != old_uuid:
                self.set('equipment.uuid', new_uuid)
                self.save_config()
                try:
                    self.logger.info(f"equipment.uuid 갱신: {old_uuid} -> {new_uuid}")
                except Exception:
                    pass
                return True
            return False
        except Exception as e:
            self.logger.error(f"equipment.uuid 저장 실패: {e}")
            return False
    
    def start_watching(self):
        """설정 파일 변경 감지 시작"""
        if not self.config_path.exists():
            return
        
        try:
            self.observer = Observer()
            handler = ConfigFileHandler(self)
            self.observer.schedule(handler, str(self.config_path.parent), recursive=False)
            self.observer.start()
            self.logger.info("설정 파일 감시 시작")
            
        except Exception as e:
            self.logger.error(f"설정 파일 감시 시작 실패: {e}")
    
    def stop_watching(self):
        """설정 파일 변경 감지 중지"""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.logger.info("설정 파일 감시 중지")
    
    def validate_config(self) -> bool:
        """설정 유효성 검사"""
        required_fields = [
            'server.host',
            'server.port'
        ]
        
        printer_config = self.get('printer', {})
        if not printer_config.get('port') and not printer_config.get('auto_detect'):
            self.logger.warning("프린터 포트가 설정되지 않았습니다. auto_detect를 true로 설정하거나 port를 지정하세요.")
        
        for field in required_fields:
            if not self.get(field):
                self.logger.error(f"필수 설정값이 누락되었습니다: {field}")
                return False
        
        return True
    
    def __del__(self):
        """소멸자"""
        self.stop_watching() 