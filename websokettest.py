import asyncio
import websockets
import json
import random


async def send_test_data():
    """
    웹소켓 서버에 'x-client-type: edge' 헤더를 포함하여
    다양한 유형의 프린터 상태 데이터를 무작위로 전송하는 클라이언트 함수
    """
    uri = "ws://localhost:3000"
    headers = {"x-client-type": "edge"}

    try:
        async with websockets.connect(uri, extra_headers=headers) as websocket:
            print(f"웹소켓 서버에 'edge' 클라이언트로 연결되었습니다: {uri}")

            while True:
                # 1. 무작위 PrinterStatus 메시지 생성 및 전송
                printer_status_data = {
                    "type": "printer_status",
                    "data": {
                        "status": random.choice(["printing", "idle", "paused", "error"]),
                        "connected": True,
                        "printing": random.choice([True, False]),
                        "error_message": "Filament runout" if random.random() > 0.9 else None
                    }
                }
                await websocket.send(json.dumps(printer_status_data))
                print(f"[printer_status] 전송: {printer_status_data['data']}")

                await asyncio.sleep(random.uniform(0.5, 1.5))  # 메시지 간 간격

                # 2. 무작위 TemperatureData 메시지 생성 및 전송
                temperature_data = {
                    "type": "temperature_update",
                    "data": {
                        "tool": {"current": round(random.uniform(190, 205), 1), "target": 200},
                        "bed": {"current": round(random.uniform(55, 65), 1), "target": 60}
                    }
                }
                await websocket.send(json.dumps(temperature_data))
                print(f"[temperature_update] 전송: {temperature_data['data']}")

                await asyncio.sleep(random.uniform(0.5, 1.5))

                # 3. 무작위 PositionData 메시지 생성 및 전송
                position_data = {
                    "type": "position_update",
                    "data": {
                        "x": round(random.uniform(0, 250), 2),
                        "y": round(random.uniform(0, 250), 2),
                        "z": round(random.uniform(0, 250), 2),
                        "e": round(random.uniform(0, 500), 2)
                    }
                }
                await websocket.send(json.dumps(position_data))
                print(f"[position_update] 전송: {position_data['data']}")

                await asyncio.sleep(random.uniform(0.5, 1.5))

                # 4. 무작위 PrintProgressData 메시지 생성 및 전송
                print_progress_data = {
                    "type": "print_progress",
                    "data": {
                        "completion": round(random.uniform(0, 100), 1),
                        "file_position": random.randint(0, 1024),
                        "file_size": 1024,
                        "print_time": random.randint(0, 3600),
                        "print_time_left": random.randint(0, 3600),
                        "filament_used": round(random.uniform(0, 50), 1)
                    }
                }
                await websocket.send(json.dumps(print_progress_data))
                print(f"[print_progress] 전송: {print_progress_data['data']}")

                print("-" * 50)
                await asyncio.sleep(5)  # 다음 전체 메시지 세트 전송까지 5초 대기

    except websockets.exceptions.ConnectionClosed as e:
        print(f"연결이 끊겼습니다: {e}")
    except Exception as e:
        print(f"오류 발생: {e}")


if __name__ == "__main__":
    asyncio.run(send_test_data())