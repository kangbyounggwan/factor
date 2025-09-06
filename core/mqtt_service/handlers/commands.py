import time


def handle_command(mqtt_client, cm, fc, payload: dict):
    cmd = str(payload.get('cmd', '')).lower()
    if not cmd:
        return

    try:
        if cmd == 'm105' and getattr(fc, 'printer_comm', None):
            fc.printer_comm.send_command("M105")
        elif cmd == 'm114' and getattr(fc, 'printer_comm', None):
            fc.printer_comm.send_command("M114")
        elif cmd == 'reboot':
            import subprocess
            subprocess.Popen(['sudo', 'reboot'])
        else:
            pass
    except Exception:
        pass

    _ = time.time()


