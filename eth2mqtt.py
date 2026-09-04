#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
#
# ETH2MQTT - standalone MQTT gateway for Devantech ETH00x/ETH80xx/dScript
# relay & I/O boards, with Home Assistant MQTT-discovery.
#
# Replaces pvOpt's readETH008.py: no globals/config/cache/eventhandler
# dependency, runs as its own process (intended as a Home Assistant add-on).
#
# Naming: every digital relay, digital input, analog channel and the supply
# voltage of every configured box is published. A channel gets the name from
# devantech.ini's [METADATA] section if one is defined there (so it can match
# an existing Homematic name 1:1); everything else gets a generic name built
# from the box and channel number.

import json
import logging
import os
import shutil
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.request
import urllib.error
import configparser

import paho.mqtt.client as mqtt

import devantech

log = logging.getLogger("eth2mqtt")

DEFAULTS = {
    "log_level": "info",
    "poll_interval": 10,
    "discovery_prefix": "homeassistant",
    "topic_prefix": "eth2mqtt",
    "push_port": devantech.TCP_PORT,
    "ini_file": "/config/devantech.ini",
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_username": "",
    "mqtt_password": "",
}

GENERIC_LABELS = {"relay": "Relay", "input": "Input", "analog": "Analog", "voltage": "Voltage"}


# ----------------------------------------------------------------------
# options / bootstrapping
# ----------------------------------------------------------------------
def load_options():
    options = dict(DEFAULTS)
    optionsFile = "/data/options.json"
    if os.path.exists(optionsFile):
        with open(optionsFile, encoding="utf-8") as f:
            options.update(json.load(f))
    else:
        # not running under the HA Supervisor (e.g. local dev run) - allow env var overrides
        for key in DEFAULTS:
            envKey = "ETH2MQTT_" + key.upper()
            if envKey in os.environ:
                options[key] = os.environ[envKey]
        if not os.path.exists(options["ini_file"]):
            local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devantech.ini")
            if os.path.exists(local):
                options["ini_file"] = local
    return options


