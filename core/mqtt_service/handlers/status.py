import json
from ..topics import topic_status, topic_dash_status
from core.system_utils import get_pi_serial


def build_status(fc):
    try:
        return {
            "printer_status": fc.get_printer_status().to_dict(),
            "temperature_info": fc.get_temperature_info().to_dict(),
            "position": fc.get_position().to_dict(),
            "progress": fc.get_print_progress().to_dict(),
            "system_info": fc.get_system_info().to_dict(),
            "connected": fc.is_connected(),
            "timestamp": getattr(fc, 'last_heartbeat', 0),
        }
    except Exception:
        return {"connected": False}


def handle_get_status(mqtt_client, cm, fc):
    data = build_status(fc)
    data["equipment_uuid"] = cm.get('equipment.uuid', None)
    topic = topic_dash_status(get_pi_serial())
    mqtt_client.publish(
        topic,
        json.dumps(data, ensure_ascii=False),
        qos=1,
        retain=False
    )


    


