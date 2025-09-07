"""
설비 정보 조회 모듈
프린터, 카메라, 소프트웨어 정보를 수집하여 반환
"""

import json
import time
import subprocess
import logging
from typing import Dict, Any, Optional
from datetime import datetime
import os
import psutil

# 프린터 통신 모듈 import
try:
    from ..printer_comm import PrinterCommunicator, PrinterState
    from ..client import FactorClient
    from ..config_manager import ConfigManager
    _HAS_PRINTER_MODULES = True
except ImportError:
    _HAS_PRINTER_MODULES = False


def get_equipment_info() -> Dict[str, Any]:
    """현재 연결된 설비의 정보를 조회하여 반환
    
    Returns:
        Dict[str, Any]: 설비 정보 딕셔너리
    """
    equipment_info = {
        "equipment": {
            "printer": get_printer_info(),
            "camera": get_camera_info(),
            "software": get_software_info()
        }
    }
    
    return equipment_info


def get_printer_info() -> Dict[str, Any]:
    """프린터 정보 조회
    
    Returns:
        Dict[str, Any]: 프린터 정보
    """
    printer_info = {
        "status": False,
        "model": "Unknown",
        "firmware": "Unknown", 
        "serial_port": "",
        "baud_rate": 115200,
        "uuid": None
    }
    
    try:
        if not _HAS_PRINTER_MODULES:
            printer_info["status"] = False
            printer_info["message"] = "Printer modules not available"
            return printer_info
            
        # 설정 파일에서 프린터 정보 읽기
        config_manager = ConfigManager()
        printer_config = config_manager.get('printer', {})
        
        printer_info["serial_port"] = printer_config.get('port', '')
        printer_info["baud_rate"] = printer_config.get('baudrate', 115200)
        
        # 프린터 연결 상태 확인
        if printer_info["serial_port"]:
            # 시리얼 포트 존재 확인
            if os.path.exists(printer_info["serial_port"]):
                # 프린터 통신 객체 생성하여 연결 상태 확인
                try:
                    pc = PrinterCommunicator(
                        port=printer_info["serial_port"],
                        baudrate=printer_info["baud_rate"]
                    )
                    
                    # 간단한 연결 테스트
                    if pc.serial_conn is None:
                        # 연결 시도
                        try:
                            import serial
                            pc.serial_conn = serial.Serial(
                                port=printer_info["serial_port"],
                                baudrate=printer_info["baud_rate"],
                                timeout=0.5,
                                write_timeout=1
                            )
                        except Exception as e:
                            logging.getLogger('ble-gatt').warning(f"시리얼 연결 실패: {e}")
                            pc.serial_conn = None
                    
                    if pc.serial_conn and pc.serial_conn.is_open:
                        printer_info["status"] = True
                        
                        # 프린터 정보 조회 시도
                        try:
                            # M115 명령으로 프린터 정보 조회
                            pc.serial_conn.write(b"M115\n")
                            pc.serial_conn.flush()
                            
                            # 응답 대기 및 여러 라인 읽기
                            time.sleep(0.5)
                            responses = []
                            while pc.serial_conn.in_waiting > 0:
                                response = pc.serial_conn.readline().decode('utf-8', 'ignore').strip()
                                if response:
                                    responses.append(response)
                            
                            # ===== M115 KV 파싱 (collection 유틸로 위임) =====
                            from core.core_collection import DataCollectionModule
                            kv = DataCollectionModule.extract_m115_kv_from_lines(responses)
                            fw = kv.get("FIRMWARE_NAME")
                            if fw:
                                printer_info["firmware"] = fw
                            model = kv.get("MACHINE_TYPE")
                            if model:
                                printer_info["model"] = model
                            uuid_val = kv.get("UUID")
                            if uuid_val:
                                printer_info["uuid"] = uuid_val
                            proto = kv.get("PROTOCOL_VERSION")
                            if proto:
                                printer_info["protocol_version"] = proto
                            ec = kv.get("EXTRUDER_COUNT")
                            if ec:
                                try:
                                    printer_info["extruder_count"] = int(ec)
                                except ValueError:
                                    pass
                            src = kv.get("SOURCE_CODE_URL")
                            if src:
                                printer_info["source_code_url"] = src

                            # ===== UUID를 설정 파일에 저장(변경 시 갱신) =====
                            try:
                                cm = ConfigManager()
                                cm.update_equipment_uuid(printer_info.get('uuid'))
                            except Exception as e:
                                logging.getLogger('ble-gatt').warning(f"설비 UUID 저장 실패: {e}")
                            
                            # MACHINE_TYPE이 없으면 FIRMWARE_NAME에서 모델명 추출 시도
                            if not printer_info.get("model") or printer_info["model"] == "Unknown":
                                if printer_info.get("firmware"):
                                    if "Ender-3 V3 SE" in printer_info["firmware"]:
                                        printer_info["model"] = "Creality Ender-3 V3 SE"
                                    elif "Ender-3 V2 Neo" in printer_info["firmware"]:
                                        printer_info["model"] = "Creality Ender-3 V2 Neo"
                                    elif "Ender-3" in printer_info["firmware"]:
                                        printer_info["model"] = "Creality Ender-3"
                                    elif "Ender-5" in printer_info["firmware"]:
                                        printer_info["model"] = "Creality Ender-5"
                                    elif "Prusa" in printer_info["firmware"]:
                                        printer_info["model"] = "Prusa i3"
                                    elif "Ultimaker" in printer_info["firmware"]:
                                        printer_info["model"] = "Ultimaker"
                                    else:
                                        printer_info["model"] = "Unknown 3D Printer"
                            
                            # 기존 펌웨어 타입 확인 (Klipper, RepRapFirmware 등)
                            for response in responses:
                                if "Klipper" in response:
                                    printer_info["firmware"] = "Klipper"
                                    if not printer_info.get("model") or printer_info["model"] == "Unknown 3D Printer":
                                        printer_info["model"] = "Klipper-based Printer"
                                    break
                                elif "RepRapFirmware" in response:
                                    printer_info["firmware"] = "RepRapFirmware"
                                    if not printer_info.get("model") or printer_info["model"] == "Unknown 3D Printer":
                                        printer_info["model"] = "RepRap-based Printer"
                                    break
                                elif "Marlin" in response:
                                    printer_info["firmware"] = "Marlin"
                                    if not printer_info.get("model") or printer_info["model"] == "Unknown 3D Printer":
                                        printer_info["model"] = "RepRap-based Printer"
                                    break
                            
                            # 설정 파일에서 모델 정보 확인
                            if printer_info["model"] == "Unknown 3D Printer":
                                model_from_config = printer_config.get('model', '')
                                if model_from_config:
                                    printer_info["model"] = model_from_config
                                    
                        except Exception as e:
                            logging.getLogger('ble-gatt').warning(f"프린터 정보 조회 실패: {e}")
                            printer_info["model"] = "Unknown 3D Printer"
                            printer_info["firmware"] = "Unknown"
                        
                        # 연결 해제
                        pc.serial_conn.close()
                    else:
                        printer_info["status"] = False
                        printer_info["message"] = "Serial port not accessible"
                        
                except Exception as e:
                    logging.getLogger('ble-gatt').warning(f"프린터 연결 테스트 실패: {e}")
                    printer_info["status"] = False
                    printer_info["message"] = str(e)
            else:
                printer_info["status"] = False
                printer_info["message"] = "Serial port not found"
        else:
            printer_info["status"] = False
            printer_info["message"] = "No printer port configured"
            
    except Exception as e:
        logging.getLogger('ble-gatt').exception("프린터 정보 조회 중 오류")
        printer_info["status"] = False
        printer_info["message"] = str(e)
    
    return printer_info


