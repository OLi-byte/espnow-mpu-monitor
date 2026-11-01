"""USB ESP-NOW receiver -> SQLite -> live browser dashboard with interactive simulator."""

import argparse
import asyncio
import json
import math
import random
import sqlite3
import struct
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import serial
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel
from serial.tools import list_ports

ROOT = Path(__file__).resolve().parent
FRAME = struct.Struct("<4sBIII6f")
DATABASE = ROOT / "packets.sqlite3"
clients: set[WebSocket] = set()
states = defaultdict(
    lambda: {
        "last_seq": None,
        "lost": 0,
        "received": 0,
        "duplicates": 0,
        "out_of_order": 0,
        "last_time": None,
        "last_rssi": None,
        "queue_drops": 0,
        "missing": set(),
    }
)
recent = deque(maxlen=400)
db: sqlite3.Connection | None = None


def list_available_ports() -> list[dict[str, Any]]:
    try:
        ports = list_ports.comports()
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    for p in sorted(ports, key=lambda x: x.device):
        desc = (p.description or "").strip()
        is_usb = bool(
            p.vid is not None
            or any(
                k in desc.upper()
                for k in ("USB", "CP210", "CH340", "CH910", "FTDI", "ESP", "UART")
            )
        )
        items.append(
            {
                "port": p.device,
                "description": desc,
                "is_usb": is_usb,
            }
        )
    return items


def detect_default_port(fallback: str = "COM3") -> str:
    ports = list_available_ports()
    for p in ports:
        if p["port"] == fallback:
            return fallback
    for p in ports:
        if p["is_usb"]:
            return p["port"]
    if ports:
        return ports[0]["port"]
    return fallback


_initial_port = detect_default_port("COM6")


class RuntimeArgs:
    port: str = _initial_port
    demo: bool = False


args = RuntimeArgs()

serial_status: dict[str, Any] = {
    "connected": False,
    "port": _initial_port,
    "error": None,
    "role": None,
    "diag_status": None,
    "diag_msg": None,
    "device_mac": None,
}


class SimulatorState:
    def __init__(self):
        self.enabled: bool = False
        self.paused: bool = False
        self.rate_hz: int = 50
        self.profile: str = "wave"  # wave | rotation | vibration | steps | idle
        self.loss_percent: float = 5.0
        self.out_of_order_percent: float = 1.5
        self.duplicate_percent: float = 0.5
        self.noise_level: float = 1.0
        self.device_count: int = 1
        self.persist_db: bool = True

        # Internal state per virtual device index
        self.devices: dict[int, dict[str, Any]] = {}
        self.delayed_packets: list[tuple[float, dict[str, Any]]] = []
        self.shock_until: float = 0.0
        self.burst_loss_remaining: int = 0
        self.step_once: int = 0

    def ensure_device(self, idx: int) -> dict[str, Any]:
        if idx not in self.devices:
            mac = f"24:6F:28:AA:BB:{idx + 1:02X}" if idx > 0 else "24:6F:28:AA:BB:CC"
            self.devices[idx] = {
                "mac": mac,
                "boot": random.getrandbits(32),
                "seq": 0,
                "start_time": time.monotonic(),
                "queue_drops": 0,
                "phase": idx * 1.1,
                "base_rssi": -52 - idx * 6,
            }
        return self.devices[idx]

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "paused": self.paused,
            "rate_hz": self.rate_hz,
            "profile": self.profile,
            "loss_percent": self.loss_percent,
            "out_of_order_percent": self.out_of_order_percent,
            "duplicate_percent": self.duplicate_percent,
            "noise_level": self.noise_level,
            "device_count": self.device_count,
            "persist_db": self.persist_db,
        }


sim = SimulatorState()


