#!/usr/bin/env python3
"""
Factor OctoPrint Client Firmware
메인 실행 파일 - 라즈베리파이 최적화

Copyright (c) 2024 Factor Client Team
License: MIT
"""

import os
import sys
import time
import argparse
import signal
from pathlib import Path
from typing import Optional

# 프로젝트 루트를 Python 경로에 추가
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from core import ConfigManager, FactorClient, setup_logger
from core.hotspot_manager import HotspotManager
from web import create_app, socketio


class FactorClientFirmware:
    """Factor 클라이언트 펌웨어 메인 클래스"""
    
    def __init__(self, config_path: Optional[str] = None):
        # 기본 설정 경로
        if config_path is None:
            config_path = str(project_root / "config" / "settings.yaml")
        
        # 설정 관리자 초기화
        self.config_manager = ConfigManager(str(config_path))
        
        # 로거 설정
        logging_config = self.config_manager.get_logging_config()
        self.logger = setup_logger(logging_config, 'factor-firmware')
        
        # 시스템 정보 로깅
        # log_system_info(self.logger)  # 함수가 구현되지 않아 주석 처리
        
        # Factor 클라이언트 초기화
        self.factor_client = None
        self.web_app = None
        self.hotspot_manager = None
        self.running = False
        
        # 시그널 핸들러 등록
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        
        self.logger.info("Factor 클라이언트 펌웨어 초기화 완료")
    
    def _signal_handler(self, signum, frame):
        """시그널 핸들러"""
        self.logger.info(f"종료 시그널 수신: {signum}")
        self.stop()
    
    def start(self):
        """펌웨어 시작"""
        try:
            self.logger.info("=== Factor 클라이언트 펌웨어 시작 ===")
            self.running = True
            
            # 설정 유효성 검사
            if not self.config_manager.validate_config():
                self.logger.error("설정이 유효하지 않습니다. 종료합니다.")
                return False
            
            # 핫스팟 관리자 초기화 (Linux에서만)
            import platform
            if platform.system() == "Linux":
                self.hotspot_manager = HotspotManager(self.config_manager)
                # 초기 WiFi 연결 확인 및 핫스팟 관리
                try:
                    self.hotspot_manager.auto_manage_hotspot()
                except Exception as e:
                    self.logger.warning(f"핫스팟 초기화 실패 (계속 진행): {e}")
            else:
                self.logger.info(f"{platform.system()} 환경에서는 핫스팟 기능을 건너뜁니다.")
                self.hotspot_manager = None
            
            # Factor 클라이언트 시작 (연결 실패해도 계속 실행)
            self.factor_client = FactorClient(self.config_manager)
            if not self.factor_client.start():
                self.logger.error("Factor 클라이언트 시작 실패 - 종료합니다")
                return False
            
            # 웹 서버 시작
            self._start_web_server()
            
            # 메인 루프
            self._main_loop()
            
        except Exception as e:
            self.logger.error(f"펌웨어 시작 오류: {e}", exc_info=True)
            return False
        
        return True
    
    def _start_web_server(self):
        """웹 서버 시작"""
        try:
            server_config = self.config_manager.get_server_config()
            
            # Flask 앱 생성
            self.web_app = create_app(self.config_manager, self.factor_client)
            
            # 핫스팟 관리자를 앱에 연결
            self.web_app.hotspot_manager = self.hotspot_manager
            
            # 웹 서버 설정
            host = server_config.get('host', '0.0.0.0')
            port = server_config.get('port', 8080)
            debug = server_config.get('debug', False)
            
            self.logger.info(f"웹 서버 시작: http://{host}:{port}")
            
            # SocketIO 서버 실행 (백그라운드)
            import threading
            def run_server():
                socketio.run(
                    self.web_app,
                    host=host,
                    port=port,
                    debug=debug,
                    use_reloader=False,
                    log_output=False
                )
            
            server_thread = threading.Thread(target=run_server, daemon=True)
            server_thread.start()
            
            # 서버 시작 대기
            time.sleep(2)
            self.logger.info("웹 서버 시작 완료")
            
        except Exception as e:
            self.logger.error(f"웹 서버 시작 오류: {e}")
            raise
    
    def _main_loop(self):
        """메인 루프"""
        self.logger.info("메인 루프 시작")
        
        try:
            while self.running:
                # 상태 체크 - 연결 상태와 관계없이 계속 실행
                if self.factor_client and not self.factor_client.running:
                    self.logger.warning("Factor 클라이언트가 중지됨 - 재시작 시도")
                    # 클라이언트 재시작 시도
                    if not self.factor_client.start():
                        self.logger.error("Factor 클라이언트 재시작 실패")
                        time.sleep(10)  # 10초 대기 후 계속
                        continue
                
                # 주기적 작업
                self._periodic_tasks()
                
                # 1초 대기
                time.sleep(1)
                
        except KeyboardInterrupt:
            self.logger.info("사용자 중단 요청")
        except Exception as e:
            self.logger.error(f"메인 루프 오류: {e}", exc_info=True)
        finally:
            self.stop()
    
    def _periodic_tasks(self):
        """주기적 작업"""
        # 매 60초마다 실행
        if int(time.time()) % 60 == 0:
            try:
                # 설정 파일 다시 로드 체크 (구현 예정)
                # if hasattr(self.config_manager, 'config_changed'):
                #     if self.config_manager.config_changed:
                #         self.logger.info("설정 변경 감지 - 재시작 필요")
                #         # 여기서 재시작 로직을 구현할 수 있음
                
                # 메모리 사용량 체크
                import psutil
                memory_percent = psutil.virtual_memory().percent
                if memory_percent > 85:
                    self.logger.warning(f"메모리 사용량 높음: {memory_percent}%")
                
                # 핫스팟 자동 관리
                if self.hotspot_manager:
                    self.hotspot_manager.auto_manage_hotspot()
                
            except Exception as e:
                self.logger.error(f"주기적 작업 오류: {e}")
    
    def stop(self):
        """펌웨어 중지"""
        if not self.running:
            return
        
        self.logger.info("Factor 클라이언트 펌웨어 중지 중...")
        self.running = False
        
        try:
            # Factor 클라이언트 중지
            if self.factor_client:
                self.factor_client.stop()
            
            # 핫스팟 관리자 정리
            if self.hotspot_manager:
                self.hotspot_manager.disable_hotspot()
            
            # 설정 관리자 정리
            if self.config_manager:
                self.config_manager.stop_watching()
            
            self.logger.info("Factor 클라이언트 펌웨어 중지 완료")
            
        except Exception as e:
            self.logger.error(f"펌웨어 중지 오류: {e}")


