import gc
import network
import socket
import time
from machine import Pin, PWM

# ====
# Wi-Fi access-point settings
# ====

AP_SSID = "FlexBreezeOss"
AP_PASS = "12345678"   # Must be at least 8 characters.

# Current firmware version — keep this in sync with Latest/version.txt in GitHub.
FIRMWARE_VERSION = "1.0.0"

# ====
# Pico GPIO assignments
# ====

# ULN2003: IN1, IN2, IN3, IN4
STEP_PINS = (
    Pin(18, Pin.OUT),
    Pin(19, Pin.OUT),
    Pin(20, Pin.OUT),
    Pin(21, Pin.OUT)
)

# RGB LED: common-anode LED. Common pin goes to 3V3(OUT).
# PWM allows brightness balancing per channel in software.
# Duty 0 = fully on; 65535 = fully off (inverted for common anode).
LED_R = PWM(Pin(13), freq=1000)
LED_G = PWM(Pin(14), freq=1000)
LED_B = PWM(Pin(15), freq=1000)

# Per-channel brightness scale (0-65535). Tune these to balance colours.
# Red has more voltage headroom so is dimmed relative to green and blue.
LED_R_DUTY_ON = 45000   # ~31% duty = dimmer red
LED_G_DUTY_ON = 10000   # ~85% duty = bright green
LED_B_DUTY_ON = 10000   # ~85% duty = bright blue
LED_DUTY_OFF  = 65535   # fully off

# ====
# LED colour helpers
#
# Status colour guide:
#   Red    (1,0,0) — booting / initialising
#   Yellow (1,1,0) — access point starting
#   Green  (0,1,0) — ready / manual hold mode
#   Blue   (0,0,1) — oscillating
#   Purple (1,0,1) — error (AP failed to start)
# ====

def set_led(red, green, blue):
    """
    Set RGB LED channels. Pass 1 to turn a channel on, 0 to turn it off.
    Common-anode logic is handled here: on = low duty, off = full duty.
    """
    LED_R.duty_u16(LED_R_DUTY_ON if red   else LED_DUTY_OFF)
    LED_G.duty_u16(LED_G_DUTY_ON if green else LED_DUTY_OFF)
    LED_B.duty_u16(LED_B_DUTY_ON if blue  else LED_DUTY_OFF)

# ====
# 28BYJ-48 motor configuration
# ====

# Half-step sequence for a 28BYJ-48 connected through ULN2003.
SEQUENCE = (
    (1, 0, 0, 0),
    (1, 1, 0, 0),
    (0, 1, 0, 0),
    (0, 1, 1, 0),
    (0, 0, 1, 0),
    (0, 0, 1, 1),
    (0, 0, 0, 1),
    (1, 0, 0, 1)
)

# Nominal 28BYJ-48 half-step count for 180 degrees.
STEPS_180 = 2048

# Half-step count for the oscillation sweep (90 degrees).
# Halving the sweep keeps the movement natural and avoids over-rotation.
STEPS_OSC = STEPS_180 // 2   # 1024 steps = 90 degrees

# Speed setting exposed on the page: 1 = slowest, 10 = fastest.
speed_setting = 6
step_interval_ms = 11

# Logical position range: 0 to STEPS_180.
current_step = 0
target_step = 0
sequence_index = 0

# Mode state.
oscillate = False
oscillation_direction = 1

last_step_time = time.ticks_ms()

# ====
# Motor functions
# ====

def set_motor_outputs(pattern_index):
    """Apply one half-step pattern to the ULN2003 inputs."""
    pattern = SEQUENCE[pattern_index]

    for pin_index in range(4):
        STEP_PINS[pin_index].value(pattern[pin_index])


def set_speed(speed):
    """
    Translate the web slider value into a motor half-step delay.

    1  = 21 ms per half-step, slowest
    10 = 3 ms per half-step, fastest
    """
    global speed_setting
    global step_interval_ms

    if speed < 1:
        speed = 1
    elif speed > 10:
        speed = 10

    speed_setting = speed
    step_interval_ms = 23 - (speed * 2)


def set_manual_position(angle):
    """
    Set a requested position between 0 and 180 degrees.
    A manual move always stops oscillation.
    """
    global target_step
    global oscillate

    if angle < 0:
        angle = 0
    elif angle > 180:
        angle = 180

    oscillate = False
    target_step = int((angle * STEPS_180) / 180)


def motor_update():
    """
    Run one possible motor half-step.

    It is called repeatedly from the main loop. This avoids using _thread,
    which is important because the Pico W Wi-Fi system uses core 1.
    """
    global current_step
    global target_step
    global sequence_index
    global oscillation_direction
    global last_step_time

    if oscillate:
        # Blue: motor is oscillating (sweeps 0 to 90 degrees).
        set_led(0, 0, 1)

        if current_step >= STEPS_OSC:
            oscillation_direction = -1
        elif current_step <= 0:
            oscillation_direction = 1

        if oscillation_direction == 1:
            target_step = STEPS_OSC
        else:
            target_step = 0

    else:
        # Green: manual mode, including while holding position.
        set_led(0, 1, 0)

    if current_step == target_step:
        return

    now = time.ticks_ms()

    if time.ticks_diff(now, last_step_time) < step_interval_ms:
        return

    if target_step > current_step:
        direction = 1
    else:
        direction = -1

    current_step += direction
    sequence_index = (sequence_index + direction) % len(SEQUENCE)

    set_motor_outputs(sequence_index)
    last_step_time = now


# ====
# Access point setup
# ====

set_led(1, 0, 0)   # Red: booting.

ap = network.WLAN(network.AP_IF)
ap.active(True)

set_led(1, 1, 0)   # Yellow: access point starting.

ap.config(essid=AP_SSID, password=AP_PASS)

