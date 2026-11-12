"""Generate a private C header from local/wifi.json; never print passwords."""
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
networks = json.loads((root / 'local' / 'wifi.json').read_text(encoding='utf-8'))['networks']
if not networks:
    raise SystemExit('Add at least one Wi-Fi network to local/wifi.json.')

def literal(value):
    return '"' + ''.join(f'\\x{b:02x}' for b in value.encode('utf-8')) + '"'

rows = []
for network in networks:
    ssid, password = network['ssid'], network['password']
    if not 1 <= len(ssid.encode('utf-8')) <= 32:
        raise SystemExit('SSID must contain 1 to 32 UTF-8 bytes.')
    if not 8 <= len(password.encode('utf-8')) <= 63:
        raise SystemExit('WPA2 password must contain 8 to 63 UTF-8 bytes.')
    rows.append('    {' + literal(ssid) + ', ' + literal(password) + '},')
header = '''#pragma once
/* Generated locally. Contains credentials: do not share this file. */
static const struct { const char *ssid; const char *password; } known_wifi[] = {
''' + '\n'.join(rows) + '''
};
#define KNOWN_WIFI_COUNT (sizeof(known_wifi) / sizeof(known_wifi[0]))
'''
destination = root / 'local' / 'wifi_credentials.h'
if not destination.exists() or destination.read_text(encoding='ascii') != header:
    destination.write_text(header, encoding='ascii')
print(f'Prepared private configuration for {len(networks)} networks.')
