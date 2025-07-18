"""
Flask 웹 애플리케이션
라즈베리파이 최적화된 웹 인터페이스
"""

from flask import Flask, render_template, request, jsonify # type: ignore
from flask_socketio import SocketIO
import logging
from pathlib import Path

from core import ConfigManager
from core.hotspot_manager import HotspotManager
from .api import api_bp
from .socketio_handler import socketio


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
    
    # 핫스팟 관리자 초기화
    app.hotspot_manager = HotspotManager(config_manager)
    
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
            logger.error(f"상태 정보 조회 오류: {e}")
            return jsonify({'error': str(e)}), 500
    
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
            socketio.emit('connection_error', {'message': 'OctoPrint에 연결되지 않음'})
    
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