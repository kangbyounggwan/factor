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
        # SD 업로드 세션 관리 맵
        self._upload_sessions = {}

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
        # 대시보드: 축 이동
        elif mtype == 'move' and msg.topic == self.dashboard_topic:
            self._handle_ctrl_move(data)
        # 대시보드: 온도 설정
        elif mtype == 'set_temperature' and msg.topic == self.dashboard_topic:
            self._handle_ctrl_set_temperature(data)
        # SD 업로드(청크): chunk/commit
        elif mtype == 'sd_upload_chunk' and msg.topic == self.dashboard_topic:
            self._handle_sd_upload_chunk(data)
        elif mtype == 'sd_upload_commit' and msg.topic == self.dashboard_topic:
            self._handle_sd_upload_commit(data)
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
            try:
                self.logger.info(f"[MQTT_RX] topic={msg.topic} payload={payload}")
            except Exception:
                pass
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
        # 웹 API를 통해 위임하여 결과 반환
        ok = False
        files = []
        error = ""
        try:
            status, resp = self._get_local_api('/printer/sd/list')
            # API 호출 결과 로깅 (raw)
            try:
                raw_preview = str(resp)
                if len(raw_preview) > 800:
                    raw_preview = raw_preview[:800] + '...'
                self.logger.info(f"[SD_LIST_API] status={status} resp_raw={raw_preview}")
            except Exception:
                pass
                    # 1) resp를 dict로 정규화
            raw_resp = resp
            try:
                if isinstance(resp, (bytes, str)):
                    resp = json.loads(resp)
            except Exception as e:
                # JSON이 아니면 그대로 에러로 남김
                error = f"JSON parse error: {e}; raw={raw_resp!r}"

            # 2) dict일 때만 파싱 진행
            if status and isinstance(resp, dict):
                # success/ok 둘 다 지원
                ok = bool(resp.get('success', resp.get('ok', False)))

                # files 위치 유연하게 처리 (top-level or data.files)
                files_val = resp.get('files')
                if files_val is None:
                    files_val = (resp.get('data') or {}).get('files')

                if ok and files_val:
                    # list(...)로 리스트 보장
                    files = list(files_val)
                else:
                    if not ok and not error:
                        error = str(resp.get('error') or '')
            else:
                if not error:
                    error = str(resp or 'api error')

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
            # 최종 페이로드 로깅
            try:
                payload_preview = json.dumps(payload, ensure_ascii=False)
                if len(payload_preview) > 800:
                    payload_preview = payload_preview[:800] + '...'
                self.logger.info(f"[MQTT_PUB] topic={self.sd_list_result_topic} payload={payload_preview}")
            except Exception:
                pass
            self.client.publish(
                self.sd_list_result_topic,
                json.dumps(payload, ensure_ascii=False),
                qos=1,
                retain=False
            )
        except Exception:
            pass
 


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

    # ===== SD 업로드(청크) 유틸 =====

    # _handle_sd_upload_init 제거됨 (첫 청크 자동 초기화)

    def _handle_sd_upload_chunk(self, data: dict):
        ok = False; err = ""
        try:
            upid = str(data.get('upload_id') or '').strip()
            s = self._upload_sessions.get(upid)
            if not s:
                # 첫 청크에서 자동 초기화 지원: name, total_size 필수
                name = str(data.get('name') or '').strip()
                try:
                    total = int(data.get('total_size') or 0)
                except Exception:
                    total = 0
                if not name or total <= 0:
                    self._publish_ctrl_result("sd_upload", False, "need name and total_size on first chunk"); return
                try:
                    import tempfile
                    fd, tmp = tempfile.mkstemp(prefix="sd_up_", suffix=".bin")
                    import os as _os
                    f = _os.fdopen(fd, "wb")
                    self._upload_sessions[upid] = {
                        'name': name,
                        'tmp': tmp,
                        'f': f,
                        'total': total,
                        'received': 0,
                        'next_index': 0,
                        'start_ts': time.time(),
                    }
                    s = self._upload_sessions[upid]
                except Exception as e:
                    self._publish_ctrl_result("sd_upload", False, f"init failed: {e}"); return
            # 인덱스 검증
            try:
                idx = int(data.get('index'))
            except Exception:
                self._publish_ctrl_result("sd_upload", False, "missing/invalid index"); return
            expected = int(s.get('next_index') or 0)
            if idx != expected:
                self._publish_ctrl_result("sd_upload", False, f"unexpected index {idx} (expected {expected})"); return
            b64 = data.get('data_b64')
            size = int(data.get('size') or 0)
            if not b64 or size <= 0:
                self._publish_ctrl_result("sd_upload", False, "invalid chunk"); return
            import base64
            try:
                chunk = base64.b64decode(b64, validate=True)
            except Exception:
                chunk = base64.b64decode(b64)
            # 길이 검증
            if len(chunk) != size:
                self._publish_ctrl_result("sd_upload", False, f"size mismatch decoded={len(chunk)} meta={size}")
                return
            s['f'].write(chunk)
            s['received'] += len(chunk)
            s['next_index'] = expected + 1
            # 진행률 통지
            try:
                total = float(s.get('total') or 0) or 1.0
                pct = (float(s['received']) / total) * 100.0
                name = s.get('name') or ''
                self._publish_ctrl_result(
                    "sd_upload_progress", True,
                    f"upload_id={upid} name={name} received={s['received']}/{int(total)} ({pct:.1f}%)"
                )
            except Exception:
                pass
            ok = True
        except Exception as e:
            err = str(e)
        self._publish_ctrl_result("sd_upload", ok, err)

    def _handle_sd_upload_commit(self, data: dict):
        ok = False; err = ""
        try:
            upid = str(data.get('upload_id') or '').strip()
            s = self._upload_sessions.get(upid)
            if not s:
                self._publish_ctrl_result("sd_upload", False, "unknown upload_id"); return
            try:
                s['f'].flush(); s['f'].close()
            except Exception:
                pass
            # 누락 검증
            try:
                total = int(s.get('total') or 0)
                if int(s.get('received') or 0) != total:
                    self._publish_ctrl_result("sd_upload", False, f"incomplete upload received={s.get('received')}/{total}")
                    return
            except Exception:
                pass
            # 로컬 API 멀티파트 업로드 위임
            try:
                with open(s['tmp'], 'rb') as rf:
                    content = rf.read()
            except Exception as e:
                self._publish_ctrl_result("sd_upload", False, f"temp read error: {e}"); return
            fields = {'name': s['name']}
            files = {'file': {'filename': s['name'], 'content': content, 'content_type': 'application/octet-stream'}}
            ok2, resp = self._post_local_api('/printer/sd/upload', fields, files=files, as_multipart=True, timeout=300.0)
            if not ok2:
                err = str(resp or '')
            ok = bool(ok2)
            # 최종 진행률 통지(100%)
            try:
                self._publish_ctrl_result(
                    "sd_upload_progress", True,
                    f"upload_id={upid} name={s.get('name','')} received={s.get('received')}/{s.get('total')} (100.0%)"
                )
            except Exception:
                pass
            # 정리
            try:
                import os
                os.remove(s['tmp'])
            except Exception:
                pass
            self._upload_sessions.pop(upid, None)
        except Exception as e:
            err = str(e)
        self._publish_ctrl_result("sd_upload", ok, err)

    def _handle_ctrl_set_temperature(self, data: dict):
        ok = False; err = ""
        try:
            pc = getattr(self.fc, 'printer_comm', None)
            if not (pc and pc.connected):
                self._publish_ctrl_result("set_temperature", False, "printer not connected")
                return

            # 입력 파싱
            try:
                tool = int(data.get("tool"))
            except Exception:
                self._publish_ctrl_result("set_temperature", False, "invalid tool")
                return

            try:
                target = float(data.get("temperature"))
            except Exception:
                self._publish_ctrl_result("set_temperature", False, "invalid temperature")
                return

            wait_flag = bool(data.get("wait", False))

            # 간단 범위 검증(선택)
            if tool == -1:
                if target < 0 or target > 120:
                    self._publish_ctrl_result("set_temperature", False, "bed temperature out of range")
                    return
            else:
                if target < 0 or target > 300:
                    self._publish_ctrl_result("set_temperature", False, "temperature out of range")
                    return

            # 전송
            if tool == -1:
                # Bed
                if wait_flag:
                    pc.send_gcode(f"M190 S{target}", wait=True, timeout=120.0)
                else:
                    pc.send_gcode(f"M140 S{target}", wait=False)
            else:
                # Tool N
                if wait_flag:
                    pc.send_gcode(f"M109 T{tool} S{target}", wait=True, timeout=120.0)
                else:
                    pc.send_gcode(f"M104 T{tool} S{target}", wait=False)

            ok = True
        except Exception as e:
            err = str(e)

        self._publish_ctrl_result("set_temperature", ok, err)

    def _handle_ctrl_move(self, data: dict):
        ok = False; err = ""
        try:
            pc = getattr(self.fc, 'printer_comm', None)
            if not (pc and pc.connected):
                self._publish_ctrl_result("move", False, "printer not connected")
                return

            # 입력 파싱
            try:
                mode = str(data.get("mode", "relative")).strip().lower()
            except Exception:
                mode = "relative"

            def _to_float(v):
                try:
                    return None if v is None else float(v)
                except Exception:
                    return None

            x = _to_float(data.get("x"))
            y = _to_float(data.get("y"))
            z = _to_float(data.get("z"))
            e = _to_float(data.get("e"))
            feedrate = _to_float(data.get("feedrate"))  # mm/min

            # 최소 하나의 축 필요
            if x is None and y is None and z is None and e is None:
                self._publish_ctrl_result("move", False, "no axes provided")
                return

            # 이동 모드 설정
            try:
                if mode.startswith("rel"):
                    pc.send_gcode("G91")  # 상대 좌표
                else:
                    pc.send_gcode("G90")  # 절대 좌표
            except Exception:
                pass

            # 이동 실행: FactorClient 래퍼를 사용해 feedrate 기본값(1000) 적용
            try:
                fc = getattr(self, 'fc', None)
                if fc and hasattr(fc, 'move_axis'):
                    fc.move_axis(x, y, z, e, feedrate)
                else:
                    pc.move_axis(x, y, z, e, feedrate)
            except Exception as e:
                err = str(e)
                self._publish_ctrl_result("move", False, err)
                return

            # 상대 모드였으면 절대 모드로 복귀
            if mode.startswith("rel"):
                try:
                    pc.send_gcode("G90")
                except Exception:
                    pass

            ok = True
        except Exception as e:
            err = str(e)

        self._publish_ctrl_result("move", ok, err)

    def _handle_ctrl_home(self, msg):
        axes = ""
        try:
            data = json.loads(msg.payload.decode("utf-8", "ignore") or "{}")
            axes = str(data.get("axes", ""))
        except Exception:
            pass
        ok = False; err = ""
        try:
            pc = getattr(self.fc, 'printer_comm', None)
            if pc and pc.connected:
                try:
                    axes_str = axes.strip()
                    cmd = f"G28 {axes_str}" if axes_str else "G28"
                    pc.send_gcode(cmd, wait=True, timeout=15.0)
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
                    pc.send_gcode("M25", wait=True, timeout=3.0)
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
                    pc.send_gcode("M24", wait=True, timeout=3.0)
                    ok = True
                except Exception as e:
                    err = str(e)
            else:
                err = "printer not connected"
        except Exception as e:
            err = str(e)
        self._publish_ctrl_result("resume", ok, err)

    def _handle_ctrl_cancel(self, msg):
        """API 위임: /api/printer/sd/cancel 로 POST 위임"""
        ok = False; err = ""
        try:
            try:
                data = json.loads(msg.payload.decode("utf-8", "ignore") or "{}")
            except Exception:
                data = {}
            # API 호출 위임
            status, resp = self._post_local_api('/printer/sd/cancel', data)
            ok = bool(status)
            if not ok:
                err = str(resp or '')
        except Exception as e:
            err = str(e)
        self._publish_ctrl_result("cancel", ok, err)

    def _post_local_api(self, path: str, payload: dict, timeout: float = 5.0, files: dict = None, as_multipart: bool = False):
        """로컬 Flask API로 POST.
        - JSON: payload(dict) → application/json
        - 멀티파트(as_multipart=True): fields=payload, files(dict: {'field': {'filename','content','content_type'}})
        성공시 (True, resp_json), 실패시 (False, error_msg)
        """
        try:
            import urllib.request
            import urllib.error
            import uuid as _uuid
        except Exception:
            return False, 'urllib not available'
        try:
            port =  int(self.cm.get('server.port', 5000)) if hasattr(self, 'cm') else 5000
        except Exception:
            port = 5000
        url = f"http://127.0.0.1:{port}/api{path}"
        headers = {}
        body = b''
        if as_multipart:
            boundary = f"----Boundary{_uuid.uuid4().hex}"
            headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
            crlf = b"\r\n"
            def _part_header(name, filename=None, content_type=None):
                disp = f'form-data; name="{name}"'
                if filename:
                    disp += f'; filename="{filename}"'
                hdr = f"--{boundary}\r\nContent-Disposition: {disp}\r\n"
                if content_type:
                    hdr += f"Content-Type: {content_type}\r\n"
                hdr += "\r\n"
                return hdr.encode('utf-8')
            buf = bytearray()
            for k, v in (payload or {}).items():
                buf += _part_header(k)
                buf += (str(v).encode('utf-8'))
                buf += crlf
            for field_name, spec in (files or {}).items():
                filename = (spec.get('filename') or 'upload.bin')
                content = (spec.get('content') or b'')
                content_type = (spec.get('content_type') or 'application/octet-stream')
                buf += _part_header(field_name, filename=filename, content_type=content_type)
                buf += content
                buf += crlf
            buf += f"--{boundary}--\r\n".encode('utf-8')
            body = bytes(buf)
        else:
            headers['Content-Type'] = 'application/json'
            try:
                body = json.dumps(payload or {}, ensure_ascii=False).encode('utf-8')
            except Exception:
                body = b'{}'
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = getattr(resp, 'status', 200)
                text = resp.read().decode('utf-8', 'ignore')
                try:
                    import json as _json
                    data = _json.loads(text) if text else {}
                except Exception:
                    data = {'raw': text}
                if 200 <= code < 300:
                    return True, data
                return False, data
        except urllib.error.HTTPError as e:
            try:
                text = e.read().decode('utf-8', 'ignore')
            except Exception:
                text = str(e)
            return False, text
        except Exception as e:
            return False, str(e)

    def _get_local_api(self, path: str, timeout: float = 5.0):
        """로컬 Flask API로 GET. 성공시 (True, resp_json), 실패시 (False, error_msg)"""
        try:
            import urllib.request
            import urllib.error
        except Exception:
            return False, 'urllib not available'
        try:
            port =  int(self.cm.get('server.port', 5000)) if hasattr(self, 'cm') else 5000
        except Exception:
            port = 5000
        url = f"http://127.0.0.1:{port}/api{path}"
        req = urllib.request.Request(url, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = getattr(resp, 'status', 200)
                text = resp.read().decode('utf-8', 'ignore')
                try:
                    import json as _json
                    data = _json.loads(text) if text else {}
                except Exception:
                    data = {'raw': text}
                if 200 <= code < 300:
                    return True, data
                return False, data
        except urllib.error.HTTPError as e:
            try:
                text = e.read().decode('utf-8', 'ignore')
            except Exception:
                text = str(e)
            return False, text
        except Exception as e:
            return False, str(e)

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


