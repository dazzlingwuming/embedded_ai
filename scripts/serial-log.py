"""Capture device output to the project logs directory; optionally reset the P4."""
import argparse
from pathlib import Path
import time
import serial

parser = argparse.ArgumentParser()
parser.add_argument('--port', default='COM5')
parser.add_argument('--seconds', type=float, default=20)
parser.add_argument('--output', default='serial-latest.log')
parser.add_argument('--reset', action='store_true')
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
output = root / 'logs' / Path(args.output).name
device = serial.Serial(port=None, baudrate=115200, timeout=0.2)
device.dtr = False
device.rts = False
device.port = args.port
with device, output.open('w', encoding='utf-8') as logfile:
    if args.reset:
        device.rts = True
        time.sleep(0.2)
        device.rts = False
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        data = device.read(max(1, device.in_waiting))
        if data:
            text = data.decode('utf-8', errors='replace')
            logfile.write(text)
            logfile.flush()
            print(text, end='', flush=True)
