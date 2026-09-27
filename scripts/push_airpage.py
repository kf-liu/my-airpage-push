#!/usr/bin/env python3
"""按 config.json 渲染 AirPage 模板，并推送到墨水屏。

设备 ID 只从环境变量 AIRPAGE_DEVICE_ID 读取，不写入仓库、不打印原文。
当前接入的模板是网站上的迷宫。
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config.json"
MAX_BYTES = 512 * 1024
ALLOWED_HOSTS = {
    "airpage.crossmux.cn",
    "airpage.crossmux.com",
    "airpage.yunhug.com",
}
FONT_CANDIDATES = (
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
    ("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc", 2),
    ("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", 2),
    ("/usr/share/fonts/noto-cjk/NotoSerifCJK-Regular.ttc", 2),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/System/Library/Fonts/STHeiti Light.ttc", 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0),
)
WALL_UP = 1
WALL_RIGHT = 2
WALL_DOWN = 4
WALL_LEFT = 8
DIRECTIONS = (
    (0, -1, WALL_UP, WALL_DOWN),
    (1, 0, WALL_RIGHT, WALL_LEFT),
    (0, 1, WALL_DOWN, WALL_UP),
    (-1, 0, WALL_LEFT, WALL_RIGHT),
)


def fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def to_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    if value >= 0x80000000:
        return value - 0x100000000
    return value


def to_uint32(value: int) -> int:
    return value & 0xFFFFFFFF


def imul(left: int, right: int) -> int:
    return to_int32(to_int32(left) * to_int32(right))


def js_round(value: float) -> int:
    if value >= 0:
        return math.floor(value + 0.5)
    return math.ceil(value - 0.5)


def hash_seed(text: str) -> int:
    value = 2166136261
    encoded = text.encode("utf-16-le")
    for index in range(0, len(encoded), 2):
        unit = encoded[index] | (encoded[index + 1] << 8)
        value = to_uint32(to_int32(value) ^ unit)
        value = to_uint32(imul(value, 16777619))
    return value


def make_rng(seed: int):
    state = to_int32(seed)

    def rng() -> float:
        nonlocal state
        state = to_int32(state + 1831565813)
        mixed = imul(to_int32(state ^ (to_uint32(state) >> 15)), to_int32(1 | state))
        updated = to_int32(mixed + imul(to_int32(mixed ^ (to_uint32(mixed) >> 7)), to_int32(61 | mixed)))
        mixed = to_int32(updated ^ mixed)
        return to_uint32(mixed ^ (to_uint32(mixed) >> 14)) / 4294967296

    return rng


def load_config() -> dict:
    if not CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        fail("config.json 不是合法的 JSON。")
    if not isinstance(data, dict):
        fail("config.json 必须是一个 JSON 对象。")
    return data


def env_override(name: str) -> str:
    return os.environ.get(name, "").strip()


def pick_text(env_name: str, configured, fallback: str) -> str:
    overridden = env_override(env_name)
    if overridden:
        return overridden
    if configured is None:
        return fallback
    text = str(configured).strip()
    return text or fallback


def pick_bool(env_name: str, configured, fallback: bool) -> bool:
    overridden = env_override(env_name).lower()
    if overridden:
        return parse_bool(overridden, env_name)
    if isinstance(configured, bool):
        return configured
    if configured is None or configured == "":
        return fallback
    return parse_bool(str(configured), env_name)


def parse_bool(value: str, name: str) -> bool:
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    fail(f"{name} 只能是 true 或 false。")


def require_device_id() -> str:
    device_id = env_override("AIRPAGE_DEVICE_ID")
    if not device_id:
        fail("缺少环境变量 AIRPAGE_DEVICE_ID。请只在 GitHub Actions Secret 中设置，不要写入仓库。")
    if len(device_id) != 16 or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for ch in device_id):
        fail("AIRPAGE_DEVICE_ID 格式不正确。它应是 16 位设备 ID，且不能写入仓库。")
    return device_id


def require_origin(configured: str) -> str:
    origin = pick_text("AIRPAGE_ORIGIN", configured, "https://airpage.crossmux.cn").rstrip("/")
    if "://" not in origin:
        fail("origin 必须是 https 地址。")
    scheme, host = origin.split("://", 1)
    host = host.split("/", 1)[0]
    if scheme != "https" or host not in ALLOWED_HOSTS:
        fail("origin 不在允许的 AirPage 域名内。")
    return f"https://{host}"


def require_positive_int(env_name: str, configured, fallback: int) -> int:
    raw = pick_text(env_name, configured, str(fallback))
    if not raw.isdecimal() or int(raw) <= 0:
        fail(f"{env_name} 必须是正整数。")
    return int(raw)


def require_mode(configured: str) -> str:
    mode = pick_text("AIRPAGE_MODE", configured, "gray4")
    if mode not in {"gray4", "gray16"}:
        fail("mode 只能是 gray4 或 gray16。")
    return mode


def capacity_ok(width: int, height: int, mode: str) -> bool:
    if mode == "gray4":
        return 1078 + ((width + 3) // 4) * 4 * height <= MAX_BYTES
    return 118 + ((width + 7) // 8) * 4 * height <= MAX_BYTES


def find_font(size: int) -> ImageFont.FreeTypeFont:
    pixel_size = max(8, int(size))
    for path, index in FONT_CANDIDATES:
        if not Path(path).is_file():
            continue
        for face in (index, 0):
            try:
                return ImageFont.truetype(path, size=pixel_size, index=face)
            except OSError:
                continue
    fail("找不到中文字体。GitHub Actions 需要先安装 fonts-noto-cjk。")


def fill_rect(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, height: int, color: int) -> None:
    if width <= 0 or height <= 0:
        return
    draw.rectangle((x, y, x + width - 1, y + height - 1), fill=color)


def resolve_seed(seed: str) -> str:
    chosen = seed.strip() or "AIRPAGE"
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if chosen.lower() == "daily":
        return now.strftime("%Y-%m-%d")
    if chosen.lower() == "timestamp":
        return str(int(now.timestamp()))
    return chosen


def build_maze(columns: int, rows: int, rng) -> bytearray:
    walls = bytearray([WALL_UP | WALL_RIGHT | WALL_DOWN | WALL_LEFT] * (columns * rows))
    visited = bytearray(columns * rows)
    stack = [0]
    visited[0] = 1

    def cell(x: int, y: int) -> int:
        return y * columns + x

    while stack:
        current = stack[-1]
        x = current % columns
        y = current // columns
        order = [0, 1, 2, 3]
        for index in range(3, 0, -1):
            swap = math.floor(rng() * (index + 1))
            order[index], order[swap] = order[swap], order[index]
        carved = False
        for direction_index in order:
            dx, dy, bit, opposite = DIRECTIONS[direction_index]
            next_x = x + dx
            next_y = y + dy
            if next_x < 0 or next_y < 0 or next_x >= columns or next_y >= rows:
                continue
            next_cell = cell(next_x, next_y)
            if visited[next_cell]:
                continue
            walls[current] &= ~bit
            walls[next_cell] &= ~opposite
            visited[next_cell] = 1
            stack.append(next_cell)
            carved = True
            break
        if not carved:
            stack.pop()

    walls[cell(0, 0)] &= ~WALL_UP
    walls[cell(columns - 1, rows - 1)] &= ~WALL_DOWN
    return walls


def solution_path(walls: bytearray, columns: int, rows: int) -> list[int]:
    total = columns * rows
    previous = [-1] * total
    visited = bytearray(total)
    queue = [0]
    visited[0] = 1
    goal = columns * rows - 1
    head = 0
    while head < len(queue):
        current = queue[head]
        head += 1
        if current == goal:
            break
        x = current % columns
        y = current // columns
        for dx, dy, bit, _opposite in DIRECTIONS:
            if walls[current] & bit:
                continue
            next_x = x + dx
            next_y = y + dy
            if next_x < 0 or next_y < 0 or next_x >= columns or next_y >= rows:
                continue
            next_cell = next_y * columns + next_x
            if visited[next_cell]:
                continue
            visited[next_cell] = 1
            previous[next_cell] = current
            queue.append(next_cell)
    path = []
    cursor = goal
    while cursor != -1:
        path.append(cursor)
        cursor = previous[cursor]
    return path


def render_maze(width: int, height: int, options: dict) -> Image.Image:
    seed = str(options.get("seed", "AIRPAGE")).strip() or "AIRPAGE"
    title = str(options.get("title", "")).strip() or "MAZE"
    solve = bool(options.get("solve", False))
    large = bool(options.get("large", False))

    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    side = js_round(width * 0.06)
    top = js_round(height * 0.075)
    bottom = js_round(height * 0.05)
    columns = 23 if large else 15
    available_width = width - 2 * side
    available_height = height - top - bottom - side
    cell = max(6, math.floor(min(available_width / columns, available_height / (38 if large else 25))))
    rows = max(2, math.floor(available_height / cell))
    maze_width = columns * cell
    maze_height = rows * cell
    origin_x = js_round((width - maze_width) / 2)
    origin_y = js_round(top + (height - top - bottom - maze_height) / 2)
    walls = build_maze(columns, rows, make_rng(hash_seed(seed)))
    stroke = max(1, js_round(cell * 0.16))

    def horizontal_wall(x: int, y: int) -> None:
        fill_rect(draw, origin_x + x * cell, origin_y + y * cell, cell + stroke, stroke, 0)

    def vertical_wall(x: int, y: int) -> None:
        fill_rect(draw, origin_x + x * cell, origin_y + y * cell, stroke, cell + stroke, 0)

    for y in range(rows):
        for x in range(columns):
            bits = walls[y * columns + x]
            if bits & WALL_UP:
                horizontal_wall(x, y)
            if bits & WALL_LEFT:
                vertical_wall(x, y)
            if x == columns - 1 and bits & WALL_RIGHT:
                vertical_wall(x + 1, y)
            if y == rows - 1 and bits & WALL_DOWN:
                horizontal_wall(x, y + 1)

    if solve:
        path = solution_path(walls, columns, rows)
        thickness = max(2, js_round(cell * 0.34))

        def center_x(index: int) -> float:
            return origin_x + (index % columns) * cell + cell / 2 + stroke / 2

        def center_y(index: int) -> float:
            return origin_y + (index // columns) * cell + cell / 2 + stroke / 2

        fill_rect(
            draw,
            js_round(center_x(0) - thickness / 2),
            origin_y - stroke,
            thickness,
            js_round(cell / 2 + stroke),
            85,
        )
        for index in range(len(path) - 1):
            x1, y1 = center_x(path[index]), center_y(path[index])
            x2, y2 = center_x(path[index + 1]), center_y(path[index + 1])
            fill_rect(
                draw,
                js_round(min(x1, x2) - thickness / 2),
                js_round(min(y1, y2) - thickness / 2),
                js_round(abs(x2 - x1) + thickness),
                js_round(abs(y2 - y1) + thickness),
                85,
            )
        goal = columns * rows - 1
        fill_rect(
            draw,
            js_round(center_x(goal) - thickness / 2),
            js_round(center_y(goal)),
            thickness,
            js_round(cell / 2 + stroke),
            85,
        )

    arrow_font = find_font(max(9, top * 0.42))
    title_font = find_font(top * 0.5)
    seed_font = find_font(top * 0.3)
    draw.text((origin_x + cell / 2, origin_y - stroke - height * 0.006), "▼", font=arrow_font, fill=0, anchor="ms")
    draw.text((origin_x + maze_width - cell / 2, origin_y + maze_height + stroke + height * 0.004), "▼", font=arrow_font, fill=0, anchor="mt")
    draw.text((side, top * 0.5), title, font=title_font, fill=0, anchor="lm")
    draw.text((width - side, top * 0.5), f"SEED · {seed}  ·  {columns}×{rows}", font=seed_font, fill=85, anchor="rm")
    return image


def render_template(template: str, width: int, height: int, config: dict) -> Image.Image:
    if template != "maze":
        fail("当前只接入了迷宫模板 maze。请把 template 设为 maze。")
    maze = config.get("maze", {})
    if not isinstance(maze, dict):
        fail("config.json 里的 maze 必须是对象。")
    options = {
        "seed": resolve_seed(pick_text("AIRPAGE_MAZE_SEED", maze.get("seed"), "timestamp")),
        "title": pick_text("AIRPAGE_MAZE_TITLE", maze.get("title"), "MAZE · 迷宫"),
        "solve": pick_bool("AIRPAGE_MAZE_SOLVE", maze.get("solve"), False),
        "large": pick_bool("AIRPAGE_MAZE_LARGE", maze.get("large"), False),
    }
    image = render_maze(width, height, options)
    print(
        "模板 maze，"
        f"种子 {options['seed']}，"
        f"标题 {options['title']}，"
        f"显示解答 {'开' if options['solve'] else '关'}，"
        f"更大网格 {'开' if options['large'] else '关'}。"
    )
    return image


def quantize(value: int, mode: str) -> int:
    if mode == "gray4":
        if value < 43:
            return 0
        if value < 128:
            return 1
        if value < 213:
            return 2
        return 3
    return min(15, (value + 8) // 17)


def encode_bmp(image: Image.Image, mode: str) -> bytes:
    width, height = image.size
    if mode == "gray4":
        levels = 4
        bit_count = 2
        pixels_per_byte = 4
        shifts = (6, 4, 2, 0)
        palette_luma = (0, 85, 170, 255)
    else:
        levels = 16
        bit_count = 4
        pixels_per_byte = 2
        shifts = (4, 0)
        palette_luma = tuple(index * 17 for index in range(16))

    stride = ((width * bit_count + 31) // 32) * 4
    header_size = 14 + 40 + levels * 4
    pixel_size = stride * height
    file_size = header_size + pixel_size
    if file_size > MAX_BYTES:
        fail(f"BMP 超过 512 KiB（当前 {file_size} 字节）。")

    palette = b"".join(struct.pack("<BBBB", luma, luma, luma, 0) for luma in palette_luma)
    header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, header_size) + struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height,
        1,
        bit_count,
        0,
        pixel_size,
        2835,
        2835,
        levels,
        levels,
    ) + palette

    pixels = image.tobytes()
    rows = bytearray()
    for y in range(height - 1, -1, -1):
        row = bytearray(stride)
        start = y * width
        for x in range(width):
            index = quantize(pixels[start + x], mode)
            row[x // pixels_per_byte] |= index << shifts[x % pixels_per_byte]
        rows.extend(row)
    return header + bytes(rows)


def redact(text: str, device_id: str) -> str:
    return text.replace(device_id, "***")[:500]


def multipart_body(bmp: bytes) -> tuple[bytes, str]:
    boundary = "----airpagepush"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="image"; filename="fallback.bmp"\r\n'
        "Content-Type: image/bmp\r\n\r\n"
    ).encode() + bmp + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def upload_bmp(origin: str, device_id: str, bmp: bytes) -> bool:
    body, content_type = multipart_body(bmp)
    endpoints = (
        f"{origin}/api/device/{device_id}/push",
        f"{origin}/api/device/{device_id}/image",
    )
    for index, endpoint in enumerate(endpoints):
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": content_type, "User-Agent": "my-airpage-push"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status = response.status
                payload_text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            status = error.code
            payload_text = error.read().decode("utf-8", errors="replace")
            if status == 404 and index == 0:
                print("推送接口返回 404，改为上传图片接口。")
                continue
            fail(f"推送失败，HTTP {status}。{redact(payload_text, device_id)}")
        except urllib.error.URLError:
            fail("无法连接 AirPage 服务。")

        if not 200 <= status < 300:
            fail(f"推送失败，HTTP {status}。{redact(payload_text, device_id)}")
        try:
            payload = json.loads(payload_text) if payload_text.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        refreshed = bool(payload.get("refreshed"))
        print(f"图片已上传，大小 {payload.get('bytes', len(bmp))} 字节，服务端自动刷新 {'是' if refreshed else '否'}。")
        return refreshed
    fail("推送失败。")


def publish_refresh(device_id: str) -> None:
    import paho.mqtt.client as mqtt

    connected = {"ok": False}

    def on_connect(_client, _userdata, _flags, reason_code, _properties=None) -> None:
        code = getattr(reason_code, "value", reason_code)
        connected["ok"] = code == 0

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"airpage-push-{os.urandom(4).hex()}",
        transport="websockets",
    )
    client.tls_set()
    client.ws_set_options(path="/mqtt")
    client.on_connect = on_connect
    try:
        client.connect("mqtt-cn.uipcat.com", 8084, keepalive=57)
    except OSError:
        fail("图片已上传，但刷新通道连接失败。请按设备向下键，不要立刻重复上传。")
    client.loop_start()
    deadline = time.time() + 8
    while time.time() < deadline and not connected["ok"]:
        time.sleep(0.1)
    if not connected["ok"]:
        client.loop_stop()
        fail("图片已上传，但刷新通道没有连上。请按设备向下键，不要立刻重复上传。")
    info = client.publish(
        f"airpage/device/{device_id}/refresh",
        json.dumps({"ts": int(time.time() * 1000)}),
        qos=0,
    )
    info.wait_for_publish(timeout=8)
    client.disconnect()
    client.loop_stop()
    if not info.is_published():
        fail("图片已上传，但刷新指令没有发出。请按设备向下键，不要立刻重复上传。")
    print("已发送刷新指令。屏幕是否更新，要看设备是否在线。")


def push_bmp(origin: str, device_id: str, bmp: bytes) -> None:
    upload_bmp(origin, device_id, bmp)
    publish_refresh(device_id)


def main() -> None:
    config = load_config()
    device_id = require_device_id()
    origin = require_origin(str(config.get("origin", "")))
    width = require_positive_int("AIRPAGE_WIDTH", config.get("width"), 480)
    height = require_positive_int("AIRPAGE_HEIGHT", config.get("height"), 800)
    mode = require_mode(str(config.get("mode", "")))
    template = pick_text("AIRPAGE_TEMPLATE", config.get("template"), "maze")
    if not capacity_ok(width, height, mode):
        fail("画面尺寸超出该模式允许的容量。")

    image = render_template(template, width, height, config)
    bmp = encode_bmp(image, mode)
    print(f"已生成 {width}x{height} {mode} BMP，大小 {len(bmp)} 字节。")
    push_bmp(origin, device_id, bmp)


if __name__ == "__main__":
    main()
