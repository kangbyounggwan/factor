"""
REST API 엔드포인트
Factor 클라이언트 데이터 접근용 API
"""

from flask import Blueprint, request, jsonify, current_app, Response # type: ignore
import logging
import json
import logging
import subprocess
import re
from typing import Dict, Any, List
import os
import io
import zipfile
import gzip

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
            import time as _t
            _t.sleep(0.5)
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


@api_bp.route('/printer/cancel', methods=['POST'])
def cancel_print():
    """인쇄 취소: 큐 비우기 → 파킹 이동 → 쿨다운"""
    try:
        fc = current_app.factor_client
        if not fc or not hasattr(fc, 'printer_comm'):
            return jsonify({'success': False, 'error': 'Factor client not available'}), 503
        pc = fc.printer_comm
        if hasattr(pc, 'control') and pc.control:
            ok = pc.control.cancel_print()
            return jsonify({'success': bool(ok)}) if ok else (jsonify({'success': False}), 500)
        return jsonify({'success': False, 'error': 'control not available'}), 500
    except Exception as e:
        logger.error(f"취소 처리 오류: {e}")
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
@api_bp.route('/printer/tx-window', methods=['GET'])
def get_tx_window():
    """현재 G-code 송신 윈도우(인플라이트/대기열) 스냅샷
    - 사용처: /dashboard 진행률 아래의 송신 로그 패널
    """
    try:
        factor_client = current_app.factor_client
        if not factor_client or not hasattr(factor_client, 'printer_comm'):
            return jsonify({'window_size': 0, 'inflight': [], 'pending_next': []})
        pc = factor_client.printer_comm
        # 1) Async TX 브리지 사용 시 정식 스냅샷
        if hasattr(pc, 'control') and pc.control and getattr(pc, 'tx_bridge', None):
            snap = pc.control.get_tx_window_snapshot()
            return jsonify(snap)
        # 2) Fallback: 동기 경로일 때, 최근 전송 스냅샷(TX_WINDOW_SNAP)을 노출
        try:
            snap = current_app.config.get('TX_WINDOW_SNAP', None)
            if isinstance(snap, dict):
                w = snap.get('window_size', getattr(pc, 'window_size', 15))
                inflight = snap.get('inflight', [])
                pending = snap.get('pending_next', [])
                return jsonify({'window_size': w, 'inflight': inflight[-w:], 'pending_next': pending})
        except Exception:
            pass

        # 3) 최후 대안: 내부 command_queue를 이용해 대기열만 노출
        pending = []
        try:
            q = getattr(pc, 'command_queue', None)
            if q is not None and hasattr(q, 'queue'):
                items = list(q.queue)  # type: ignore[attr-defined]
                for i, line in enumerate(items[:15]):
                    pending.append({'id': i + 1, 'line': str(line)})
        except Exception:
            pass
        return jsonify({'window_size': getattr(pc, 'window_size', 15), 'inflight': [], 'pending_next': pending})
    except Exception as e:
        logger.error(f"송신 윈도우 조회 오류: {e}")
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
            return jsonify(snap)
        return jsonify({'phase': 'unknown', 'since': 0})
    except Exception as e:
        logger.error(f"프린트 단계 조회 오류: {e}")
        return jsonify({'phase': 'unknown', 'since': 0})


# ===== UFP 업로드 프리뷰(자동 인쇄 금지) =====
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

def _find_gcode_in_ufp(zf: zipfile.ZipFile) -> (str, bool):
    gname = None; gz = False
    for name in zf.namelist():
        if name.lower().endswith('.gcode'):
            return name, False
    for name in zf.namelist():
        if name.lower().endswith('.gcode.gz'):
            return name, True
    return None, False

