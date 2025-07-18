"""
REST API 엔드포인트
Factor 클라이언트 데이터 접근용 API
"""

from flask import Blueprint, request, jsonify, current_app # type: ignore
import logging
import json
import logging
import subprocess
import re
from typing import Dict, Any, List

api_bp = Blueprint('api', __name__)
logger = logging.getLogger('api')


@api_bp.route('/status')
def get_status():
    """전체 상태 정보 반환"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        status_data = {
            'printer_status': factor_client.get_printer_status().to_dict(),
            'temperature_info': factor_client.get_temperature_info().to_dict(),
            'position': factor_client.get_position().to_dict(),
            'progress': factor_client.get_print_progress().to_dict(),
            'system_info': factor_client.get_system_info().to_dict(),
            'connected': factor_client.is_connected(),
            'timestamp': factor_client.last_heartbeat
        }
        
        return jsonify(status_data)
        
    except Exception as e:
        logger.error(f"상태 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/printer/status')
def get_printer_status():
    """프린터 상태 정보"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        status = factor_client.get_printer_status()
        return jsonify(status.to_dict())
        
    except Exception as e:
        logger.error(f"프린터 상태 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/printer/temperature')
def get_temperature():
    """온도 정보"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        temp_info = factor_client.get_temperature_info()
        return jsonify(temp_info.to_dict())
        
    except Exception as e:
        logger.error(f"온도 정보 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/printer/position')
def get_position():
    """위치 정보"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        position = factor_client.get_position()
        return jsonify(position.to_dict())
        
    except Exception as e:
        logger.error(f"위치 정보 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/printer/progress')
def get_progress():
    """프린트 진행률"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        progress = factor_client.get_print_progress()
        return jsonify(progress.to_dict())
        
    except Exception as e:
        logger.error(f"진행률 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/system/info')
def get_system_info():
    """시스템 정보"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        system_info = factor_client.get_system_info()
        return jsonify(system_info.to_dict())
        
    except Exception as e:
        logger.error(f"시스템 정보 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/printer/command', methods=['POST'])
def send_command():
    """G-code 명령 전송"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        data = request.get_json()
        if not data or 'command' not in data:
            return jsonify({'error': 'Missing command parameter'}), 400
        
        command = data['command'].strip()
        if not command:
            return jsonify({'error': 'Empty command'}), 400
        
        success = factor_client.send_gcode(command)
        
        if success:
            return jsonify({'success': True, 'command': command})
        else:
            return jsonify({'error': 'Failed to send command'}), 500
            
    except Exception as e:
        logger.error(f"명령 전송 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/config', methods=['GET'])
def get_config():
    """현재 설정 반환"""
    try:
        config_manager = current_app.config_manager
        if not config_manager:
            return jsonify({'error': 'Config manager not available'}), 503
        
        # 민감한 정보 제외하고 반환
        config_data = config_manager.config_data.copy()
        if 'octoprint' in config_data and 'api_key' in config_data['octoprint']:
            config_data['octoprint']['api_key'] = '***'
        
        return jsonify(config_data)
        
    except Exception as e:
        logger.error(f"설정 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/config', methods=['POST'])
def update_config():
    """설정 업데이트"""
    try:
        config_manager = current_app.config_manager
        if not config_manager:
            return jsonify({'error': 'Config manager not available'}), 503
        
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        # 설정 업데이트
        for section, section_data in data.items():
            if isinstance(section_data, dict):
                for key, value in section_data.items():
                    # 보안상 API 키는 웹에서 변경 불가
                    if section == 'octoprint' and key == 'api_key':
                        continue
                    
                    config_key = f"{section}.{key}"
                    config_manager.set(config_key, value)
            else:
                # 단일 값인 경우
                config_manager.set(section, section_data)
        
        # 설정 파일 저장
        config_manager.save_config()
        
        return jsonify({'success': True, 'message': 'Configuration updated successfully'})
        
    except Exception as e:
        logger.error(f"설정 업데이트 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/health')
def health_check():
    """헬스 체크"""
    try:
        factor_client = current_app.factor_client
        
        health_data = {
            'status': 'healthy',
            'version': '1.0.0',
            'timestamp': factor_client.last_heartbeat if factor_client else 0,
            'connected': factor_client.is_connected() if factor_client else False
        }
        
        return jsonify(health_data)
        
    except Exception as e:
        logger.error(f"헬스 체크 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/logs')
def get_logs():
    """최근 로그 반환"""
    try:
        import os
        from pathlib import Path
        
        log_file = '/var/log/factor-client/factor-client.log'
        if not os.path.exists(log_file):
            return jsonify({'logs': []})
        
        # 최근 100줄 읽기
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            recent_lines = lines[-100:] if len(lines) > 100 else lines
        
        return jsonify({'logs': recent_lines})
        
    except Exception as e:
        logger.error(f"로그 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/printer/type')
def get_printer_type():
    """프린터 타입 정보 반환"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        if hasattr(factor_client, 'printer_comm'):
            type_info = factor_client.printer_comm.get_printer_type_info()
            return jsonify(type_info)
        return jsonify({'error': 'Printer not connected'}), 503
    except Exception as e:
        logger.error(f"프린터 타입 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/printer/capabilities')
def get_printer_capabilities():
    """프린터 기능 정보 반환"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        if hasattr(factor_client, 'printer_comm'):
            capabilities = factor_client.printer_comm.get_printer_capabilities()
            return jsonify(capabilities)
        return jsonify({'error': 'Printer not connected'}), 503
    except Exception as e:
        logger.error(f"프린터 기능 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/printer/extended-data')
def get_extended_data():
    """확장 데이터 수집"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        if hasattr(factor_client, 'printer_comm'):
            extended_data = factor_client.printer_comm.collect_extended_data()
            return jsonify(extended_data)
        return jsonify({'error': 'Printer not connected'}), 503
    except Exception as e:
        logger.error(f"확장 데이터 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.errorhandler(404)
def api_not_found(error):
    """API 404 에러 핸들러"""
    return jsonify({'error': 'API endpoint not found'}), 404


@api_bp.errorhandler(500)
def api_error(error):
    """API 500 에러 핸들러"""
    logger.error(f"API 내부 오류: {error}")
    return jsonify({'error': 'Internal server error'}), 500


# 핫스팟 관련 API
@api_bp.route('/hotspot/info', methods=['GET'])
def get_hotspot_info():
    """핫스팟 정보 조회"""
    try:
        hotspot_manager = getattr(current_app, 'hotspot_manager', None)
        if not hotspot_manager:
            return jsonify({'error': 'Hotspot manager not available'}), 503
        
        return jsonify(hotspot_manager.get_hotspot_info())
        
    except Exception as e:
        logger.error(f"핫스팟 정보 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/hotspot/enable', methods=['POST'])
def enable_hotspot():
    """핫스팟 활성화"""
    try:
        hotspot_manager = getattr(current_app, 'hotspot_manager', None)
        if not hotspot_manager:
            return jsonify({'error': 'Hotspot manager not available'}), 503
        
        success = hotspot_manager.enable_hotspot()
        if success:
            return jsonify({'success': True, 'message': 'Hotspot enabled'})
        else:
            return jsonify({'success': False, 'error': 'Failed to enable hotspot'}), 500
            
    except Exception as e:
        logger.error(f"핫스팟 활성화 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/hotspot/disable', methods=['POST'])
def disable_hotspot():
    """핫스팟 비활성화"""
    try:
        hotspot_manager = getattr(current_app, 'hotspot_manager', None)
        if not hotspot_manager:
            return jsonify({'error': 'Hotspot manager not available'}), 503
        
        success = hotspot_manager.disable_hotspot()
        if success:
            return jsonify({'success': True, 'message': 'Hotspot disabled'})
        else:
            return jsonify({'success': False, 'error': 'Failed to disable hotspot'}), 500
            
    except Exception as e:
        logger.error(f"핫스팟 비활성화 오류: {e}")
        return jsonify({'error': str(e)}), 500


# WiFi 관련 API
@api_bp.route('/wifi/scan', methods=['GET'])
def scan_wifi():
    """WiFi 네트워크 스캔"""
    try:
        networks = _scan_wifi_networks()
        return jsonify({
            'success': True,
            'networks': networks
        })
        
    except Exception as e:
        logger.error(f"WiFi 스캔 오류: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@api_bp.route('/wifi/connect', methods=['POST'])
def connect_wifi():
    """WiFi 연결"""
    try:
        data = request.get_json()
        if not data or 'ssid' not in data:
            return jsonify({'error': 'SSID is required'}), 400
        
        hotspot_manager = getattr(current_app, 'hotspot_manager', None)
        if not hotspot_manager:
            return jsonify({'error': 'Hotspot manager not available'}), 503
        
        success = hotspot_manager.apply_wifi_config(data)
        if success:
            return jsonify({'success': True, 'message': 'WiFi configuration applied'})
        else:
            return jsonify({'success': False, 'error': 'Failed to apply WiFi configuration'}), 500
            
    except Exception as e:
        logger.error(f"WiFi 연결 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/wifi/status', methods=['GET'])
def wifi_status():
    """WiFi 연결 상태 확인"""
    try:
        hotspot_manager = getattr(current_app, 'hotspot_manager', None)
        if not hotspot_manager:
            return jsonify({'error': 'Hotspot manager not available'}), 503
        
        connected = hotspot_manager.check_wifi_connection()
        
        # 현재 연결된 네트워크 정보 가져오기
        current_ssid = None
        if connected:
            try:
                result = subprocess.run(['iwgetid', '-r'], capture_output=True, text=True)
                if result.returncode == 0:
                    current_ssid = result.stdout.strip()
            except:
                pass
        
        return jsonify({
            'connected': connected,
            'ssid': current_ssid,
            'hotspot_active': hotspot_manager.is_hotspot_active
        })
        
    except Exception as e:
        logger.error(f"WiFi 상태 확인 오류: {e}")
        return jsonify({'error': str(e)}), 500


# 설정 완료 API
@api_bp.route('/setup/complete', methods=['POST'])
def complete_setup():
    """초기 설정 완료"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        config_manager = current_app.config_manager
        hotspot_manager = getattr(current_app, 'hotspot_manager', None)
        
        if not config_manager or not hotspot_manager:
            return jsonify({'error': 'Managers not available'}), 503
        
        # 1. WiFi 설정 적용
        if 'wifi' in data:
            success = hotspot_manager.apply_wifi_config(data['wifi'])
            if not success:
                return jsonify({'success': False, 'error': 'WiFi configuration failed'}), 500
        
        # 2. Factor Client 설정 적용
        if 'octoprint' in data:
            for key, value in data['octoprint'].items():
                config_manager.set(f'octoprint.{key}', value)
        
        if 'printer' in data:
            for key, value in data['printer'].items():
                config_manager.set(f'printer.{key}', value)
        
        # 3. 설정 파일 저장
        config_manager.save_config()
        
        # 4. 핫스팟 비활성화 (WiFi 연결 후)
        hotspot_manager.disable_hotspot()
        
        return jsonify({'success': True, 'message': 'Setup completed successfully'})
        
    except Exception as e:
        logger.error(f"설정 완료 오류: {e}")
        return jsonify({'error': str(e)}), 500


def _scan_wifi_networks() -> List[Dict[str, Any]]:
    """WiFi 네트워크 스캔 실행"""
    try:
        # iwlist 명령어로 WiFi 네트워크 스캔
        result = subprocess.run(['sudo', 'iwlist', 'wlan0', 'scan'], 
                              capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0:
            raise Exception("WiFi scan failed")
        
        networks = []
        current_network = {}
        
        for line in result.stdout.split('\n'):
            line = line.strip()
            
            # SSID 추출
            if 'ESSID:' in line:
                match = re.search(r'ESSID:"([^"]*)"', line)
                if match:
                    current_network['ssid'] = match.group(1)
            
            # 신호 강도 추출
            elif 'Signal level=' in line:
                match = re.search(r'Signal level=(-?\d+)', line)
                if match:
                    signal_level = int(match.group(1))
                    # dBm을 퍼센트로 변환 (대략적인 계산)
                    signal_percent = max(0, min(100, 2 * (signal_level + 100)))
                    current_network['signal'] = signal_percent
            
            # 암호화 방식 확인
            elif 'Encryption key:' in line:
                current_network['encrypted'] = 'on' in line
            
            # 새로운 네트워크 시작
            elif 'Cell ' in line and current_network:
                if 'ssid' in current_network and current_network['ssid']:
                    networks.append(current_network)
                current_network = {}
        
        # 마지막 네트워크 추가
        if current_network and 'ssid' in current_network and current_network['ssid']:
            networks.append(current_network)
        
        # SSID로 정렬하고 중복 제거
        unique_networks = {}
        for network in networks:
            ssid = network['ssid']
            if ssid not in unique_networks or network.get('signal', 0) > unique_networks[ssid].get('signal', 0):
                unique_networks[ssid] = network
        
        return sorted(unique_networks.values(), key=lambda x: x.get('signal', 0), reverse=True)
        
    except Exception as e:
        logger.error(f"WiFi 스캔 실행 오류: {e}")
        return []


# 데이터 취득 관련 API
@api_bp.route('/data/start', methods=['POST'])
def start_data_acquisition():
    """데이터 취득 시작"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        # 데이터 취득 설정 저장
        config_manager = current_app.config_manager
        config_manager.set('data_acquisition.enabled', True)
        config_manager.set('data_acquisition.settings', data)
        config_manager.save_config()
        
        return jsonify({'success': True, 'message': 'Data acquisition started'})
        
    except Exception as e:
        logger.error(f"데이터 취득 시작 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/data/stop', methods=['POST'])
def stop_data_acquisition():
    """데이터 취득 중지"""
    try:
        config_manager = current_app.config_manager
        config_manager.set('data_acquisition.enabled', False)
        config_manager.save_config()
        
        return jsonify({'success': True, 'message': 'Data acquisition stopped'})
        
    except Exception as e:
        logger.error(f"데이터 취득 중지 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/data/settings', methods=['GET'])
def get_data_settings():
    """데이터 취득 설정 조회"""
    try:
        config_manager = current_app.config_manager
        settings = config_manager.get('data_acquisition.settings', {})
        enabled = config_manager.get('data_acquisition.enabled', False)
        
        return jsonify({
            'settings': settings,
            'enabled': enabled
        })
        
    except Exception as e:
        logger.error(f"데이터 설정 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/data/settings', methods=['POST'])
def save_data_settings():
    """데이터 취득 설정 저장"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        config_manager = current_app.config_manager
        config_manager.set('data_acquisition.settings', data)
        config_manager.save_config()
        
        return jsonify({'success': True, 'message': 'Data settings saved'})
        
    except Exception as e:
        logger.error(f"데이터 설정 저장 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/data/stats', methods=['GET'])
def get_data_stats():
    """데이터 통계 조회"""
    try:
        # 실제 구현에서는 데이터베이스에서 통계를 가져와야 함
        stats = {
            'total_records': 1234,
            'active_sensors': 5,
            'data_size_mb': 45.2,
            'collection_rate': 12.5
        }
        
        return jsonify(stats)
        
    except Exception as e:
        logger.error(f"데이터 통계 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/data/preview', methods=['GET'])
def get_data_preview():
    """데이터 미리보기"""
    try:
        factor_client = current_app.factor_client
        
        if not factor_client:
            return jsonify({'data': {}})
        
        # 현재 수집 가능한 데이터
        preview_data = {}
        
        if factor_client.is_connected():
            try:
                preview_data['printer_status'] = factor_client.get_printer_status().to_dict()
            except:
                pass
            
            try:
                preview_data['temperature'] = factor_client.get_temperature_info().to_dict()
            except:
                pass
            
            try:
                preview_data['position'] = factor_client.get_position().to_dict()
            except:
                pass
            
            try:
                preview_data['progress'] = factor_client.get_print_progress().to_dict()
            except:
                pass
            
            try:
                preview_data['system_info'] = factor_client.get_system_info().to_dict()
            except:
                pass
        
        return jsonify({'data': preview_data})
        
    except Exception as e:
        logger.error(f"데이터 미리보기 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/data/export', methods=['GET'])
def export_data():
    """데이터 내보내기"""
    try:
        # 실제 구현에서는 데이터베이스에서 데이터를 가져와야 함
        export_data = {
            'timestamp': '2024-01-01T00:00:00Z',
            'records': [
                {'type': 'temperature', 'value': 200, 'timestamp': '2024-01-01T00:00:00Z'},
                {'type': 'position', 'x': 100, 'y': 100, 'z': 0, 'timestamp': '2024-01-01T00:00:01Z'}
            ]
        }
        
        return jsonify({'data': export_data})
        
    except Exception as e:
        logger.error(f"데이터 내보내기 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/data/clear', methods=['POST'])
def clear_data():
    """데이터 초기화"""
    try:
        # 실제 구현에서는 데이터베이스를 초기화해야 함
        return jsonify({'success': True, 'message': 'Data cleared successfully'})
        
    except Exception as e:
        logger.error(f"데이터 초기화 오류: {e}")
        return jsonify({'error': str(e)}), 500 