def get_camera_info() -> Dict[str, Any]:
    """카메라 정보 조회
    
    Returns:
        Dict[str, Any]: 카메라 정보
    """
    camera_info = {
        "status": False,
        "model": "Unknown",
        "resolution": "Unknown",
        "fps": 0,
        "stream_url": ""
    }
    
    try:
        # 라즈베리파이 카메라 모듈 확인
        if os.path.exists("/dev/video0"):
            camera_info["status"] = True
            camera_info["model"] = "Raspberry Pi Camera Module"
            
            # 카메라 해상도 확인
            try:
                result = subprocess.run(
                    ["v4l2-ctl", "--list-formats-ext", "-d", "/dev/video0"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    # 해상도 정보 파싱
                    output = result.stdout
                    if "1920x1080" in output:
                        camera_info["resolution"] = "1920x1080"
                        camera_info["fps"] = 30
                    elif "1280x720" in output:
                        camera_info["resolution"] = "1280x720"
                        camera_info["fps"] = 30
                    else:
                        camera_info["resolution"] = "Unknown"
                        camera_info["fps"] = 0
                else:
                    camera_info["resolution"] = "Unknown"
                    camera_info["fps"] = 0
            except Exception:
                camera_info["resolution"] = "Unknown"
                camera_info["fps"] = 0
            
            # 스트림 URL 설정
            camera_info["stream_url"] = "http://localhost:8080/stream"
            
        else:
            camera_info["status"] = False
            camera_info["message"] = "Camera device not found"
            
    except Exception as e:
        logging.getLogger('ble-gatt').exception("카메라 정보 조회 중 오류")
        camera_info["status"] = False
        camera_info["message"] = str(e)
    
    return camera_info


def get_software_info() -> Dict[str, Any]:
    """소프트웨어 정보 조회
    
    Returns:
        Dict[str, Any]: 소프트웨어 정보
    """
    software_info = {
        "firmware_version": "1.0.0",
        "api_version": "1.0.0",
        "last_update": datetime.now().isoformat() + "Z",
        "update_available": False,
        "uuid": None
    }
    
    try:
        # 버전 정보 파일 확인
        version_file = "version.json"
        if os.path.exists(version_file):
            with open(version_file, 'r') as f:
                version_data = json.load(f)
                software_info["firmware_version"] = version_data.get("version", "1.0.0")
                software_info["api_version"] = version_data.get("api_version", "1.0.0")
        
        # 시스템 정보 추가
        software_info["system"] = {
            "platform": "Raspberry Pi",
            "python_version": f"{psutil.sys.version_info.major}.{psutil.sys.version_info.minor}.{psutil.sys.version_info.micro}",
            "uptime": int(time.time() - psutil.boot_time())
        }
        
        # 라즈베리파이 모델 정보 조회
        try:
            with open('/proc/cpuinfo', 'r') as f:
                cpuinfo = f.read()
                if 'Raspberry Pi' in cpuinfo:
                    for line in cpuinfo.split('\n'):
                        if 'Model' in line:
                            model = line.split(':')[1].strip()
                            software_info["system"]["hardware_model"] = model
                            break
        except Exception:
            software_info["system"]["hardware_model"] = "Unknown"
        
        # OS 정보 조회
        try:
            import platform
            software_info["system"]["os"] = f"{platform.system()} {platform.release()}"
        except Exception:
            software_info["system"]["os"] = "Unknown"
        
        # 라즈베리파이 고유 시리얼을 uuid로 설정
        try:
            serial_paths = [
                "/proc/device-tree/serial-number",
                "/sys/firmware/devicetree/base/serial-number"
            ]
            for spath in serial_paths:
                if os.path.exists(spath):
                    with open(spath, "rb") as f:
                        raw = f.read()
                        value = raw.decode("utf-8", "ignore").replace("\x00", "").strip()
                        if value:
                            software_info["uuid"] = value
                            break
        except Exception as e:
            logging.getLogger('ble-gatt').warning(f"RPi serial-number 읽기 실패: {e}")
        
        # Factor Client 버전 정보
        try:
            from core import __version__
            software_info["firmware_version"] = __version__
        except ImportError:
            pass
        
    except Exception as e:
        logging.getLogger('ble-gatt').exception("소프트웨어 정보 조회 중 오류")
        software_info["error"] = str(e)
    
    return software_info
