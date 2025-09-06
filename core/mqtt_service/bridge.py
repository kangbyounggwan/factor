import json
import uuid
import os
import threading
import time
from core.system_utils import get_pi_serial
import paho.mqtt.client as mqtt
from .topics import (
    topic_cmd, topic_lwt,
    topic_dashboard, topic_admin_cmd, topic_admin_mcode
)
from .handlers.status import handle_get_status
from .handlers.commands import handle_command


class MQTTService:
    def __init__(self, config_manager, factor_client=None):
        self.cm = config_manager
        self.fc = factor_client
        self.host = self.cm.get('mqtt.host', None)
        self.port = int(self.cm.get('mqtt.port', 1883))
        self.username = self.cm.get('mqtt.username', None)
        self.password = self.cm.get('mqtt.password', None)
        self.tls = bool(self.cm.get('mqtt.tls', False))
        client_id = f"factor-{self.cm.get('equipment.uuid','unknown')}"
        self.client = mqtt.Client(client_id=client_id, clean_session=True)
        if self.username:
            self.client.username_pw_set(self.username, self.password or None)
        if self.tls:
            self.client.tls_set()

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self._running = False
        # 대시보드/관리자 채널 토픽들
        device_serial = get_pi_serial()
        self.dashboard_topic = topic_dashboard(device_serial)
        self.admin_cmd_topic = topic_admin_cmd(device_serial)
        self.admin_mcode_topic = topic_admin_mcode(device_serial)
        # 상태 스트리밍 관리 변수
        try:
            self._status_interval = float(self.cm.get('mqtt.status_interval', 1.5))
        except Exception:
            self._status_interval = 1.5
        self._status_streaming = False
        self._status_thread = None

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe(topic_cmd(self.cm), qos=1)
        client.subscribe(self.dashboard_topic, qos=1)
        client.subscribe(self.admin_cmd_topic, qos=1)
        client.subscribe(self.admin_mcode_topic, qos=1)
        client.publish(topic_lwt(self.cm), json.dumps({"online": True}), qos=1, retain=True)

    def _on_disconnect(self, client, userdata, rc):
        try:
            client.publish(topic_lwt(self.cm), json.dumps({"online": False}), qos=1, retain=True)
        except Exception:
            pass

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode('utf-8', 'ignore')
            data = json.loads(payload) if payload else {}
        except Exception:
            data = {}

        mtype = str(data.get('type', '')).lower()

        # 대시보드 상태 요청: 1.5초 간격 지속 발행 시작
        if mtype == 'get_status':
            # 즉시 한 번 발행
            handle_get_status(self.client, self.cm, self.fc)
            # 이미 스트리밍 중이면 무시
            if not self._status_streaming:
                self._start_status_stream()
        # 상태 스트리밍 중단
        elif mtype == 'get_status_stop':
            self._stop_status_stream()
        # 관리자 일반 명령 (reboot 등)
        elif mtype == 'command' and msg.topic == self.admin_cmd_topic:
            handle_command(self.client, self.cm, self.fc, data)
        # 관리자 M코드 전용 채널 (데이터 조회 전용)
        elif mtype == 'command' and msg.topic == self.admin_mcode_topic:
            cmd = str(data.get('cmd', '')).lower()
            # m코드만 허용 (예: m105, m114)
            if cmd and cmd.startswith('m') and cmd[1:].isdigit():
                handle_command(self.client, self.cm, self.fc, data)
            else:
                # 허용되지 않는 명령은 무시
                pass
        else:
            pass

    def start(self):
        if self._running:
            return
        self._running = True
        self.client.will_set(
            topic_lwt(self.cm),
            json.dumps({"online": False}),
            qos=1,
            retain=True
        )
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self):
        if not self._running:
            return
        self._running = False
        # 상태 스트리밍 종료
        try:
            self._stop_status_stream()
        except Exception:
            pass
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    # ===== 내부: 상태 스트리밍 구현 =====
    def _start_status_stream(self):
        self._status_streaming = True

        def _run():
            while self._status_streaming:
                try:
                    handle_get_status(self.client, self.cm, self.fc)
                except Exception:
                    pass
                time.sleep(self._status_interval)

        self._status_thread = threading.Thread(target=_run, daemon=True)
        self._status_thread.start()

    def _stop_status_stream(self):
        self._status_streaming = False
        try:
            if self._status_thread and self._status_thread.is_alive():
                # 짧게 대기 후 해제
                self._status_thread.join(timeout=0.2)
        except Exception:
            pass
        self._status_thread = None


