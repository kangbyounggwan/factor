import json
import uuid
import os
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

        # 대시보드 상태 요청
        if mtype == 'get_status':
            handle_get_status(self.client, self.cm, self.fc)
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
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass


