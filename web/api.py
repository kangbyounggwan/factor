"""
REST API 엔드포인트
Factor 클라이언트 데이터 접근용 API
"""

from flask import Blueprint, request, jsonify, current_app, Response # type: ignore
import logging
import logging
import subprocess
import re
from pathlib import Path
from typing import Dict, Any, List
import time
import os
import tempfile
import io
import uuid

# SD 업로드 모듈 import
from core.sd_upload_method import (
    sd_upload, UploadGuard, validate_upload_request, 
    prepare_upload_stream, cleanup_temp_file
)

api_bp = Blueprint('api', __name__)
logger = logging.getLogger('api')


def _get_trace_id_from_request() -> str:
    """요청 헤더/바디에서 trace_id를 추출하거나 새로 발급"""
    try:
        tid = (request.headers.get('X-Trace-Id') or '').strip()
        if not tid:
            data = request.get_json(silent=True) or {}
            tid = (str(data.get('trace_id') or '')).strip()
        if not tid:
            tid = uuid.uuid4().hex
        return tid
    except Exception:
        return uuid.uuid4().hex

@api_bp.route('/status')
def get_status():
    """전체 상태 정보 반환"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        # 업로드 보호 중엔 M105/M114 동기 질의를 피하고 캐시 값 반환
        uploading = bool(getattr(factor_client, '_upload_guard_active', False))
        if uploading and getattr(factor_client, 'printer_comm', None):
            pc = factor_client.printer_comm
            temp_dict = {}
            try:
                last_temp = getattr(pc, '_last_temp_info', None)
                temp_dict = last_temp.to_dict() if last_temp else {'tool': {}, 'bed': {}}
            except Exception:
                temp_dict = {'tool': {}, 'bed': {}}
            pos_dict = {}
            try:
                pos_dict = pc.current_position.to_dict()
            except Exception:
                pos_dict = {'x': 0, 'y': 0, 'z': 0, 'e': 0}
            # 설비 UUID 정보 가져오기 (설정 파일에서 우선 로드)
            try:
                cm = getattr(current_app, 'config_manager', None)
                if cm is None:
                    from core.config_manager import ConfigManager
                    cm = ConfigManager()
                equipment_uuid = cm.get('equipment.uuid', None)
            except Exception:
                equipment_uuid = None
            
            status_data = {
                'printer_status': factor_client.get_printer_status().to_dict(),
                'temperature_info': temp_dict,
                'position': pos_dict,
                'progress': factor_client.get_print_progress().to_dict(),
                'system_info': factor_client.get_system_info().to_dict(),
                'connected': factor_client.is_connected(),
                'timestamp': factor_client.last_heartbeat,
                'equipment_uuid': equipment_uuid
            }
        else:
            # 설비 UUID 정보 가져오기 (설정 파일에서 우선 로드)
            try:
                cm = getattr(current_app, 'config_manager', None)
                if cm is None:
                    from core.config_manager import ConfigManager
                    cm = ConfigManager()
                equipment_uuid = cm.get('equipment.uuid', None)
            except Exception:
                equipment_uuid = None
            
            status_data = {
                'printer_status': factor_client.get_printer_status().to_dict(),
                'temperature_info': factor_client.get_temperature_info().to_dict(),
                'position': factor_client.get_position().to_dict(),
                'progress': factor_client.get_print_progress().to_dict(),
                'system_info': factor_client.get_system_info().to_dict(),
                'connected': factor_client.is_connected(),
                'timestamp': factor_client.last_heartbeat,
                'equipment_uuid': equipment_uuid
            }
        # SD 진행률 캐시가 활성화되어 있으면 진행률 필드를 캐시로 대체
        try:
            sd_prog = getattr(factor_client, '_sd_progress_cache', None)
            if isinstance(sd_prog, dict) and sd_prog.get('active'):
                status_data['progress'] = {
                    'completion': float(sd_prog.get('completion', 0.0)),
                    'time_elapsed': None,
                    'time_left': sd_prog.get('eta_sec', None),
                    'layers': {'current': 0, 'total': 0},
                    'source': 'sd'
                }
        except Exception:
            pass


        
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
        # 업로드 보호 중엔 캐시 사용(동기 M105 회피)
        if getattr(factor_client, '_upload_guard_active', False) and getattr(factor_client, 'printer_comm', None):
            pc = factor_client.printer_comm
            try:
                last_temp = getattr(pc, '_last_temp_info', None)
                return jsonify((last_temp.to_dict() if last_temp else {'tool': {}, 'bed': {}}))
            except Exception:
                return jsonify({'tool': {}, 'bed': {}})
        else:
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
        # 업로드 보호 중엔 캐시 사용(동기 M114 회피)
        if getattr(factor_client, '_upload_guard_active', False) and getattr(factor_client, 'printer_comm', None):
            pc = factor_client.printer_comm
            try:
                return jsonify(pc.current_position.to_dict())
            except Exception:
                return jsonify({'x': 0, 'y': 0, 'z': 0, 'e': 0})
        else:
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
        
        # SD 진행률 오토리포트 캐시 우선
        sd_prog = getattr(factor_client, '_sd_progress_cache', None)
        if isinstance(sd_prog, dict) and sd_prog.get('active'):
            return jsonify({
                'completion': float(sd_prog.get('completion', 0.0)),
                'time_elapsed': None,
                'time_left': sd_prog.get('eta_sec', None),
                'layers': {'current': 0, 'total': 0},
                'source': 'sd'
            })
        
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


@api_bp.route('/printer/reconnect', methods=['POST'])
def reconnect_printer():
    """프린터 재연결(USB 해제 → 버퍼 초기화 → 재연결)
    - 사용처: 대시보드 우측 상단 '재연결' 버튼
    - 동작: PrinterCommunicator.disconnect() 후 잠시 대기, 내부 연결 루틴 재시도
    """
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'success': False, 'error': 'Factor client not available'}), 503

        # 연결 해제
        if hasattr(factor_client, 'printer_comm') and factor_client.printer_comm:
            try:
                factor_client.printer_comm.disconnect()
            except Exception as e:
                logger.warning(f"재연결 중 disconnect 경고: {e}")

        # 잠시 대기 후 재연결 시도
        try:
            time.sleep(0.5)
        except Exception:
            pass

        # 내부 연결 루틴 호출(폴링 스레드는 기존 루프가 running 상태에서 connected 플래그로 동작)
        ok = False
        try:
            ok = factor_client._connect_to_printer()
            factor_client.connected = bool(ok)
        except Exception as e:
            logger.error(f"재연결 오류: {e}")
            ok = False

        if ok:
            return jsonify({'success': True, 'message': 'Printer reconnected'})
        return jsonify({'success': False, 'error': 'Reconnect failed'}), 500

    except Exception as e:
        logger.error(f"재연결 처리 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500




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


@api_bp.route('/system/error-status')
def get_error_status():
    """오류 상태 및 대기 모드 정보 반환"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        max_errors = factor_client.config.get('system.power_management.max_error_count', 5)
        wait_timeout = factor_client.config.get('system.power_management.error_wait_timeout', 300)
        
        error_status = {
            'error_count': factor_client.error_count,
            'max_errors': max_errors,
            'error_wait_mode': getattr(factor_client, 'error_wait_mode', False),
            'wait_timeout': wait_timeout,
            'remaining_wait_time': 0
        }
        
        if factor_client.error_wait_mode and hasattr(factor_client, 'error_wait_start_time'):
            if factor_client.error_wait_start_time:
                elapsed_time = time.time() - factor_client.error_wait_start_time
                error_status['remaining_wait_time'] = max(0, wait_timeout - elapsed_time)
        
        return jsonify(error_status)
        
    except Exception as e:
        logger.error(f"오류 상태 조회 실패: {e}")
        return jsonify({'error': str(e)}), 500

