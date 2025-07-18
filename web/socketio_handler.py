"""
SocketIO 핸들러
실시간 웹소켓 통신 관리
"""

from flask_socketio import SocketIO

# SocketIO 인스턴스 생성
socketio = SocketIO(cors_allowed_origins="*") 