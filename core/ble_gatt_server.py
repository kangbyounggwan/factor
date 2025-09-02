#!/usr/bin/env python3
"""
BlueZ D-Bus 기반 BLE GATT 서버
- Service: SERVICE_UUID
- Characteristics:
  - WIFI_REGISTER_CHAR_UUID (write/notify): wifi_scan, wifi_register 처리
  - EQUIPMENT_SETTINGS_CHAR_UUID (write/notify): 설비 설정 수신

주의: 시스템에 bluez 가 설치되어 있어야 하며, polkit 규칙을 통해 bluetooth 그룹 사용자가
      org.bluez 액션을 사용할 수 있어야 한다(install.sh에서 설정).
"""

import asyncio
import json
import time
import logging
import subprocess
from typing import Any, Dict, List

from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method, dbus_property
from dbus_next.constants import PropertyAccess
from dbus_next import Variant, BusType


# ===== 고정 UUID =====
SERVICE_UUID = "12345678-1234-1234-1234-123456789abc"
WIFI_REGISTER_CHAR_UUID = "87654321-4321-4321-4321-cba987654321"
EQUIPMENT_SETTINGS_CHAR_UUID = "87654321-4321-4321-4321-cba987654322"

# BlueZ IFACE
BLUEZ = 'org.bluez'
GATT_MANAGER_IFACE = 'org.bluez.GattManager1'
LE_ADV_MANAGER_IFACE = 'org.bluez.LEAdvertisingManager1'

# 기본 GATT 객체 경로
APP_PATH = '/org/factor/gatt'
SERVICE_PATH = APP_PATH + '/service0'
WIFI_CHAR_PATH = SERVICE_PATH + '/char0'
EQUIP_CHAR_PATH = SERVICE_PATH + '/char1'
ADV_PATH = '/org/factor/advertisement0'


def _json_bytes(obj: Dict[str, Any]) -> bytes:
    try:
        return json.dumps(obj, ensure_ascii=False).encode('utf-8', errors='ignore')
    except Exception:
        return b'{}'


def _now_ts() -> int:
    return int(time.time())


