import asyncio
import json
import cv2
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, RTCIceCandidate
import av

WS_SERVER_URI = "ws://localhost:3000"
CAMERA_ID = 0
ROOM_ID = "camera:CAM-001"  # 웹과 동일하게 맞추기

video_capture = cv2.VideoCapture(CAMERA_ID)
pc = None

class VideoTrack(VideoStreamTrack):
    def __init__(self):
        super().__init__()

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        ret, frame = video_capture.read()
        if not ret:
            raise StopAsyncIteration
        new_frame = av.VideoFrame.from_ndarray(frame, format="bgr24")
        new_frame.pts = pts
        new_frame.time_base = time_base
        return new_frame


async def run_publisher_client():
    global pc
    headers = {"x-client-type": "edge"}

    try:
        async with websockets.connect(WS_SERVER_URI, extra_headers=headers) as ws:
            print(f"웹소켓 서버에 연결되었습니다: {WS_SERVER_URI}")

            await ws.send(json.dumps({"type": "webrtc_join", "data": {"roomId": ROOM_ID, "role": "publisher"}}))
            print(f"[{ROOM_ID}] 퍼블리셔로 참여 요청")

            pc = RTCPeerConnection()
            pc.addTrack(VideoTrack())

            async def send_offer():
                offer = await pc.createOffer()
                await pc.setLocalDescription(offer)
                while pc.iceGatheringState != "complete":
                    await asyncio.sleep(0.05)

                await ws.send(json.dumps({
                    "type": "webrtc_offer",
                    "data": {
                        "roomId": ROOM_ID,
                        "sdp": pc.localDescription.sdp,
                        "type": pc.localDescription.type
                    }
                }))
                print("offer 전송 완료")

            # --- 수정된 부분 ---
            async for message in ws:
                msg = json.loads(message)
                t, d = msg.get("type"), msg.get("data", {})
                if d.get("roomId") != ROOM_ID:
                    continue

                if t == "webrtc_peer_joined":
                    print("뷰어 합류 감지 → offer 생성")
                    await send_offer()

                elif t == "webrtc_answer":
                    print("answer 수신 → remoteDescription 설정")

                    # 수정된 부분: d 딕셔너리에서 필요한 인자만 추출
                    answer_data = {
                        "sdp": d.get("sdp"),
                        "type": d.get("type")
                    }

                    # RTCSessionDescriptionInit으로 사용 가능한 딕셔너리 전달
                    answer = RTCSessionDescription(**answer_data)

                    await pc.setRemoteDescription(answer)

                elif t == "webrtc_ice_candidate":
                    print("ICE 후보자 수신")
                    candidate_dict = d.get("candidate")
                    if candidate_dict:
                        try:
                            # 딕셔너리를 사용하여 RTCIceCandidate 객체 생성
                            # aiortc가 자동으로 변환합니다.
                            candidate = RTCIceCandidate(**candidate_dict)
                            await pc.addIceCandidate(candidate)
                            print("remote ICE 후보 추가")
                        except Exception as e:
                            print(f"ICE 후보 추가 실패: {e}")
            # --- 수정된 부분 끝 ---

    except websockets.exceptions.ConnectionClosed as e:
        print("웹소켓 연결 종료:", e)
    except Exception as e:
        print("오류 발생:", e)
    finally:
        if pc:
            await pc.close()
        video_capture.release()


if __name__ == "__main__":
    asyncio.run(run_publisher_client())