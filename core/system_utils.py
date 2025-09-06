import os
import uuid


def get_pi_serial() -> str:
    serial_paths = [
        "/proc/device-tree/serial-number",
        "/sys/firmware/devicetree/base/serial-number",
    ]
    for spath in serial_paths:
        try:
            if os.path.exists(spath):
                with open(spath, "rb") as f:
                    raw = f.read()
                value = raw.decode("utf-8", "ignore").replace("\x00", "").strip()
                if value:
                    return value
        except Exception:
            pass
    try:
        mac = uuid.getnode()
        return f"{mac:012x}"
    except Exception:
        return "UNKNOWN"


