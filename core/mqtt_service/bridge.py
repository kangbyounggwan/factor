import json
import uuid
import os
import logging
import threading
import time
from core.system_utils import get_pi_serial
import paho.mqtt.client as mqtt
from .topics import (
    topic_cmd, topic_lwt,
    topic_dashboard, topic_admin_cmd, topic_admin_mcode, topic_dash_status,
    topic_sd_list, topic_sd_list_result,
    topic_ctrl_home, topic_ctrl_pause, topic_ctrl_resume, topic_ctrl_cancel, topic_ctrl_result,
)
from .handlers.status import handle_get_status
from .handlers.commands import handle_command


class MQTTService:
    def __init__(self, config_manager, factor_client=None):
        self.cm = config_manager
        self.fc = factor_client
        self.logger = logging.getLogger('mqtt-bridge')
        self.host = self.cm.get('mqtt.host', None)
        self.port = int(self.cm.get('mqtt.port', 1883))
        self.username = self.cm.get('mqtt.username', None)
        self.password = self.cm.get('mqtt.password', None)
        self.tls = bool(self.cm.get('mqtt.tls', False))
        # 고유 client_id 구성: equipment.uuid → serial → random suffix
        try:
            eq_uuid = (self.cm.get('equipment.uuid', None) or '').strip()
        except Exception:
            eq_uuid = ''
        suffix = eq_uuid or (get_pi_serial() or '') or uuid.uuid4().hex[:8]
        client_id = f"factor-{suffix}"
        self.client = mqtt.Client(client_id=client_id, clean_session=True)
        if self.username:
            self.client.username_pw_set(self.username, self.password or None)
        if self.tls:
            self.client.tls_set()

        # 로거/재연결/큐 튜닝
        try:
            self.client.enable_logger(self.logger)
        except Exception:
            pass
        try:
            self.client.reconnect_delay_set(min_delay=1, max_delay=10)
            self.client.max_queued_messages_set(2000)
            self.client.max_inflight_messages_set(40)
        except Exception:
            pass

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self._running = False
        # 대시보드/관리자 채널 토픽들
        device_serial = get_pi_serial()
        self.device_serial = device_serial
        self.dashboard_topic = topic_dashboard(device_serial)
        self.admin_cmd_topic = topic_admin_cmd(device_serial)
        self.admin_mcode_topic = topic_admin_mcode(device_serial)
        self.sd_list_topic = topic_sd_list(device_serial)
        self.sd_list_result_topic = topic_sd_list_result(device_serial)
        self.ctrl_home_topic = topic_ctrl_home(device_serial)
        self.ctrl_pause_topic = topic_ctrl_pause(device_serial)
        self.ctrl_resume_topic = topic_ctrl_resume(device_serial)
        self.ctrl_cancel_topic = topic_ctrl_cancel(device_serial)
        self.ctrl_result_topic = topic_ctrl_result(device_serial)
        # 상태 스트리밍 관리 변수
        try:
            self._status_interval = float(self.cm.get('mqtt.status_interval', 1.5))
        except Exception:
            self._status_interval = 1.5
        self._status_streaming = False
        self._status_thread = None
        # keepalive 설정(기본 120초)
        try:
            self.keepalive = int(self.cm.get('mqtt.keepalive', 120))
        except Exception:
            self.keepalive = 120

    def _on_connect(self, client, userdata, flags, rc):
        # collection
        client.subscribe(topic_cmd(self.cm), qos=1)
        client.subscribe(self.dashboard_topic, qos=1)
        client.subscribe(self.admin_cmd_topic, qos=1)
        client.subscribe(self.admin_mcode_topic, qos=1)
        client.subscribe(self.sd_list_topic, qos=1)
        client.publish(topic_lwt(self.cm), json.dumps({"online": True}), qos=1, retain=True)


        # controll
        client.subscribe(self.ctrl_home_topic, qos=1)
        client.subscribe(self.ctrl_pause_topic, qos=1)
        client.subscribe(self.ctrl_resume_topic, qos=1)
        client.subscribe(self.ctrl_cancel_topic, qos=1)

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
            try:
                self.logger.info(f"get_status 요청 수신 - 스트리밍 시작(interval={self._status_interval}s)")
            except Exception:
                pass
            # 즉시 한 번 발행
            handle_get_status(self.client, self.cm, self.fc)
            # 이미 스트리밍 중이면 무시
            if not self._status_streaming:
                self._start_status_stream()
        # 상태 스트리밍 중단
        elif mtype == 'get_status_stop':
            try:
                self.logger.info("get_status_stop 요청 수신 - 스트리밍 중단")
            except Exception:
                pass
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
        # SD 카드 파일 리스트 요청 (payload type 무관, 토픽으로만 구분)
        elif msg.topic == self.sd_list_topic:
            self._handle_sd_list_request()
        # control: home/pause/resume/cancel
        elif msg.topic == self.ctrl_home_topic:
            self._handle_ctrl_home(msg)
        elif msg.topic == self.ctrl_pause_topic:
            self._handle_ctrl_pause(msg)
        elif msg.topic == self.ctrl_resume_topic:
            self._handle_ctrl_resume(msg)
        elif msg.topic == self.ctrl_cancel_topic:
            self._handle_ctrl_cancel(msg)
        else:
            pass

    def _handle_sd_list_request(self):
        ok = False
        files = []
        error = ""
        try:
            fc = self.fc
            pc = getattr(fc, 'printer_comm', None) if fc else None
            if pc and getattr(pc, 'connected', False):
                files, ok, error = self._get_sd_list_via_cache_or_query(pc)
            else:
                error = "printer not connected"
        except Exception as e:
            error = str(e)

        payload = {
            "type": "sd_list_result",
            "ok": bool(ok),
            "files": files,
            "error": (error or None),
            "timestamp": int(time.time() * 1000),
        }
        try:
            self.client.publish(
                self.sd_list_result_topic,
                json.dumps(payload, ensure_ascii=False),
                qos=1,
                retain=False
            )
        except Exception:
            pass

    def _get_sd_list_via_cache_or_query(self, pc, max_wait_s: float = 4.0, fresh_threshold_s: float = 5.0):
        files = []
        ok = False
        error = ""
        try:
            info = getattr(pc, 'sd_card_info', None) or {}
            last_update = float(info.get('last_update') or 0.0) if isinstance(info, dict) else 0.0
            now = time.time()
            # 1) 캐시가 신선하면 즉시 반환
            if isinstance(info, dict) and (now - last_update) <= fresh_threshold_s:
                files = list(info.get('files') or [])
                return files, True, ""

            # 2) 갱신 요청 후 캐시 완료를 대기
            prev_ts = last_update
            try:
                pc.send_command("M20")
            except Exception:
                pass
            deadline = now + max_wait_s
            while time.time() < deadline:
                try:
                    info = getattr(pc, 'sd_card_info', None) or {}
                    lu = float(info.get('last_update') or 0.0) if isinstance(info, dict) else 0.0
                    if lu > prev_ts:
                        files = list(info.get('files') or [])
                        return files, True, ""
                except Exception:
                    pass
                time.sleep(0.1)
            # 3) 실패 시 현재 보유 캐시라도 반환
            if isinstance(info, dict):
                files = list(info.get('files') or [])
                ok = len(files) > 0
                if not ok:
                    error = "no sd list available"
            else:
                error = "no sd list available"
        except Exception as e:
            error = str(e)
        return files, ok, error

    # ===== Control handlers =====
    def _publish_ctrl_result(self, action: str, ok: bool, message: str = ""):
        payload = {
            "type": "control_result",
            "action": action,
            "ok": bool(ok),
            "message": (message or None),
            "timestamp": int(time.time() * 1000),
        }
        try:
            try:
                self.logger.info(f"CONTROL {action} -> ok={ok} msg={message or ''}")
            except Exception:
                pass
            self.client.publish(self.ctrl_result_topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=False)
        except Exception:
            pass

    def _handle_ctrl_home(self, msg):
        axes = ""
        try:
            data = json.loads(msg.payload.decode("utf-8", "ignore") or "{}")
            axes = str(data.get("axes", ""))
        except Exception:
            pass
        ok = False; err = ""
        try:
            fc = self.fc
            if fc and getattr(fc, 'printer_comm', None) and fc.printer_comm.connected:
                try:
                    fc.home_axes(axes=axes)
                    ok = True
                except Exception as e:
                    err = str(e)
            else:
                err = "printer not connected"
        except Exception as e:
            err = str(e)
        self._publish_ctrl_result("home", ok, err)

    def _handle_ctrl_pause(self, msg):
        ok = False; err = ""
        try:
            pc = getattr(self.fc, 'printer_comm', None)
            if pc and pc.connected:
                try:
                    pc.send_command_and_wait("M25", timeout=3.0)
                    ok = True
                except Exception as e:
                    err = str(e)
            else:
                err = "printer not connected"
        except Exception as e:
            err = str(e)
        self._publish_ctrl_result("pause", ok, err)

    def _handle_ctrl_resume(self, msg):
        ok = False; err = ""
        try:
            pc = getattr(self.fc, 'printer_comm', None)
            if pc and pc.connected:
                try:
                    pc.send_command_and_wait("M24", timeout=3.0)
                    ok = True
                except Exception as e:
                    err = str(e)
            else:
                err = "printer not connected"
        except Exception as e:
            err = str(e)
        self._publish_ctrl_result("resume", ok, err)

    def _handle_ctrl_cancel(self, msg):
        ok = False; err = ""
        try:
            pc = getattr(self.fc, 'printer_comm', None)
            if pc and pc.connected and getattr(pc, 'control', None):
                ok = bool(pc.control.stop_sd_print_with_park())
            else:
                err = "printer not connected"
        except Exception as e:
            err = str(e)
        self._publish_ctrl_result("cancel", ok, err)

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
        # 비동기 연결 + 자동 재연결(loop_start)
        try:
            self.client.connect_async(self.host, self.port, keepalive=self.keepalive)
        except Exception:
            self.client.connect(self.host, self.port, keepalive=self.keepalive)
        self.client.loop_start()
        try:
            cid = getattr(self.client, '_client_id', b'')
            cid_str = cid.decode('utf-8', 'ignore') if isinstance(cid, (bytes, bytearray)) else str(cid)
            self.logger.info(f"MQTT connecting to {self.host}:{self.port} keepalive={self.keepalive} client_id={cid_str}")
        except Exception:
            pass

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
        try:
            self.logger.info("상태 스트리밍 스레드 시작")
        except Exception:
            pass

        def _run():
            while self._status_streaming:
                try:
                    # 일반 상태 발행 로그는 과다 → DEBUG로 전환
                    try:
                        self.logger.debug(f"status publish -> {topic_dash_status(self.device_serial)}")
                    except Exception:
                        pass
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
            try:
                self.logger.info("상태 스트리밍 스레드 종료")
            except Exception:
                pass
        except Exception:
            pass
        self._status_thread = None