def _scan_wifi_networks() -> List[Dict[str, Any]]:
    """iwlist를 사용하여 주변 Wi-Fi 네트워크 스캔(ssid, rssi, security)"""
    networks: List[Dict[str, Any]] = []
    try:
        result = subprocess.run(
            ['sudo', 'iwlist', 'wlan0', 'scan'], capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return networks

        current: Dict[str, Any] = {}
        for raw in (result.stdout or '').split('\n'):
            line = (raw or '').strip()
            if not line:
                continue
            if line.startswith('Cell '):
                if current.get('ssid'):
                    networks.append(current)
                current = {}
                continue
            if 'ESSID:' in line:
                try:
                    ssid = line.split('ESSID:', 1)[1].strip().strip('"')
                    current['ssid'] = ssid
                except Exception:
                    pass
                continue
            if 'Signal level=' in line:
                try:
                    import re as _re
                    m = _re.search(r'Signal level=\s*(-?\d+)', line)
                    if m:
                        current['rssi'] = int(m.group(1))
                except Exception:
                    pass
                continue
            if 'Encryption key:' in line:
                enc_on = 'on' in line.lower()
                current.setdefault('_enc', enc_on)
                continue
            if ('WPA2' in line) or ('IEEE 802.11i' in line) or ('RSN' in line):
                current['security'] = 'WPA2'
                continue
            if 'WPA' in line and 'WPA2' not in line:
                current.setdefault('security', 'WPA')
                continue
            if 'WEP' in line:
                current.setdefault('security', 'WEP')
                continue

        if current.get('ssid'):
            networks.append(current)

        for n in networks:
            if 'security' not in n:
                if n.get('_enc'):
                    n['security'] = 'Protected'
                else:
                    n['security'] = 'Open'
            if 'rssi' not in n:
                n['rssi'] = -100
            n.pop('_enc', None)
        return networks
    except Exception:
        return networks


class GattService(ServiceInterface):
    def __init__(self, uuid: str):
        super().__init__('org.bluez.GattService1')
        self.uuid = uuid
        self.path = SERVICE_PATH

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> 's':  # type: ignore
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> 'b':  # type: ignore
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> 'ao':  # type: ignore
        return []


class GattCharacteristic(ServiceInterface):
    def __init__(self, uuid: str, flags: List[str], path: str):
        super().__init__('org.bluez.GattCharacteristic1')
        self.uuid = uuid
        self.flags = flags
        self.path = path
        self._value: bytes = b''
        self._notifying = False

    def _notify_value(self, value: bytes):
        self._value = value
        if self._notifying:
            try:
                self.emit_properties_changed({'Value': Variant('ay', value)}, [])
            except Exception:
                pass

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> 's':  # type: ignore
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> 'as':  # type: ignore
        return self.flags

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> 'o':  # type: ignore
        return SERVICE_PATH

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> 'ay':  # type: ignore
        return self._value

    @method()
    def StartNotify(self):
        self._notifying = True

    @method()
    def StopNotify(self):
        self._notifying = False

    @method()
    def ReadValue(self, options: 'a{sv}') -> 'ay':  # type: ignore
        return list(self._value)

    @method()
    def WriteValue(self, value: 'ay', options: 'a{sv}'):  # type: ignore
        # override
        pass


class ObjectManager(ServiceInterface):
    """org.freedesktop.DBus.ObjectManager 구현
    GetManagedObjects()는 BlueZ가 APP_PATH 이하의 객체들을 수집할 때 사용됨
    """
    def __init__(self, service_uuid: str):
        super().__init__('org.freedesktop.DBus.ObjectManager')
        self.service_uuid = service_uuid

    @method()
    def GetManagedObjects(self) -> 'a{oa{sa{sv}}}':  # type: ignore
        managed = {}
        # Service properties
        managed[SERVICE_PATH] = {
            'org.bluez.GattService1': {
                'UUID': Variant('s', self.service_uuid),
                'Primary': Variant('b', True),
                'Includes': Variant('ao', [])
            }
        }
        # Characteristic properties
        managed[WIFI_CHAR_PATH] = {
            'org.bluez.GattCharacteristic1': {
                'UUID': Variant('s', WIFI_REGISTER_CHAR_UUID),
                'Service': Variant('o', SERVICE_PATH),
                'Flags': Variant('as', ['write', 'notify']),
                'Value': Variant('ay', [])
            }
        }
        managed[EQUIP_CHAR_PATH] = {
            'org.bluez.GattCharacteristic1': {
                'UUID': Variant('s', EQUIPMENT_SETTINGS_CHAR_UUID),
                'Service': Variant('o', SERVICE_PATH),
                'Flags': Variant('as', ['write', 'notify']),
                'Value': Variant('ay', [])
            }
        }
        return managed

class WifiRegisterChar(GattCharacteristic):
    def __init__(self):
        super().__init__(WIFI_REGISTER_CHAR_UUID, ['write', 'notify'], WIFI_CHAR_PATH)

    @method()
    def WriteValue(self, value: 'ay', options: 'a{sv}'):
        raw = bytes(value)
        try:
            msg = json.loads(raw.decode('utf-8', 'ignore'))
            mtype = str(msg.get('type', '')).lower()
        except Exception:
            rsp = {"type": "wifi_scan_result", "data": {"success": False, "error": "invalid_json"}, "timestamp": _now_ts()}
            self._notify_value(_json_bytes(rsp))
            return

        if mtype == 'wifi_scan':
            nets = _scan_wifi_networks()
            rsp = {"type": "wifi_scan_result", "data": nets, "timestamp": _now_ts()}
            self._notify_value(_json_bytes(rsp))
        elif mtype == 'wifi_register':
            ok = True
            rsp = {"type": "wifi_register_result", "data": {"ok": ok, "message": "applied"}, "timestamp": _now_ts()}
            self._notify_value(_json_bytes(rsp))
        else:
            rsp = {"type": "wifi_error", "data": {"success": False, "error": "unknown_type", "type": mtype}, "timestamp": _now_ts()}
            self._notify_value(_json_bytes(rsp))


class EquipmentSettingsChar(GattCharacteristic):
    def __init__(self):
        super().__init__(EQUIPMENT_SETTINGS_CHAR_UUID, ['write', 'notify'], EQUIP_CHAR_PATH)
        self._settings: Dict[str, Any] = {}

    @method()
    def WriteValue(self, value: 'ay', options: 'a{sv}'):
        raw = bytes(value)
        try:
            msg = json.loads(raw.decode('utf-8', 'ignore'))
            if str(msg.get('type', '')).lower() == 'equipment_update':
                self._settings.update(msg.get('data') or {})
                rsp = {"type": "equipment_update_result", "data": {"ok": True, "applied": self._settings}, "timestamp": _now_ts()}
            else:
                rsp = {"type": "equipment_error", "data": {"ok": False, "error": "unknown_type"}, "timestamp": _now_ts()}
        except Exception:
            rsp = {"type": "equipment_error", "data": {"ok": False, "error": "invalid_json"}, "timestamp": _now_ts()}
        self._notify_value(_json_bytes(rsp))


class LEAdvertisement(ServiceInterface):
    def __init__(self, service_uuid: str):
        super().__init__('org.bluez.LEAdvertisement1')
        self.service_uuid = service_uuid
        self.path = ADV_PATH

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> 's':  # type: ignore
        return 'peripheral'

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> 'as':  # type: ignore
        return [self.service_uuid]

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> 's':  # type: ignore
        return 'Factor-Client'

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> 'as':  # type: ignore
        return ['tx-power']

    @method()
    def Release(self):
        pass


async def _async_run(logger: logging.Logger):
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        await bus.request_name('org.factor.gatt')
    except Exception:
        pass

    adapter_path = '/org/bluez/hci0'
    obj = await bus.introspect(BLUEZ, adapter_path)
    adapter = bus.get_proxy_object(BLUEZ, adapter_path, obj)
    gatt_mgr = adapter.get_interface(GATT_MANAGER_IFACE)
    # 광고 매니저는 일부 환경에서 -E 필요 → 실패해도 무시
    adv_mgr = None
    try:
        adv_mgr = adapter.get_interface(LE_ADV_MANAGER_IFACE)
    except Exception:
        adv_mgr = None

    # PropertiesChanged 브로드캐스트용 인터페이스
    props_iface = adapter.get_interface('org.freedesktop.DBus.Properties')
    # 어댑터 전원 켜기 (필요 시)
    try:
        await props_iface.call_set('org.bluez.Adapter1', 'Powered', Variant('b', True))
        logger.info('Adapter Powered=on')
    except Exception:
        pass

    # ObjectManager + 서비스/특성 export
    obj_manager = ObjectManager(SERVICE_UUID)
    bus.export(APP_PATH, obj_manager)
    svc = GattService(SERVICE_UUID)
    wifi_char = WifiRegisterChar()
    equip_char = EquipmentSettingsChar()

    # 구성 로그: 서비스/특성/플래그
    try:
        logger.info(
            "BLE GATT 구성 - service=%s, chars=[{%s:%s}, {%s:%s}]",
            SERVICE_UUID,
            WIFI_REGISTER_CHAR_UUID,
            ','.join(['write', 'notify']),
            EQUIPMENT_SETTINGS_CHAR_UUID,
            ','.join(['write', 'notify'])
        )
    except Exception:
        pass

    bus.export(SERVICE_PATH, svc)
    bus.export(WIFI_CHAR_PATH, wifi_char)
    bus.export(EQUIP_CHAR_PATH, equip_char)

    # GATT 등록
    await gatt_mgr.call_register_application(APP_PATH, {})
    try:
        logger.info(
            "BLE GATT Application 등록 완료 (app=%s, service=%s)",
            APP_PATH,
            SERVICE_PATH
        )
    except Exception:
        logger.info("BLE GATT Application 등록 완료")

    # 광고 등록: 실패 시 원인 진단에 도움이 되도록 경고 강화
    if adv_mgr:
        try:
            adv = LEAdvertisement(SERVICE_UUID)
            bus.export(ADV_PATH, adv)
            await adv_mgr.call_register_advertisement(ADV_PATH, {})
            try:
                logger.info(
                    "BLE 광고 등록 완료 (LEAdvertisingManager1, adv=%s, service_uuid=%s)",
                    ADV_PATH,
                    SERVICE_UUID
                )
            except Exception:
                logger.info("BLE 광고 등록 완료 (LEAdvertisingManager1)")
        except Exception as e:
            logger.warning(
                "BLE 광고 등록 실패: %s. 중복 광고 서비스(ble-headless, bluetoothctl advertise on) 혹은 권한/experimental(-E) 설정을 확인하세요.",
                e
            )

    # keep alive
    try:
        while True:
            await asyncio.sleep(60)
    finally:
        # 정리 루틴: 광고/앱 등록 해제(가능한 경우)
        try:
            if adv_mgr:
                await adv_mgr.call_unregister_advertisement(ADV_PATH)
        except Exception:
            pass
        try:
            await gatt_mgr.call_unregister_application(APP_PATH)
        except Exception:
            pass


def start_ble_gatt_server(logger: logging.Logger) -> None:
    """비동기 BLE GATT 서버를 백그라운드에서 실행"""
    import threading
    import atexit
    import functools

    def runner():
        try:
            asyncio.run(_async_run(logger))
        except Exception as e:
            logger.error(f"BLE GATT 서버 실행 오류: {e}")

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    # 베스트-에포트 종료 훅: 별도 버스로 정리 시도
    async def _cleanup():
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            adapter_path = '/org/bluez/hci0'
            obj = await bus.introspect(BLUEZ, adapter_path)
            adapter = bus.get_proxy_object(BLUEZ, adapter_path, obj)
            gatt_mgr = adapter.get_interface(GATT_MANAGER_IFACE)
            try:
                await gatt_mgr.call_unregister_application(APP_PATH)
            except Exception:
                pass
            try:
                adv_mgr = adapter.get_interface(LE_ADV_MANAGER_IFACE)
                await adv_mgr.call_unregister_advertisement(ADV_PATH)
            except Exception:
                pass
        except Exception:
            pass

    def _atexit():
        try:
            asyncio.run(_cleanup())
        except Exception:
            pass

    atexit.register(_atexit)