def ensure_ini_file(path):
    """On first start inside the add-on, seed /config/devantech.ini from the
    image's bundled copy so the gateway has something to load and the user
    has a starting point to edit."""
    if os.path.exists(path):
        return
    bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devantech.ini")
    if os.path.exists(bundled) and os.path.abspath(bundled) != os.path.abspath(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copy(bundled, path)
        log.info("seeded %s from bundled default - edit it there", path)


def get_supervisor_mqtt():
    """Ask the HA Supervisor for the Mosquitto add-on's connection details,
    if we're running under Supervisor and a broker add-on is installed."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        req = urllib.request.Request(
            "http://supervisor/services/mqtt",
            headers={"Authorization": "Bearer %s" % token},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.load(resp)
        data = payload.get("data") or {}
        if not data.get("host"):
            return None
        return {
            "host": data.get("host"),
            "port": int(data.get("port", 1883)),
            "username": data.get("username", "") or "",
            "password": data.get("password", "") or "",
        }
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.info("Supervisor MQTT service discovery unavailable (%s), falling back to add-on options", e)
        return None


# ----------------------------------------------------------------------
# devantech.ini parsing
# ----------------------------------------------------------------------
def parse_ini(path):
    """[boxes]   box_id = host[:port]
       [METADATA] ETH/<box_id>[/<channel>] = Name, flag, type   (same syntax pvOpt used)
    channel is one of: <int> (relay, 1-based), "V" (voltage), "A<n>" (analog),
    "I<n>" (digital input - new, pvOpt never had per-input aliases).
    flag "n" (case-insensitive) means "physically inverted"; any other flag
    text is used as the unit_of_measurement for analog channels.
    """
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = lambda x: x  # keys are case-sensitive box/channel names
    if not cp.read(path, encoding="utf-8"):
        raise FileNotFoundError("devantech.ini not found at %s" % path)

    boxes = {}
    if cp.has_section("boxes"):
        for box_id, value in cp.items("boxes"):
            value = value.strip()
            if ":" in value:
                host, _, portStr = value.rpartition(":")
                port = int(portStr)
            else:
                host, port = value, devantech.TCP_PORT
            boxes[box_id] = (host, port)

    boxIdsLower = {b.lower() for b in boxes}
    aliases = {}
    if cp.has_section("METADATA"):
        for key, value in cp.items("METADATA"):
            if not key.upper().startswith("ETH/"):
                continue
            parts = key.split("/")
            if len(parts) < 2:
                continue
            boxPart = parts[1]
            channelPart = parts[2] if len(parts) > 2 else "__box__"
            boxLower = boxPart.lower()
            if boxLower not in boxIdsLower:
                log.warning("devantech.ini: METADATA key %r references box %r which is not listed in [boxes] - ignored", key, boxPart)
                continue
            fields = [f.strip() for f in value.split(",")]
            name = fields[0] if fields else ""
            flag = fields[1] if len(fields) > 1 else ""
            dtype = fields[2] if len(fields) > 2 else ""
            invert = flag.strip().lower() == "n"
            unit = flag if (flag and not invert) else None
            aliases.setdefault(boxLower, {})[channelPart] = (name, unit, invert, dtype)

    return boxes, aliases


def alias_for(box_id, channelKey, aliases):
    return aliases.get(box_id.lower(), {}).get(channelKey)


def box_display_name(box_id, aliases):
    entry = alias_for(box_id, "__box__", aliases)
    if entry and entry[0]:
        return entry[0]
    return box_id


def entity_name(box_display, kind, n, aliasEntry):
    if aliasEntry and aliasEntry[0]:
        return aliasEntry[0]
    label = GENERIC_LABELS[kind]
    return "%s %s" % (box_display, label) if n is None else "%s %s %s" % (box_display, label, n)


# ----------------------------------------------------------------------
# per-box runtime state
# ----------------------------------------------------------------------
class BoxRuntime:
    def __init__(self, box_id, board, analogChannels):
        self.box_id = box_id
        self.board = board
        self.analogChannels = analogChannels  # explicitly aliased channel numbers only
        self.numRelayBits = 0
        self.numInputBits = 0
        self.relayState = {}
        self.inputState = {}
        self.voltage = None
        self.voltageDiscoveryDone = False
        self.analogState = {}
        self.analogDiscoveryDone = set()
        self.available = None  # tri-state so the first result always triggers a publish


# ----------------------------------------------------------------------
# MQTT + Home Assistant discovery
# ----------------------------------------------------------------------
class MqttGateway:
    def __init__(self, options, boxesCfg, aliases, mqttCreds):
        self.options = options
        self.aliases = aliases
        self.topicPrefix = options["topic_prefix"].rstrip("/")
        self.discoveryPrefix = options["discovery_prefix"].rstrip("/")
        self.stopping = threading.Event()

        self.boxes = {}
        for box_id, (host, port) in boxesCfg.items():
            channels = sorted(int(k[1:]) for k in aliases.get(box_id.lower(), {}) if k.startswith("A") and k[1:].isdigit())
            self.boxes[box_id] = BoxRuntime(box_id, devantech.Board(host, port), channels)

        self.client = mqtt.Client(client_id="eth2mqtt")
        if mqttCreds.get("username"):
            self.client.username_pw_set(mqttCreds["username"], mqttCreds.get("password") or None)
        self.client.will_set(self._bridgeTopic(), payload="offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self._mqttHost = mqttCreds["host"]
        self._mqttPort = int(mqttCreds["port"])

    def _bridgeTopic(self):
        return "%s/bridge/status" % self.topicPrefix

    def _availabilityTopic(self, box_id):
        return "%s/%s/availability" % (self.topicPrefix, box_id)

    def start(self):
        log.info("connecting to MQTT broker %s:%d", self._mqttHost, self._mqttPort)
        self.client.connect(self._mqttHost, self._mqttPort, keepalive=30)
        self.client.loop_start()

    def shutdown(self):
        try:
            self.client.publish(self._bridgeTopic(), "offline", qos=1, retain=True)
            for box_id in self.boxes:
                self.client.publish(self._availabilityTopic(box_id), "offline", qos=1, retain=True)
            time.sleep(0.2)
        finally:
            self.client.loop_stop()
            self.client.disconnect()

    # ---- devantech.ini hot reload --------------------------------------
    # Only [METADATA] (names/units/invert) is applied live. Adding/removing
    # a box in [boxes] still needs a restart of the add-on, since that
    # changes which poll threads exist - we warn about that case here
    # rather than trying to spin threads up/down on the fly.
    def reload_config(self):
        try:
            newBoxesCfg, newAliases = parse_ini(self.options["ini_file"])
        except FileNotFoundError as e:
            log.error("devantech.ini reload failed: %s", e)
            return
        changedBoxes = set(newBoxesCfg) ^ set(self.boxes)
        if changedBoxes:
            log.warning("[boxes] changed (%s) - restart the ETH2MQTT app to apply that; reloading names/aliases only", ", ".join(sorted(changedBoxes)))
        self.aliases = newAliases
        for box_id, rt in self.boxes.items():
            rt.analogChannels = sorted(int(k[1:]) for k in newAliases.get(box_id.lower(), {}) if k.startswith("A") and k[1:].isdigit())
            if rt.numRelayBits:
                self.publish_relay_discovery(box_id)
            if rt.numInputBits:
                self.publish_input_discovery(box_id)
            if rt.voltageDiscoveryDone:
                self.publish_voltage_discovery(box_id)
            for n in list(rt.analogDiscoveryDone):
                self.publish_analog_discovery(box_id, n)
        log.info("devantech.ini reloaded")

    def watch_ini_loop(self):
        """Polls the ini file's mtime and calls reload_config() when it changes,
        so editing devantech.ini on the addon_config share takes effect on its
        own within one poll period - no restart needed for name/alias edits."""
        path = self.options["ini_file"]
        interval = 15
        try:
            lastMtime = os.path.getmtime(path)
        except OSError:
            lastMtime = None
        while not self.stopping.wait(interval):
            try:
                mtime = os.path.getmtime(path)
            except OSError as e:
                log.debug("could not stat %s: %s", path, e)
                continue
            if lastMtime is not None and mtime != lastMtime:
                log.info("%s changed on disk", path)
                self.reload_config()
            lastMtime = mtime

    # ---- connection lifecycle -----------------------------------------
    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed, rc=%s", rc)
            return
        log.info("MQTT connected")
        client.publish(self._bridgeTopic(), "online", qos=1, retain=True)
        client.subscribe("%s/+/relay/+/set" % self.topicPrefix)
        # reconnect case: re-announce whatever we already know about (retained state stays valid)
        for box_id, rt in self.boxes.items():
            if rt.numRelayBits:
                self.publish_relay_discovery(box_id)
            if rt.numInputBits:
                self.publish_input_discovery(box_id)
            if rt.voltageDiscoveryDone:
                self.publish_voltage_discovery(box_id)
            for n in rt.analogDiscoveryDone:
                self.publish_analog_discovery(box_id, n)

    def _on_disconnect(self, client, userdata, rc):
        if not self.stopping.is_set():
            log.warning("MQTT disconnected unexpectedly (rc=%s)", rc)

    def _on_message(self, client, userdata, msg):
        try:
            parts = msg.topic.split("/")
            if len(parts) == 5 and parts[0] == self.topicPrefix and parts[2] == "relay" and parts[4] == "set":
                self.handle_relay_command(parts[1], int(parts[3]), msg.payload.decode("utf-8", "replace"))
        except Exception:
            log.exception("error handling MQTT message on %s", msg.topic)

    # ---- commands from Home Assistant ----------------------------------
    def handle_relay_command(self, box_id, n, payload):
        rt = self.boxes.get(box_id)
        if rt is None:
            log.warning("relay command for unknown box %r ignored", box_id)
            return
        desired = payload.strip().upper() == "ON"
        aliasEntry = alias_for(box_id, str(n), self.aliases)
        invert = aliasEntry[2] if aliasEntry else False
        physical = desired != invert
        ok = rt.board.setRelay(n, physical)
        if ok:
            rt.relayState[n] = desired
            self.publish_relay_state(box_id, n, desired)
        else:
            log.warning("%s: failed to set relay %d to %s", box_id, n, payload)

    # ---- push events (board -> gateway, unsolicited) --------------------
    def on_push_event(self, box_id, switch, physicalState):
        aliasEntry = alias_for(box_id, str(switch), self.aliases)
        invert = aliasEntry[2] if aliasEntry else False
        logical = physicalState != invert
        rt = self.boxes.get(box_id)
        if rt is not None:
            rt.relayState[switch] = logical
        self.publish_relay_state(box_id, switch, logical)
        self.client.publish(
            "%s/%s/event/%s" % (self.topicPrefix, box_id, switch),
            json.dumps({"event_type": "on" if logical else "off"}),
            qos=0, retain=False,
        )

    # ---- discovery -------------------------------------------------------
    def _device(self, box_id):
        return {
            "identifiers": ["eth2mqtt_%s" % box_id.lower()],
            "name": box_display_name(box_id, self.aliases),
            "manufacturer": "Devantech",
            "model": "ETH relay/IO board",
        }

    def _availability(self, box_id):
        return {
            "availability_mode": "all",
            "availability": [
                {"topic": self._bridgeTopic()},
                {"topic": self._availabilityTopic(box_id)},
            ],
        }

    def _publish_discovery(self, component, box_id, objectId, payload):
        topic = "%s/%s/eth2mqtt_%s/%s/config" % (self.discoveryPrefix, component, box_id.lower(), objectId)
        
        log.info(f"publish discovery: {topic}")

        self.client.publish(topic, json.dumps(payload), qos=0, retain=True)

    def publish_relay_discovery(self, box_id):
        rt = self.boxes[box_id]
        display = box_display_name(box_id, self.aliases)
        for n in range(1, rt.numRelayBits + 1):
            aliasEntry = alias_for(box_id, str(n), self.aliases)
            objectId = "relay_%d" % n
            payload = {
                "name": entity_name(display, "relay", n, aliasEntry),
                "unique_id": "eth2mqtt_%s_%s" % (box_id.lower(), objectId),
                "state_topic": "%s/%s/relay/%d/state" % (self.topicPrefix, box_id, n),
                "command_topic": "%s/%s/relay/%d/set" % (self.topicPrefix, box_id, n),
                "payload_on": "ON", "payload_off": "OFF",
                "state_on": "ON", "state_off": "OFF",
                "device": self._device(box_id),
            }
            payload.update(self._availability(box_id))
            self._publish_discovery("switch", box_id, objectId, payload)

    def publish_input_discovery(self, box_id):
        rt = self.boxes[box_id]
        display = box_display_name(box_id, self.aliases)
        for n in range(rt.numInputBits):
            aliasEntry = alias_for(box_id, "I%d" % n, self.aliases)
            objectId = "input_%d" % n
            payload = {
                "name": entity_name(display, "input", n, aliasEntry),
                "unique_id": "eth2mqtt_%s_%s" % (box_id.lower(), objectId),
                "state_topic": "%s/%s/input/%d/state" % (self.topicPrefix, box_id, n),
                "payload_on": "ON", "payload_off": "OFF",
                "device": self._device(box_id),
            }
            payload.update(self._availability(box_id))
            self._publish_discovery("binary_sensor", box_id, objectId, payload)

    def publish_voltage_discovery(self, box_id):
        display = box_display_name(box_id, self.aliases)
        aliasEntry = alias_for(box_id, "V", self.aliases)
        objectId = "voltage"
        payload = {
            "name": entity_name(display, "voltage", None, aliasEntry),
            "unique_id": "eth2mqtt_%s_%s" % (box_id.lower(), objectId),
            "state_topic": "%s/%s/voltage/state" % (self.topicPrefix, box_id),
            "unit_of_measurement": "V",
            "device_class": "voltage",
            "state_class": "measurement",
            "device": self._device(box_id),
        }
        payload.update(self._availability(box_id))
        self._publish_discovery("sensor", box_id, objectId, payload)

    def publish_analog_discovery(self, box_id, n):
        display = box_display_name(box_id, self.aliases)
        aliasEntry = alias_for(box_id, "A%d" % n, self.aliases)
        unit = aliasEntry[1] if aliasEntry and aliasEntry[1] else None
        objectId = "analog_%d" % n
        payload = {
            "name": entity_name(display, "analog", n, aliasEntry),
            "unique_id": "eth2mqtt_%s_%s" % (box_id.lower(), objectId),
            "state_topic": "%s/%s/analog/%d/state" % (self.topicPrefix, box_id, n),
            "state_class": "measurement",
            "device": self._device(box_id),
        }
        if unit:
            payload["unit_of_measurement"] = unit
        payload.update(self._availability(box_id))
        self._publish_discovery("sensor", box_id, objectId, payload)

    # ---- state -------------------------------------------------------
    def publish_relay_state(self, box_id, n, logical):
        self.client.publish("%s/%s/relay/%d/state" % (self.topicPrefix, box_id, n), "ON" if logical else "OFF", qos=0, retain=True)

    def publish_input_state(self, box_id, n, logical):
        self.client.publish("%s/%s/input/%d/state" % (self.topicPrefix, box_id, n), "ON" if logical else "OFF", qos=0, retain=True)

    def publish_voltage_state(self, box_id, voltage):
        self.client.publish("%s/%s/voltage/state" % (self.topicPrefix, box_id), "%.1f" % voltage, qos=0, retain=True)

    def publish_analog_state(self, box_id, n, value):
        self.client.publish("%s/%s/analog/%d/state" % (self.topicPrefix, box_id, n), str(value), qos=0, retain=True)

    def set_box_available(self, box_id, available):
        rt = self.boxes[box_id]
        if rt.available != available:
            rt.available = available
            self.client.publish(self._availabilityTopic(box_id), "online" if available else "offline", qos=1, retain=True)
            log.info("%s: %s", box_id, "back online" if available else "unreachable")

    # ---- polling -------------------------------------------------------
    def poll_once(self, box_id):
        rt = self.boxes[box_id]
        board = rt.board

        relayMask, relayBits = board.readRelays()
        if relayMask is None:
            return False

        if relayBits and rt.numRelayBits != relayBits:
            rt.numRelayBits = relayBits
            self.publish_relay_discovery(box_id)

        for n in range(1, relayBits + 1):
            bitOn = bool(relayMask & (1 << (n - 1)))
            aliasEntry = alias_for(box_id, str(n), self.aliases)
            invert = aliasEntry[2] if aliasEntry else False
            logical = bitOn != invert
            if rt.relayState.get(n) != logical:
                rt.relayState[n] = logical
                self.publish_relay_state(box_id, n, logical)

        if board.boardType == devantech.BOARD_NEW:
            inputMask, inputBits = board.readInputs()
            if inputMask is not None:
                if inputBits and rt.numInputBits != inputBits:
                    rt.numInputBits = inputBits
                    self.publish_input_discovery(box_id)
                for n in range(inputBits):
                    bitOn = bool(inputMask & (1 << n))
                    aliasEntry = alias_for(box_id, "I%d" % n, self.aliases)
                    invert = aliasEntry[2] if aliasEntry else False
                    logical = bitOn != invert
                    if rt.inputState.get(n) != logical:
                        rt.inputState[n] = logical
                        self.publish_input_state(box_id, n, logical)

        voltage = board.readVoltage()
        if voltage is not None:
            if not rt.voltageDiscoveryDone:
                self.publish_voltage_discovery(box_id)
                rt.voltageDiscoveryDone = True
            if rt.voltage != voltage:
                rt.voltage = voltage
                self.publish_voltage_state(box_id, voltage)

        for n in rt.analogChannels:
            value = board.readAnalog(n)
            if value is None:
                continue
            if n not in rt.analogDiscoveryDone:
                self.publish_analog_discovery(box_id, n)
                rt.analogDiscoveryDone.add(n)
            if rt.analogState.get(n) != value:
                rt.analogState[n] = value
                self.publish_analog_state(box_id, n, value)

        return True

    def poll_loop(self, box_id):
        interval = float(self.options["poll_interval"])
        while not self.stopping.is_set():
            try:
                ok = self.poll_once(box_id)
            except Exception:
                log.exception("%s: unexpected error while polling", box_id)
                ok = False
            self.set_box_available(box_id, ok)
            self.stopping.wait(interval)


# ----------------------------------------------------------------------
# push/event listener - boards call *us* here to report input changes
# ----------------------------------------------------------------------
class IpResolver:
    def __init__(self, boxesCfg):
        self.boxesCfg = boxesCfg

    def resolve(self, ip):
        for box_id, (host, port) in self.boxesCfg.items():
            try:
                if socket.gethostbyname(host) == ip:
                    return box_id
            except OSError:
                continue
        return None


class PushHandler(socketserver.BaseRequestHandler):
    def handle(self):
        ip = self.client_address[0]
        box_id = self.server.resolver.resolve(ip)
        label = box_id or ip
        log.info("%s connected on push channel", label)
        while True:
            data = self.request.recv(1024)
            if not data:
                break
            log.debug("%s push: %s", label, data.hex(":"))
            cmd = data[0]
            if cmd == 0x79:  # password entry - every password is accepted, same as the original driver
                reply = bytes([1])
            elif cmd in (0x20, 0x21) and len(data) > 1:
                state = cmd == 0x20
                switch = data[1]
                if box_id is None:
                    log.warning("push from %s (switch %d, state %s) - IP does not match any box in [boxes]", ip, switch, state)
                else:
                    self.server.gateway.on_push_event(box_id, switch, state)
                reply = bytes([0x00])
            else:
                reply = bytes([0x02])
            self.request.send(reply)
        log.info("%s disconnected from push channel", label)


class PushServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port, resolver, gateway):
        super().__init__(("", port), PushHandler)
        self.resolver = resolver
        self.gateway = gateway


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main():
    options = load_options()
    logging.basicConfig(
        level=getattr(logging, str(options["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(threadName)-14s %(name)s: %(message)s",
    )
    log.info("ETH2MQTT gateway starting")

    ensure_ini_file(options["ini_file"])
    try:
        boxesCfg, aliases = parse_ini(options["ini_file"])
    except FileNotFoundError as e:
        log.error(str(e))
        return 1

    if not boxesCfg:
        log.error("no [boxes] configured in %s - nothing to do", options["ini_file"])
        return 1

    mqttCreds = get_supervisor_mqtt()
    if mqttCreds is None:
        mqttCreds = {
            "host": options["mqtt_host"],
            "port": options["mqtt_port"],
            "username": options["mqtt_username"],
            "password": options["mqtt_password"],
        }
    if not mqttCreds["host"]:
        log.error("no MQTT broker configured (neither a Supervisor mqtt service nor the mqtt_host option)")
        return 1

    gateway = MqttGateway(options, boxesCfg, aliases, mqttCreds)
    gateway.start()

    resolver = IpResolver(boxesCfg)
    pushServer = PushServer(int(options["push_port"]), resolver, gateway)
    pushThread = threading.Thread(target=pushServer.serve_forever, name="PushServer", daemon=True)
    pushThread.start()
    log.info("push/event listener on port %d", options["push_port"])

    for box_id in boxesCfg:
        threading.Thread(target=gateway.poll_loop, args=(box_id,), name="poll-%s" % box_id, daemon=True).start()

    threading.Thread(target=gateway.watch_ini_loop, name="ini-watch", daemon=True).start()

    stopEvent = threading.Event()

    def handle_signal(signum, frame):
        log.info("received signal %d, shutting down", signum)
        stopEvent.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while not stopEvent.is_set():
        stopEvent.wait(1)

    gateway.stopping.set()
    gateway.shutdown()
    pushServer.shutdown()
    pushServer.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
