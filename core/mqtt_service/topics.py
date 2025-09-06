def topic_prefix(cm) -> str:
    return cm.get('mqtt.topic_prefix', 'factor')


def equipment_uuid(cm) -> str:
    return cm.get('equipment.uuid', 'unknown')


def topic_cmd(cm) -> str:
    return f"{topic_prefix(cm)}/{equipment_uuid(cm)}/cmd"


def topic_status(cm) -> str:
    return f"{topic_prefix(cm)}/{equipment_uuid(cm)}/status"


def topic_lwt(cm) -> str:
    return f"{topic_prefix(cm)}/{equipment_uuid(cm)}/lwt"



# Device serial 기반 토픽들 (라즈베리 시리얼 사용)
def topic_dashboard(device_serial: str) -> str:
    return f"DASHBOARD/{device_serial}"


def topic_admin_cmd(device_serial: str) -> str:
    return f"ADMIN_COMMAND/{device_serial}"


def topic_admin_mcode(device_serial: str) -> str:
    return f"ADMIN_COMMAND/MCOD_MODE/{device_serial}"


def topic_dash_status(device_serial: str) -> str:
    return f"dash_status/{device_serial}"


def topic_admin_result(device_serial: str) -> str:
    return f"admin_result/{device_serial}"

