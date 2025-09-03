import subprocess
import time
import logging
from typing import Any, Dict, List


def scan_wifi_networks() -> List[Dict[str, Any]]:
    """주변 Wi‑Fi 네트워크를 스캔하여 요약 리스트 반환.

    - 출력 항목 예: [{"ssid": str, "rssi": int, "security": str}, ...]
    - RSSI, 보안 유형을 가능한 한 파싱하여 채움(실패 시 기본값)
    - 오류 발생 시 예외를 로깅하고 가능한 범위 내 결과 반환
    """
    networks: List[Dict[str, Any]] = []
    try:
        result = subprocess.run(['sudo', 'iwlist', 'wlan0', 'scan'], capture_output=True, text=True, timeout=15)
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
                    logging.getLogger('ble-gatt').exception("iwlist 파싱(ESSID) 실패")
                continue
            if 'Signal level=' in line:
                try:
                    import re as _re
                    m = _re.search(r'Signal level=\s*(-?\d+)', line)
                    if m:
                        current['rssi'] = int(m.group(1))
                except Exception:
                    logging.getLogger('ble-gatt').exception("iwlist 파싱(RSSI) 실패")
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
                n['security'] = 'Protected' if n.get('_enc') else 'Open'
            if 'rssi' not in n:
                n['rssi'] = -100
            n.pop('_enc', None)
        return networks
    except Exception:
        logging.getLogger('ble-gatt').exception("Wi-Fi 스캔 실패(iwlist)")
        return networks


def get_network_status() -> Dict[str, Any]:
    """현재 네트워크 상태 요약 반환.

    - wifi: {interface, connected, ssid, ip, gateway}
    - ethernet: {interface, connected, ip, gateway}
    - 일부 항목은 사용 환경에 따라 비어 있을 수 있음
    """
    status: Dict[str, Any] = {
        'wifi': {'interface': 'wlan0', 'connected': False, 'ssid': '', 'ip': '', 'gateway': ''},
        'ethernet': {'interface': 'eth0', 'connected': False, 'ip': '', 'gateway': ''},
    }
    try:
        r = subprocess.run(['iwgetid', '-r'], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            ssid = (r.stdout or '').strip()
            if ssid:
                status['wifi']['ssid'] = ssid
                status['wifi']['connected'] = True
    except Exception:
        logging.getLogger('ble-gatt').exception("iwgetid 실행 실패")
    try:
        import psutil  # type: ignore
        addrs = psutil.net_if_addrs()
        def _first_ipv4_addr(ifname: str) -> str:
            for snic in addrs.get(ifname, []) or []:
                import socket as _sock
                if getattr(snic, 'family', None) == getattr(_sock, 'AF_INET', None):
                    return snic.address or ''
            return ''
        status['wifi']['ip'] = _first_ipv4_addr('wlan0')
        status['ethernet']['ip'] = _first_ipv4_addr('eth0')
        if status['ethernet']['ip']:
            status['ethernet']['connected'] = True
    except Exception:
        logging.getLogger('ble-gatt').exception("IP 주소 조회(psutil) 실패")
    try:
        r = subprocess.run(['ip', 'route', 'show', 'default'], capture_output=True, text=True, timeout=3)
        line = (r.stdout or '').splitlines()[0] if (r.returncode == 0 and (r.stdout or '').strip()) else ''
        if line:
            parts = line.split()
            gw = ''
            dev = ''
            if 'via' in parts:
                try:
                    gw = parts[parts.index('via') + 1]
                except Exception:
                    logging.getLogger('ble-gatt').exception("기본 게이트웨이 파싱(via) 실패")
                    gw = ''
            if 'dev' in parts:
                try:
                    dev = parts[parts.index('dev') + 1]
                except Exception:
                    logging.getLogger('ble-gatt').exception("기본 게이트웨이 파싱(dev) 실패")
                    dev = ''
            if dev == 'wlan0':
                status['wifi']['gateway'] = gw
            elif dev == 'eth0':
                status['ethernet']['gateway'] = gw
    except Exception:
        logging.getLogger('ble-gatt').exception("기본 게이트웨이 조회(ip route) 실패")
    return status


def wpa_connect_immediate(data: Dict[str, Any], persist: bool = False) -> Dict[str, Any]:
    """wpa_cli를 사용해 설정 파일 저장 없이 즉시 Wi‑Fi 연결 시도.

    - 입력 data: {ssid, password, security(OPEN|WPA2|WPA3), hidden, priority}
    - persist=False: 저장 없이 런타임 연결만 시도(요청사항 반영)
    - 성공: {ok: True, message: 'connected', ssid}
    - 실패: {ok: False, message: str, ssid, error?}
    """
    ssid = str(data.get('ssid', '')).strip()
    password = str(data.get('password') or '')
    security = str(data.get('security', 'WPA2')).upper()
    hidden = bool(data.get('hidden', False))
    priority = int(data.get('priority', 0))

    if not ssid:
        return {"ok": False, "message": "ssid required"}

    def run(args: List[str], timeout: int = 5) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    try:
        r = run(['wpa_cli', '-i', 'wlan0', 'add_network'])
        if r.returncode != 0 or not (r.stdout or '').strip().isdigit():
            return {"ok": False, "message": f"add_network failed: {r.stdout or r.stderr}"}
        nid = (r.stdout or '').strip()

        def set_net(key: str, value: str):
            rr = run(['wpa_cli', '-i', 'wlan0', 'set_network', nid, key, value])
            if rr.returncode != 0 or 'FAIL' in (rr.stdout or ''):
                raise RuntimeError(f"set_network {key} failed: {rr.stdout or rr.stderr}")

        set_net('ssid', f'"{ssid}"')
        if hidden:
            set_net('scan_ssid', '1')
        if isinstance(priority, int) and priority != 0:
            set_net('priority', str(priority))

        if security == 'OPEN':
            set_net('key_mgmt', 'NONE')
        elif security == 'WPA3':
            if len(password) < 8:
                return {"ok": False, "message": "WPA3 password length < 8", "ssid": ssid}
            set_net('key_mgmt', 'SAE')
            set_net('ieee80211w', '2')
            set_net('sae_password', '"%s"' % password.replace('"', '\\"'))
        else:
            if len(password) < 8:
                return {"ok": False, "message": "WPA2 password length < 8", "ssid": ssid}
            set_net('key_mgmt', 'WPA-PSK')
            set_net('psk', '"%s"' % password.replace('"', '\\"'))

        for cmd in (['enable_network', nid], ['select_network', nid], ['reassociate']):
            rr = run(['wpa_cli', '-i', 'wlan0'] + cmd)
            if rr.returncode != 0 or 'FAIL' in (rr.stdout or ''):
                return {"ok": False, "message": f"{' '.join(cmd)} failed: {rr.stdout or rr.stderr}", "ssid": ssid}

        if persist:
            run(['wpa_cli', '-i', 'wlan0', 'save_config'])

        res = wait_wifi_connected(timeout_sec=25)
        if res.get('ok'):
            return {"ok": True, "message": "connected", "ssid": ssid}
        return {"ok": False, "message": "apply_done_but_not_connected", "ssid": ssid, "error": res.get('error')}
    except Exception:
        logging.getLogger('ble-gatt').exception("wpa_cli 즉시 연결 실패")
        return {"ok": False, "message": "exception", "ssid": ssid}


