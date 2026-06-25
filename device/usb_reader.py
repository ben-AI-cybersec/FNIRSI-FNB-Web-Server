"""
USB HID Reader for FNIRSI power meters.
Uses hidapi (hid) instead of PyUSB so no libusb/Zadig driver is needed on Windows.
Protocol reverse-engineered from baryluk/fnirsi-usb-power-data-logger and
hello-world-dot-c/fnirsi-usb-power-meter.
"""

import hid
import time
import threading
from collections import deque
from datetime import datetime


class USBReader:
    """USB HID communication with FNIRSI devices"""

    # Supported FNIRSI devices: (vendor_id, product_id)
    SUPPORTED_DEVICES = [
        (0x2e3c, 0x0049),  # FNIRSI FNB48P / FNB48S
        (0x2e3c, 0x5558),  # FNIRSI FNB58
        (0x0483, 0x003a),  # FNIRSI FNB48 (older)
        (0x0483, 0x003b),  # FNIRSI C1 and FNAC28 (same VID/PID, distinguished by product string)
    ]

    # HID report ID prepended to every outgoing write
    REPORT_ID = 0x00

    def __init__(self, vendor_id=None, product_ids=None):
        self.vendor_id = vendor_id
        self.product_ids = product_ids
        self._dev = None               # hid.device instance
        self._device_info_raw = None   # dict from hid.enumerate
        self.is_connected = False
        self.is_reading = False
        self.is_fnb58_or_fnb48s = False
        self.device_name = None        # Resolved during connect()
        self.read_thread = None
        self.data_callback = None
        self.data_buffer = deque(maxlen=1000)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connect(self):
        """Find and open the first supported FNIRSI HID device."""
        info = self._find_device()
        if info is None:
            raise ConnectionError("FNIRSI device not found. Make sure it's connected via USB.")

        dev = hid.device()
        dev.open_path(info['path'])

        self._dev = dev
        self._device_info_raw = info

        vid = info['vendor_id']
        pid = info['product_id']
        self.is_fnb58_or_fnb48s = (vid == 0x2e3c)
        self.device_name = self._resolve_device_name(vid, pid, info)
        print(f"Found device: VID=0x{vid:04x} PID=0x{pid:04x} → {self.device_name}")

        self._send_init_handshake()
        self.is_connected = True
        return True

    def start_reading(self, callback=None):
        """Start reading data in a background thread."""
        if not self.is_connected:
            raise ConnectionError("Device not connected")
        self.data_callback = callback
        self.is_reading = True
        self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.read_thread.start()

    def stop_reading(self):
        """Stop the reading thread."""
        self.is_reading = False
        if self.read_thread:
            self.read_thread.join(timeout=2)

    def disconnect(self):
        """Stop reading and close the HID handle."""
        self.stop_reading()
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
        self._dev = None
        self.is_connected = False

    def get_device_info(self):
        """Return device metadata dict."""
        if not self._device_info_raw:
            return None
        info = self._device_info_raw
        return {
            'vendor_id': f"0x{info['vendor_id']:04x}",
            'product_id': f"0x{info['product_id']:04x}",
            'manufacturer': info.get('manufacturer_string') or 'Unknown',
            'product': info.get('product_string') or 'Unknown',
            'serial': info.get('serial_number') or 'Unknown',
            'device_name': self.device_name or 'Unknown',
        }

    def trigger_voltage(self, protocol, voltage):
        """Send a fast-charging protocol trigger command."""
        if not self.is_connected:
            raise ConnectionError("Device not connected")

        trigger_commands = {
            'pd':   {5: b"\x5a\x01\x05", 9: b"\x5a\x01\x09", 12: b"\x5a\x01\x0c",
                     15: b"\x5a\x01\x0f", 20: b"\x5a\x01\x14"},
            'qc':   {5: b"\x5a\x02\x05", 9: b"\x5a\x02\x09", 12: b"\x5a\x02\x0c"},
            'afc':  {5: b"\x5a\x03\x05", 9: b"\x5a\x03\x09", 12: b"\x5a\x03\x0c"},
            'fcp':  {5: b"\x5a\x04\x05", 9: b"\x5a\x04\x09", 12: b"\x5a\x04\x0c"},
            'scp':  {5: b"\x5a\x05\x05", 9: b"\x5a\x05\x09", 12: b"\x5a\x05\x0c"},
            'vooc': {5: b"\x5a\x06\x05", 10: b"\x5a\x06\x0a"},
        }
        if protocol not in trigger_commands:
            raise ValueError(f"Unknown protocol: {protocol}")
        if voltage not in trigger_commands[protocol]:
            raise ValueError(f"Unsupported voltage {voltage}V for {protocol.upper()}")

        command = trigger_commands[protocol][voltage]
        padded = command + b"\x00" * (64 - len(command))
        try:
            self._write(padded)
            print(f"✓ Triggered {protocol.upper()} {voltage}V")
            return True
        except Exception as e:
            print(f"❌ Trigger command failed: {e}")
            raise

    def adjust_qc3_voltage(self, target_voltage):
        """Adjust QC 3.0 voltage in fine steps (3.6 V – 12.0 V)."""
        if not self.is_connected:
            raise ConnectionError("Device not connected")
        if not (3.6 <= target_voltage <= 12.0):
            raise ValueError("QC 3.0 voltage must be between 3.6 V and 12.0 V")

        millivolts = int(target_voltage * 1000)
        command = b"\x5a\x02" + millivolts.to_bytes(2, byteorder='little')
        padded = command + b"\x00" * (64 - len(command))
        try:
            self._write(padded)
            print(f"✓ QC 3.0 adjusted to {target_voltage:.2f} V")
            return True
        except Exception as e:
            print(f"❌ QC 3.0 adjustment failed: {e}")
            raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write(self, payload64: bytes):
        """Write a 64-byte payload to the HID device (prepends report ID)."""
        self._dev.write([self.REPORT_ID] + list(payload64))

    def _find_device(self):
        """Return the first matching hid.enumerate dict, or None."""
        candidates = [(self.vendor_id, self.product_ids)] if self.vendor_id else None
        if candidates:
            for pid in self.product_ids:
                for d in hid.enumerate(self.vendor_id, pid):
                    return d
            return None

        for vid, pid in self.SUPPORTED_DEVICES:
            for d in hid.enumerate(vid, pid):
                return d
        return None

    def _resolve_device_name(self, vendor_id, product_id, info):
        """Return a human-readable device name, distinguishing C1 from FNAC28."""
        name_map = {
            (0x2e3c, 0x0049): 'FNB48P/S',
            (0x2e3c, 0x5558): 'FNB58',
            (0x0483, 0x003a): 'FNB48',
        }
        if (vendor_id, product_id) in name_map:
            return name_map[(vendor_id, product_id)]
        if vendor_id == 0x0483 and product_id == 0x003b:
            product_str = (info.get('product_string') or '').lower()
            return 'C1' if 'c1' in product_str else 'FNAC28'
        return f'Unknown (VID=0x{vendor_id:04x} PID=0x{product_id:04x})'

    def _send_init_handshake(self):
        """Send the initialization sequence required to start data streaming."""
        try:
            self._write(b"\xaa\x81" + b"\x00" * 61 + b"\x8e")
            self._write(b"\xaa\x82" + b"\x00" * 61 + b"\x96")
            if self.is_fnb58_or_fnb48s:
                self._write(b"\xaa\x82" + b"\x00" * 61 + b"\x96")
            else:
                self._write(b"\xaa\x83" + b"\x00" * 61 + b"\x9e")
            print("Initialization handshake sent")
        except Exception as e:
            print(f"Warning: Handshake failed: {e}")

    def _read_loop(self):
        """Background thread: poll device and forward decoded readings.

        FNIRSI devices don't auto-stream; they must be explicitly polled.
        FNB48/C1/FNAC28: send AA82+AA83 before each read (3 ms cycle).
        FNB58/FNB48S: send AA82 before each read (1 s cycle).
        """
        time.sleep(0.1)

        poll_aa82 = b"\xaa\x82" + b"\x00" * 61 + b"\x96"
        poll_aa83 = b"\xaa\x83" + b"\x00" * 61 + b"\x9e"
        # FNB58/FNB48S: 200 ms read timeout per cycle; others: 50 ms
        read_timeout = 200 if self.is_fnb58_or_fnb48s else 50
        cycle_sleep  = 1.0 if self.is_fnb58_or_fnb48s else 0.003

        while self.is_reading:
            try:
                self._write(poll_aa82)
                if not self.is_fnb58_or_fnb48s:
                    self._write(poll_aa83)

                raw = self._dev.read(64, timeout_ms=read_timeout)
                if raw:
                    readings = self._decode_packet(bytes(raw))
                    for reading in readings:
                        self.data_buffer.append(reading)
                        if self.data_callback:
                            self.data_callback(reading)

                time.sleep(cycle_sleep)

            except Exception as e:
                if self.is_reading:
                    print(f"USB read error: {e}")
                    time.sleep(0.1)

    def _decode_packet(self, data: bytes):
        """Decode an AA04 data packet into up to 4 reading dicts."""
        readings = []
        if len(data) < 64 or data[1] != 0x04:
            return readings

        timestamp = datetime.now().isoformat()
        for i in range(4):
            off = 2 + 15 * i
            if off + 14 >= len(data):
                break

            voltage  = int.from_bytes(data[off:off+4],    'little') / 100000.0
            current  = int.from_bytes(data[off+4:off+8],  'little') / 100000.0
            dp       = int.from_bytes(data[off+8:off+10], 'little') / 1000.0
            dn       = int.from_bytes(data[off+10:off+12],'little') / 1000.0
            temp     = int.from_bytes(data[off+13:off+15],'little') / 10.0
            power    = voltage * current

            readings.append({
                'timestamp':   timestamp,
                'voltage':     round(voltage, 5),
                'current':     round(current, 5),
                'power':       round(power,   5),
                'dp':          round(dp,      3),
                'dn':          round(dn,      3),
                'temperature': round(temp,    1),
                'sample':      i,
            })

        return readings
