import gc
import json
import network
import socket
import time
from machine import Pin, PWM

# ====
# Wi-Fi access-point settings
# ====

AP_SSID = "FlexBreezeOss"
AP_PASS = "12345678"   # Must be at least 8 characters.

# Current firmware version — keep in sync with Latest/update.json on GitHub.
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

# Per-channel brightness scale (0-65535). Tune to balance colours.
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


def clear_gpio():
    """
    Safe shutdown: turn off all outputs before the program exits.
    Prevents the motor coils staying energised and the LED staying lit.
    """
    try:
        LED_R.duty_u16(LED_DUTY_OFF)
        LED_G.duty_u16(LED_DUTY_OFF)
        LED_B.duty_u16(LED_DUTY_OFF)
        LED_R.deinit()
        LED_G.deinit()
        LED_B.deinit()
    except:
        pass

    for pin in STEP_PINS:
        try:
            pin.value(0)
        except:
            pass


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
    1  = 21 ms per half-step (slowest)
    10 = 3 ms per half-step (fastest)
    """
    global speed_setting, step_interval_ms

    speed = max(1, min(10, speed))
    speed_setting = speed
    step_interval_ms = 23 - (speed * 2)


def set_manual_position(angle):
    """
    Set a requested position between 0 and 180 degrees.
    A manual move always stops oscillation.
    """
    global target_step, oscillate

    angle = max(0, min(180, angle))
    oscillate = False
    target_step = int((angle * STEPS_180) / 180)


def motor_update():
    """
    Run one possible motor half-step. Called repeatedly from the main loop
    and from inside the Wi-Fi connection wait so the motor keeps moving.
    """
    global current_step, target_step, sequence_index
    global oscillation_direction, last_step_time

    if oscillate:
        set_led(0, 0, 1)   # Blue: oscillating.

        if current_step >= STEPS_OSC:
            oscillation_direction = -1
        elif current_step <= 0:
            oscillation_direction = 1

        target_step = STEPS_OSC if oscillation_direction == 1 else 0

    else:
        set_led(0, 1, 0)   # Green: manual / hold.

    if current_step == target_step:
        return

    now = time.ticks_ms()
    if time.ticks_diff(now, last_step_time) < step_interval_ms:
        return

    direction = 1 if target_step > current_step else -1
    current_step += direction
    sequence_index = (sequence_index + direction) % len(SEQUENCE)
    set_motor_outputs(sequence_index)
    last_step_time = now


# ====
# Config and URL helpers
# ====

def load_config():
    """Return saved Wi-Fi credentials dict, or None if not set."""
    try:
        with open("config.json", "r") as f:
            return json.loads(f.read())
    except:
        return None


def save_config(ssid, password):
    """Persist Wi-Fi credentials to config.json on flash."""
    with open("config.json", "w") as f:
        f.write(json.dumps({"ssid": ssid, "password": password}))


def url_decode(s):
    """Decode a percent-encoded URL component (e.g. from a query string)."""
    result = []
    i = 0
    while i < len(s):
        if s[i] == "%" and i + 2 < len(s):
            try:
                result.append(chr(int(s[i + 1:i + 3], 16)))
                i += 3
                continue
            except ValueError:
                pass
        if s[i] == "+":
            result.append(" ")
        else:
            result.append(s[i])
        i += 1
    return "".join(result)


def parse_query(path):
    """Extract {key: value} dict from a path like /endpoint?key=val&k2=v2."""
    params = {}
    if "?" not in path:
        return params
    query = path.split("?", 1)[1]
    for pair in query.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[url_decode(k)] = url_decode(v)
    return params


# ====
# Update check state
#
# States:
#   idle        — no check requested
#   needs_setup — no Wi-Fi credentials saved; show setup form
#   connecting  — attempting to join home Wi-Fi
#   fetching    — connected; downloading update.json from GitHub
#   done        — check complete; result is in update_result
#   error       — something failed; message is in update_result
# ====

update_state  = "idle"
update_result = {}
update_pending = False


def scan_networks():
    """
    Activate STA interface, scan for visible Wi-Fi networks, and return a
    list of {ssid, rssi} dicts sorted strongest first.
    STA is left active so a follow-up connect() call does not need to
    re-activate it.
    """
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    try:
        raw = sta.scan()
        seen = set()
        nets = []
        for entry in raw:
            try:
                ssid = entry[0].decode("utf-8", "ignore").strip()
            except:
                continue
            if ssid and ssid not in seen:
                seen.add(ssid)
                nets.append({"ssid": ssid, "rssi": entry[3]})
        nets.sort(key=lambda n: -n["rssi"])
        return nets
    except:
        return []


def run_update_check():
    """
    Connect to saved home Wi-Fi (STA mode) while keeping the Pico AP active,
    fetch update.json from GitHub, then disconnect.

    During the connection wait the motor and DNS/web handlers are kept
    running so the phone stays responsive. The brief HTTPS fetch (~3-5 s)
    is the only window where the main loop is fully blocked.
    """
    global update_state, update_result

    sta = network.WLAN(network.STA_IF)

    try:
        config = load_config()
        if not config or not config.get("ssid"):
            update_state = "needs_setup"
            return

        ssid     = config["ssid"]
        password = config.get("password", "")

        update_state = "connecting"

        sta.active(True)
        if sta.isconnected():
            sta.disconnect()
            time.sleep_ms(300)

        sta.connect(ssid, password)

        # Wait up to 15 s for connection.
        # Motor, DNS, and web requests are all served during the wait so
        # the phone stays connected to the AP and the UI stays responsive.
        connected = False
        for _ in range(150):
            motor_update()
            handle_dns()
            handle_web_request()
            if sta.isconnected():
                connected = True
                break
            time.sleep_ms(100)

        if not connected:
            update_state  = "error"
            update_result = {"message": "Could not connect to \"" + ssid + "\". Check the password."}
            return

        update_state = "fetching"
        gc.collect()

        # The Pico now has internet via STA while the phone stays on the AP.
        try:
            import urequests
        except ImportError:
            import requests as urequests

        resp = urequests.get(
            "https://raw.githubusercontent.com/Pagem02/FlexBreeze/main/Latest/update.json",
            timeout=10
        )
        data = resp.json()
        resp.close()
        del resp
        gc.collect()

        update_result = {
            "latest" : data.get("version", "unknown"),
            "notes"  : data.get("notes", "")
        }
        update_state = "done"

    except Exception as exc:
        update_state  = "error"
        update_result = {"message": str(exc)}

    finally:
        try:
            sta.disconnect()
        except:
            pass
        try:
            sta.active(False)
        except:
            pass
        gc.collect()


# ====
# Access point setup
# ====

set_led(1, 0, 0)   # Red: booting.

ap = network.WLAN(network.AP_IF)
ap.active(True)

set_led(1, 1, 0)   # Yellow: AP starting.

ap.config(essid=AP_SSID, password=AP_PASS)
time.sleep_ms(800)

if not ap.active():
    set_led(1, 0, 1)   # Purple: AP failed.
    clear_gpio()
    raise RuntimeError("Access point failed to start")

PICO_IP  = ap.ifconfig()[0]
ip_bytes = bytes(map(int, PICO_IP.split(".")))

print("Access point started")
print("SSID    :", AP_SSID)
print("Firmware:", FIRMWARE_VERSION)
print("Pico IP :", PICO_IP)
print("Browse to: http://" + PICO_IP)

set_led(0, 1, 0)   # Green: ready.

# ====
# Captive-portal DNS server
# ====

# These hostnames get SERVFAIL so the phone's OS can still resolve them
# via cellular DNS if needed (belt-and-braces — the Pico now fetches
# GitHub itself, so the phone never needs to reach it directly).
DNS_PASSTHROUGH = (
    "raw.githubusercontent.com",
    "github.com",
)

dns_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
dns_socket.bind(("0.0.0.0", 53))
dns_socket.setblocking(False)


def parse_dns_hostname(data):
    """Extract the queried hostname from a raw DNS request packet."""
    try:
        offset = 12
        labels = []
        while offset < len(data):
            length = data[offset]
            offset += 1
            if length == 0:
                break
            labels.append(data[offset:offset + length].decode("utf-8", "ignore"))
            offset += length
        return ".".join(labels)
    except:
        return ""


def handle_dns():
    """
    Resolve all hostnames to the Pico IP (captive portal), except those in
    DNS_PASSTHROUGH which receive SERVFAIL so the phone can fall back to
    cellular DNS for those specific domains.
    """
    try:
        request, client_address = dns_socket.recvfrom(512)
        if len(request) < 12:
            return

        transaction_id = request[0:2]
        hostname       = parse_dns_hostname(request)

        if hostname in DNS_PASSTHROUGH:
            servfail = (
                transaction_id
                + b"\x81\x82"
                + b"\x00\x01"
                + b"\x00\x00"
                + b"\x00\x00"
                + b"\x00\x00"
                + request[12:]
            )
            dns_socket.sendto(servfail, client_address)
            return

        header = (
            transaction_id
            + b"\x81\x80"
            + b"\x00\x01"
            + b"\x00\x01"
            + b"\x00\x00"
            + b"\x00\x00"
        )
        question = request[12:]
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
    sent_total = 0
    while sent_total < len(data):
        sent = connection.send(data[sent_total:])
        if sent is None or sent <= 0:
            raise OSError("Connection closed before response completed")
        sent_total += sent


def send_response(connection, status, content_type, body):
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
    try:
        return first_line.split(" ")[1]
    except IndexError:
        return ""


def handle_web_request():
    """Handle one pending browser request, if any."""
    global oscillate, update_state, update_result, update_pending

    try:
        connection, address = web_socket.accept()
    except OSError:
        return

    try:
        connection.setblocking(True)
        connection.settimeout(0.3)

        request = connection.recv(1024).decode()
        if not request:
            return

        first_line = request.split("\r\n")[0]
        path       = get_request_path(first_line)

        print(first_line)

        # ---- Current firmware version (JSON) ----
        if path == "/version":
            send_response(
                connection, "200 OK", "application/json",
                '{"version":"' + FIRMWARE_VERSION + '"}'
            )

        # ---- Update check status (polled by browser) ----
        elif path == "/update-status":
            body = json.dumps({
                "state"  : update_state,
                "result" : update_result
            })
            send_response(connection, "200 OK", "application/json", body)

        # ---- Trigger an update check ----
        elif path == "/check-update":
            config = load_config()
            if not config or not config.get("ssid"):
                update_state  = "needs_setup"
                update_result = {}
                body = json.dumps({"state": "needs_setup"})
            else:
                update_state   = "connecting"
                update_result  = {}
                update_pending = True
                body = json.dumps({"state": "connecting"})
            send_response(connection, "200 OK", "application/json", body)

        # ---- Scan for visible Wi-Fi networks ----
        elif path == "/scan-wifi":
            nets = scan_networks()
            send_response(connection, "200 OK", "application/json", json.dumps(nets))

        # ---- Save Wi-Fi credentials and start update check ----
        # Called as: /save-wifi?ssid=MyNetwork&pass=MyPassword
        elif path.startswith("/save-wifi"):
            params   = parse_query(path)
            ssid     = params.get("ssid", "").strip()
            password = params.get("pass", "")
            if ssid:
                save_config(ssid, password)
                update_state   = "connecting"
                update_result  = {}
                update_pending = True
                body = json.dumps({"state": "connecting"})
            else:
                body = json.dumps({"state": "error", "result": {"message": "No SSID provided."}})
            send_response(connection, "200 OK", "application/json", body)

        # ---- Oscillation on ----
        elif path == "/mode?osc=true":
            oscillate = True
            send_response(connection, "200 OK", "text/plain", "oscillation enabled")

        # ---- Oscillation off ----
        elif path == "/mode?osc=false":
            oscillate = False
            send_response(connection, "200 OK", "text/plain", "manual mode enabled")

        # ---- Speed ----
        elif path.startswith("/speed?value="):
            try:
                set_speed(int(path.split("/speed?value=")[1]))
                send_response(connection, "200 OK", "text/plain", "speed updated")
            except (ValueError, IndexError):
                send_response(connection, "400 Bad Request", "text/plain", "invalid speed")

        # ---- Manual position ----
        elif path.startswith("/move?pos="):
            try:
                set_manual_position(int(path.split("/move?pos=")[1]))
                send_response(connection, "200 OK", "text/plain", "moving to selected position")
            except (ValueError, IndexError):
                send_response(connection, "400 Bad Request", "text/plain", "invalid position")

        # ---- Captive portal + all other paths → serve the control page ----
        else:
            send_response(connection, "200 OK", "text/html; charset=utf-8", HTML_PAGE)

    except OSError as error:
        error_code = error.args[0] if error.args else None
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

gc_counter = 0

try:
    while True:
        if update_pending:
            # Set False before calling so a second press during the check
            # does not queue a duplicate run.
            update_pending = False
            run_update_check()
        else:
            motor_update()

        handle_dns()
        handle_web_request()

        gc_counter += 1
        if gc_counter >= 50:
            gc.collect()
            gc_counter = 0

        time.sleep_ms(1)

except KeyboardInterrupt:
    pass

finally:
    # Always clear GPIO on exit — stops motor coils staying energised
    # and the LED staying lit if the program is stopped from Thonny.
    clear_gpio()
