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
from core.ble_service.utils import json_bytes as _json_bytes_ext, now_ts as _now_ts_ext, now_ms as _now_ms_ext
from core.ble_service.wifi import scan_wifi_networks as _scan_wifi_networks_ext, get_network_status as _get_network_status_ext, wpa_connect_immediate as _wpa_connect_immediate_ext, nm_connect_immediate as _nm_connect_immediate_ext, _nm_is_running as _nm_is_running_ext

from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method, dbus_property
from dbus_next.constants import PropertyAccess
from dbus_next import Variant, BusType


# ===== 고정 UUID =====
SERVICE_UUID = "12345678-1234-1234-1234-123456789abc"
WIFI_REGISTER_CHAR_UUID = "87654321-4321-4321-4321-cba987654321"
EQUIPMENT_SETTINGS_CHAR_UUID = "87654321-4321-4321-4321-cba987654322"

# Notify 청크 크기(보수적으로 180~200 권장)
MAX_CHUNK = 20

# BlueZ IFACE
BLUEZ = 'org.bluez'
GATT_MANAGER_IFACE = 'org.bluez.GattManager1'
LE_ADV_MANAGER_IFACE = 'org.bluez.LEAdvertisingManager1'

# Notify framing (ACK/RESULT 경계 식별용)
FRAME_MAGIC = b'FCF1'          # 4 bytes
FRAME_END = b'FCF1END'         # 7 bytes

# 기본 GATT 객체 경로
APP_PATH = '/org/factor/gatt'
SERVICE_PATH = APP_PATH + '/service0'
WIFI_CHAR_PATH = SERVICE_PATH + '/char0'
EQUIP_CHAR_PATH = SERVICE_PATH + '/char1'
ADV_PATH = '/org/factor/advertisement0'


# NOTE: JSON 직렬화 유틸은 ble_service.utils 로 이동하여 사용 중


# NOTE: now_ts 유틸은 ble_service.utils 로 이동하여 사용 중


# NOTE: Wi-Fi 스캔 로직은 ble_service.wifi 로 이동하여 사용 중


