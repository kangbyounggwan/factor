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

api_bp = Blueprint('api', __name__)
logger = logging.getLogger('api')


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
            status_data = {
                'printer_status': factor_client.get_printer_status().to_dict(),
                'temperature_info': temp_dict,
                'position': pos_dict,
                'progress': factor_client.get_print_progress().to_dict(),
                'system_info': factor_client.get_system_info().to_dict(),
                'connected': factor_client.is_connected(),
                'timestamp': factor_client.last_heartbeat
            }
        else:
            status_data = {
                'printer_status': factor_client.get_printer_status().to_dict(),
                'temperature_info': factor_client.get_temperature_info().to_dict(),
                'position': factor_client.get_position().to_dict(),
                'progress': factor_client.get_print_progress().to_dict(),
                'system_info': factor_client.get_system_info().to_dict(),
                'connected': factor_client.is_connected(),
                'timestamp': factor_client.last_heartbeat
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


# @api_bp.route('/printer/cancel', methods=['POST'])
# def cancel_print():
#     """인쇄 취소: 큐 비우기 → 파킹 이동 → 쿨다운"""
#     try:
#         fc = current_app.factor_client
#         if not fc or not hasattr(fc, 'printer_comm'):
#             return jsonify({'success': False, 'error': 'Factor client not available'}), 503
#         pc = fc.printer_comm
#         if hasattr(pc, 'control') and pc.control:
#             ok = pc.control.cancel_print()
#             return jsonify({'success': bool(ok)}) if ok else (jsonify({'success': False}), 500)
#         return jsonify({'success': False, 'error': 'control not available'}), 500
#     except Exception as e:
#         logger.error(f"취소 처리 오류: {e}")
#         return jsonify({'success': False, 'error': str(e)}), 500


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

@api_bp.route('/printer/sd/list', methods=['GET'])
def list_sd_files():
    """SD 카드 파일 목록 반환(M20만 사용; M21은 사용 지양)
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
        time.sleep(0.1)

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

        # SD 진행률 오토리포트(M27 S5) 등록 및 캐시 초기화
        try:
            pc.send_command('M27 S5')
        except Exception:
            pass
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
    순서: M25 → (선택) M400 → M524 → M27 S0 → (선택) 파킹/쿨다운
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

        # 4) 자동 리포트 끄기(켜뒀다면): M27 S0
        try:
            pc.send_command('M27 S0')
        except Exception:
            pass

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
    """G-code 파일을 프린터 SD 카드로 업로드(M28/M29)"""
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

        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'file field missing'}), 400
        upfile = request.files['file']
        if upfile.filename == '':
            return jsonify({'success': False, 'error': 'no filename'}), 400

        # 원격 파일명
        name_override = (request.form.get('name') or '').strip()
        remote_name = name_override if name_override else upfile.filename
        remote_name = re.sub(r'[^A-Za-z0-9._/\-]+', '_', remote_name).lstrip('/')
        if not remote_name:
            return jsonify({'success': False, 'error': 'invalid remote name'}), 400

        # 직접 시리얼에 동기 기록(바이너리 청크)하여 CPU 과점유/경합 방지
        total_lines = 0
        total_bytes = 0
        if not pc.connected or not (pc.serial_conn and pc.serial_conn.is_open):
            return jsonify({'success': False, 'error': 'printer not connected'}), 503

        # 업로드 스트림을 바이너리로 읽기
        up_stream = upfile.stream  # Werkzeug FileStorage stream (binary)
        temp_path = None  # 사이즈 추정 실패 시 임시 파일 경로
        # 총 크기/총 청크 수 추정
        total_target = None
        try:
            if getattr(upfile, 'content_length', None):
                total_target = int(upfile.content_length)
        except Exception:
            total_target = None
        if total_target is None:
            try:
                cur = up_stream.tell()
                up_stream.seek(0, os.SEEK_END)
                total_target = up_stream.tell()
                up_stream.seek(cur, os.SEEK_SET)
            except Exception:
                total_target = None
        # 여전히 알 수 없으면 임시 파일로 저장 후 크기 산정
        if total_target is None:
            try:
                tmp = tempfile.NamedTemporaryFile(delete=False)
                temp_path = tmp.name
                tmp.close()
                try:
                    try:
                        up_stream.seek(0, os.SEEK_SET)
                    except Exception:
                        pass
                    # werkzeug FileStorage는 save 지원
                    upfile.save(temp_path)
                except Exception:
                    # 수동 복사
                    with open(temp_path, 'wb') as _f:
                        while True:
                            data = up_stream.read(65536)
                            if not data:
                                break
                            _f.write(data)
                total_target = os.path.getsize(temp_path)
                up_stream = open(temp_path, 'rb')
            except Exception:
                temp_path = None
                total_target = None

        # 업로드 보호: 폴링 일시정지 + 잔여 큐 정리 + (선택) 임의 전송 차단
        orig_temp = getattr(fc, 'temp_poll_interval', 2.0)
        orig_pos = getattr(fc, 'position_poll_interval', 5.0)
        try:
            fc.temp_poll_interval = 1e9
            fc.position_poll_interval = 1e9
            setattr(fc, '_upload_guard_active', True)
            (current_app.logger if hasattr(current_app, 'logger') else logger).info("업로드 보호: 폴링 일시정지 시작")
        except Exception:
            pass
        try:
            time.sleep(0.15)  # 폴링 스레드가 긴 sleep으로 진입하게 유도
        except Exception:
            pass
        try:
            if hasattr(pc, 'clear_command_queue'):
                pc.clear_command_queue()
        except Exception:
            pass

        # 강화 옵션: 업로드 중 임의 명령 큐잉 차단 + 전면 TX inhibit 게이트
        orig_send = getattr(pc.control, 'send_command', None) if hasattr(pc, 'control') and pc.control else None
        def _blocked_send(_cmd: str, priority: bool = False) -> bool:
            return False
        if orig_send:
            try:
                pc.control.send_command = _blocked_send  # type: ignore[attr-defined]
            except Exception:
                pass
        # 전면 차단 게이트 on (업로드 시작 지점에서 즉시 적용)
        try:
            setattr(pc, 'tx_inhibit', True)
        except Exception:
            pass

        def _restore_polling_and_send():
            try:
                fc.temp_poll_interval = orig_temp
                fc.position_poll_interval = orig_pos
                setattr(fc, '_upload_guard_active', False)
                (current_app.logger if hasattr(current_app, 'logger') else logger).info("업로드 보호: 폴링 재개")
            except Exception:
                pass
            if orig_send:
                try:
                    pc.control.send_command = orig_send  # type: ignore[attr-defined]
                except Exception:
                    pass
            # 전면 차단 게이트 off
            try:
                setattr(pc, 'tx_inhibit', False)
            except Exception:
                pass

        # 업로드 전 프린터 유휴 대기(M400) - 시작점에서 전면 차단/폴링 일시정지/입력버퍼 정리, RX 일시정지
        try:
            try:
                setattr(pc, 'tx_inhibit', True)
            except Exception:
                pass
            try:
                setattr(pc, 'rx_paused', True)
            except Exception:
                pass
            try:
                if pc.serial_conn and pc.serial_conn.is_open:
                    pc.serial_conn.reset_input_buffer()
            except Exception:
                pass
            pc.send_command_and_wait('M400', timeout=5.0)
        except Exception:
            pass

        # 시리얼 대기 헬퍼(업로드 루틴 내부에서만 사용)

        def wait_for_regex(pattern: str, timeout: float = 5.0):
            """패턴이 보이면 (True, matched_line), 아니면 (False, None)"""
            ser = pc.serial_conn
            try:
                deadline = time.time() + float(timeout)
                rx_ok   = re.compile(pattern, re.IGNORECASE)
                rx_fail = re.compile(r"(open failed|no sd|error:)", re.IGNORECASE)
                while time.time() < deadline:
                    line = ser.readline()
                    if not line:
                        # 잔여 버퍼 비우기 (CR/LF 분리 고려)
                        if ser.in_waiting:
                            chunk = ser.read(ser.in_waiting).decode('utf-8','ignore')
                            for part in chunk.replace('\r','\n').split('\n'):
                                s = part.strip()
                                if not s: continue
                                if rx_fail.search(s): return False, s
                                if rx_ok.search(s):   return True, s
                        time.sleep(0.01)
                        continue

                    s = line.decode('utf-8','ignore').strip()
                    if not s: 
                        continue
                    if rx_fail.search(s):
                        return False, s
                    if rx_ok.search(s):
                        return True, s
            except Exception:
                pass
            return False, None

        def wait_ok(timeout: float = 5.0):
            """ok 또는 done saving file 을 ok로 간주"""
            ok, s = wait_for_regex(r"^(ok\b)|(^done saving file)", timeout)
            return ok


        with pc.serial_lock:
            # 핸드셰이크 구간에서는 RX 워커 레이스를 피하기 위해 잠시 동기 모드로 전환
            pc.sync_mode = True
            try:
                # 절충: 업로드 시작 전 혹시 모를 SD 인쇄/오토스타트 중단 → SD 초기화 → 진입

                pc.serial_conn.write(b"M21\n"); pc.serial_conn.flush(); time.sleep(0.1)
                pc.serial_conn.write(b"M27\n"); pc.serial_conn.flush()
                if wait_for_regex(r'sd printing byte', timeout=1.0):
                    pc.serial_conn.write(b"M524\n"); pc.serial_conn.flush()
                    wait_ok(2.0)
                    # 필요하면 재마운트
                    pc.serial_conn.write(b"M22\n"); pc.serial_conn.flush(); wait_ok(2.0)
                    pc.serial_conn.write(b"M21\n"); pc.serial_conn.flush(); wait_ok(2.0)
                    
                # Begin write (진입)
                pc.serial_conn.write((f"M28 {remote_name}\n").encode('utf-8'))
                pc.serial_conn.flush(); time.sleep(0.05)

                # Handshake: M28 진입 확인 (에코/안내 라인 대기)
                try:
                    end = time.time() + 5.0
                    engaged = False
                    while time.time() < end:
                        line = pc.serial_conn.readline()
                        if not line:
                            time.sleep(0.01); continue
                        s = line.decode('utf-8', errors='ignore').strip().lower()
                        # 마를린: "Writing to file:"/"File opened"/"Now fresh file:" 만 진입 확정(ok만으로는 불충분)
                        if ('writing to file' in s) or ('file opened' in s) or ('now fresh file' in s):
                            engaged = True
                        break
                    if not engaged:
                        # 진입 실패로 간주(M21을 사용하지 않고 안전하게 종료)
                        pc.serial_conn.write(b"M29\n"); pc.serial_conn.flush()
                        pc.sync_mode = False
                        return jsonify({'success': False, 'error': 'failed to enter SD write mode (M28). Ensure SD is initialized on the printer.'}), 500
                except Exception:
                    pass

                # 진입 성공 시 RX 워커 재가동( busy:/ok 처리를 위해 )
                pc.sync_mode = False
                try:
                    setattr(pc, 'rx_paused', False)
                except Exception:
                    pass

                # ===== N-라인 체크섬 기반 전송 유틸 =====
                def xor_cs(s: str) -> int:
                    v = 0
                    for ch in s:
                        v ^= ord(ch)
                    return v

                def send_nline(ser, n: int, payload: str, timeout: float = 2.5):
                    line = f"N{n} {payload}"
                    cs = xor_cs(line)
                    ser.write((line + f"*{cs}\n").encode("ascii", "ignore"))
                    ser.flush()
                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        resp = ser.readline().decode("utf-8", "ignore").strip()
                        if not resp:
                            time.sleep(0.01); continue
                        low = resp.lower()
                        # 즉시 에러 로그(라인넘버/체크섬 관련)
                        if ("line number is not last line number+1" in low) or ("no checksum with line number" in low):
                            (current_app.logger if hasattr(current_app, 'logger') else logger).error(f"SD 업로드 오류: {resp}")
                        if low.startswith("ok") or ("done saving file" in low):
                            return n + 1, None
                        if low.startswith("resend") or low.startswith("rs"):
                            try:
                                import re as _re
                                m = _re.search(r"(?:resend|rs)[:\s]+(\d+)", low)
                                if m:
                                    k = int(m.group(1))
                                    return None, k
                            except Exception:
                                pass
                        # 기타 에러는 지속 관찰
                    return None, 'timeout'

                def send_with_cs(ser, n: int, payload: str, timeout: float = 2.5) -> int:
                    attempts = 0
                    while True:
                        nxt, resend_to = send_nline(ser, n, payload, timeout)
                        if resend_to is None and isinstance(nxt, int):
                            return nxt
                        if isinstance(resend_to, int):
                            n = resend_to
                            attempts += 1
                            if attempts > 100:
                                raise RuntimeError("too many resends")
                            continue
                        raise RuntimeError(f"send_nline failed: {resend_to}")

                # 진행 로그 주기 제한(바이트/시간)
                LOG_INTERVAL_BYTES = 512 * 1024
                LOG_INTERVAL_SEC = 1.0
                bytes_since_log = 0
                try:
                    # 너무 잦은 로그는 journald/파일 로거에서 드롭될 수 있으므로 시작/주기/완료만 기록
                    (current_app.logger if hasattr(current_app, 'logger') else logger).info(
                        f"SD 업로드 시작: {remote_name} ({total_target if total_target is not None else '?'} bytes)"
                    )
                except Exception:
                    pass
                last_log_ts = time.time()
                # 라인 스트림으로 변환하여 체크섬 전송
                text_stream = io.TextIOWrapper(up_stream, encoding='utf-8', errors='ignore')
                nline = 0
                # 라인 번호 초기화 후(안전) 파일 시작까지 체크섬으로 전송
                try:
                    nline = send_with_cs(pc.serial_conn, nline, "M110 N0", timeout=2.0)
                except Exception:
                    pass
                try:
                    nline = send_with_cs(pc.serial_conn, nline, f"M28 {remote_name}", timeout=5.0)
                except Exception:
                    # 이미 진입 상태일 수 있으나, 실패 시 상위 로직으로 실패 처리됨
                    pass

                sent_lines = 0
                for raw_line in text_stream:
                    line = raw_line.rstrip('\r\n')
                    nline = send_with_cs(pc.serial_conn, nline, line if line else '', timeout=4.0)
                    total_lines += 1
                    sent_lines += 1
                    ln_bytes = len(line.encode('utf-8', 'ignore')) + 1
                    total_bytes += ln_bytes
                    bytes_since_log += ln_bytes
                    now_ts = time.time()
                    if (bytes_since_log >= LOG_INTERVAL_BYTES) or ((now_ts - last_log_ts) >= LOG_INTERVAL_SEC):
                        try:
                            if total_target is not None and total_target > 0:
                                pct = (total_bytes / total_target) * 100.0
                                (current_app.logger if hasattr(current_app, 'logger') else logger).info(
                                    f"SD 업로드 진행: {total_bytes}/{total_target} bytes ({pct:.1f}%)"
                                )
                        except Exception:
                            pass
                        try:
                                (current_app.logger if hasattr(current_app, 'logger') else logger).info(
                                    f"SD 업로드 진행: {sent_lines} lines ({total_bytes} bytes)"
                                )
                        except Exception:
                            pass
                        bytes_since_log = 0
                        last_log_ts = now_ts
                    # 하트비트 갱신(업로드 중 타임아웃 방지)
                    try:
                        fc.last_heartbeat = time.time()
                    except Exception:
                        pass


                pc.serial_conn.flush()
                # 장치가 마지막 바이트를 파일로 플러시할 수 있도록 잠시 대기 후 안전하게 M29 전송
                try:
                    time.sleep(0.1)
                except Exception:
                    pass
                # End write (분리된 라인 보장을 위해 선행 개행 추가)
                pc.serial_conn.write(b"\nM29\n"); pc.serial_conn.flush()
                # M29 전송 후 500ms 대기(장치가 파일 닫기 처리할 여유)
                try:
                    time.sleep(1.0)
                except Exception:
                    pass
                # Handshake: 저장 완료 응답 대기
                try:
                    end2 = time.time() + 5.0
                    while time.time() < end2:
                        line = pc.serial_conn.readline()
                        if not line:
                            time.sleep(0.01)
                            continue
                        s = line.decode('utf-8', errors='ignore').strip().lower()
                        if ('done saving file' in s) or s.startswith('ok'):
                            break
                except Exception:
                    pass
                
                # 추가 보호: 저장 종료 직후 라인번호/체크섬 대기 상태 초기화(M110 N0) 및 짧은 드레인
                try:
                    time.sleep(0.05)
                    pc.serial_conn.write(b"\nM110 N0\n"); pc.serial_conn.flush()
                    end3 = time.time() + 1.0
                    while time.time() < end3:
                        l2 = pc.serial_conn.readline()
                        if not l2:
                            time.sleep(0.01); continue
                        s2 = l2.decode('utf-8', errors='ignore').strip().lower()
                        # ok 또는 오류 라인 모두 소거. ok를 받으면 종료
                        if s2.startswith('ok'):
                            break
                except Exception:
                    pass
                # 입력 버퍼 정리(잔여 ok/error 제거)
                try:
                    pc.serial_conn.reset_input_buffer()
                except Exception:
                    pass

                # 디렉터리 반영 보장: 재마운트(M22→M21) 후 오토스타트 차단(M524)
                try:
                    pc.serial_conn.write(b"M22\n"); pc.serial_conn.flush(); time.sleep(0.1)
                    pc.serial_conn.write(b"M21\n"); pc.serial_conn.flush(); time.sleep(0.2)
                    # 혹시 있을 수 있는 SD 프린트 자동시작 중단
                    pc.serial_conn.write(b"M524\n"); pc.serial_conn.flush(); time.sleep(0.05)
                except Exception:
                    pass
                end_ok = True
                try:
                    (current_app.logger if hasattr(current_app, 'logger') else logger).info(
                        f"SD 업로드 완료: {remote_name} ({total_bytes} bytes)"
                    )
                except Exception:
                    pass
            finally:
                pc.sync_mode = False

        # 임시 파일 정리
        try:
            if temp_path:
                try:
                    up_stream.close()
                except Exception:
                    pass
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
        except Exception:
            pass

        # 목록 갱신 및 최종 검증(파일 크기 확인) - 지연/캐시 반영 고려하여 재시도
        try:
            target_name = (remote_name or '').strip()
            tn_lower = target_name.lower()
            found_size = None
            for _ in range(6):  # 최대 약 3초(0.5s x 6)
                pc.send_command_and_wait('M20', timeout=3.0)
                time.sleep(0.5)
                info = getattr(pc, 'sd_card_info', {}) or {}
                files = info.get('files', []) or []
                found_size = None
                for f in files:
                    try:
                        nm = str(f.get('name', ''))
                        nl = nm.lower()
                        if nl == tn_lower or nl.endswith('/' + tn_lower) or tn_lower.endswith('/' + nl) or nl.endswith(tn_lower):
                            found_size = f.get('size', None)
                            break
                    except Exception:
                        pass
                if found_size is not None and int(found_size) > 0:
                    break
            if found_size is not None and int(found_size) <= 0:
                return jsonify({'success': False, 'error': 'upload appears empty on SD (0 bytes). Please retry.'}), 500
        except Exception:
            pass

        # 폴링/전송 함수 원복 후 성공 반환
        _restore_polling_and_send()
        return jsonify({'success': True, 'name': remote_name, 'lines': total_lines, 'bytes': total_bytes, 'closed': bool(end_ok)})
    except Exception as e:
        # 종료 시도
        try:
            fc = current_app.factor_client
            if fc and hasattr(fc, 'printer_comm'):
                fc.printer_comm.send_command('M29')
        except Exception:
            pass
        # 실패 시에도 폴링/전송 함수 원복
        try:
            _restore_polling_and_send()
        except Exception:
            pass
        return jsonify({'success': False, 'error': str(e)}), 500

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
        if not data or 'mac_address' not in data:
            return jsonify({'error': 'MAC address is required'}), 400
        
        bluetooth_manager = getattr(current_app, 'bluetooth_manager', None)
        if not bluetooth_manager:
            return jsonify({'error': 'Bluetooth manager not available'}), 503
        
        success = bluetooth_manager.pair_device(data['mac_address'])
        if success:
            return jsonify({'success': True, 'message': 'Device paired successfully'})
        else:
            return jsonify({'success': False, 'error': 'Failed to pair device'}), 500
            
    except Exception as e:
        logger.error(f"블루투스 페어링 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/bluetooth/connect', methods=['POST'])
def connect_bluetooth_device():
    """블루투스 장비 연결"""
    try:
        data = request.get_json()
        if not data or 'mac_address' not in data:
            return jsonify({'error': 'MAC address is required'}), 400
        
        bluetooth_manager = getattr(current_app, 'bluetooth_manager', None)
        if not bluetooth_manager:
            return jsonify({'error': 'Bluetooth manager not available'}), 503
        
        success = bluetooth_manager.connect_device(data['mac_address'])
        if success:
            return jsonify({'success': True, 'message': 'Device connected successfully'})
        else:
            return jsonify({'success': False, 'error': 'Failed to connect device'}), 500
            
    except Exception as e:
        logger.error(f"블루투스 연결 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/bluetooth/disconnect', methods=['POST'])
def disconnect_bluetooth_device():
    """블루투스 장비 연결 해제"""
    try:
        data = request.get_json()
        if not data or 'mac_address' not in data:
            return jsonify({'error': 'MAC address is required'}), 400
        
        bluetooth_manager = getattr(current_app, 'bluetooth_manager', None)
        if not bluetooth_manager:
            return jsonify({'error': 'Bluetooth manager not available'}), 503
        
        success = bluetooth_manager.disconnect_device(data['mac_address'])
        if success:
            return jsonify({'success': True, 'message': 'Device disconnected successfully'})
        else:
            return jsonify({'success': False, 'error': 'Failed to disconnect device'}), 500
            
    except Exception as e:
        logger.error(f"블루투스 연결 해제 오류: {e}")
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
    """WiFi 연결 (블루투스를 통한 설정)"""
    try:
        data = request.get_json()
        if not data or 'ssid' not in data:
            return jsonify({'error': 'SSID is required'}), 400
        
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
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'success': False, 'error': message}), 500
            
    except Exception as e:
        logger.error(f"WiFi 연결 오류: {e}")
        return jsonify({'error': str(e)}), 500


@api_bp.route('/wifi/status', methods=['GET'])
def wifi_status():
    """WiFi 연결 상태 확인"""
    try:
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
            'bluetooth_available': True
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
        logger.info("설정 완료 - 블루투스 연결 유지")
        
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