class SimConfigUpdate(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None
    rate_hz: int | None = None
    profile: str | None = None
    loss_percent: float | None = None
    out_of_order_percent: float | None = None
    duplicate_percent: float | None = None
    noise_level: float | None = None
    device_count: int | None = None
    persist_db: bool | None = None


class SimEventRequest(BaseModel):
    action: str  # shock | burst_loss | reordering | queue_drop | reboot | step


class ResetRequest(BaseModel):
    clear_db: bool = False


class SerialPortUpdate(BaseModel):
    port: str


def decode(raw: dict):
    data = bytes.fromhex(raw["hex"])
    if len(data) != FRAME.size:
        raise ValueError("wrong frame length")
    magic, version, boot, seq, uptime, ax, ay, az, gx, gy, gz = FRAME.unpack(data)
    if magic != b"MPU1" or version != 1:
        raise ValueError("unsupported frame")
    readings = (ax, ay, az, gx, gy, gz)
    if not all(math.isfinite(x) for x in readings):
        raise ValueError("invalid sensor reading")
    return {
        "timestamp": time.time(),
        "mac": raw["mac"],
        "rssi": int(raw["rssi"]),
        "boot": boot,
        "seq": seq,
        "uptime_us": uptime,
        "ax": ax,
        "ay": ay,
        "az": az,
        "gx": gx,
        "gy": gy,
        "gz": gz,
        "queue_drops": int(raw.get("queue_drops", 0)),
        "hex": raw["hex"],
    }


def process(packet, persist: bool = True):
    key = (packet["mac"], packet["boot"])
    s = states[key]
    seq, previous = packet["seq"], s["last_seq"]
    status = "ok"
    lost_delta = 0

    # Handle 32-bit sequence wrap-around; distinguish older/duplicate packets.
    if previous is None:
        s["last_seq"] = seq
        s["received"] += 1
    else:
        advance = (seq - previous) & 0xFFFFFFFF
        if advance == 0:
            s["duplicates"] += 1
            status = "duplicate"
        elif advance < 0x80000000:
            lost_delta = advance - 1
            s["lost"] += lost_delta
            if lost_delta > 0:
                status = "gap"
            # Keep a bounded window so late arrivals can correct loss estimates.
            if advance <= 2048:
                s["missing"].update(
                    (previous + i) & 0xFFFFFFFF for i in range(1, advance)
                )
            if len(s["missing"]) > 2048:
                s["missing"] = set(sorted(s["missing"])[-2048:])
            s["received"] += 1
            s["last_seq"] = seq
        else:
            s["out_of_order"] += 1
            status = "out_of_order"
            if seq in s["missing"]:
                s["missing"].remove(seq)
                s["lost"] -= 1
                s["received"] += 1

    packet["gap_ms"] = (
        (packet["timestamp"] - s["last_time"]) * 1000 if s["last_time"] else None
    )
    packet["status"] = status
    packet["lost_delta"] = lost_delta

    s["last_time"] = packet["timestamp"]
    s["last_rssi"] = packet["rssi"]
    s["queue_drops"] = packet["queue_drops"]

    if persist and db is not None:
        db.execute(
            """INSERT INTO packets
            (timestamp,mac,boot,seq,rssi,ax,ay,az,gx,gy,gz,gap_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(
                packet[k]
                for k in (
                    "timestamp",
                    "mac",
                    "boot",
                    "seq",
                    "rssi",
                    "ax",
                    "ay",
                    "az",
                    "gx",
                    "gy",
                    "gz",
                    "gap_ms",
                )
            ),
        )

    packet["type"] = "packet"
    recent.append(packet)
    return packet


def summary():
    devices = {}
    for (mac, boot), s in states.items():
        current = devices.setdefault(
            mac,
            {
                "mac": mac,
                "received": 0,
                "lost": 0,
                "duplicates": 0,
                "out_of_order": 0,
                "rssi": None,
                "queue_drops": 0,
                "boot": boot,
            },
        )
        for k in ("received", "lost", "duplicates", "out_of_order"):
            current[k] += s[k]
        if s["last_time"] and (current.get("last_time") or 0) < s["last_time"]:
            current.update(
                last_time=s["last_time"],
                rssi=s["last_rssi"],
                queue_drops=s["queue_drops"],
                boot=boot,
            )
    for d in devices.values():
        expected = d["received"] + d["lost"]
        d["loss_percent"] = round(100 * d["lost"] / expected, 2) if expected else 0
    stored_count = (
        db.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
        if db is not None
        else 0
    )
    return {
        "type": "summary",
        "devices": list(devices.values()),
        "stored": stored_count,
        "sim": sim.to_dict(),
        "serial": {
            **serial_status,
            "port": args.port,
            "available_ports": list_available_ports(),
        },
    }


async def publish(obj):
    for client in tuple(clients):
        try:
            await asyncio.wait_for(client.send_json(obj), timeout=0.5)
        except Exception:
            clients.discard(client)


async def record(raw, persist: bool = True):
    try:
        packet = process(decode(raw), persist=persist)
    except (KeyError, ValueError, TypeError, struct.error):
        return
    await publish(packet)


def _read_serial_batch(port: serial.Serial) -> list[bytes]:
    first = port.readline()
    if not first:
        return []
    lines = [first]
    while port.in_waiting > 0 and len(lines) < 32:
        nxt = port.readline()
        if not nxt:
            break
        lines.append(nxt)
    return lines


async def collect_serial():
    while True:
        if sim.enabled:
            serial_status["connected"] = False
            await asyncio.sleep(0.25)
            continue
        target_port = args.port
        serial_status["port"] = target_port
        port = None
        try:
            port = await asyncio.to_thread(
                serial.Serial, target_port, 115200, timeout=0.08
            )
            port.reset_input_buffer()
            serial_status["connected"] = True
            serial_status["error"] = None
            serial_status["role"] = None
            serial_status["diag_status"] = None
            serial_status["diag_msg"] = None
            serial_status["device_mac"] = None
            print("Connected to", target_port, flush=True)
            await publish(summary())
            while not sim.enabled and args.port == target_port:
                lines = await asyncio.to_thread(_read_serial_batch, port)
                if not lines:
                    continue
                for line in lines:
                    try:
                        payload = json.loads(line.decode("utf-8"))
                        if isinstance(payload, dict) and payload.get("type") == "diag":
                            serial_status["role"] = payload.get("role")
                            serial_status["diag_status"] = payload.get("status")
                            serial_status["diag_msg"] = payload.get("msg")
                            serial_status["device_mac"] = payload.get("mac")
                            continue
                        serial_status["role"] = "receiver"
                        serial_status["diag_status"] = "receiving"
                        await record(payload, persist=True)
                    except (UnicodeError, json.JSONDecodeError):
                        pass
        except (serial.SerialException, OSError) as exc:
            serial_status["connected"] = False
            serial_status["error"] = str(exc)
            for _ in range(15):
                if args.port != target_port or sim.enabled:
                    break
                await asyncio.sleep(0.1)
        finally:
            if port is not None:
                try:
                    await asyncio.to_thread(port.close)
                except Exception:
                    pass
            if args.port != target_port:
                serial_status["connected"] = False


def generate_imu_reading(t: float, profile: str, noise_scale: float, shock_active: bool):
    g = 9.80665
    n_acc = 0.035 * noise_scale
    n_gyr = 0.008 * noise_scale

    if profile == "rotation":
        roll = 0.9 * t
        pitch = 0.6 * math.sin(0.5 * t)
        gx = 0.9
        gy = 0.3 * math.cos(0.5 * t)
        gz = 0.4 * math.sin(0.3 * t)
        ax = -g * math.sin(pitch)
        ay = g * math.sin(roll) * math.cos(pitch)
        az = g * math.cos(roll) * math.cos(pitch)
    elif profile == "vibration":
        roll = 0.08 * math.sin(1.2 * t)
        pitch = 0.05 * math.cos(0.9 * t)
        vib_a = 1.8 * math.sin(2 * math.pi * 14 * t) + 0.9 * math.cos(2 * math.pi * 23 * t)
        vib_g = 0.45 * math.cos(2 * math.pi * 14 * t)
        ax = -g * math.sin(pitch) + 0.6 * vib_a
        ay = g * math.sin(roll) * math.cos(pitch) + 0.8 * vib_a
        az = g * math.cos(roll) * math.cos(pitch) + 1.2 * vib_a
        gx = 0.1 * math.cos(1.2 * t) + vib_g
        gy = -0.05 * math.sin(0.9 * t) + 0.7 * vib_g
        gz = 0.25 * vib_g
    elif profile == "steps":
        cadence = 2.2 * math.pi * 1.8
        step_phase = cadence * t
        impact = max(0.0, math.sin(step_phase)) ** 4 * 6.5
        roll = 0.15 * math.sin(step_phase / 2)
        pitch = 0.12 * math.cos(step_phase)
        ax = -g * math.sin(pitch) + 1.4 * math.sin(step_phase)
        ay = g * math.sin(roll) * math.cos(pitch) + 1.1 * math.cos(step_phase / 2)
        az = g * math.cos(roll) * math.cos(pitch) + impact - 1.2
        gx = 0.45 * math.cos(step_phase / 2)
        gy = -0.6 * math.sin(step_phase)
        gz = 0.3 * math.sin(step_phase / 2)
    elif profile == "idle":
        ax, ay, az = 0.0, 0.0, g
        gx, gy, gz = 0.0, 0.0, 0.0
    else:  # "wave" (default)
        roll = 0.55 * math.sin(1.4 * t)
        pitch = 0.40 * math.cos(1.0 * t)
        ax = -g * math.sin(pitch)
        ay = g * math.sin(roll) * math.cos(pitch)
        az = g * math.cos(roll) * math.cos(pitch)
        gx = 0.55 * 1.4 * math.cos(1.4 * t)
        gy = -0.40 * 1.0 * math.sin(1.0 * t)
        gz = 0.18 * math.sin(0.7 * t)

    if shock_active:
        decay = math.sin(2 * math.pi * 18 * t)
        ax += 14.0 * decay
        ay -= 9.5 * decay
        az += 18.0 * math.cos(2 * math.pi * 18 * t)
        gx += 3.2 * decay
        gy -= 2.8 * decay
        gz += 1.9 * decay

    return (
        ax + random.gauss(0, n_acc),
        ay + random.gauss(0, n_acc),
        az + random.gauss(0, n_acc),
        gx + random.gauss(0, n_gyr),
        gy + random.gauss(0, n_gyr),
        gz + random.gauss(0, n_gyr),
    )


async def simulate_loop():
    while True:
        if not sim.enabled:
            await asyncio.sleep(0.1)
            continue

        if sim.paused and sim.step_once <= 0:
            await asyncio.sleep(0.05)
            continue

        if sim.step_once > 0:
            sim.step_once -= 1

        now = time.monotonic()
        shock_active = now < sim.shock_until

        # Flush any delayed out-of-order packets ready to arrive
        ready = [item for item in sim.delayed_packets if item[0] <= now]
        sim.delayed_packets = [item for item in sim.delayed_packets if item[0] > now]
        for _, raw_pkt in ready:
            await record(raw_pkt, persist=sim.persist_db)

        for idx in range(sim.device_count):
            dev = sim.ensure_device(idx)
            dev["seq"] = (dev["seq"] + 1) & 0xFFFFFFFF
            seq = dev["seq"]

            # Check burst loss or random loss
            if sim.burst_loss_remaining > 0:
                sim.burst_loss_remaining -= 1
                continue
            if random.random() * 100.0 < sim.loss_percent:
                continue

            t = (now - dev["start_time"]) + dev["phase"]
            uptime_us = int((now - dev["start_time"]) * 1e6) & 0xFFFFFFFF
            values = generate_imu_reading(t, sim.profile, sim.noise_level, shock_active)
            frame = FRAME.pack(b"MPU1", 1, dev["boot"], seq, uptime_us, *values)
            rssi = int(
                dev["base_rssi"]
                + 4.0 * math.sin(0.35 * t)
                + random.gauss(0, 1.2)
            )
            raw_packet = {
                "mac": dev["mac"],
                "rssi": max(-95, min(-25, rssi)),
                "hex": frame.hex(),
                "queue_drops": dev["queue_drops"],
            }

            # Check if this packet should be delayed (out-of-order arrival)
            if random.random() * 100.0 < sim.out_of_order_percent:
                delay_s = random.uniform(2.0, 4.0) / max(1, sim.rate_hz)
                sim.delayed_packets.append((now + delay_s, raw_packet))
                continue

            await record(raw_packet, persist=sim.persist_db)

            # Check if duplicate packet happens
            if random.random() * 100.0 < sim.duplicate_percent:
                await record(raw_packet, persist=sim.persist_db)

        period = 1.0 / max(1, min(200, sim.rate_hz))
        await asyncio.sleep(period)


async def heartbeat():
    while True:
        await asyncio.sleep(1)
        if db is not None:
            try:
                db.commit()
            except Exception:
                pass
        await publish(summary())


@asynccontextmanager
async def lifespan(app):
    global db
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("""CREATE TABLE IF NOT EXISTS packets (
        id INTEGER PRIMARY KEY, timestamp REAL, mac TEXT, boot INTEGER,
        seq INTEGER, rssi INTEGER, ax REAL, ay REAL, az REAL,
        gx REAL, gy REAL, gz REAL, gap_ms REAL)""")
    db.commit()
    if args.demo:
        sim.enabled = True
    tasks = [
        asyncio.create_task(collect_serial()),
        asyncio.create_task(simulate_loop()),
        asyncio.create_task(heartbeat()),
    ]
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    db.close()


app = FastAPI(title="ESP-NOW MPU Monitor", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(
        ROOT / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/api/history")
async def history(limit: int = 200):
    limit = max(1, min(limit, 2000))
    if recent:
        return list(recent)[-limit:]
    if not sim.enabled:
        return []
    db.row_factory = sqlite3.Row
    data = db.execute(
        "SELECT * FROM packets ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in reversed(data)]


@app.get("/api/summary")
async def get_summary():
    return summary()


@app.get("/api/sim/config")
async def get_sim_config():
    return sim.to_dict()


@app.post("/api/sim/config")
async def update_sim_config(cfg: SimConfigUpdate):
    if cfg.enabled is not None:
        sim.enabled = cfg.enabled
    if cfg.paused is not None:
        sim.paused = cfg.paused
    if cfg.rate_hz is not None:
        sim.rate_hz = max(1, min(200, cfg.rate_hz))
    if cfg.profile is not None and cfg.profile in (
        "wave",
        "rotation",
        "vibration",
        "steps",
        "idle",
    ):
        sim.profile = cfg.profile
    if cfg.loss_percent is not None:
        sim.loss_percent = max(0.0, min(80.0, cfg.loss_percent))
    if cfg.out_of_order_percent is not None:
        sim.out_of_order_percent = max(0.0, min(30.0, cfg.out_of_order_percent))
    if cfg.duplicate_percent is not None:
        sim.duplicate_percent = max(0.0, min(20.0, cfg.duplicate_percent))
    if cfg.noise_level is not None:
        sim.noise_level = max(0.0, min(5.0, cfg.noise_level))
    if cfg.device_count is not None:
        sim.device_count = max(1, min(4, cfg.device_count))
    if cfg.persist_db is not None:
        sim.persist_db = cfg.persist_db
    state = summary()
    await publish(state)
    return state


@app.post("/api/sim/event")
async def trigger_sim_event(req: SimEventRequest):
    action = req.action
    if action == "shock":
        sim.shock_until = time.monotonic() + 0.45
    elif action == "burst_loss":
        sim.burst_loss_remaining += 8
    elif action == "reordering":
        dev = sim.ensure_device(0)
        now = time.monotonic()
        t = now - dev["start_time"]
        for offset in (1, 2):
            dev["seq"] = (dev["seq"] + 1) & 0xFFFFFFFF
            values = generate_imu_reading(t, sim.profile, sim.noise_level, False)
            frame = FRAME.pack(
                b"MPU1",
                1,
                dev["boot"],
                dev["seq"],
                int(t * 1e6) & 0xFFFFFFFF,
                *values,
            )
            raw_pkt = {
                "mac": dev["mac"],
                "rssi": dev["base_rssi"],
                "hex": frame.hex(),
                "queue_drops": dev["queue_drops"],
            }
            sim.delayed_packets.append((now + 0.12 * offset, raw_pkt))
    elif action == "queue_drop":
        for idx in range(sim.device_count):
            dev = sim.ensure_device(idx)
            dev["queue_drops"] += random.randint(1, 5)
    elif action == "reboot":
        for idx in range(sim.device_count):
            dev = sim.ensure_device(idx)
            dev["boot"] = random.getrandbits(32)
            dev["seq"] = 0
            dev["start_time"] = time.monotonic()
    elif action == "step":
        sim.enabled = True
        sim.step_once += 1
    state = summary()
    await publish(state)
    return {"ok": True, "action": action, "sim": sim.to_dict()}


@app.get("/api/serial/ports")
async def get_serial_ports():
    return {
        **serial_status,
        "port": args.port,
        "available_ports": list_available_ports(),
    }


@app.post("/api/serial/port")
async def update_serial_port(req: SerialPortUpdate):
    new_port = req.port.strip()
    if new_port:
        args.port = new_port
        serial_status["port"] = new_port
        serial_status["connected"] = False
        serial_status["error"] = None
        sim.enabled = False
    state = summary()
    await publish(state)
    return state


@app.post("/api/reset")
async def reset_session(req: ResetRequest):
    states.clear()
    recent.clear()
    sim.devices.clear()
    sim.delayed_packets.clear()
    if req.clear_db and db is not None:
        db.execute("DELETE FROM packets")
        db.commit()
    state = summary()
    await publish(state)
    return state


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_json(summary())
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--port",
        default=_initial_port,
        help="Serial port (e.g. COM5 or /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--demo", action="store_true", help="Start with simulated MPU packets active"
    )
    parsed = parser.parse_args()
    args.port = parsed.port
    serial_status["port"] = parsed.port
    args.demo = parsed.demo
    uvicorn.run(app, host="127.0.0.1", port=8000)