@api_bp.route('/ufp/upload', methods=['POST'])
def upload_ufp_only():
    """UFP/G-code 업로드만 수행(프린트 시작하지 않음) → 업로드 토큰 반환
    프론트는 이 토큰으로 프리뷰 API 호출
    """
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'file field missing'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'no filename'}), 400

        fname = file.filename
        lower = fname.lower()
        allowed = (
            lower.endswith('.ufp') or
            lower.endswith('.gcode') or
            lower.endswith('.gcode.gz')
        )
        if not allowed:
            return jsonify({'success': False, 'error': 'only .ufp, .gcode, .gcode.gz allowed'}), 400

        save_path = os.path.join(UPLOAD_DIR, fname)
        file.save(save_path)
        return jsonify({'success': True, 'token': fname})
    except Exception as e:
        logger.error(f"UFP/G-code 업로드 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ===== SD 카드 업로드/인쇄 API =====
@api_bp.route('/sd/list', methods=['GET'])
def sd_list():
    try:
        fc = current_app.factor_client
        if not fc or not hasattr(fc, 'printer_comm'):
            return jsonify({'success': False, 'error': 'Factor client not available'}), 503
        pc = fc.printer_comm
        raw = pc.sd_list()
        parsed = pc.sd_list_parsed() if hasattr(pc, 'sd_list_parsed') else []
        return jsonify({'success': True, 'raw': raw, 'files': parsed})
    except Exception as e:
        logger.error(f"SD 목록 조회 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/sd/upload', methods=['POST'])
def sd_upload():
    """로컬 업로드 파일을 SD에 저장만 수행(M28/M29)"""
    try:
        fc = current_app.factor_client
        if not fc or not hasattr(fc, 'printer_comm'):
            return jsonify({'success': False, 'error': 'Factor client not available'}), 503
        pc = fc.printer_comm

        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'file field missing'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'no filename'}), 400

        sd_name = request.form.get('sd_name', None)
        if not sd_name:
            # 8.3 호환 기본 이름 생성
            base = os.path.splitext(file.filename)[0]
            base = re.sub(r'[^A-Za-z0-9]', '', base).upper()[:8]
            sd_name = (base or 'JOB') + '.GCO'

        # 파일 스트림을 텍스트 라인 이터레이터로 변환
        stream = io.TextIOWrapper(file.stream, encoding='utf-8', errors='ignore')
        ok = pc.sd_upload_gcode_lines(sd_name, stream)
        return jsonify({'success': bool(ok), 'sd_name': sd_name}) if ok else (jsonify({'success': False, 'error': 'upload failed'}), 500)
    except Exception as e:
        logger.error(f"SD 업로드 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/sd/print', methods=['POST'])
def sd_print():
    """로컬 파일을 SD에 업로드하고 인쇄 시작(M23/M24)"""
    try:
        fc = current_app.factor_client
        if not fc or not hasattr(fc, 'printer_comm'):
            return jsonify({'success': False, 'error': 'Factor client not available'}), 503
        pc = fc.printer_comm

        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'file field missing'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'no filename'}), 400

        sd_name = request.form.get('sd_name', None)
        if not sd_name:
            base = os.path.splitext(file.filename)[0]
            base = re.sub(r'[^A-Za-z0-9]', '', base).upper()[:8]
            sd_name = (base or 'JOB') + '.GCO'

        # 임시 파일로 저장 후 업로드 & 인쇄
        UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        temp_path = os.path.join(UPLOAD_DIR, file.filename)
        file.save(temp_path)
        ok = pc.sd_upload_and_print(sd_name, temp_path, start=True)
        return jsonify({'success': bool(ok), 'sd_name': sd_name}) if ok else (jsonify({'success': False, 'error': 'print failed'}), 500)
    except Exception as e:
        logger.error(f"SD 업로드/인쇄 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/sd/progress', methods=['GET'])
def sd_progress():
    try:
        fc = current_app.factor_client
        if not fc or not hasattr(fc, 'printer_comm'):
            return jsonify({'success': False, 'error': 'Factor client not available'}), 503
        pc = fc.printer_comm
        raw = pc.sd_print_progress()
        # 간단 파싱: byte a/b → 퍼센트
        pct = None
        try:
            m = re.search(r"(\d+)\s*/\s*(\d+)", raw)
            if m:
                a = int(m.group(1)); b = int(m.group(2)); pct = round((a / b * 100.0), 1) if b > 0 else None
        except Exception:
            pass
        return jsonify({'success': True, 'raw': raw, 'percent': pct})
    except Exception as e:
        logger.error(f"SD 진행률 조회 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/sd/init', methods=['POST'])
def sd_init():
    try:
        fc = current_app.factor_client
        if not fc or not hasattr(fc, 'printer_comm'):
            return jsonify({'success': False, 'error': 'Factor client not available'}), 503
        pc = fc.printer_comm
        ok = pc.sd_init()
        return jsonify({'success': bool(ok)}) if ok else (jsonify({'success': False}), 500)
    except Exception as e:
        logger.error(f"SD 초기화 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/sd/release', methods=['POST'])
def sd_release():
    try:
        fc = current_app.factor_client
        if not fc or not hasattr(fc, 'printer_comm'):
            return jsonify({'success': False, 'error': 'Factor client not available'}), 503
        pc = fc.printer_comm
        ok = pc.sd_release()
        return jsonify({'success': bool(ok)}) if ok else (jsonify({'success': False}), 500)
    except Exception as e:
        logger.error(f"SD 언마운트 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@api_bp.route('/ufp/preview/<token>', methods=['GET'])
def preview_ufp(token: str):
    """UFP 내부의 G-code 일부 또는 G-code 파일 일부(앞부분)를 미리보기용으로 반환"""
    try:
        path = os.path.join(UPLOAD_DIR, token)
        if not os.path.exists(path):
            return jsonify({'success': False, 'error': 'token not found'}), 404

        lower = token.lower()
        head_lines: List[str] = []

        # 1) UFP 컨테이너 처리
        if lower.endswith('.ufp'):
            with zipfile.ZipFile(path, 'r') as zf:
                gname, gz = _find_gcode_in_ufp(zf)
                if not gname:
                    return jsonify({'success': False, 'error': 'gcode not found in ufp'}), 400
                f = zf.open(gname, 'r')
                stream = io.TextIOWrapper(
                    gzip.GzipFile(fileobj=f), encoding='utf-8', errors='ignore'
                ) if gz else io.TextIOWrapper(f, encoding='utf-8', errors='ignore')
                for _ in range(200):
                    try:
                        line = next(stream)
                    except StopIteration:
                        break
                    head_lines.append(line.rstrip('\n'))
            return jsonify({'success': True, 'token': token, 'gcode_name': gname, 'gcode_head': head_lines})

        # 2) Raw G-code(.gcode / .gcode.gz) 처리
        if lower.endswith('.gcode.gz'):
            open_stream = lambda p: gzip.open(p, 'rt', encoding='utf-8', errors='ignore')
        else:
            open_stream = lambda p: open(p, 'r', encoding='utf-8', errors='ignore')

        with open_stream(path) as stream:
            for _ in range(200):
                line = stream.readline()
                if not line:
                    break
                head_lines.append(line.rstrip('\n'))

        return jsonify({'success': True, 'token': token, 'gcode_name': token, 'gcode_head': head_lines})
    except Exception as e:
        logger.error(f"UFP/G-code 프리뷰 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


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