# NOTE: 네트워크 상태 조회 로직은 ble_service.wifi 로 이동하여 사용 중


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
        # 직렬화 전송용 큐/워커 상태
        try:
            import asyncio as _asyncio  # noqa: F401
            self._notify_queue = asyncio.Queue()
        except Exception:
            self._notify_queue = None  # type: ignore
        self._notify_worker_running = False
        self._notify_worker_task = None

    def _notify_value(self, value: bytes):
        self._value = value
        # 전체 본문은 전송하지 않고, 청크만 전송/로깅
        logging.getLogger('ble-gatt').info(
            "Notify-begin [%s] total_bytes=%d chunk_size=%d",
            self.uuid, len(value), MAX_CHUNK
        )
        if not self._notifying:
            return

        loop = asyncio.get_event_loop()

        async def _send_chunks(data: bytes):
            await asyncio.sleep(0.1)
            for off in range(0, len(data), MAX_CHUNK):
                chunk = data[off:off + MAX_CHUNK]
                # 청크별 로깅 (프리뷰: 텍스트/헥스)
                try:
                    preview_text = chunk[:128].decode('utf-8', 'replace')
                except Exception:
                    logging.getLogger('ble-gatt').exception("Notify-chunk 프리뷰 로깅 실패")
                    preview_text = ''
                preview_hex = chunk[:32].hex()

                try:
                    logging.getLogger('ble-gatt').info(
                        "Notify-chunk [%s] off=%d len=%d/%d preview=%s hex=%s",
                        self.uuid, off, len(chunk), len(data), preview_text, preview_hex
                    )
                except Exception:
                    logging.getLogger('ble-gatt').exception("Notify-chunk 프리뷰 로깅 실패")

                try:
                    self.emit_properties_changed({'Value': bytes(chunk)}, [])
                except Exception:
                    logging.getLogger('ble-gatt').exception(
                        "Notify-chunk error [%s] off=%d len=%d/%d",
                        self.uuid, off, len(chunk), len(data)
                    )
                # 너무 빠른 연속 notify 방지
                await asyncio.sleep(0.05)
        try:
            loop.create_task(_send_chunks(value))
        except RuntimeError:
            logging.getLogger('ble-gatt').exception("실행 루프 없음")
            # 실행 루프가 없을 때 동기식 베스트-에포트
            for off in range(0, len(value), MAX_CHUNK):
                chunk = value[off:off + MAX_CHUNK]
                try:
                    self.emit_properties_changed({'Value': bytes(chunk)}, [])
                except Exception:
                    logging.getLogger('ble-gatt').exception(
                        "Notify-chunk error(sync) [%s] off=%d len=%d/%d",
                        self.uuid, off, len(chunk), len(value)
                    )

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

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> 'b':  # type: ignore
        return self._notifying

    @method()
    def StartNotify(self):
        self._notifying = True
        try:
            logging.getLogger('ble-gatt').info(
                "StartNotify [%s] path=%s", self.uuid, self.path
            )
        except Exception:
            logging.getLogger('ble-gatt').exception("StartNotify 로깅 실패")
        # Notifying=True 상태 변경 브로드캐스트
        try:
            self.emit_properties_changed({'Notifying': True}, [])
        except Exception:
            logging.getLogger('ble-gatt').exception("StartNotify PropertiesChanged 실패")
        # 직렬화 워커 시작
        try:
            if isinstance(self._notify_queue, asyncio.Queue) and not self._notify_worker_running:
                loop = asyncio.get_event_loop()
                self._notify_worker_running = True
                self._notify_worker_task = loop.create_task(self._notify_worker())
        except Exception:
            logging.getLogger('ble-gatt').exception("Notify 워커 시작 실패")

    @method()
    def StopNotify(self):
        self._notifying = False
        try:
            logging.getLogger('ble-gatt').info(
                "StopNotify  [%s] path=%s", self.uuid, self.path
            )
        except Exception:
            logging.getLogger('ble-gatt').exception("StopNotify 로깅 실패")
        # Notifying=False 상태 변경 브로드캐스트
        try:
            self.emit_properties_changed({'Notifying': False}, [])
        except Exception:
            logging.getLogger('ble-gatt').exception("StopNotify PropertiesChanged 실패")
        # 직렬화 워커 중지
        try:
            self._notify_worker_running = False
            self._notify_worker_task = None
        except Exception:
            logging.getLogger('ble-gatt').exception("Notify 워커 중지 실패")

    @method()
    def ReadValue(self, options: 'a{sv}') -> 'ay':  # type: ignore
        return list(self._value)

    @method()
    def WriteValue(self, value: 'ay', options: 'a{sv}'):  # type: ignore
        # override
        pass

    def _enqueue_notify(self, value: bytes) -> None:
        """응답을 큐에 넣어 직렬화 전송."""
        try:
            if isinstance(self._notify_queue, asyncio.Queue):
                self._notify_queue.put_nowait(value)
            else:
                # 폴백: 즉시 전송
                self._notify_value(value)
        except Exception:
            logging.getLogger('ble-gatt').exception("enqueue 실패")

    async def _notify_worker(self) -> None:
        lg = logging.getLogger('ble-gatt')
        try:
            while self._notify_worker_running:
                try:
                    data: bytes = await self._notify_queue.get()  # type: ignore
                except Exception:
                    break
                try:
                    if not self._notifying:
                        continue
                    # 청크를 동기 순서로 전송
                    lg.info(
                        "Notify-serialized [%s] total_bytes=%d chunk_size=%d",
                        self.uuid, len(data), MAX_CHUNK
                    )
                    for off in range(0, len(data), MAX_CHUNK):
                        chunk = data[off:off + MAX_CHUNK]
                        try:
                            preview_text = chunk[:128].decode('utf-8', 'replace')
                        except Exception:
                            lg.exception("Notify-chunk 프리뷰 디코드 실패")
                            preview_text = ''
                        preview_hex = chunk[:32].hex()
                        try:
                            lg.info(
                                "Notify-chunk [%s] off=%d len=%d/%d preview=%s hex=%s",
                                self.uuid, off, len(chunk), len(data), preview_text, preview_hex
                            )
                        except Exception:
                            lg.exception("Notify-chunk 프리뷰 로깅 실패")
                        try:
                            self.emit_properties_changed({'Value': bytes(chunk)}, [])
                        except Exception:
                            lg.exception(
                                "Notify-chunk error [%s] off=%d len=%d/%d",
                                self.uuid, off, len(chunk), len(data)
                            )
                        await asyncio.sleep(0.05)
                finally:
                    try:
                        self._notify_queue.task_done()  # type: ignore
                    except Exception:
                        pass
        except Exception:
            lg.exception("Notify 워커 오류")

    def _enqueue_framed(self, kind: str, payload: bytes) -> None:
        """프레이밍된 메시지를 직렬화 큐에 넣어 전송.

        - 헤더: FRAME_MAGIC(4B) + length(4B, big-endian) + kind(1B: 0x01=ACK, 0x02=RESULT)
        - 바디: payload(JSON bytes)
        - 종료: FRAME_END(7B)
        """
        try:
            kind_flag = b'\x01' if kind == 'ack' else b'\x02'
            length = len(payload).to_bytes(4, 'big')
            header = FRAME_MAGIC + length + kind_flag
            self._enqueue_notify(header)
            self._enqueue_notify(payload)
            self._enqueue_notify(FRAME_END)
        except Exception:
            logging.getLogger('ble-gatt').exception("프레이밍 전송 enqueue 실패")


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
                'Flags': Variant('as', ['write', 'write-without-response', 'notify']),
                'Value': Variant('ay', [])
            }
        }
        managed[EQUIP_CHAR_PATH] = {
            'org.bluez.GattCharacteristic1': {
                'UUID': Variant('s', EQUIPMENT_SETTINGS_CHAR_UUID),
                'Service': Variant('o', SERVICE_PATH),
                'Flags': Variant('as', ['write', 'write-without-response', 'notify']),
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
        # 요청(Write) 프리뷰 로깅
        try:
            preview = raw[:256].decode('utf-8', 'replace')
            logging.getLogger('ble-gatt').info(
                "Write [%s] bytes=%d preview=%s", self.uuid, len(raw), preview
            )
        except Exception:
            logging.getLogger('ble-gatt').exception(
                "Write 프리뷰 로깅 실패 (non-utf8 처리) [%s] bytes=%d", self.uuid, len(raw)
            )
        try:
            msg = json.loads(raw.decode('utf-8', 'ignore'))
            mtype = str(msg.get('type', '')).lower()
        except Exception:
            logging.getLogger('ble-gatt').exception("WriteValue JSON 파싱 실패")
            rsp = {"type": "wifi_scan_result", "data": {"success": False, "error": "invalid_json"}, "timestamp": _now_ts()}
            self._notify_value(_json_bytes(rsp))
            return
        
        
        # 라즈베리파이에서 네트워크 스캔 결과 반환
        if mtype == 'wifi_scan':
            # 즉시 ACK
            try:
                ack = {
                    "ver": int(msg.get('ver', 1)),
                    "id": msg.get('id') or "",
                    "type": "wifi_scan_ack",
                    "ts": _now_ms_ext(),
                }
                # ACK 프레이밍 전송
                self._enqueue_framed('ack', _json_bytes_ext(ack))
            except Exception:
                logging.getLogger('ble-gatt').exception("wifi_scan ACK 전송 실패")

            # 결과는 ACK 송신 후 잠시 대기하여 직렬화
            import time as _t
            _t.sleep(0.2)
            nets = _scan_wifi_networks_ext()
            # RSSI 내림차순 정렬 후 상위 15개만 반환
            try:
                nets_sorted = sorted(nets, key=lambda n: n.get('rssi', -100), reverse=True)
                nets_top = nets_sorted[:15]
            except Exception:
                logging.getLogger('ble-gatt').exception("Wi-Fi 스캔 결과 정렬 실패")
                nets_top = nets[:15]
            rsp = {"type": "wifi_scan_result", "data": nets_top, "timestamp": _now_ts()}
            self._enqueue_framed('result', _json_bytes(rsp))
        
        # 네트워크 상태 조회
        elif mtype == 'get_network_status':
            # 즉시 ACK
            try:
                ack = {
                    "ver": int(msg.get('ver', 1)),
                    "id": msg.get('id') or "",
                    "type": "get_network_status_ack",
                    "ts": _now_ms_ext(),
                }
                self._enqueue_framed('ack', _json_bytes_ext(ack))
            except Exception:
                logging.getLogger('ble-gatt').exception("get_network_status ACK 전송 실패")

            # 결과는 ACK 송신 후 잠시 대기하여 직렬화
            import time as _t
            _t.sleep(0.2)
            status = _get_network_status_ext()
            rsp = {"type": "get_network_status_result", "data": status, "timestamp": _now_ts_ext()}
            payload = _json_bytes(rsp)
            self._enqueue_framed('result', payload)
        
        # 네트워크 연결
        elif mtype == 'wifi_register':
            payload_in = msg.get('data') or {}
            # 즉시 ACK
            try:
                ack = {
                    "ver": int(msg.get('ver', 1)),
                    "id": msg.get('id') or "",
                    "type": "wifi_register_ack",
                    "ts": _now_ms_ext(),
                }
                self._enqueue_framed('ack', _json_bytes_ext(ack))
            except Exception:
                logging.getLogger('ble-gatt').exception("wifi_register ACK 전송 실패")

            # 결과는 ACK 송신 후 잠시 대기하여 직렬화
            import time as _t
            _t.sleep(0.2)
            try:
                use_nm = _nm_is_running_ext()
            except Exception:
                use_nm = False
            res = _nm_connect_immediate_ext(payload_in) if use_nm else _wpa_connect_immediate_ext(payload_in, persist=False)
            rsp = {
                "ver": int(msg.get('ver', 1)),
                "id": msg.get('id') or "",
                "type": "wifi_register_result",
                "ts": _now_ms_ext(),
                "data": {
                    "ok": bool(res.get('ok')),
                    "message": res.get('message', ''),
                    "ssid": res.get('ssid', str(payload_in.get('ssid', '')))
                }
            }
            self._enqueue_framed('result', _json_bytes_ext(rsp))
        else:
            rsp = {"type": "wifi_error", "data": {"success": False, "error": "unknown_type", "type": mtype}, "timestamp": _now_ts()}
            self._notify_value(_json_bytes(rsp))


class EquipmentSettingsChar(GattCharacteristic):
    def __init__(self):
        super().__init__(EQUIPMENT_SETTINGS_CHAR_UUID, ['write','write-without-response', 'notify'], EQUIP_CHAR_PATH)
        self._settings: Dict[str, Any] = {}

    @method()
    def WriteValue(self, value: 'ay', options: 'a{sv}'):
        raw = bytes(value)
        # 요청(Write) 프리뷰 로깅
        try:
            preview = raw[:256].decode('utf-8', 'replace')
            logging.getLogger('ble-gatt').info(
                "Write [%s] bytes=%d preview=%s", self.uuid, len(raw), preview
            )
        except Exception:
            logging.getLogger('ble-gatt').info(
                "Write [%s] bytes=%d (non-utf8)", self.uuid, len(raw)
            )
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


class NoIOAgent(ServiceInterface):
    """BlueZ Agent1 구현 - NoInputNoOutput (자동 수락)"""
    def __init__(self, path: str = '/org/factor/agent'):
        super().__init__('org.bluez.Agent1')
        self.path = path

    @method()
    def Release(self):
        pass

    @method()
    def RequestPinCode(self, device: 'o') -> 's':  # type: ignore
        return ''

    @method()
    def RequestPasskey(self, device: 'o') -> 'u':  # type: ignore
        return 0

    @method()
    def DisplayPasskey(self, device: 'o', passkey: 'u', entered: 'y'):  # type: ignore
        pass

    @method()
    def DisplayPinCode(self, device: 'o', pincode: 's'):  # type: ignore
        pass

    @method()
    def RequestConfirmation(self, device: 'o', passkey: 'u'):  # type: ignore
        # 숫자 비교 자동 승인
        return

    @method()
    def AuthorizeService(self, device: 'o', uuid: 's'):  # type: ignore
        # 서비스 사용 허용
        return

    @method()
    def Cancel(self):
        pass

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
        logging.getLogger('ble-gatt').exception("DBus 이름 요청 실패(org.factor.gatt)")

    adapter_path = '/org/bluez/hci0'
    obj = await bus.introspect(BLUEZ, adapter_path)
    adapter = bus.get_proxy_object(BLUEZ, adapter_path, obj)
    gatt_mgr = adapter.get_interface(GATT_MANAGER_IFACE)
    # 광고 매니저는 일부 환경에서 -E 필요 → 실패해도 무시
    adv_mgr = None
    try:
        adv_mgr = adapter.get_interface(LE_ADV_MANAGER_IFACE)
    except Exception:
        logging.getLogger('ble-gatt').exception("LEAdvertisingManager1 인터페이스 획득 실패")
        adv_mgr = None

    # PropertiesChanged 브로드캐스트용 인터페이스
    props_iface = adapter.get_interface('org.freedesktop.DBus.Properties')
    # 어댑터 전원 켜기 (필요 시)
    try:
        await props_iface.call_set('org.bluez.Adapter1', 'Powered', Variant('b', True))
        logger.info('Adapter Powered=on')
    except Exception:
        logging.getLogger('ble-gatt').exception("Adapter 전원 On 실패")

    # ObjectManager + 서비스/특성 export
    obj_manager = ObjectManager(SERVICE_UUID)
    bus.export(APP_PATH, obj_manager)
    svc = GattService(SERVICE_UUID)
    wifi_char = WifiRegisterChar()
    equip_char = EquipmentSettingsChar()

    # Agent 등록 (자동 수락)
    try:
        root_obj = await bus.introspect(BLUEZ, '/org/bluez')
        root = bus.get_proxy_object(BLUEZ, '/org/bluez', root_obj)
        agent_mgr = root.get_interface('org.bluez.AgentManager1')

        agent = NoIOAgent()
        agent_path = agent.path
        bus.export(agent_path, agent)

        await agent_mgr.call_register_agent(agent_path, 'DisplayYesNo')
        await agent_mgr.call_request_default_agent(agent_path)
        logger.info("BLE Agent 등록 완료 (capability=DisplayYesNo, path=%s)", agent_path)
    except Exception:
        logging.getLogger('ble-gatt').exception("BLE Agent 등록 실패")

    # 구성 로그: 서비스/특성/플래그
    try:
        logger.info(
            "BLE GATT 구성 - service=%s, chars=[{%s:%s}, {%s:%s}]",
            SERVICE_UUID,
            WIFI_REGISTER_CHAR_UUID,
            ','.join(['write','write-without-response', 'notify']),
            EQUIPMENT_SETTINGS_CHAR_UUID,
            ','.join(['write','write-without-response', 'notify'])
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
        logging.getLogger('ble-gatt').exception("BLE GATT Application 등록 완료 로그 실패")

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
                logging.getLogger('ble-gatt').exception("BLE 광고 등록 완료 로그 실패")
        except Exception:
            logging.getLogger('ble-gatt').exception(
                "BLE 광고 등록 실패: 중복 광고 서비스(ble-headless 등) 또는 권한/experimental(-E) 확인 필요"
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
            logging.getLogger('ble-gatt').exception("광고 해제 실패")
        try:
            await gatt_mgr.call_unregister_application(APP_PATH)
        except Exception:
            logging.getLogger('ble-gatt').exception("GATT 앱 해제 실패")


def start_ble_gatt_server(logger: logging.Logger) -> None:
    """비동기 BLE GATT 서버를 백그라운드에서 실행"""
    import threading
    import atexit
    import functools

    def runner():
        try:
            asyncio.run(_async_run(logger))
        except Exception:
            logger.exception("BLE GATT 서버 실행 오류")

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
                logging.getLogger('ble-gatt').exception("GATT 앱 해제 호출 실패")
            try:
                adv_mgr = adapter.get_interface(LE_ADV_MANAGER_IFACE)
                await adv_mgr.call_unregister_advertisement(ADV_PATH)
            except Exception:
                logging.getLogger('ble-gatt').exception("광고 해제 호출 실패")
        except Exception:
            logging.getLogger('ble-gatt').exception("정리 루틴(bus 연결/조회) 실패")

    def _atexit():
        try:
            asyncio.run(_cleanup())
        except Exception:
            logging.getLogger('ble-gatt').exception("종료 훅(_atexit) 실패")

    atexit.register(_atexit)


