"""Close-then-reset-then-read: capture ESP32-S3 boot log including the banner.

The banner is printed in app_main() before WiFi init, i.e. within the first
~200 ms of boot. Opening the port afterwards always misses it, so this script
holds the port, pulses RTS to reset the chip, and reads for a few seconds.
"""
import sys
import time

import serial

PORT = "COM18"
BAUD = 115200
DURATION = 5.0

ser = serial.Serial(PORT, BAUD, timeout=0.05)
ser.reset_input_buffer()

# ESP32-S3 USB-Serial/JTAG: pulsing RTS resets the chip.
ser.setDTR(False)
ser.setRTS(True)
time.sleep(0.15)
ser.setRTS(False)
time.sleep(0.05)

buf = bytearray()
deadline = time.time() + DURATION
while time.time() < deadline:
    chunk = ser.read(4096)
    if chunk:
        buf.extend(chunk)
ser.close()

text = buf.decode("utf-8", errors="replace")
# strip ANSI colour codes so the banner is readable
import re

text = re.sub(r"\x1b\[[0-9;]*m", "", text)
sys.stdout.write(text)
sys.stdout.write("\n--- MARKERS ---\n")
for line in text.splitlines():
    if "dap-" in line or "wifi list" in line or "===" in line:
        print("HIT:", line.strip())
