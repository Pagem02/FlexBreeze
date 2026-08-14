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
#   White  (1,1,1) — checking for update (connecting or fetching)
#   Purple (1,0,1) — error (AP failed to start)
#   Cyan   (0,1,1) — boot LED sequence only
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

    # LED reflects current operational state.
    if checking_update:
        set_led(1, 1, 1)   # White: checking for update.
    elif oscillate:
        set_led(0, 0, 1)   # Blue: oscillating.
    else:
        set_led(0, 1, 0)   # Green: manual / hold.

    # Keep oscillation target up to date when in auto mode.
    if oscillate:
        if current_step >= STEPS_OSC:
            oscillation_direction = -1
        elif current_step <= 0:
            oscillation_direction = 1

        target_step = STEPS_OSC if oscillation_direction == 1 else 0

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


def motor_sleep_ms(duration_ms):
    """
    Non-blocking replacement for time.sleep_ms() used inside run_update_check.
    Keeps the motor stepping, DNS answering, and web requests handled during
    waits so the phone stays connected to the AP and the UI stays responsive.
    """
    deadline = time.ticks_add(time.ticks_ms(), duration_ms)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        motor_update()
        handle_dns()
        handle_web_request()
        time.sleep_ms(1)


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
#
# LED colour guide (full set):
#   Red    (1,0,0) — booting
#   Yellow (1,1,0) — AP starting
#   Green  (0,1,0) — ready / manual hold
#   Blue   (0,0,1) — oscillating
#   White  (1,1,1) — checking for update (Wi-Fi connecting or fetching)
#   Purple (1,0,1) — error
# ====

update_state    = "idle"
update_result   = {}
update_pending  = False
checking_update = False   # True while run_update_check() is running.


def _dns_query(hostname, dns_ip, sta_ip):
    """
    Resolve hostname via a raw UDP DNS query sent directly to dns_ip:53.
    The socket is bound to sta_ip so the packet routes out the STA
    interface rather than the AP interface (critical in AP+STA mode).
    Returns the first IPv4 address string, or None on failure.
    """
    qname = b""
    for label in hostname.encode().split(b"."):
        qname += bytes([len(label)]) + label
    qname += b"\x00"

    packet = (
        b"\xab\xcd"    # transaction ID
        b"\x01\x00"    # flags: standard query, recursion desired
        b"\x00\x01"    # QDCOUNT = 1
        b"\x00\x00"    # ANCOUNT = 0
        b"\x00\x00"    # NSCOUNT = 0
        b"\x00\x00"    # ARCOUNT = 0
        + qname
        + b"\x00\x01"  # QTYPE  = A
        + b"\x00\x01"  # QCLASS = IN
    )

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(5)
    try:
        # Bind to the STA IP so the OS routes via the STA interface, not AP.
        s.bind((sta_ip, 0))
        s.sendto(packet, (dns_ip, 53))
        resp = s.recv(512)
    except Exception as e:
        print("[dns] query failed:", e)
        return None
    finally:
        try:
            s.close()
        except:
            pass

    if len(resp) < 12:
        return None
    ancount = (resp[6] << 8) | resp[7]
    if ancount == 0:
        print("[dns] no answers for", hostname)
        return None

    # Skip header (12 bytes) + question section
    pos = 12
    while pos < len(resp):
        ln = resp[pos]
        if ln == 0:
            pos += 1
            break
        if (ln & 0xC0) == 0xC0:
            pos += 2
            break
        pos += 1 + ln
    pos += 4   # skip QTYPE + QCLASS

    # Walk answer records looking for the first A record
    for _ in range(ancount):
        if pos >= len(resp):
            break
        if (resp[pos] & 0xC0) == 0xC0:
            pos += 2
        else:
            while pos < len(resp) and resp[pos] != 0:
                pos += resp[pos] + 1
            pos += 1
        if pos + 10 > len(resp):
            break
        rtype = (resp[pos]     << 8) | resp[pos + 1]
        rdlen = (resp[pos + 8] << 8) | resp[pos + 9]
        pos  += 10
        if rtype == 1 and rdlen == 4 and pos + 4 <= len(resp):
            return "{}.{}.{}.{}".format(
                resp[pos], resp[pos + 1], resp[pos + 2], resp[pos + 3]
            )
        pos += rdlen

    print("[dns] no A record found for", hostname)
    return None


