import socket
import threading
import time
from typing import List


class B_RFCOMMServer:
    """
    블루투스 클래식 SPP(RFCOMM) 최소 서버
    - 표준 라이브러리만 사용 (PyBluez 불필요)
    - SDP/광고 없이 RFCOMM 채널 고정(기본 1)
    - 줄바꿈(\n) 단위 텍스트 프로토콜: ping -> pong, echo <text> -> <text>, 기타 -> ok
    """

    def __init__(self, channel: int = 1):
        self.channel = channel
        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._running = False
        self._clients: List[socket.socket] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return
        self._server = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        # 빈 문자열("")은 로컬 어댑터(any) 바인딩
        self._server.bind(("", self.channel))
        self._server.listen(1)
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            for cs in self._clients:
                try:
                    cs.close()
                except Exception:
                    pass
            self._clients.clear()
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None

    def _accept_loop(self) -> None:
        while self._running and self._server:
            try:
                client_sock, _client_addr = self._server.accept()
                with self._lock:
                    self._clients.append(client_sock)
                t = threading.Thread(target=self._client_loop, args=(client_sock,), daemon=True)
                t.start()
            except OSError:
                break
            except Exception:
                time.sleep(0.2)

    def _client_loop(self, sock: socket.socket) -> None:
        sock.settimeout(2.0)
        buffer = b""
        try:
            while self._running:
                try:
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        text = line.decode("utf-8", "replace").strip()
                        if not text:
                            continue
                        # 초간단 프로토콜
                        if text.lower() == "ping":
                            self._send_line(sock, "pong")
                        elif text.lower().startswith("echo "):
                            self._send_line(sock, text[5:])
                        else:
                            self._send_line(sock, "ok")
                except socket.timeout:
                    continue
                except Exception:
                    break
        finally:
            try:
                sock.close()
            except Exception:
                pass
            with self._lock:
                if sock in self._clients:
                    self._clients.remove(sock)

    def _send_line(self, sock: socket.socket, text: str) -> None:
        try:
            sock.sendall((text + "\n").encode("utf-8", "ignore"))
        except Exception:
            pass


