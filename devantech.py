#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
#
# Low-level binary protocol for Devantech ETH00x / ETH80xx / dScript ("2824"-style)
# relay boards.  Reimplements the wire protocol that used to live in pvOpt's
# readETH008.py, but without any of pvOpt's globals/config/cache/eventhandler
# machinery, so it can run standalone inside the ETH2MQTT gateway.
#
# There are two command sets in the wild, auto-detected per box (see BoardType):
#
#   OLD (ETH008 / ETH8020, firmware-native binary protocol):
#     0x10 Get Module Info      0x20 Digital active (relay on)
#     0x21 Digital inactive     0x23 Digital set outputs (all relays at once)
#     0x24 Digital get outputs  0x32 Get Analogue Voltage
#     0x78 Get Volts
#
#   NEW (dScript-style boards, e.g. 2824):
#     0x30 Get Status   0x31 Set relay   0x32 Set output
#     0x33 Get Relays (byte 0 = echo of the selected/queried relay, i.e. 0
#                       when "all" was requested; the following bytes are the
#                       full relay bitmap)
#     0x34 Get Inputs (not documented beyond the name; assumed to return the
#                       digital-input bitmap directly, with no leading echo
#                       byte -- unlike 0x33. This is new functionality (the
#                       original driver never read digital inputs at all) and
#                       has not been verified against real hardware yet.)
#     0x35 Get Analogue

import logging
import select
import socket
import struct

log = logging.getLogger("devantech")

TCP_PORT = 17494
BUFFER_SIZE = 80

BOARD_OLD = 0  # ETH008 / ETH8020 native binary protocol
BOARD_NEW = 1  # dScript-style ("2824") protocol


class BoardUnreachable(Exception):
    """Raised when a box refuses the TCP connection entirely."""


def _send_command(host, port, message, timeout):
    """Open a fresh TCP connection, send `message` (bytes), return the reply.

    Devantech boards are queried with one short-lived connection per command
    (as the original driver did) rather than a kept-open socket.

    Returns:
        bytes   -- the (possibly empty) reply on a normal round trip
        None    -- the connection itself failed (host down / refused)
    Raises:
        nothing -- a timeout waiting for a reply yields b"", matching the
                   original driver's behaviour, so callers can tell "box
                   unreachable" (None) apart from "box didn't answer this
                   particular command" (b"").
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect((host, port))
        s.send(message)
        s.setblocking(False)
        ready = select.select([s], [], [], timeout)
        if ready[0]:
            return s.recv(BUFFER_SIZE)
        hexString = ":".join("{:02x}".format(c) for c in message)
        log.warning("%s:%d did not answer within %.1fs to command 0x%s", host, port, timeout, hexString)
        return b""
    except OSError as e:
        log.debug("%s:%d unreachable: %s", host, port, e)
        return None
    finally:
        s.close()


class Board:
    """One Devantech box, addressed by hostname/IP."""

    def __init__(self, host, port=TCP_PORT, timeout=2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._boardType = None

    def _send(self, message, timeout=None):
        return _send_command(self.host, self.port, message, timeout or self.timeout)

    # ------------------------------------------------------------------
    @property
    def boardType(self):
        if self._boardType is None:
            self._boardType = self._detectBoardType()
        return self._boardType

    def _detectBoardType(self):
        # 0x10 (Get Module Info) is answered by old boards and ignored (-> timeout) by new ones.
        status = self._send(bytes([0x10]), timeout=0.5)
        if status is not None and len(status) == 0:
            return BOARD_NEW
        return BOARD_OLD  # also the fallback if the box was unreachable

    # ------------------------------------------------------------------
    def readRelays(self):
        """Returns (bitmask:int, numBits:int) or (None, 0) if unreachable."""
        if self.boardType == BOARD_OLD:
            raw = self._send(bytes([0x24, 0x00, 0x00]))
            if not raw:
                return None, 0
            return raw[0], 8
        else:
            raw = self._send(bytes([0x33, 0x00]))
            if raw is None or len(raw) < 2:
                return None, 0
            bitmap = raw[1:]  # raw[0] is the echoed "selected relay" (0 == all)
            value = 0
            for b in bitmap:
                value = value * 256 + b
            return value, len(bitmap) * 8

    def readInputs(self):
        """Returns (bitmask:int, numBits:int) or (None, 0). New boards only."""
        if self.boardType != BOARD_NEW:
            return None, 0
        raw = self._send(bytes([0x34, 0x00]))
        if raw is None or len(raw) == 0:
            return None, 0
        value = 0
        for b in raw:
            value = value * 256 + b
        return value, len(raw) * 8

    def readVoltage(self):
        """Returns supply voltage in Volts (float), or None."""
        if self.boardType == BOARD_OLD:
            raw = self._send(bytes([0x78, 0x00, 0x00]))
            if not raw:
                return None
            value = 0
            for b in raw:
                value = value * 256 + b
            return value / 10
        else:
            raw = self._send(bytes([0x30, 0x00, 0x00]))
            if raw is None or len(raw) <= 5:
                log.warning("%s: status reply too short (%d bytes) to contain voltage", self.host, len(raw or b""))
                return None
            return raw[5] / 10

    def readAnalog(self, channel):
        """channel is 1-8 on old boards, 0-7 on new boards. Returns int or None."""
        if self.boardType == BOARD_OLD:
            raw = self._send(bytes([0x32, channel, 0x00]))
            if not raw:
                return None
            value = 0
            for b in raw:
                value = value * 256 + b
            return value
        else:
            raw = self._send(bytes([0x35, channel, 0x00]))
            i = channel * 2
            if raw is None or len(raw) < i + 2:
                log.warning("%s: analog reply too short for channel %d", self.host, channel)
                return None
            return raw[i] * 256 + raw[i + 1]

    def setRelay(self, switch, state, pulse=None):
        """switch is 1-based. state is truthy/falsy. pulse is in 100ms steps (0=permanent)."""
        pulse = pulse if isinstance(pulse, int) else 0
        if self.boardType == BOARD_OLD:
            if pulse > 255:
                log.error("pulse %d > 255 for %s switch %d, clamping", pulse, self.host, switch)
                pulse = 255
            cmd = bytes([0x20 if state else 0x21, switch, pulse])
        else:
            cmd = bytes([0x31, switch, 1 if state else 0]) + struct.pack(">L", pulse)
        raw = self._send(cmd)
        return raw is not None and len(raw) > 0 and raw[0] == 0
