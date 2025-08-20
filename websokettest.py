# random_edge_client.py
import asyncio
import json
import random
import websockets

WS_URL = "ws://localhost:3000"
HEADERS = {
    "x-client-type": "edge",
    "User-Agent": "python-edge-client/1.0"
}

async def send(ws, type_, data):
    """메시지 전송"""
    await ws.send(json.dumps({"type": type_, "data": data}))

async def random_data_loop(ws):
    """1~2초 간격으로 랜덤 데이터 전송"""
    while True:
        # 랜덤 상태 생성
        temperature = {
            "tool": {"current": round(random.uniform(20, 250), 1), "target": round(random.uniform(0, 250), 1)},
            "bed":  {"current": round(random.uniform(20, 100), 1), "target": round(random.uniform(0, 100), 1)}
        }
        position = {
            "x": round(random.uniform(0, 220), 2),
            "y": round(random.uniform(0, 220), 2),
            "z": round(random.uniform(0, 200), 2),
            "e": round(random.uniform(0, 500), 2)
        }
        progress = {
            "completion": round(random.uniform(0, 100), 2),
            "file_position": random.randint(0, 1000000),
            "file_size": 1000000,
            "print_time": random.randint(0, 7200),
            "print_time_left": random.randint(0, 7200),
            "filament_used": random.randint(0, 5000)
        }
        status = random.choice(["idle", "printing", "paused", "error"])

        # 서버로 전송
        await send(ws, "temperature_update", temperature)
        await send(ws, "position_update", position)
        await send(ws, "print_progress", progress)
        await send(ws, "printer_status", {
            "status": status,
            "connected": True,
            "printing": status == "printing",
            "error_message": None if status != "error" else "Nozzle jam detected",
            "temperature": temperature,
            "position": position,
            "printProgress": progress
        })

        print(f"[전송 완료] status={status}, temp={temperature['tool']['current']}°C, completion={progress['completion']}%")
        await asyncio.sleep(random.uniform(1.0, 2.0))  # 1~2초 대기

async def recv_loop(ws):
    """서버가 보내는 메시지 출력"""
    async for msg in ws:
        try:
            data = json.loads(msg)
            print(f"[서버 -> 클라] {data.get('type')}")
        except json.JSONDecodeError:
            print("[서버 메시지] (JSON 아님)", msg)

async def main():
    while True:
        try:
            print(f"Connecting to {WS_URL} ...")
            async with websockets.connect(WS_URL, extra_headers=HEADERS) as ws:
                print("✅ 연결 성공")
                await asyncio.gather(
                    random_data_loop(ws),
                    recv_loop(ws)
                )
        except Exception as e:
            print("❌ 연결 끊김:", e)
            await asyncio.sleep(3)

if __name__ == "__main__":
    asyncio.run(main())