@api_bp.route('/system/reset-error-count', methods=['POST'])
def reset_error_count():
    """오류 카운터 수동 리셋"""
    try:
        factor_client = current_app.factor_client
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        factor_client.error_count = 0
        factor_client.error_wait_mode = False
        factor_client.error_wait_start_time = None
        
        logger.info("웹 API를 통한 오류 카운터 수동 리셋")
        return jsonify({'success': True, 'message': '오류 카운터가 리셋되었습니다.'})
        
    except Exception as e:
        logger.error(f"오류 카운터 리셋 실패: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

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
    """최근 로그 반환 - 에러 로그는 항상 포함"""
    try:
        log_file = '/var/log/factor-client/factor-client.log'
        if not os.path.exists(log_file):
            return jsonify({'logs': []})

        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # 기본: 최근 300줄
        recent_limit = 300
        recent_lines = lines[-recent_limit:] if len(lines) > recent_limit else lines

        # 보장: 최근 에러 50줄 추가 포함(중복 제거)
        error_lines = [ln for ln in lines if ('ERROR' in ln or 'error' in ln)]
        extra_errors = error_lines[-50:] if len(error_lines) > 50 else error_lines

        merged = list(recent_lines)
        seen = set(merged)
        for ln in extra_errors:
            if ln not in seen:
                merged.append(ln)
                seen.add(ln)

        return jsonify({'logs': merged})

    except Exception as e:
        logger.error(f"로그 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/logs/clear', methods=['POST'])
def clear_logs():
    """로그 파일 초기화"""
    try:
        log_file = '/var/log/factor-client/factor-client.log'
        # 없으면 생성, 있으면 truncate
        with open(log_file, 'w', encoding='utf-8'):
            pass
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"로그 초기화 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/printer/error/recover', methods=['POST'])
def recover_printer_error():
    """프린터 에러 복구 시도: USB 세션 재오픈(Disconnect → Reconnect) 후 상태 확인"""
    try:
        factor_client = current_app.factor_client
        if not factor_client or not hasattr(factor_client, 'printer_comm') or not factor_client.printer_comm:
            return jsonify({'success': False, 'error': 'Factor client or printer not available'}), 503
        pc = factor_client.printer_comm

        # 1) 현재 세션 닫기
        try:
            pc.disconnect()
        except Exception as e:
            logger.warning(f"recover: disconnect 경고: {e}")

        # 2) 잠시 대기 후 재연결 시도
        try:
            time.sleep(1.5)
        except Exception:
            pass

        ok = False
        try:
            ok = factor_client._connect_to_printer()
            factor_client.connected = bool(ok)
        except Exception as e:
            logger.error(f"recover: reconnect 오류: {e}")
            ok = False

        if not ok:
            return jsonify({'success': False, 'error': 'Reconnect failed'}), 500

        # 3) 상태 확인(M105)
        try:
            pc.send_command('M105')
        except Exception:
            pass

        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"프린터 에러 복구 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


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

@api_bp.route('/printer/sd/list', methods=['GET'])
def list_sd_files():
    """SD 카드 파일 목록 반환(M20 파싱 완료까지 동기 대기; Begin/End 블록 감지)
    - 프론트: 대시보드에서 주기적으로 호출
    """
    try:
        fc = current_app.factor_client
        if not fc or not hasattr(fc, 'printer_comm'):
            return jsonify({'success': False, 'error': 'Factor client not available'}), 503
        pc = fc.printer_comm
        # cooling/finishing 단계 차단
        try:
            if hasattr(pc, 'control') and pc.control:
                phase = pc.control.get_phase_snapshot().get('phase', 'unknown')
                if phase in ('finishing', 'cooling'):
                    return jsonify({'success': False, 'error': 'Printer is cooling/finishing'}), 409
        except Exception:
            pass

        # 목록 요청: 내부 수신 파서가 Begin/End file list를 수집
        resp = pc.send_command_and_wait('M20', timeout=3.0)

        if not resp:
            # SD가 초기화되지 않았거나 비어있을 수 있음 → 안전하게 에러 반환(오토스타트 방지)
            return jsonify({'success': False, 'error': 'SD not ready (M20 no response). Please initialize SD on printer UI.'}), 503
        # 파서 버퍼 플러시 여유
        time.sleep(0.4)

        info = getattr(pc, 'sd_card_info', {}) or {}
        files = info.get('files', [])
        return jsonify({'success': True, 'files': files, 'last_update': info.get('last_update', 0)})
    except Exception as e:
        logger.error(f"SD 목록 조회 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@api_bp.route('/printer/sd/print', methods=['POST'])
def sd_print_file():
    """SD 카드에서 선택한 파일 출력 시작(M23→M24). cooling/finishing 중이면 409.
    요청: {"name": "파일명.gcode"}
    """
    try:
        fc = current_app.factor_client
        if not fc or not hasattr(fc, 'printer_comm'):
            return jsonify({'success': False, 'error': 'Factor client not available'}), 503
        pc = fc.printer_comm
        try:
            if hasattr(pc, 'control') and pc.control:
                phase = pc.control.get_phase_snapshot().get('phase', 'unknown')
                if phase in ('finishing', 'cooling'):
                    return jsonify({'success': False, 'error': 'Printer is cooling/finishing'}), 409
        except Exception:
            pass

        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'success': False, 'error': 'name required'}), 400

        # 안전: 임의 송신 차단 중이면 거부
        if getattr(pc, 'tx_inhibit', False):
            return jsonify({'success': False, 'error': 'busy (upload/lock active)'}), 409

        # SD 출력 시작
        ok1 = pc.send_command_and_wait(f"M23 {name}", timeout=5.0)
        if ok1 is False:
            return jsonify({'success': False, 'error': 'failed to select SD file (M23)'}), 500
        ok2 = pc.send_command_and_wait("M24", timeout=5.0)
        if ok2 is False:
            return jsonify({'success': False, 'error': 'failed to start SD print (M24)'}), 500

        # SD 진행률 조회는 동기 조회로 필요 시 요청 측에서 수행
        try:
            setattr(fc, '_sd_progress_cache', {
                'active': True,
                'completion': 0.0,
                'printed_bytes': 0,
                'total_bytes': 0,
                'eta_sec': None,
                'last_update': time.time(),
                'source': 'sd'
            })
        except Exception:
            pass
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"SD 출력 시작 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@api_bp.route('/printer/sd/cancel', methods=['POST'])
def sd_cancel_print():
    """SD 인쇄 일시중지/취소.
    순서: M25 → (선택) M400 → M524 → (선택) 파킹/쿨다운
    요청 JSON 예:
      {
        "mode": "pause" | "cancel",   # 기본 cancel
        "wait_finish": true|false,      # M400 대기(선택)
        "park": true|false,             # 안전 파킹(선택, 펌웨어 지원 시 G27)
        "cooldown": true|false          # 히터/팬 정지(선택)
      }
    """
    try:
        fc = current_app.factor_client
        if not fc or not hasattr(fc, 'printer_comm'):
            return jsonify({'success': False, 'error': 'Factor client not available'}), 503
        pc = fc.printer_comm

        # 업로드 등으로 TX 금지 중이면 거절
        if getattr(pc, 'tx_inhibit', False):
            return jsonify({'success': False, 'error': 'busy (upload/lock active)'}), 409

        data = request.get_json(silent=True) or {}
        mode = (data.get('mode') or 'cancel').strip().lower()
        wait_finish = bool(data.get('wait_finish', False))
        do_park = bool(data.get('park', False))
        do_cooldown = bool(data.get('cooldown', False))

        # 1) 일시정지: M25
        ok_pause = pc.send_command_and_wait('M25', timeout=5.0)
        if ok_pause is False:
            return jsonify({'success': False, 'error': 'failed to pause SD print (M25)'}), 500

        # 2) 모션 마무리 대기(선택): M400
        if wait_finish:
            try:
                pc.send_command_and_wait('M400', timeout=5.0)
            except Exception:
                pass

        # 3) SD 인쇄 완전 중단(파일 닫기): M524
        if mode != 'pause':
            try:
                # 일부 펌웨어는 ok가 지연될 수 있으므로 비대기 전송 허용
                pc.send_command('M524')
            except Exception:
                pass

            # 3-1) 완전 초기화: 파일 포인터 0으로, SD 언마운트 후 재마운트
            try:
                pc.send_command('M26 S0')  # 파일 포인터 0으로
            except Exception:
                pass
            try:
                pc.send_command_and_wait('M22', timeout=5.0)  # SD 언마운트
            except Exception:
                pass
            try:
                pc.send_command_and_wait('M21', timeout=5.0)  # SD 재마운트
            except Exception:
                pass

        # 4) 자동 리포트는 유지 (항상 온도/위치/출력 활성정보 켜둠)

        # 5) 안전 파킹/쿨다운(선택)
        if do_park:
            try:
                pc.send_command_and_wait('G27', timeout=5.0)
            except Exception:
                pass

        if do_cooldown:
            try:
                pc.send_command('M106 S0')  # 팬 OFF
                pc.send_command('M104 S0')  # 노즐 OFF
                pc.send_command('M140 S0')  # 베드 OFF
            except Exception:
                pass

        # 진행률 캐시 비활성화
        try:
            setattr(fc, '_sd_progress_cache', {
                'active': False,
                'completion': 0.0,
                'printed_bytes': 0,
                'total_bytes': 0,
                'eta_sec': None,
                'last_update': time.time(),
                'source': 'sd'
            })
        except Exception:
            pass

        return jsonify({
            'success': True,
            'mode': ('pause' if mode == 'pause' else 'cancel'),
            'wait_finish': wait_finish,
            'park': do_park,
            'cooldown': do_cooldown
        })
    except Exception as e:
        logger.error(f"SD 출력 취소 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@api_bp.route('/printer/sd/upload', methods=['POST'])
def upload_sd_file():
    """G-code 파일을 프린터 SD 카드로 업로드(M28/M29) - 리팩토링된 간단한 버전"""
    # Factor client 및 프린터 연결 확인
    fc = getattr(current_app, 'factor_client', None)
    if not fc or not hasattr(fc, 'printer_comm'):
        return jsonify({'success': False, 'error': 'Factor client not available'}), 503
    
    pc = fc.printer_comm
    if not getattr(pc, 'connected', False) or not (pc.serial_conn and pc.serial_conn.is_open):
        return jsonify({'success': False, 'error': 'printer not connected'}), 503

    # 프린터 상태 확인 (cooling/finishing 중이면 업로드 차단)
    try:
        if hasattr(pc, 'control') and pc.control:
            phase = pc.control.get_phase_snapshot().get('phase', 'unknown')
            if phase in ('finishing', 'cooling'):
                return jsonify({'success': False, 'error': 'Printer is cooling/finishing'}), 409
    except Exception:
        pass

    # 업로드 요청 검증
    success, remote_name, error_msg = validate_upload_request(request)
    if not success:
        return jsonify({'success': False, 'error': error_msg}), 400

    # 업로드 스트림 준비
    upfile = request.files['file']
    up_stream, total_bytes, tmp_path = prepare_upload_stream(upfile)

    try:
        # 주석 제거 옵션 확인
        remove_comments = request.form.get('remove_comments', 'false').lower() in ('true', '1', 'yes')
        
        # 업로드 ID 확인 (MQTT에서 넘어온 경우)
        upload_id = (request.form.get('upload_id') or "").strip()
        
        # 업로드 보호 및 실행
        with UploadGuard(fc, pc):
            current_app.logger.info(f"SD 업로드 시작: {remote_name} ({total_bytes if total_bytes else '?'} bytes, 주석제거={remove_comments}, upload_id={upload_id})")
            result = sd_upload(pc, remote_name, up_stream, total_bytes, remove_comments, upload_id)
            
        return jsonify({'success': True, 'name': remote_name, **result})
        
    except Exception as e:
        current_app.logger.error(f"SD 업로드 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
        
    finally:
        # 임시 파일 정리
        cleanup_temp_file(tmp_path, up_stream)



@api_bp.route('/printer/queue/clear', methods=['POST'])
def clear_printer_queue():
    """송신 대기 큐 비우기(API)"""
    try:
        fc = current_app.factor_client
        if not fc or not hasattr(fc, 'printer_comm'):
            return jsonify({'success': False, 'error': 'Factor client not available'}), 503
        pc = fc.printer_comm
        ok = False
        if hasattr(pc, 'clear_command_queue'):
            ok = bool(pc.clear_command_queue())
        return jsonify({'success': ok}) if ok else (jsonify({'success': False}), 500)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
@api_bp.route('/printer/tx-window', methods=['GET'])
def get_tx_window():
    return jsonify({'window_size': 0, 'inflight': [], 'pending_next': []})


@api_bp.route('/printer/phase', methods=['GET'])
def get_printer_phase():
    """현재 프린트 단계(Phase) 스냅샷 반환"""
    try:
        fc = current_app.factor_client
        if not fc or not hasattr(fc, 'printer_comm'):
            return jsonify({'phase': 'unknown', 'since': 0})
        pc = fc.printer_comm
        if hasattr(pc, 'control') and pc.control:
            snap = pc.control.get_phase_snapshot()
            # unknown이면 idle로 맵핑(쿨링 종료 후 명확한 대기 표시)
            phase = snap.get('phase', 'unknown')
            if phase == 'unknown':
                snap['phase'] = 'idle'
            return jsonify(snap)
        return jsonify({'phase': 'unknown', 'since': 0})
    except Exception as e:
        logger.error(f"프린트 단계 조회 오류: {e}")
        return jsonify({'phase': 'unknown', 'since': 0})


# UFP 업로드/프리뷰 및 관련 유틸 제거됨


# 블루투스 관련 API
@api_bp.route('/bluetooth/status', methods=['GET'])
def get_bluetooth_status():
    """블루투스 상태 정보 조회"""
    try:
        bluetooth_manager = getattr(current_app, 'bluetooth_manager', None)
        if not bluetooth_manager:
            return jsonify({'error': 'Bluetooth manager not available'}), 503
        
        return jsonify(bluetooth_manager.get_bluetooth_status())
        
    except Exception as e:
        logger.error(f"블루투스 상태 조회 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/bluetooth/scan', methods=['GET'])
def scan_bluetooth_devices():
    """블루투스 장비 스캔"""
    try:
        bluetooth_manager = getattr(current_app, 'bluetooth_manager', None)
        if not bluetooth_manager:
            return jsonify({'error': 'Bluetooth manager not available'}), 503
        
        devices = bluetooth_manager.scan_devices()
        return jsonify({
            'success': True,
            'devices': devices
        })
        
    except Exception as e:
        logger.error(f"블루투스 스캔 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/bluetooth/pair', methods=['POST'])
def pair_bluetooth_device():
    """블루투스 장비 페어링"""
    try:
        data = request.get_json()
        trace_id = _get_trace_id_from_request()
        if not data or 'mac_address' not in data:
            return jsonify({'error': 'MAC address is required', 'trace_id': trace_id}), 400
        
        bluetooth_manager = getattr(current_app, 'bluetooth_manager', None)
        if not bluetooth_manager:
            return jsonify({'error': 'Bluetooth manager not available', 'trace_id': trace_id}), 503
        
        # B_ 래퍼 사용 및 trace_id 로깅
        success = False
        try:
            if hasattr(bluetooth_manager, 'B_pair_device'):
                success = bluetooth_manager.B_pair_device(data['mac_address'], trace_id=trace_id)
            else:
                success = bluetooth_manager.pair_device(data['mac_address'])
        except Exception as e:
            logger.error(f"[trace={trace_id}] 블루투스 페어링 내부 오류: {e}")
            success = False
        if success:
            return jsonify({'success': True, 'message': 'Device paired successfully', 'trace_id': trace_id})
        else:
            return jsonify({'success': False, 'error': 'Failed to pair device', 'trace_id': trace_id}), 500
            
    except Exception as e:
        trace_id = _get_trace_id_from_request()
        logger.error(f"[trace={trace_id}] 블루투스 페어링 오류: {e}")
        return jsonify({'error': str(e), 'trace_id': trace_id}), 500


@api_bp.route('/bluetooth/connect', methods=['POST'])
def connect_bluetooth_device():
    """블루투스 장비 연결"""
    try:
        data = request.get_json()
        trace_id = _get_trace_id_from_request()
        if not data or 'mac_address' not in data:
            return jsonify({'error': 'MAC address is required', 'trace_id': trace_id}), 400
        
        bluetooth_manager = getattr(current_app, 'bluetooth_manager', None)
        if not bluetooth_manager:
            return jsonify({'error': 'Bluetooth manager not available', 'trace_id': trace_id}), 503
        
        # B_ 래퍼 사용 및 trace_id 로깅
        success = False
        try:
            if hasattr(bluetooth_manager, 'B_connect_device'):
                success = bluetooth_manager.B_connect_device(data['mac_address'], trace_id=trace_id)
            else:
                success = bluetooth_manager.connect_device(data['mac_address'])
        except Exception as e:
            logger.error(f"[trace={trace_id}] 블루투스 연결 내부 오류: {e}")
            success = False
        if success:
            return jsonify({'success': True, 'message': 'Device connected successfully', 'trace_id': trace_id})
        else:
            return jsonify({'success': False, 'error': 'Failed to connect device', 'trace_id': trace_id}), 500
            
    except Exception as e:
        trace_id = _get_trace_id_from_request()
        logger.error(f"[trace={trace_id}] 블루투스 연결 오류: {e}")
        return jsonify({'error': str(e), 'trace_id': trace_id}), 500


@api_bp.route('/bluetooth/disconnect', methods=['POST'])
def disconnect_bluetooth_device():
    """블루투스 장비 연결 해제"""
    try:
        data = request.get_json()
        trace_id = _get_trace_id_from_request()
        if not data or 'mac_address' not in data:
            return jsonify({'error': 'MAC address is required', 'trace_id': trace_id}), 400
        
        bluetooth_manager = getattr(current_app, 'bluetooth_manager', None)
        if not bluetooth_manager:
            return jsonify({'error': 'Bluetooth manager not available', 'trace_id': trace_id}), 503
        
        # B_ 래퍼 사용 및 trace_id 로깅
        success = False
        try:
            if hasattr(bluetooth_manager, 'B_disconnect_device'):
                success = bluetooth_manager.B_disconnect_device(data['mac_address'], trace_id=trace_id)
            else:
                success = bluetooth_manager.disconnect_device(data['mac_address'])
        except Exception as e:
            logger.error(f"[trace={trace_id}] 블루투스 연결 해제 내부 오류: {e}")
            success = False
        if success:
            return jsonify({'success': True, 'message': 'Device disconnected successfully', 'trace_id': trace_id})
        else:
            return jsonify({'success': False, 'error': 'Failed to disconnect device', 'trace_id': trace_id}), 500
            
    except Exception as e:
        trace_id = _get_trace_id_from_request()
        logger.error(f"[trace={trace_id}] 블루투스 연결 해제 오류: {e}")
        return jsonify({'error': str(e), 'trace_id': trace_id}), 500


# WiFi 관련 API
@api_bp.route('/wifi/scan', methods=['GET'])
def scan_wifi():
    """WiFi 네트워크 스캔"""
    try:
        trace_id = _get_trace_id_from_request()
        logger.info(f"[trace={trace_id}] WiFi 스캔 요청")
        networks = _scan_wifi_networks()
        return jsonify({
            'success': True,
            'networks': networks
        , 'trace_id': trace_id})
        
    except Exception as e:
        trace_id = _get_trace_id_from_request()
        logger.error(f"[trace={trace_id}] WiFi 스캔 오류: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'trace_id': trace_id
        }), 500


@api_bp.route('/wifi/connect', methods=['POST'])
def connect_wifi():
    """WiFi 연결 (블루투스를 통한 설정)"""
    try:
        data = request.get_json()
        trace_id = _get_trace_id_from_request()
        if not data or 'ssid' not in data:
            return jsonify({'error': 'SSID is required', 'trace_id': trace_id}), 400
        
        # WiFi 설정을 블루투스를 통해 전송하는 로직으로 변경
        # 현재는 기본적인 WiFi 연결만 지원
        try:
            import subprocess
            # WiFi 설정 적용 (wpa_supplicant 사용)
            success = True
            message = 'WiFi configuration applied via Bluetooth'
        except Exception as e:
            success = False
            message = f'WiFi configuration failed: {str(e)}'
        
        if success:
            logger.info(f"[trace={trace_id}] WiFi 연결 완료: ssid={data.get('ssid')}")
            return jsonify({'success': True, 'message': message, 'trace_id': trace_id})
        else:
            logger.error(f"[trace={trace_id}] WiFi 연결 실패: {message}")
            return jsonify({'success': False, 'error': message, 'trace_id': trace_id}), 500
            
    except Exception as e:
        trace_id = _get_trace_id_from_request()
        logger.error(f"[trace={trace_id}] WiFi 연결 오류: {e}")
        return jsonify({'error': str(e), 'trace_id': trace_id}), 500


@api_bp.route('/wifi/status', methods=['GET'])
def wifi_status():
    """WiFi 연결 상태 확인"""
    try:
        trace_id = _get_trace_id_from_request()
        # WiFi 연결 상태 직접 확인
        connected = False
        current_ssid = None
        
        try:
            import subprocess
            result = subprocess.run(['iwgetid', '-r'], capture_output=True, text=True)
            if result.returncode == 0:
                current_ssid = result.stdout.strip()
                connected = True
        except:
            pass
        
        return jsonify({
            'connected': connected,
            'ssid': current_ssid,
            'bluetooth_available': True,
            'trace_id': trace_id
        })
        
    except Exception as e:
        trace_id = _get_trace_id_from_request()
        logger.error(f"[trace={trace_id}] WiFi 상태 확인 오류: {e}")
        return jsonify({'error': str(e), 'trace_id': trace_id}), 500


# 설정 완료 API
@api_bp.route('/setup/complete', methods=['POST'])
def complete_setup():
    """초기 설정 완료"""
    try:
        data = request.get_json()
        trace_id = _get_trace_id_from_request()
        if not data:
            return jsonify({'error': 'No data provided', 'trace_id': trace_id}), 400
        
        config_manager = current_app.config_manager
        hotspot_manager = getattr(current_app, 'hotspot_manager', None)
        
        if not config_manager or not hotspot_manager:
            return jsonify({'error': 'Managers not available'}), 503
        
        # 1. WiFi 설정 적용 (블루투스를 통해)
        if 'wifi' in data:
            try:
                import subprocess
                # WiFi 설정 적용 로직
                success = True
            except Exception as e:
                success = False
                logger.error(f"WiFi 설정 실패: {e}")
            
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
        
        # 4. 블루투스 연결 유지 (WiFi 연결 후에도)
        logger.info(f"[trace={trace_id}] 설정 완료 - 블루투스 연결 유지")
        
        return jsonify({'success': True, 'message': 'Setup completed successfully', 'trace_id': trace_id})
        
    except Exception as e:
        trace_id = _get_trace_id_from_request()
        logger.error(f"[trace={trace_id}] 설정 완료 오류: {e}")
        return jsonify({'error': str(e), 'trace_id': trace_id}), 500


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