def _https_get_json(host_ip, hostname, path, sta_ip):
    """
    Open a TLS connection to host_ip:443 and return the parsed JSON body.
    The socket is bound to sta_ip so traffic routes via the STA interface.
    HTTP/1.0 is used so the server closes the connection after the body —
    no chunked-encoding parsing required.
    """
    import ssl as _ssl

    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(20)
    try:
        # Bind to STA interface before connecting.
        raw.bind((sta_ip, 0))
        raw.connect((host_ip, 443))
        tls = _ssl.wrap_socket(raw, server_hostname=hostname)
        tls.write((
            "GET " + path + " HTTP/1.0\r\n"
            "Host: " + hostname + "\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode())

        buf = bytearray()
        while True:
            chunk = tls.read(512)
            if not chunk:
                break
            buf.extend(chunk)

        sep = buf.find(b"\r\n\r\n")
        if sep < 0:
            raise ValueError("no HTTP header separator in response")
        return json.loads(buf[sep + 4:])
    finally:
        try:
            raw.close()
        except:
            pass


def scan_networks():
    """
    Scan for visible Wi-Fi networks and return a list of {ssid, rssi} dicts
    sorted strongest first.  STA is permanently active from boot so no
    active() call is needed here.
    """
    sta = network.WLAN(network.STA_IF)
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

    CYW43 / MicroPython AP+STA rules enforced here:
    - STA is activated once at boot and never toggled.  Calling
      sta.active(False) while the AP is running disrupts the shared CYW43
      radio and kills the AP.
    - Every individual WiFi driver call (sta.status, sta.isconnected,
      sta.connect, sta.disconnect) is wrapped in its own try/except so a
      driver OSError can never silently propagate to the generic handler.
    - The connect section and the fetch section each have their own
      try/except so the error message always indicates WHERE the failure
      occurred, making field diagnosis possible without Thonny.
    - urequests is imported before connecting to free as much RAM as
      possible for the TLS stack during the HTTPS fetch.
    - motor_sleep_ms() keeps motor / DNS / web alive during all waits.
    """
    global update_state, update_result, checking_update

    checking_update = True

    STAT_GOT_IP       =  3
    STAT_CONNECTING   =  1
    STAT_IDLE         =  0
    STAT_CONNECT_FAIL = -1
    STAT_NO_AP_FOUND  = -2
    STAT_WRONG_PASS   = -3
    DEFINITIVE_FAILS  = (STAT_WRONG_PASS, STAT_NO_AP_FOUND, STAT_CONNECT_FAIL)

    MAX_ATTEMPTS      = 3
    CONNECT_TIMEOUT_S = 30

    sta = network.WLAN(network.STA_IF)

    try:
        # ---- Check saved credentials ----
        config = load_config()
        if not config or not config.get("ssid"):
            update_state = "needs_setup"
            return

        ssid     = config["ssid"]
        password = config.get("password", "")

        # urequests is NOT used — we do our own DNS + raw TLS fetch.
        # (urequests calls socket.getaddrinfo() which in AP+STA mode
        # uses lwIP's internal resolver; that resolver queries our own
        # captive-portal DNS server and gets SERVFAIL for GitHub.)

        # ---- Connect to home Wi-Fi ----
        update_state = "connecting"
        connected    = False
        last_status  = STAT_IDLE

        for attempt in range(1, MAX_ATTEMPTS + 1):
            print("[update] attempt", attempt, "/ connecting to:", ssid)

            # Cleanly end any previous session.
            try:
                sta.disconnect()
            except:
                pass

            # Wait for driver to settle at IDLE before issuing connect().
            # Calling connect() while the driver is still in a failure
            # state from a previous attempt can cause it to fail instantly.
            idle_deadline = time.ticks_add(time.ticks_ms(), 3000)
            while time.ticks_diff(idle_deadline, time.ticks_ms()) > 0:
                try:
                    s = sta.status()
                except:
                    s = STAT_IDLE
                if s in (STAT_IDLE,) + DEFINITIVE_FAILS:
                    break
                motor_sleep_ms(100)

            # Issue connect().  On Pico W this can raise OSError(-2)
            # immediately when the SSID is not currently visible.
            last_status = STAT_CONNECTING
            try:
                sta.connect(ssid, password)
            except OSError as ce:
                code = ce.args[0] if ce.args else 0
                last_status = {
                    -2: STAT_NO_AP_FOUND,
                    -3: STAT_WRONG_PASS,
                }.get(code, STAT_CONNECT_FAIL)
                print("[update] connect() raised OSError", code,
                      "→ mapped status", last_status)

            # Poll until connected or definitively failed.
            if last_status == STAT_CONNECTING:
                deadline = time.ticks_add(time.ticks_ms(),
                                          CONNECT_TIMEOUT_S * 1000)
                while time.ticks_diff(deadline, time.ticks_ms()) > 0:
                    try:
                        if sta.isconnected():
                            connected   = True
                            last_status = STAT_GOT_IP
                            break
                        s = sta.status()
                    except OSError as se:
                        # Driver error during polling — treat as a failure.
                        s = se.args[0] if se.args else STAT_CONNECT_FAIL
                    except:
                        s = STAT_CONNECT_FAIL

                    if s in DEFINITIVE_FAILS:
                        last_status = s
                        break

                    motor_sleep_ms(200)

            print("[update] attempt", attempt, "done —",
                  "CONNECTED" if connected else "failed, status=" + str(last_status))

            if connected:
                break

            if attempt < MAX_ATTEMPTS:
                motor_sleep_ms(2000)

        # ---- Build connection error message if still not connected ----
        if not connected:
            if last_status == STAT_WRONG_PASS:
                msg = (
                    'Wrong password for "' + ssid + '". '
                    'Please re-enter your Wi-Fi password and try again.'
                )
            elif last_status == STAT_NO_AP_FOUND:
                msg = (
                    'Network "' + ssid + '" not found. '
                    'Move FlexBreeze closer to your router and try again.'
                )
            elif last_status == STAT_CONNECT_FAIL:
                msg = (
                    'Connection to "' + ssid + '" failed. '
                    'Check your router is working and try again.'
                )
            else:
                msg = (
                    'Could not connect to "' + ssid + '" '
                    '(code ' + str(last_status) + '). '
                    'Check your Wi-Fi password and router, then try again.'
                )
            update_state  = "error"
            update_result = {"message": msg}
            return

        # ---- Fetch update.json from GitHub ----
        # Motor pauses during the TLS fetch — acceptable per design.
        # We:
        #   1. Let the STA routing table settle briefly (motor still runs)
        #   2. Do our OWN DNS query to the STA's DNS server — this avoids
        #      lwIP's internal resolver routing the query through the AP
        #      interface and hitting our own captive-portal DNS (which
        #      returns SERVFAIL for GitHub hostnames).
        #   3. Open a raw TCP+TLS socket to the resolved IP, send an
        #      HTTP/1.0 GET, and parse the JSON body.
        update_state = "fetching"
        gc.collect()
        motor_sleep_ms(800)
        gc.collect()

        GITHUB_HOST = "raw.githubusercontent.com"
        GITHUB_PATH = "/Pagem02/FlexBreeze/main/Latest/update.json"

        sta_cfg = sta.ifconfig()
        sta_ip  = sta_cfg[0]
        gw_ip   = sta_cfg[2]
        dns_ip  = sta_cfg[3]
        print("[update] STA ifconfig:", sta_cfg)

        # ---- Wait for the default route to be installed in lwIP ----
        # sta.isconnected() returns True when DHCP assigns the IP, but lwIP
        # may not have written the default gateway route (0.0.0.0/0) yet.
        # Until it has, any packet to a non-local address is immediately
        # rejected with EHOSTUNREACH (errno 113).
        #
        # IMPORTANT: the probe must target an EXTERNAL IP (not the gateway).
        # The gateway is on the local subnet and is reachable even without a
        # default route, so probing it never actually waits for the route.
        # 8.8.8.8 (Google DNS) is definitively external on any home network.
        # sendto() succeeds as soon as lwIP can queue the packet — i.e. the
        # moment the default route exists — so EHOSTUNREACH is the only error
        # that means "not ready yet".  Any other result means ready.
        print("[update] waiting for default route (probing 8.8.8.8)")
        route_ready = False
        route_deadline = time.ticks_add(time.ticks_ms(), 10000)
        while time.ticks_diff(route_deadline, time.ticks_ms()) > 0:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.settimeout(1)
            try:
                probe.bind((sta_ip, 0))
                probe.sendto(b"", ("8.8.8.8", 53))
                route_ready = True
                break
            except OSError as pe:
                if pe.args[0] == 113:   # EHOSTUNREACH — route not ready yet
                    motor_sleep_ms(300)
                else:
                    # Any other OSError (ETIMEDOUT etc.) means the packet was
                    # at least queued — default route is present.
                    route_ready = True
                    break
            except:
                route_ready = True
                break
            finally:
                try:
                    probe.close()
                except:
                    pass

        if not route_ready:
            update_state  = "error"
            update_result = {
                "message": (
                    'Connected to "' + ssid + '" but the network route '
                    'did not become ready in time. '
                    'Try again — if this keeps happening, restart FlexBreeze.'
                )
            }
            return

        print("[update] route ready")

        # ---- DNS: query the gateway first ----
        # The gateway (e.g. 10.0.0.1) is on the local subnet — reachable
        # without a default route — and home routers always forward DNS.
        # We only fall back to the configured DNS server (e.g. 1.1.1.1)
        # if the gateway doesn't answer, since that may also need routing.
        print("[update] resolving", GITHUB_HOST, "via gateway", gw_ip)
        github_ip = _dns_query(GITHUB_HOST, gw_ip, sta_ip)

        if not github_ip and dns_ip != gw_ip:
            print("[update] gateway DNS failed, retrying via", dns_ip)
            github_ip = _dns_query(GITHUB_HOST, dns_ip, sta_ip)

        print("[update] resolved to:", github_ip)

        if not github_ip:
            update_state  = "error"
            update_result = {
                "message": (
                    'Connected to "' + ssid + '" but could not resolve '
                    'the update server hostname. '
                    'Check your router has internet access and try again.'
                )
            }
            return

        print("[update] fetching from", GITHUB_HOST, "at", github_ip)
        try:
            data = _https_get_json(github_ip, GITHUB_HOST, GITHUB_PATH, sta_ip)
        except OSError as fe:
            code = fe.args[0] if fe.args else 0
            print("[update] HTTPS fetch OSError:", code)
            update_state  = "error"
            update_result = {
                "message": (
                    'Connected to "' + ssid + '" but could not download '
                    'the update (error ' + str(code) + '). '
                    'Check your router has internet access and try again.'
                )
            }
            return
        except Exception as fe:
            print("[update] HTTPS fetch error:", fe)
            update_state  = "error"
            update_result = {
                "message": (
                    'Connected to "' + ssid + '" but update download '
                    'failed: ' + str(fe)
                )
            }
            return

        gc.collect()
        update_result = {
            "latest": data.get("version", "unknown"),
            "notes":  data.get("notes", "")
        }
        update_state = "done"
        print("[update] done — latest version:", update_result["latest"])

    except Exception as exc:
        # Last-resort handler.  The state variable tells us where it was.
        print("[update] unexpected error at state=" + update_state + ":", exc)
        update_state  = "error"
        update_result = {
            "message": (
                "Unexpected error during " + update_state +
                ": " + str(type(exc).__name__) + ": " + str(exc)
            )
        }

    finally:
        checking_update = False
        try:
            sta.disconnect()
        except:
            pass
        # Do NOT call sta.active(False) — STA must stay permanently active.
        # Toggling it while the AP is running disrupts the shared CYW43 radio.
        gc.collect()


# ====
# Boot LED sequence — flash every colour to confirm LED health.
# ====

for _r, _g, _b in (
    (1, 0, 0),  # Red
    (1, 1, 0),  # Yellow
    (0, 1, 0),  # Green
    (0, 1, 1),  # Cyan
    (0, 0, 1),  # Blue
    (1, 0, 1),  # Purple
    (1, 1, 1),  # White
):
    set_led(_r, _g, _b)
    time.sleep_ms(200)

set_led(0, 0, 0)   # Brief off — end of sequence.
time.sleep_ms(100)

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

# Activate STA now and leave it permanently active.
# The CYW43 chip shares one radio between AP and STA — calling
# sta.active(False) at any point while the AP is running would disrupt
# the shared radio.  By activating STA here once, run_update_check()
# only needs sta.disconnect() / sta.connect() and never active().
network.WLAN(network.STA_IF).active(True)

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
