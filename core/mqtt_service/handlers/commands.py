import time
import json
from core.system_utils import get_pi_serial
from ..topics import topic_admin_result


def _publish_admin_result(mqtt_client, ok: bool, cmd: str, message: str = ""):
    topic = topic_admin_result(get_pi_serial())
    payload = {
        "ok": bool(ok),
        "cmd": cmd,
        "message": message,
        "timestamp": int(time.time() * 1000),
    }
    try:
        mqtt_client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=False)
    except Exception:
        pass


def handle_command(mqtt_client, cm, fc, payload: dict):
    cmd = str(payload.get('cmd', '')).lower()
    if not cmd:
        _publish_admin_result(mqtt_client, False, cmd, "empty cmd")
        return

    try:
        if cmd == 'm105' and getattr(fc, 'printer_comm', None):
            fc.printer_comm.send_command("M105")
            _publish_admin_result(mqtt_client, True, cmd, "sent")
        elif cmd == 'm114' and getattr(fc, 'printer_comm', None):
            fc.printer_comm.send_command("M114")
            _publish_admin_result(mqtt_client, True, cmd, "sent")
        elif cmd == 'm27' and getattr(fc, 'printer_comm', None):
            try:
                resp = fc.printer_comm.send_command_and_wait("M27", timeout=3.0)
                _publish_admin_result(mqtt_client, True, cmd, f"resp: {resp}")
            except Exception as e:
                _publish_admin_result(mqtt_client, False, cmd, f"error: {e}")
        elif cmd == 'reboot':
            import subprocess
            subprocess.Popen(['sudo', 'reboot'])
            _publish_admin_result(mqtt_client, True, cmd, "rebooting")
        else:
            _publish_admin_result(mqtt_client, False, cmd, "unsupported or not connected")
    except Exception as e:
        _publish_admin_result(mqtt_client, False, cmd, f"error: {e}")

    _ = time.time()


