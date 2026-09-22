# OSHI MeshCore Bridge

A fork of the [Akita Meshtastic-MeshCore Bridge](https://github.com/AkitaEngineering/Akita-Meshtastic-Meshcore-Bridge)
(GPL-3.0) with one addition: an **OSHI transport** (`oshi_bridge/`, `run_oshi_bridge.py`). It relays
[OSHI Mesh Protocol](https://github.com/Lastoneparis/oshi-mesh-firmware/tree/oshi/src/oshi) (OMP) frames
between a Meshtastic mesh and a MeshCore mesh, so an OSHI message can reach its destination
across both networks:

```
 OSHI phone ── OSHI node ~~ Meshtastic ~~ [bridge A] ~~~~ MeshCore mesh ~~~~ [bridge B] ~~ Meshtastic ~~ OSHI node ── OSHI phone
                         (OSHI channel)   (Pi + 2 radios)  (#oshi-bridge ch.)  (Pi + 2 radios)
```

The upstream Akita text bridge (`run_bridge.py`, `ammb/`) is unchanged and documented further down.
The two cannot share the same radios at the same time: a serial port has one owner.

> **Status: not yet tested on hardware.** Everything below is verified against the MeshCore
> companion firmware and meshcore_py *sources* and by the unit/simulation tests in `tests_oshi/`,
> not on air. See [Limits](#limits).

## What it does

| OMP frame (heard on Meshtastic) | What the bridge does |
|---|---|
| `DATA` broadcast on any channel (normally the private `OSHI` channel) | sent byte-for-byte into MeshCore; the far bridge re-injects it as a `PRIVATE_APP` broadcast on its `OSHI` channel. The OMP header carries `origin` and final `dest`, so the destination accepts it even though its Meshtastic `from` is the bridge. |
| `SACK` from the destination to the far bridge | **not relayed as-is** (see below). Complete → the far bridge sends an OMP `RECEIPT` over MeshCore, and the near bridge DMs it to the origin, whose phone shows *delivered*. Partial → the far bridge re-injects the fragments it holds and asks the near bridge (an OMP SACK over MeshCore) for the ones it never received. |
| `DATA` to `OMP_DEST_INTERNET`, `CUSTODY`, `BEACON`, `PULL`, `STATUS` | never relayed. They are link-local; a relayed `BEACON` would make OSHI nodes pick the bridge as a custodian, which it is not. |
| Anything that is not `PRIVATE_APP` starting with `OS` v1 | ignored. |

On MeshCore, frames travel as **group-channel datagrams** (`PAYLOAD_TYPE_GRP_DATA`, companion command
`CMD_SEND_CHANNEL_DATA` = 0x3E, received as `RESP_CODE_CHANNEL_DATA_RECV` = 0x1B) on a dedicated channel
(default `#oshi-bridge`, slot 7) with `data_type = 0xFF4F`. A MeshCore user who does not carry that
channel cannot decrypt them and never sees them; repeaters relay them like any other flood packet.

### MeshCore payload limit and re-fragmentation

A MeshCore packet payload is at most 184 bytes (`MAX_PACKET_PAYLOAD`). A channel datagram carries at most
**165 bytes** of application data in the current firmware (`MAX_GROUP_DATA_LENGTH = 184 - 16 - 3`; the
companion frame limit `MAX_CHANNEL_DATA_LENGTH = MAX_FRAME_SIZE - 9` is 167, the protocol doc states 163).
An OMP frame is up to 200 bytes (18-byte header + 182 data bytes), so it **does not fit**. The bridge wraps
every frame in a 9-byte envelope and splits it into datagrams of at most 160 bytes (`max_datagram`):

```
"OB" | ver<<4 | bridge u32 (Meshtastic node num) | seq u8 | part<<4 | total | slice of the OMP frame
```

A full 200-byte DATA frame costs two MeshCore packets; SACK (20 B) and RECEIPT (15 B) fit in one.
Parts are reassembled per `(bridge, seq)` and dropped after 90 s if incomplete; OMP's own repair
recovers the frame.

### Why SACKs are not relayed as-is

In `oshi-mesh-firmware`, `Outbox::onSack` only accepts a SACK whose Meshtastic sender is the node the
message was sent to (`e.linkTo == from`). A SACK re-injected by a bridge comes from the bridge's node
number, so the origin would silently ignore it. `RECEIPT` is accepted from any sender that passes
`controlFrameTrusted` and names the origin, so that is what the bridge sends back. See
[Firmware changes that would help](#firmware-changes-that-would-help) for what this does *not* fix.

### Loops, duplicates, duty cycle

* **Dedup** by `(origin, msgId, type, idx)`: a frame received from the other network within
  `loop_ttl_s` (600 s) is a loop and dropped; the same frame forwarded again from the same side within
  `repeat_s` (15 s) is a duplicate; at most `max_forwards` (6) per side per `loop_ttl_s` as a hard cap.
  Per message, a bridge that sent a message into MeshCore never injects it back, and a bridge that injected
  it never sends it back. Two bridges in range of each other on both networks therefore do not ping-pong
  (covered by `test_two_bridges_between_joined_islands_do_not_ping_pong`).
* **Airtime budget** per radio: at most `mesh_duty_percent` / `mc_duty_percent` (default 5 %) of
  `duty_window_s` (1 h), and `min_gap_s` (2.5 s, OMP's own pacing) between two transmissions. Airtime is
  computed with the Semtech formula from the LoRa settings in the config. Control traffic (receipts, repair
  requests) goes first; a full queue drops data before control.
* After delivery, the origin keeps polling (see limits); the near bridge answers those polls with a local
  RECEIPT instead of spending MeshCore airtime.

## Hardware and wiring

Per bridge site: one host (Raspberry Pi, PC, Mac) and two radios on USB.

1. **Meshtastic radio running stock Meshtastic firmware (2.5+), not OSHI firmware.** The OSHI firmware's
   `OshiModule` swallows OMP frames before they reach the client API and treats OMP frames from the
   client as its own outbox, so a bridge on an OSHI radio would see nothing.
2. **MeshCore radio running the companion-radio firmware, v1.15 or newer** (USB serial, or TCP/BLE).
   Repeater or room-server firmware does not expose the companion protocol.

The two radios must not be the same device: one LoRa radio cannot listen on two networks. Keep antennas
apart (or different bands) so one does not deafen the other.

### Meshtastic radio setup

Add the OSHI channel (same name and PSK as OSHI firmware, `SHA-256("OSHI Mesh channel v1")`) as a
secondary channel. With the Meshtastic CLI, on the first free index (here 1):

```
meshtastic --ch-add OSHI
meshtastic --ch-index 1 --ch-set psk base64:KUNtGa9VvU4gJHMjyP9FV4bwAxGYF+Fs998wA2hMuVA=
```

(hex `29436d19af55bd4e20247323c8ff455786f003119817e16cf7df3003684cb950`). The bridge finds the channel by
name and refuses to start without it.

### MeshCore radio setup

Nothing to do by hand: with `configure_channel = true` the bridge writes the channel into slot
`channel_idx`. Every bridge that should interoperate must use the same channel name/secret and
`data_type`. `#oshi-bridge` is a public hashtag channel (its key is derived from the name); set
`channel_secret_hex` for a private network of bridges.

## Install and run

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # meshtastic, meshcore (meshcore_py needs Python 3.10+)
cp examples/oshi_bridge.ini.example oshi_bridge.ini   # set the two ports
python run_oshi_bridge.py -c oshi_bridge.ini
```

The process exits non-zero if either radio disconnects; `packaging/oshi-bridge.service` restarts it under
systemd. Stats (frames each way, receipts, repairs, drops, airtime used) are logged every
`stats_interval_s`.

## Tests

```
pip install pytest
pytest tests_oshi
```

No hardware needed. `tests_oshi/sim.py` builds an in-memory world (two Meshtastic islands, a MeshCore
medium, OSHI end nodes modelled on `OshiModule::receiveData`) and checks: a 3-fragment message crossing
and the origin getting its RECEIPT, repair of a fragment lost on MeshCore and of one lost on the far
mesh, two bridges on joined islands not looping, two near bridges delivering once, duty-cycle and queue
bounds, forged SACKs ignored, the exact companion command bytes, and the runner's thread/async wiring.

## Limits

* **Not verified on air.** Command/response framing comes from MeshCore `examples/companion_radio/MyMesh.cpp`
  and meshcore_py `reader.py`; the OMP codec is checked byte-for-byte against `OshiProtocol.cpp`, but no
  frame has crossed a real MeshCore network yet.
* **Only broadcast DATA is visible.** OSHI firmware sends a frame as a PKI DM when it holds the destination's
  public key; the bridge radio cannot decrypt or even receive a DM addressed to someone else. A destination
  behind a bridge is normally never heard, so its key is normally unknown and frames go out as broadcasts,
  but an origin that learned the key earlier (e.g. both were once in range) will send DMs the bridge cannot
  carry. Fix: firmware change 2 below.
* **The origin keeps retrying after delivery.** RECEIPT only updates the phone; the outbox entry keeps
  polling for up to 5 rounds, then parks the message and reports FAILED after 72 h (`parkedTtlMs`). The
  phone shows *delivered* first, but may later show *in custody* / *failed*. Fix: firmware change 1 below.
* **RECEIPT auth.** The origin accepts the bridge's RECEIPT only if it is PKI-encrypted whenever the origin
  holds the bridge's key. The bridge sends it as a DM, which Meshtastic firmware PKI-encrypts when the
  bridge radio holds the origin's key; if only one side has the other's key, the RECEIPT is dropped.
* **Anyone on the MeshCore channel can inject frames** (group datagrams are unauthenticated). OMP bodies are
  end-to-end protected by the OSHI envelope, but a forged RECEIPT could show a false *delivered*. Use a
  private `channel_secret_hex` among trusted bridges.
* **Airtime.** A 200-byte OMP frame costs ~1.9 s on Meshtastic LONG_FAST and two MeshCore packets. Every
  bridged DATA frame is flooded on the far Meshtastic mesh whether or not the destination is there
  (`inject_only_known_dests` restricts it to nodes in the bridge radio's NodeDB).
* **Two near bridges** in range of the same origin both send each frame into MeshCore (the far bridge
  delivers it once). One bridge per island is the intended layout.

## Firmware changes that would help

Proposed for `oshi-mesh-firmware` (not applied here):

1. **Finish the outbox on RECEIPT** so a bridged message stops retrying once delivered. In
   `src/oshi/OshiOutbox.h` add `void onReceipt(const NoticeFrame &n, uint32_t nowMs);` and in
   `OshiOutbox.cpp`:
   ```cpp
   void Outbox::onReceipt(const NoticeFrame &n, uint32_t nowMs)
   {
       (void)nowMs;
       for (auto &e : entries)
           if (e.msg.msgId == n.msgId && e.msg.origin == n.origin && e.msg.dest == n.dest && e.state != State::DONE) {
               finish(e, MsgState::DELIVERED, n.dest);
               return;
           }
   }
   ```
   and in `OshiModule::handleOmp`, `case FrameType::RECEIPT`, call `outbox.onReceipt(nf, now);` next to
   `statusToPhone(s)` (and skip the `statusToPhone` there when `onReceipt` already emitted DELIVERED, to
   avoid two status frames).
2. **Do not DM a destination that is not directly heard.** In `OshiModule::transmit`, use PKI only when the
   destination was heard recently (e.g. `nodeDB->getMeshNode(linkTo)->last_heard` within 2 h and
   `hops_away` known), otherwise broadcast on the OSHI channel as today. Frames then remain visible to a
   bridge whenever the destination is not local.
3. **A `FLAG_VIA_BRIDGE` (1 << 3) DATA flag**, set by a far bridge when it re-injects (the bridge would
   rewrite the flags byte at offset 17). A destination seeing it would SACK as usual but the origin could
   treat the bridge's RECEIPT as authoritative and the app could show "via MeshCore". It would also let a
   second bridge on the same mesh refuse to carry it back, instead of relying on timing windows.
4. **Optional: accept a bridge's SACK.** `Outbox::onSack` could accept a SACK from a node that advertises a
   new `CAP_BRIDGE` bit in its BEACON when `e.linkTo` is not directly heard. That would let partial-SACK
   repair reach the origin itself instead of being handled bridge-to-bridge.

---

# Upstream: Akita Meshtastic Meshcore Bridge (AMMB)

**AMMB** is a flexible and robust software bridge designed by **Akita Engineering** to facilitate seamless, bidirectional communication between Meshtastic LoRa mesh networks and external systems via Serial or MQTT.

This bridge enables interoperability, allowing messages, sensor data (with appropriate translation), and potentially other information to flow between Meshtastic and devices connected via Serial (like MeshCore) or platforms integrated with MQTT.

---

## Features

### Core Functionality
- **Bidirectional Message Forwarding:** Relays messages originating from Meshtastic nodes to the configured external system (Serial or MQTT), and vice-versa.  
- **Multiple External Transports:** Supports connecting to the external system via:  
  - **Direct Serial:** Interfaces directly with devices (like MeshCore) via standard RS-232/USB serial ports.  
  - **MQTT:** Connects to an MQTT broker to exchange messages with IoT platforms or other MQTT clients.  
- **Configurable Serial Protocol:** Supports different serial communication protocols via `config.ini`. Includes `raw_serial` and `companion_radio` handlers for MeshCore Companion Mode (Binary + framed USB protocol).  
- **Robust Connection Management:** Automatically attempts to reconnect if connections are lost.

### Enhanced Features
- **REST API:** Built-in HTTP API for monitoring bridge status, metrics, and health (optional, configurable)
- **Health Monitoring:** Real-time health status tracking for all bridge components
- **Metrics Collection:** Comprehensive statistics on messages, connections, and performance
- **Full-Screen Command Center:** Textual-based terminal UI with live bridge state, metrics, health, queue depth, event feed, and log tail
- **Message Validation:** Automatic validation and sanitization of all messages
- **Rate Limiting:** Configurable per-window rate limiting to prevent message flooding
- **MQTT TLS/SSL Support:** Secure MQTT connections with TLS/SSL encryption
- **Comprehensive Logging:** Detailed logging with configurable log levels
- **Message Persistence:** Optional message logging to file for analysis and debugging  

---

## Installation & Usage

### Clone the Repository
    git clone https://github.com/AkitaEngineering/akita-meshtastic-meshcore-bridge.git
    cd akita-meshtastic-meshcore-bridge

### Set up Environment
    python -m venv venv
    source venv/bin/activate  # or .\venv\Scripts\activate on Windows
    pip install -r requirements.txt

### Configure
Copy `examples/config.ini.example` to `config.ini` and edit it.

- **For MeshCore (Companion USB):**  
  Set `EXTERNAL_TRANSPORT = serial` and `SERIAL_PROTOCOL = companion_radio`.

  Optional companion settings in `config.ini`:
  - `COMPANION_HANDSHAKE_ENABLED = True` (send initial device query/app start)
  - `COMPANION_CONTACTS_POLL_S = 0` (poll contacts/adverts; 0 disables)
  - `COMPANION_DEBUG = False` (enable raw byte logging)
  - `SERIAL_AUTO_SWITCH = True` (auto-switch between `json_newline` and `raw_serial` on repeated decode failures)
  - `MESHTASTIC_CHANNEL_INDEX = 1` and `MESHCORE_CHANNEL_INDEX = 2` e.g. only bridge messages from Meshtastic channel index 1 to/from MeshCore channel index 2
  - Companion device info, self info, contact sync, and adverts are decoded into structured events and surfaced in the sync logs and the terminal command center log tail

- **For MQTT:**  
  Set `EXTERNAL_TRANSPORT = mqtt` and configure broker details. Optionally enable TLS/SSL for secure connections.

  MeshCore observer firmware (for example [observer.gessaman.com](https://observer.gessaman.com/)) publishes LetsMesh PACKET JSON on `meshcore/{IATA}/{device_id}/packets`. The bridge accepts that format on `MQTT_TOPIC_IN` (wildcards such as `meshcore/+/+/packets` work). Group-channel text is decrypted with the MeshCore Public key plus any extra keys you configure. Set `MQTT_PAYLOAD_FORMAT = observer` to publish Meshtastic text as hashed MeshCore GRP_TXT packets that MQTT clients understand. `mqtt.rx=true` on the observer uplinks RF to MQTT; it does not by itself TX AMMB JSON onto LoRa.

- **For REST API (Optional):**  
  Set `API_ENABLED = True` and configure `API_HOST` and `API_PORT` to enable the monitoring API. Set `API_TOKEN` if the API is reachable beyond localhost.


### Run (Sync or Async)

- **Preflight check (recommended before field use):**
  python run_bridge_tui.py --check

- **Show effective config without secrets:**
  python run_bridge_tui.py --print-config

- **Production / headless (recommended):**
  python run_bridge.py

- **Full-screen terminal command center:**
  python run_bridge_tui.py

- **Async wrapper (same production bridge, optional in-process API):**
  python run_bridge_async.py

The command center uses the same `config.ini` as the synchronous bridge and adds:
  - preflight diagnostics with actionable dependency, config, serial, MQTT, and API warnings
  - config selection via `--config /path/to/config.ini` or the `AMMB_CONFIG` environment variable
  - redacted config inspection with `--print-config`
  - live bridge state, queue depth, and connection visibility
  - a full-screen health and metrics dashboard
  - recent events and a scrolling log tail
  - keyboard shortcuts: `S` start/stop, `R` restart, `M` reset metrics, `P` pause logs, `C` clear logs, `Q` quit
  - crash reports written to `ammb_tui_crash.log` if the dashboard hits an unhandled startup/runtime exception

Use `run_bridge.py` under a process manager for unattended production. Use the command center when an operator is present. `run_bridge_async.py` runs the same bidirectional bridge and can host FastAPI in-process so `/api/*` sees live metrics.

Both `run_bridge.py` and `run_bridge_async.py` accept `--config` and honor `AMMB_CONFIG`.


### REST API Endpoints (if enabled)
Endpoints are available on the configured API host/port (default: http://127.0.0.1:8080):

- `GET /api/health` — Health status of all components
- `GET /api/metrics` — Detailed metrics and statistics
- `GET /api/status` — Combined health and metrics
- `GET /api/info` — Bridge information
- `POST /api/control` — Control actions (e.g., reset metrics)

Example:
  curl http://localhost:8080/api/health
  curl http://localhost:8080/api/metrics

---

## Maintainer / Contact
This project is maintained by **Akita Engineering**.  

- **Website:** [www.akitaengineering.com](http://www.akitaengineering.com)  
- **Contact:** info@akitaengineering.com  

---

## License
This project is licensed under the **GNU General Public License v3.0**.  
(See the LICENSE file for the full license text.)
