"""PlatformIO pre-build script: allow several Modbus clients from the same IP.

modbus-esp8266 4.1.0 defines MODBUSIP_UNIQUE_CLIENTS in src/ModbusSettings.h. With it, a new
connection from an IP closes the previous connection from that IP. Our gateway, twin and poke
all run on one laptop (one IP), so they would keep kicking each other off. The macro has no
override hook, so we remove that one line from the downloaded copy under .pio/libdeps.
Idempotent; fails the build if the line is neither present nor already patched.
"""
Import("env")  # noqa: F821  (provided by PlatformIO)
from pathlib import Path

hdr = Path(env.subst("$PROJECT_LIBDEPS_DIR")) / env.subst("$PIOENV") / "modbus-esp8266" / "src" / "ModbusSettings.h"  # noqa: F821
if hdr.exists():
    text = hdr.read_text(encoding="utf-8")
    if "\n#define MODBUSIP_UNIQUE_CLIENTS" in text:
        hdr.write_text(text.replace("\n#define MODBUSIP_UNIQUE_CLIENTS", "\n//#define MODBUSIP_UNIQUE_CLIENTS  (disabled by tools/patch_modbus_lib.py)"), encoding="utf-8")
        print("patch_modbus_lib: disabled MODBUSIP_UNIQUE_CLIENTS")
    elif "disabled by tools/patch_modbus_lib.py" not in text:
        raise SystemExit("patch_modbus_lib: MODBUSIP_UNIQUE_CLIENTS line not found; library changed?")