def main():
    """메인 함수"""
    parser = argparse.ArgumentParser(description='Factor Client Firmware')
    parser.add_argument('-c', '--config', 
                       help='설정 파일 경로',
                       default=None)
    parser.add_argument('-e', '--environment',
                       choices=['wsl', 'rpi', 'auto'],
                       default='auto',
                       help='실행 환경 선택 (wsl: WSL, rpi: 라즈베리파이, auto: 자동 감지)')
    parser.add_argument('-d', '--daemon',
                       action='store_true',
                       help='데몬 모드로 실행')
    parser.add_argument('--version',
                       action='version',
                       version='Factor Client Firmware 1.0.0')
    
    args = parser.parse_args()
    
    # 환경별 설정 파일 자동 선택
    config_path = args.config
    if config_path is None:
        if args.environment == 'wsl':
            config_path = str(project_root / "config" / "settings_wsl.yaml")
            print(f"WSL 환경 설정 파일 사용: {config_path}")
        elif args.environment == 'rpi':
            config_path = str(project_root / "config" / "settings_rpi.yaml")
            print(f"라즈베리파이 환경 설정 파일 사용: {config_path}")
        elif args.environment == 'auto':
            # 자동 환경 감지
            import platform
            if "Microsoft" in platform.release():
                config_path = str(project_root / "config" / "settings_wsl.yaml")
                print(f"WSL 환경 감지됨 - 설정 파일: {config_path}")
            else:
                config_path = str(project_root / "config" / "settings_rpi.yaml")
                print(f"라즈베리파이 환경 감지됨 - 설정 파일: {config_path}")
    
    # 설정 파일 존재 확인
    if not Path(config_path).exists():
        print(f"오류: 설정 파일을 찾을 수 없습니다: {config_path}")
        print("사용 가능한 설정 파일:")
        print("  - config/settings_wsl.yaml (WSL용)")
        print("  - config/settings_rpi.yaml (라즈베리파이용)")
        print("  - config/settings.yaml (기본)")
        sys.exit(1)
    
    print(f"설정 파일: {config_path}")
    
    # 데몬 모드 처리
    if args.daemon:
        try:
            import daemon  # type: ignore
            import daemon.pidfile  # type: ignore
            
            pid_file = '/var/run/factor-client.pid'
            
            with daemon.DaemonContext(
                pidfile=daemon.pidfile.PIDLockFile(pid_file),
                working_directory='/',
                umask=0o002,
            ):
                firmware = FactorClientFirmware(config_path)
                firmware.start()
                
        except ImportError:
            print("데몬 모드를 위해 python-daemon 패키지가 필요합니다.")
            print("설치: pip install python-daemon")
            sys.exit(1)
        except Exception as e:
            print(f"데몬 시작 오류: {e}")
            sys.exit(1)
    else:
        # 일반 모드
        firmware = FactorClientFirmware(config_path)
        
        try:
            success = firmware.start()
            sys.exit(0 if success else 1)
        except KeyboardInterrupt:
            print("\n사용자 중단")
            firmware.stop()
            sys.exit(0)
        except Exception as e:
            print(f"실행 오류: {e}")
            sys.exit(1)


if __name__ == '__main__':
    main() 