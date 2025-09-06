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