time.sleep_ms(800)

if not ap.active():
    set_led(1, 0, 1)   # Purple: error — AP failed to start.
    raise RuntimeError("Access point failed to start")

PICO_IP = ap.ifconfig()[0]
ip_bytes = bytes(map(int, PICO_IP.split(".")))

print("Access point started")
print("SSID:", AP_SSID)
print("Firmware:", FIRMWARE_VERSION)
print("Pico IP:", PICO_IP)
print("Captive portal active.")
print("Connect to Wi-Fi:", AP_SSID)
print("If it does not open automatically, browse to: http://" + PICO_IP)

set_led(0, 1, 0)   # Green: ready.

# ====
# Captive-portal DNS server
# ====

dns_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
dns_socket.bind(("0.0.0.0", 53))
dns_socket.setblocking(False)


def handle_dns():
    """
    Make every hostname resolve to the Pico's access-point IP address.

    This directs captive-portal checks toward the Pico W.
    """
    try:
        request, client_address = dns_socket.recvfrom(512)

        if len(request) < 12:
            return

        transaction_id = request[0:2]

        # DNS response: standard query response, 1 question, 1 answer.
        header = (
            transaction_id
            + b"\x81\x80"
            + b"\x00\x01"
            + b"\x00\x01"
            + b"\x00\x00"
            + b"\x00\x00"
        )

        # Keep the incoming question section.
        question = request[12:]

        # Answer with the Pico AP IP address.
        answer = (
            b"\xc0\x0c"
            + b"\x00\x01"
            + b"\x00\x01"
            + b"\x00\x00\x00\x3c"
            + b"\x00\x04"
            + ip_bytes
        )

        dns_socket.sendto(header + question + answer, client_address)

    except OSError:
        # Expected when there is no DNS request waiting.
        pass


# ====
# HTTP web server
# ====

with open("index.html", "r") as html_file:
    HTML_PAGE = html_file.read()

web_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
web_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
web_socket.bind(("0.0.0.0", 80))
web_socket.listen(2)
web_socket.setblocking(False)

def send_all(connection, data):
    """Send all bytes, including larger HTML pages."""
    sent_total = 0

    while sent_total < len(data):
        sent = connection.send(data[sent_total:])

        if sent is None or sent <= 0:
            raise OSError("Connection closed before response completed")

        sent_total += sent

def send_response(connection, status, content_type, body):
    """Send a complete HTTP response without redirects."""
    body_bytes = body.encode("utf-8")

    header = (
        "HTTP/1.1 " + status + "\r\n"
        "Content-Type: " + content_type + "\r\n"
        "Content-Length: " + str(len(body_bytes)) + "\r\n"
        "Connection: close\r\n"
        "Cache-Control: no-store\r\n"
        "\r\n"
    ).encode("utf-8")

    send_all(connection, header)

    if body_bytes:
        send_all(connection, body_bytes)


def get_request_path(first_line):
    """Return only the path/query part of a normal HTTP GET request."""
    try:
        return first_line.split(" ")[1]
    except IndexError:
        return ""


def handle_web_request():
    """Handle one browser request, if one is waiting."""
    global oscillate

    try:
        connection, address = web_socket.accept()

    except OSError:
        return

    try:
        connection.setblocking(True)
        connection.settimeout(1)

        request = connection.recv(1024).decode()

        if not request:
            return

        first_line = request.split("\r\n")[0]
        path = get_request_path(first_line)

        print(first_line)

        # Return the current firmware version as JSON.
        # The browser fetches this when the user taps Check Update.
        if path == "/version":
            send_response(
                connection,
                "200 OK",
                "application/json",
                '{"version":"' + FIRMWARE_VERSION + '"}'
            )

        # Enable continuous 0 to 90 degree oscillation.
        elif path == "/mode?osc=true":
            oscillate = True
            send_response(
                connection,
                "200 OK",
                "text/plain",
                "oscillation enabled"
            )

        # Disable oscillator; manual control becomes active.
        elif path == "/mode?osc=false":
            oscillate = False
            send_response(
                connection,
                "200 OK",
                "text/plain",
                "manual mode enabled"
            )

        # Set movement speed: 1 through 10.
        elif path.startswith("/speed?value="):
            try:
                speed = int(path.split("/speed?value=")[1])
                set_speed(speed)

                send_response(
                    connection,
                    "200 OK",
                    "text/plain",
                    "speed updated"
                )

            except (ValueError, IndexError):
                send_response(
                    connection,
                    "400 Bad Request",
                    "text/plain",
                    "invalid speed"
                )

        # Move to a selected manual position: 0 through 180 degrees.
        elif path.startswith("/move?pos="):
            try:
                angle = int(path.split("/move?pos=")[1])
                set_manual_position(angle)

                send_response(
                    connection,
                    "200 OK",
                    "text/plain",
                    "moving to selected position"
                )

            except (ValueError, IndexError):
                send_response(
                    connection,
                    "400 Bad Request",
                    "text/plain",
                    "invalid position"
                )

        # This deliberately includes /, /auth.html, /generate_204,
        # and all other captive-portal probe paths.
        else:
            send_response(
                connection,
                "200 OK",
                "text/html; charset=utf-8",
                HTML_PAGE
            )

    except OSError as error:
        error_code = error.args[0] if error.args else None

        # Ignore normal socket timing/cancel events.
        if error_code not in (11, 110):
            print("Web socket error:", error)

    except Exception as error:
        print("Web request error:", error)

    finally:
        try:
            connection.close()
        except:
            pass

# ====
# Main loop
# ====

while True:
    motor_update()
    handle_dns()
    handle_web_request()

    # Run less often to limit unnecessary garbage-collection work.
    gc.collect()
    time.sleep_ms(1)
