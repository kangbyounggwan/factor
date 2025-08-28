#!/usr/bin/env python3
"""
핫스팟 기능 테스트 스크립트
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.config_manager import ConfigManager
from core.hotspot_manager import HotspotManager

def test_hotspot():
    """핫스팟 기능 테스트"""
    print("🔍 핫스팟 기능 테스트 시작...")
    
    try:
        # 설정 매니저 초기화
        config_manager = ConfigManager()
        print("✅ 설정 매니저 초기화 완료")
        
        # 핫스팟 매니저 초기화
        hotspot_manager = HotspotManager(config_manager)
        print("✅ 핫스팟 매니저 초기화 완료")
        
        # 현재 설정 출력
        print(f"📶 핫스팟 SSID: {hotspot_manager.hotspot_config['ssid']}")
        print(f"🔑 비밀번호: {hotspot_manager.hotspot_config['password']}")
        print(f"🌐 게이트웨이: {hotspot_manager.hotspot_config['gateway']}")
        
        # WiFi 연결 상태 확인
        wifi_connected = hotspot_manager.check_wifi_connection()
        print(f"📡 WiFi 연결 상태: {'연결됨' if wifi_connected else '연결 안됨'}")
        
        if not wifi_connected:
            print("🔄 WiFi 연결이 없습니다. 핫스팟 모드를 활성화합니다...")
            
            # 핫스팟 활성화 시도
            success = hotspot_manager.enable_hotspot()
            if success:
                print("✅ 핫스팟이 성공적으로 활성화되었습니다!")
                print(f"📱 이제 '{hotspot_manager.hotspot_config['ssid']}' 네트워크에 연결하세요")
                print(f"🌐 설정 페이지: http://{hotspot_manager.hotspot_config['gateway']}:8080/setup")
            else:
                print("❌ 핫스팟 활성화에 실패했습니다.")
        else:
            print("✅ WiFi가 이미 연결되어 있습니다.")
            
    except Exception as e:
        print(f"❌ 테스트 중 오류 발생: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_hotspot()
