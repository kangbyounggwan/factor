"""
Flask 웹 애플리케이션
라즈베리파이 최적화된 웹 인터페이스
"""

from flask import Flask, render_template, request, jsonify, Response # type: ignore
from flask_socketio import SocketIO
import logging
import time
from pathlib import Path
import os
import io
import zipfile
import gzip
import threading
import re
from werkzeug.utils import secure_filename

from core import ConfigManager
from core.hotspot_manager import HotspotManager
from .api import api_bp
from .socketio_handler import socketio
# 업로드 디렉토리 준비
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)



def create_app(config_manager: ConfigManager, factor_client=None):
    """Flask 애플리케이션 생성"""
    
    app = Flask(__name__)
    
    # 설정
    server_config = config_manager.get_server_config()
    app.config['SECRET_KEY'] = 'factor-client-secret-key'
    app.config['DEBUG'] = server_config.get('debug', False)
    
    # 로거 설정
    logger = logging.getLogger('web-server')
    
    # SocketIO 초기화
    socketio.init_app(app, cors_allowed_origins="*")
    
    # 블루프린트 등록
    app.register_blueprint(api_bp, url_prefix='/api')
    
    # Factor 클라이언트 참조 설정
    app.factor_client = factor_client
    app.config_manager = config_manager
    
    # 진행률/핫스팟 관리자 초기화
    app.hotspot_manager = HotspotManager(config_manager)
    app.config.setdefault('PRINT_PROGRESS', {
        'completion': 0.0,
        'time_elapsed': 0,
        'time_left': 0,
        'layers': {'current': 0, 'total': 0},
        'timestamp': time.time()
    })
    # 동기 모드에서도 전송창에 보여줄 최근 전송 라인 스냅샷(간이)
    app.config.setdefault('TX_WINDOW_SNAP', {
        'window_size': 15,
        'inflight': [],
        'pending_next': [],
        'timestamp': time.time()
    })
    
    # 정적 파일 경로 설정
    static_folder = Path(__file__).parent / 'static'
    template_folder = Path(__file__).parent / 'templates'
    
    @app.route('/')
    def index():
        """메인 페이지"""
        return render_template('index.html')
    
    @app.route('/dashboard')
    def dashboard():
        """대시보드 페이지"""
        return render_template('dashboard.html')
    
    @app.route('/settings')
    def settings():
        """설정 페이지"""
        return render_template('settings.html')
    
    @app.route('/data')
    def data_acquisition():
        """데이터 취득 페이지"""
        return render_template('data_acquisition.html')
    
    @app.route('/data/logs')
    def data_logs():
        """데이터 로그 페이지"""
        return render_template('data_logs.html')
    
    @app.route('/setup')
    def setup():
        """초기 설정 페이지"""
        return render_template('setup.html')
    
    @app.route('/status')
    def status():
        """상태 정보 API"""
        if not factor_client:
            return jsonify({'error': 'Factor client not available'}), 503
        
        try:
            # 프린터 연결 상태 확인
            is_connected = factor_client.is_connected()
            
            if is_connected:
                status_data = {
                    'printer_status': factor_client.get_printer_status().to_dict(),
                    'temperature_info': factor_client.get_temperature_info().to_dict(),
                    'position': factor_client.get_position().to_dict(),
                    'progress': factor_client.get_print_progress().to_dict(),
                    'system_info': factor_client.get_system_info().to_dict(),
                    'connected': True,
                    'timestamp': factor_client.last_heartbeat
                }
                # 스트리머가 기록한 진행률 병합(간이 진행률)
                try:
                    pp = app.config.get('PRINT_PROGRESS')
                    if pp:
                        status_data['progress'].update(pp)
                except Exception:
                    pass
            else:
                # 프린터가 연결되지 않은 경우 기본 상태 반환
                status_data = {
                    'printer_status': {'state': 'disconnected', 'message': '프린터 연결 대기 중'},
                    'temperature_info': {'tool': {}, 'bed': {}},
                    'position': {'x': 0, 'y': 0, 'z': 0, 'e': 0},
                    'progress': {'completion': 0, 'print_time': 0},
                    'system_info': factor_client.get_system_info().to_dict(),
                    'connected': False,
                    'timestamp': time.time(),
                    'message': 'USB 포트에 프린터 연결을 기다리는 중입니다'
                }
            
            return jsonify(status_data)
        except Exception as e:
            logger.error(f"상태 정보 조회 오류: {e}")
            return jsonify({'error': str(e)}), 500

    # ===== UFP/G-code 업로드 & 인쇄 =====
    def _is_allowed(filename: str) -> bool:
        low = filename.lower()
        return low.endswith('.ufp') or low.endswith('.gcode') or low.endswith('.gcode.gz')

    def _count_effective_lines(zf: zipfile.ZipFile, gcode_name: str, gz: bool) -> int:
        cnt = 0
        f = zf.open(gcode_name, 'r')
        stream = io.TextIOWrapper(gzip.GzipFile(fileobj=f), encoding='utf-8', errors='ignore') if gz else io.TextIOWrapper(f, encoding='utf-8', errors='ignore')
        for raw in stream:
            line = raw.strip()
            if not line or line.startswith(';'):
                continue
            if ';' in line:
                line = line.split(';', 1)[0].strip()
                if not line:
                    continue
            cnt += 1
        return cnt

    def _should_wait(line: str, sent_idx: int) -> bool:
        # 블로킹이 필요한 명령 위주 + 주기적으로 한 번 대기
        if re.match(r"^(M109|M190|M400|G4|G28|G29|M112)\b", line, re.IGNORECASE):
            return True
        # 100줄마다 한 번은 ok 대기 (버퍼 보호)
        if sent_idx > 0 and (sent_idx % 100 == 0):
            return True
        return False

    def _count_effective_lines_raw(path: str, gz: bool) -> int:
        cnt = 0
        open_fn = (lambda p: gzip.open(p, 'rt', encoding='utf-8', errors='ignore')) if gz else (lambda p: open(p, 'r', encoding='utf-8', errors='ignore'))
        with open_fn(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith(';'):
                    continue
                if ';' in line:
                    line = line.split(';', 1)[0].strip()
                    if not line:
                        continue
                cnt += 1
        return cnt

    def _push_tx_line(line: str):
        """대시보드 전송창 표시용 최근 전송 라인 버퍼 갱신(동기/비동기 공통)
        - inflight에 최근 전송 N줄을 보관한다. pending_next는 동기 모드에서는 비움.
        """
        try:
            snap = app.config.get('TX_WINDOW_SNAP', {})
            inflight = snap.get('inflight', [])
            inflight.append({'id': int(time.time() * 1000) % 1000000, 'line': line})
            # 최대 window_size*2 줄만 유지
            w = int(snap.get('window_size', 15))
            if len(inflight) > (w * 2):
                inflight[:] = inflight[-(w * 2):]
            snap['inflight'] = inflight
            snap['pending_next'] = []
            snap['timestamp'] = time.time()
            app.config['TX_WINDOW_SNAP'] = snap
        except Exception:
            pass

    def _stream_ufp_gcode(printer_comm, ufp_path: str, wait_ok: bool = True, send_delay: float = 0.0) -> int:
        """UFP(zip) 내부의 .gcode/.gcode.gz를 찾아 메모리 스트림으로 라인 단위 전송"""
        sent = 0
        with zipfile.ZipFile(ufp_path, 'r') as zf:
            gcode_name = None
            gz = False
            for name in zf.namelist():
                if name.lower().endswith('.gcode'):
                    gcode_name = name; gz = False; break
            if not gcode_name:
                for name in zf.namelist():
                    if name.lower().endswith('.gcode.gz'):
                        gcode_name = name; gz = True; break
            if not gcode_name:
                raise FileNotFoundError('UFP 내부에 .gcode/.gcode.gz 를 찾을 수 없습니다.')

            # 총 라인 수 계산(진행률 표시용)
            try:
                total = _count_effective_lines(zf, gcode_name, gz)
            except Exception:
                total = 0

            f = zf.open(gcode_name, 'r')
            stream = io.TextIOWrapper(gzip.GzipFile(fileobj=f), encoding='utf-8', errors='ignore') if gz else io.TextIOWrapper(f, encoding='utf-8', errors='ignore')

            # 폴링 간섭 최소화: 인쇄 동안 폴링 간격 임시 완화
            prev_temp = None; prev_pos = None
            try:
                if hasattr(app.factor_client, 'temp_poll_interval'):
                    prev_temp = app.factor_client.temp_poll_interval
                    app.factor_client.temp_poll_interval = max(prev_temp, 30.0)
                if hasattr(app.factor_client, 'position_poll_interval'):
                    prev_pos = app.factor_client.position_poll_interval
                    app.factor_client.position_poll_interval = max(prev_pos, 30.0)
            except Exception:
                pass

            for raw in stream:
                line = raw.strip()
                if not line or line.startswith(';'):
                    continue
                if ';' in line:
                    line = line.split(';', 1)[0].strip()
                    if not line:
                        continue
                # 로그: 너무 잦은 I/O 방지 위해 50줄마다 또는 M73만 기록
                if sent % 50 == 0 or line.upper().startswith('M73'):
                    try:
                        logging.getLogger('factor-firmware').info(f"[PRINT] {line}")
                    except Exception:
                        app.logger.info(f"[PRINT] {line}")

                # 전송: 중요 명령/주기적 구간만 ok 대기
                must_wait = _should_wait(line, sent)
                ok = printer_comm.send_gcode(line, wait=(wait_ok or must_wait))
                if not ok:
                    try:
                        logging.getLogger('factor-firmware').warning(f"[PRINT] 전송 실패: {line}")
                    except Exception:
                        app.logger.warning(f"[PRINT] 전송 실패: {line}")
                # 대시보드 전송창(동기 모드용 간이 표시) 갱신
                _push_tx_line(line)
                sent += 1
                # 진행률 갱신
                try:
                    # 우선 순위 1) M73 사용
                    m = re.match(r"\s*M73\s+(.*)", line, re.IGNORECASE)
                    if m:
                        params = m.group(1)
                        p = re.search(r"P\s*(\d+(?:\.\d+)?)", params, re.IGNORECASE)
                        r = re.search(r"R\s*(\d+)", params, re.IGNORECASE)
                        completion = float(p.group(1)) if p else None
                        time_left = int(r.group(1)) * 60 if r else None
                        if completion is not None or time_left is not None:
                            current = app.config['PRINT_PROGRESS'].get('layers', {}).get('current', sent)
                            app.config['PRINT_PROGRESS'] = {
                                'completion': round(completion if completion is not None else 0.0, 1),
                                'time_elapsed': app.config['PRINT_PROGRESS'].get('time_elapsed', 0),
                                'time_left': time_left if time_left is not None else app.config['PRINT_PROGRESS'].get('time_left', 0),
                                'layers': {'current': current, 'total': total},
                                'timestamp': time.time()
                            }
                        else:
                            # M73가 있지만 파라미터가 없으면 라인 기반으로 보완
                            completion = (sent / total * 100.0) if total > 0 else 0.0
                            app.config['PRINT_PROGRESS'] = {
                                'completion': round(completion, 1),
                                'time_elapsed': app.config['PRINT_PROGRESS'].get('time_elapsed', 0),
                                'time_left': app.config['PRINT_PROGRESS'].get('time_left', 0),
                                'layers': {'current': sent, 'total': total},
                                'timestamp': time.time()
                            }
                    else:
                        # 우선 순위 2) 라인 기반 진행률
                        completion = (sent / total * 100.0) if total > 0 else 0.0
                        app.config['PRINT_PROGRESS'] = {
                            'completion': round(completion, 1),
                            'time_elapsed': app.config['PRINT_PROGRESS'].get('time_elapsed', 0),
                            'time_left': app.config['PRINT_PROGRESS'].get('time_left', 0),
                            'layers': {'current': sent, 'total': total},
                            'timestamp': time.time()
                        }
                except Exception:
                    pass
                if send_delay > 0:
                    import time as _t; _t.sleep(send_delay)
            # 폴링 간격 복원
            try:
                if prev_temp is not None:
                    app.factor_client.temp_poll_interval = prev_temp
                if prev_pos is not None:
                    app.factor_client.position_poll_interval = prev_pos
            except Exception:
                pass
        return sent

    def _stream_gcode_file(printer_comm, file_path: str, wait_ok: bool = True, send_delay: float = 0.0) -> int:
        """로컬 .gcode / .gcode.gz 파일을 라인 단위로 전송"""
        sent = 0
        gz = file_path.lower().endswith('.gcode.gz')
        # 총 라인 수 계산(진행률 표시용)
        try:
            total = _count_effective_lines_raw(file_path, gz)
        except Exception:
            total = 0

        open_fn = (lambda p: gzip.open(p, 'rt', encoding='utf-8', errors='ignore')) if gz else (lambda p: open(p, 'r', encoding='utf-8', errors='ignore'))

        # 폴링 간섭 최소화: 인쇄 동안 폴링 간격 임시 완화
        prev_temp = None; prev_pos = None
        try:
            if hasattr(app.factor_client, 'temp_poll_interval'):
                prev_temp = app.factor_client.temp_poll_interval
                app.factor_client.temp_poll_interval = max(prev_temp, 30.0)
            if hasattr(app.factor_client, 'position_poll_interval'):
                prev_pos = app.factor_client.position_poll_interval
                app.factor_client.position_poll_interval = max(prev_pos, 30.0)
        except Exception:
            pass

        try:
            with open_fn(file_path) as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith(';'):
                        continue
                    if ';' in line:
                        line = line.split(';', 1)[0].strip()
                        if not line:
                            continue

                    # 로그: 너무 잦은 I/O 방지 위해 50줄마다 또는 M73만 기록
                    if sent % 50 == 0 or line.upper().startswith('M73'):
                        try:
                            logging.getLogger('factor-firmware').info(f"[PRINT] {line}")
                        except Exception:
                            app.logger.info(f"[PRINT] {line}")

                    must_wait = _should_wait(line, sent)
                    ok = printer_comm.send_gcode(line, wait=(wait_ok or must_wait))
                    if not ok:
                        try:
                            logging.getLogger('factor-firmware').warning(f"[PRINT] 전송 실패: {line}")
                        except Exception:
                            app.logger.warning(f"[PRINT] 전송 실패: {line}")
                    # 대시보드 전송창(동기 모드용 간이 표시) 갱신
                    _push_tx_line(line)
                    sent += 1

                    # 진행률 갱신
                    try:
                        m = re.match(r"\s*M73\s+(.*)", line, re.IGNORECASE)
                        if m:
                            params = m.group(1)
                            p = re.search(r"P\s*(\d+(?:\.\d+)?)", params, re.IGNORECASE)
                            r = re.search(r"R\s*(\d+)", params, re.IGNORECASE)
                            completion = float(p.group(1)) if p else None
                            time_left = int(r.group(1)) * 60 if r else None
                            if completion is not None or time_left is not None:
                                current = app.config['PRINT_PROGRESS'].get('layers', {}).get('current', sent)
                                app.config['PRINT_PROGRESS'] = {
                                    'completion': round(completion if completion is not None else 0.0, 1),
                                    'time_elapsed': app.config['PRINT_PROGRESS'].get('time_elapsed', 0),
                                    'time_left': time_left if time_left is not None else app.config['PRINT_PROGRESS'].get('time_left', 0),
                                    'layers': {'current': current, 'total': total},
                                    'timestamp': time.time()
                                }
                            else:
                                completion = (sent / total * 100.0) if total > 0 else 0.0
                                app.config['PRINT_PROGRESS'] = {
                                    'completion': round(completion, 1),
                                    'time_elapsed': app.config['PRINT_PROGRESS'].get('time_elapsed', 0),
                                    'time_left': app.config['PRINT_PROGRESS'].get('time_left', 0),
                                    'layers': {'current': sent, 'total': total},
                                    'timestamp': time.time()
                                }
                        else:
                            completion = (sent / total * 100.0) if total > 0 else 0.0
                            app.config['PRINT_PROGRESS'] = {
                                'completion': round(completion, 1),
                                'time_elapsed': app.config['PRINT_PROGRESS'].get('time_elapsed', 0),
                                'time_left': app.config['PRINT_PROGRESS'].get('time_left', 0),
                                'layers': {'current': sent, 'total': total},
                                'timestamp': time.time()
                            }
                    except Exception:
                        pass

                    if send_delay > 0:
                        import time as _t; _t.sleep(send_delay)
        finally:
            # 폴링 간격 복원
            try:
                if prev_temp is not None:
                    app.factor_client.temp_poll_interval = prev_temp
                if prev_pos is not None:
                    app.factor_client.position_poll_interval = prev_pos
            except Exception:
                pass

        return sent

    def _start_print_job(file_path: str):
        try:
            if not factor_client or not factor_client.is_connected():
                app.logger.error('프린터 미연결 상태입니다.')
                return
            pc = factor_client.printer_comm
            # 새 작업 시작: 전송창 스냅샷 초기화
            try:
                app.config['TX_WINDOW_SNAP'] = {
                    'window_size': 15,
                    'inflight': [],
                    'pending_next': [],
                    'timestamp': time.time()
                }
            except Exception:
                pass
            low = file_path.lower()
            if low.endswith('.ufp'):
                total = _stream_ufp_gcode(pc, file_path, wait_ok=False, send_delay=0.0)
                app.logger.info(f'UFP 인쇄 전송 완료: {total} lines')
            elif low.endswith('.gcode') or low.endswith('.gcode.gz'):
                total = _stream_gcode_file(pc, file_path, wait_ok=False, send_delay=0.0)
                app.logger.info(f'G-code 인쇄 전송 완료: {total} lines')
            else:
                app.logger.error('지원하지 않는 파일 형식입니다.')
        except Exception as e:
            app.logger.error(f"인쇄 중 오류: {e}")

    @app.route('/ufp', methods=['GET', 'POST'])
    def upload_ufp():
        if request.method == 'POST':
            if 'file' not in request.files:
                return Response('<p>파일 필드가 없습니다.</p><a href="/ufp">뒤로</a>', mimetype='text/html')
            file = request.files['file']
            if file.filename == '':
                return Response('<p>파일이 선택되지 않았습니다.</p><a href="/ufp">뒤로</a>', mimetype='text/html')
            if not _is_allowed(file.filename):
                return Response('<p>허용되지 않은 파일 형식입니다(.ufp, .gcode, .gcode.gz)</p><a href="/ufp">뒤로</a>', mimetype='text/html', status=400)

            filename = secure_filename(file.filename)
            save_path = os.path.join(UPLOAD_FOLDER, filename)
            file.save(save_path)

            t = threading.Thread(target=_start_print_job, args=(save_path,), daemon=True)
            t.start()

            return Response(f'''<h3>업로드 완료: {filename}</h3>
                <p>인쇄 전송을 시작했습니다. 로그에서 진행 상황을 확인하세요.</p>
                <pre>sudo journalctl -u factor-client.service -f</pre>
                <a href="/dashboard">대시보드로 돌아가기</a>''', mimetype='text/html')

        return Response('''<html><body style="font-family: sans-serif; margin: 24px;">
            <h2>UFP/G-code 업로드 및 인쇄</h2>
            <form action="/ufp" method="post" enctype="multipart/form-data">
              <input type="file" name="file" accept=".ufp,.gcode,.gcode.gz" />
              <button type="submit">업로드 & 인쇄 시작</button>
            </form>
            <p><a href="/dashboard">대시보드로 돌아가기</a></p>
            </body></html>''', mimetype='text/html')
    
    @app.route('/health')
    def health():
        """헬스 체크"""
        return jsonify({
            'status': 'healthy',
            'version': '1.0.0',
            'timestamp': factor_client.last_heartbeat if factor_client else 0
        })
    
    @app.errorhandler(404)
    def not_found(error):
        """404 에러 핸들러"""
        return render_template('error.html', error='페이지를 찾을 수 없습니다'), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        """500 에러 핸들러"""
        logger.error(f"내부 서버 오류: {error}")
        return render_template('error.html', error='내부 서버 오류가 발생했습니다'), 500
    
    # SocketIO 이벤트 핸들러 설정
    if factor_client:
        setup_socketio_handlers(socketio, factor_client)
    
    logger.info("Flask 애플리케이션 생성 완료")
    return app


def setup_socketio_handlers(socketio, factor_client):
    """SocketIO 이벤트 핸들러 설정"""
    
    @socketio.on('connect')
    def handle_connect():
        """클라이언트 연결"""
        logger = logging.getLogger('socketio')
        logger.info(f"클라이언트 연결: {request.sid}")
        
        # 초기 데이터 전송
        if factor_client.is_connected():
            socketio.emit('printer_status', factor_client.get_printer_status().to_dict())
            socketio.emit('temperature_update', factor_client.get_temperature_info().to_dict())
            socketio.emit('system_info', factor_client.get_system_info().to_dict())
    
    @socketio.on('disconnect')
    def handle_disconnect():
        """클라이언트 연결 해제"""
        logger = logging.getLogger('socketio')
        logger.info(f"클라이언트 연결 해제: {request.sid}")
    
    @socketio.on('send_gcode')
    def handle_gcode(data):
        """G-code 명령 전송"""
        logger = logging.getLogger('socketio')
        command = data.get('command', '').strip()
        
        if not command:
            socketio.emit('gcode_response', {'error': '명령이 비어있습니다'})
            return
        
        logger.info(f"G-code 명령 수신: {command}")
        
        if factor_client.send_gcode(command):
            socketio.emit('gcode_response', {'success': True, 'command': command})
        else:
            socketio.emit('gcode_response', {'error': '명령 전송 실패'})
    
    @socketio.on('get_status')
    def handle_get_status():
        """상태 정보 요청"""
        if factor_client.is_connected():
            socketio.emit('printer_status', factor_client.get_printer_status().to_dict())
            socketio.emit('temperature_update', factor_client.get_temperature_info().to_dict())
            socketio.emit('progress_update', factor_client.get_print_progress().to_dict())
            socketio.emit('system_info', factor_client.get_system_info().to_dict())
        else:
            # 프린터가 연결되지 않은 경우 연결 대기 상태 메시지 전송
            socketio.emit('connection_status', {
                'connected': False,
                'message': 'USB 포트에 프린터 연결을 기다리는 중입니다',
                'timestamp': time.time()
            })
    
    # Factor 클라이언트 콜백 등록
    def on_printer_state_change(status):
        socketio.emit('printer_status', status.to_dict())
    
    def on_temperature_update(temp_info):
        socketio.emit('temperature_update', temp_info.to_dict())
    
    def on_progress_update(progress):
        socketio.emit('progress_update', progress.to_dict())
    
    def on_position_update(position):
        socketio.emit('position_update', position.to_dict())
    
    def on_connect():
        socketio.emit('octoprint_connected', {'message': 'OctoPrint 연결됨'})
    
    def on_disconnect():
        socketio.emit('octoprint_disconnected', {'message': 'OctoPrint 연결 해제됨'})
    
    # 콜백 등록
    factor_client.add_callback('on_printer_state_change', on_printer_state_change)
    factor_client.add_callback('on_temperature_update', on_temperature_update)
    factor_client.add_callback('on_progress_update', on_progress_update)
    factor_client.add_callback('on_position_update', on_position_update)
    factor_client.add_callback('on_connect', on_connect)
    factor_client.add_callback('on_disconnect', on_disconnect) 