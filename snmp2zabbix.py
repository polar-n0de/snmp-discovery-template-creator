#!/usr/bin/env python3
"""
=============================================================================
 snmp2zabbix.py  v3.0  –  SNMPWALK → Zabbix 7.0 LTS Template Generator
=============================================================================
 Based on the original concept by Sean Bradley (SNMPWALK2ZABBIX, AGPL-3.0)
 https://github.com/Sean-Bradley/SNMPWALK2ZABBIX
 Rewritten and extended for production use with Zabbix 7.0 LTS.

 What changed vs older Zabbix versions (all handled here):
   - YAML export version is now '7.0'  (was 6.4)
   - XML  export version stays  '6.0'  (Zabbix 7.x still imports it)
   - Default history is now 31d        (was 7d/90d in older versions)
   - Numeric OIDs required in snmp_oid (no MIB text names – proxy safety)
   - template_groups required at YAML root level
   - DISCARD_UNCHANGED_HEARTBEAT preprocessing recommended on slow items
   - value_maps block supported and used (ifOperStatus, supply types)
   - Printer-MIB (RFC 3805) OIDs fully built-in for all printer vendors

 Requirements:
   Ubuntu/Debian:  apt install snmp snmp-mibs-downloader python3-yaml
   RHEL/Rocky:     dnf install net-snmp-utils && pip3 install pyyaml --break-system-packages

 Usage:
   # SNMPv2c full walk (printer, switch, router…):
   python3 snmp2zabbix.py -c public -t 192.168.1.100

   # SNMPv2c restricted to a subtree:
   python3 snmp2zabbix.py -c public -t 192.168.1.100 -o 1.3.6.1.2.1

   # SNMPv3 authPriv:
   python3 snmp2zabbix.py -t 192.168.1.100 --v3 \
       --v3user monitor --v3auth SHA --v3authkey MyAuthPass \
       --v3priv AES   --v3privkey MyPrivPass

   # Force device type and output YAML for Zabbix 7.0:
   python3 snmp2zabbix.py -c public -t 192.168.1.100 --type printer --format yaml

   # Narrow walk to Printer-MIB only:
   python3 snmp2zabbix.py -c public -t 192.168.1.100 -o 1.3.6.1.2.1.43

 Import into Zabbix 7.0 LTS:
   Data collection → Templates → Import
   (XML and YAML both supported – YAML is the preferred format in 7.0)

 All items/rules are imported DISABLED. Review, enable, tune macros.
=============================================================================
"""

import argparse
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone

try:
    import yaml
    YAML_OK = True
except ImportError:
    YAML_OK = False

VERSION = "3.0.0"

# =============================================================================
# Zabbix 7.0 LTS – format constants
# =============================================================================
ZBX_XML_VERSION  = "6.0"   # Zabbix 7.x still imports 6.0 XML
ZBX_YAML_VERSION = "7.0"   # YAML must declare 7.0 for 7.x-specific features
ZBX_HISTORY      = "31d"   # DEFAULT CHANGED in 7.0 (was 7d/90d)
ZBX_TRENDS_NUM   = "365d"
ZBX_TRENDS_STR   = "0"

# =============================================================================
# SNMP type → Zabbix value_type
# Valid strings: FLOAT | CHAR | LOG | UNSIGNED | TEXT
# None = omit field → Zabbix defaults to UNSIGNED
# =============================================================================
DATATYPES: dict = {
    "STRING":       "CHAR",
    "OID":          "CHAR",
    "TIMETICKS":    None,
    "BITS":         "TEXT",
    "COUNTER":      None,
    "COUNTER32":    None,
    "COUNTER64":    None,
    "GAUGE":        None,
    "GAUGE32":      None,
    "INTEGER":      "FLOAT",
    "INTEGER32":    "FLOAT",
    "IPADDR":       "TEXT",
    "IPADDRESS":    "TEXT",
    "NETADDR":      "TEXT",
    "OBJECTID":     "TEXT",
    "OCTETSTR":     "TEXT",
    "OPAQUE":       "TEXT",
    "TICKS":        None,
    "UNSIGNED32":   None,
    "HEX-STRING":   "TEXT",
    '""':           "TEXT",
    "WRONG TYPE (SHOULD BE GAUGE32 OR UNSIGNED32)": "TEXT",
}

# sysObjectID prefix → vendor label
ENTERPRISE_PREFIX = {
    ".1.3.6.1.4.1.9":     "cisco",
    ".1.3.6.1.4.1.11":    "hp",
    ".1.3.6.1.4.1.253":   "xerox",
    ".1.3.6.1.4.1.289":   "canon",
    ".1.3.6.1.4.1.311":   "windows",
    ".1.3.6.1.4.1.641":   "lexmark",
    ".1.3.6.1.4.1.1347":  "kyocera",
    ".1.3.6.1.4.1.2011":  "huawei",
    ".1.3.6.1.4.1.2021":  "net-snmp",
    ".1.3.6.1.4.1.2435":  "brother",
    ".1.3.6.1.4.1.2590":  "ricoh",
    ".1.3.6.1.4.1.2636":  "juniper",
    ".1.3.6.1.4.1.5951":  "netscaler",
    ".1.3.6.1.4.1.6027":  "dell",
    ".1.3.6.1.4.1.8072":  "net-snmp",
    ".1.3.6.1.4.1.12356": "fortinet",
    ".1.3.6.1.4.1.14988": "mikrotik",
    ".1.3.6.1.4.1.25506": "h3c",
    ".1.3.6.1.4.1.1916":  "extreme",
    ".1.3.6.1.4.1.236":   "samsung",
}
PRINTER_VENDORS = {"hp", "xerox", "brother", "kyocera", "samsung",
                   "lexmark", "ricoh", "canon"}

DESCR_HINTS = [
    (r"printer|laserjet|officejet|inkjet|mfp|multifunction|print server|copier", "printer"),
    (r"ios|catalyst|nexus|asr|isr",                  "cisco"),
    (r"junos",                                        "juniper"),
    (r"fortios|fortigate",                            "fortinet"),
    (r"routeros|mikrotik",                            "mikrotik"),
    (r"windows|microsoft",                            "windows"),
    (r"linux|ubuntu|debian|centos|rhel|rocky|suse",   "linux"),
    (r"procurve|aruba",                               "hp"),
]

# =============================================================================
# Printer-MIB (RFC 3805) – hardcoded OID bank
# All OIDs are NUMERIC – no MIB text names (Zabbix 7.0 guideline)
# =============================================================================

# Scalar items injected for printers (in addition to walked items)
# (name, key, oid, value_type, units, description, update_interval, heartbeat)
PRINTER_SCALARS = [
    # Page counters
    ("Total pages printed",
     "printer.pages.total[1.1]",
     "1.3.6.1.2.1.43.10.2.1.4.1.1",
     None, "pages",
     "prtMarkerLifeCount - Total lifetime page count (marker 1, unit 1)",
     "5m", "1h"),
    ("Pages printed since power-on",
     "printer.pages.powerOn[1.1]",
     "1.3.6.1.2.1.43.10.2.1.5.1.1",
     None, "pages",
     "prtMarkerPowerOnCount - Pages printed since last power-on",
     "5m", "1h"),
    # Serial number
    ("Serial number",
     "printer.serialNumber[1]",
     "1.3.6.1.2.1.43.5.1.1.17.1",
     "CHAR", "",
     "prtGeneralSerialNumber - Device serial number",
     "1h", "6h"),
    # Printer status
    ("Printer status",
     "printer.status[1]",
     "1.3.6.1.2.1.25.3.5.1.1.1",
     None, "",
     "hrPrinterStatus - 1=other 2=unknown 3=idle 4=printing 5=warmup",
     "1m", "10m"),
    ("Printer detected error state",
     "printer.errorState[1]",
     "1.3.6.1.2.1.25.3.5.1.2.1",
     "TEXT", "",
     "hrPrinterDetectedErrorState - Bitmask of current error conditions",
     "1m", "10m"),
    # Cover/door state
    ("Cover open status",
     "printer.cover.status[1.1]",
     "1.3.6.1.2.1.43.6.1.1.2.1.1",
     None, "",
     "prtCoverStatus - 1=other 3=open 4=closed",
     "1m", "10m"),
]

# Printer LLD rules (discovery) – injected for printer device type
# Each entry: (rule_name, rule_key, discovery_oid_columns, item_prototypes)
# discovery_oid_columns: list of (macro, numeric_oid)
# item_prototypes: list of (name, key_suffix, oid_suffix, data_type, units, description, heartbeat)
PRINTER_LLD_RULES = [
    # ── Toner / ink / supplies ────────────────────────────────────────────
    {
        "name":  "Printer supplies discovery",
        "key":   "printer.supplies.discovery",
        "discovery_oids": [
            ("{#SUPPLYINDEX}",   "1.3.6.1.2.1.43.11.1.1.1.1"),
            ("{#SUPPLYDESCR}",   "1.3.6.1.2.1.43.11.1.1.6.1"),
            ("{#SUPPLYUNIT}",    "1.3.6.1.2.1.43.11.1.1.8.1"),
            ("{#SUPPLYMAX}",     "1.3.6.1.2.1.43.11.1.1.9.1"),
        ],
        "filter": None,
        "prototypes": [
            ("Supply {#SUPPLYDESCR}: Max capacity",
             "printer.supply.max[{#SUPPLYINDEX}]",
             "1.3.6.1.2.1.43.11.1.1.9.1.{#SUPPLYINDEX}",
             None, "units",
             "prtMarkerSuppliesMaxCapacity - Maximum capacity (-1=unlimited -2=unknown)",
             "1h"),
            ("Supply {#SUPPLYDESCR}: Current level",
             "printer.supply.level[{#SUPPLYINDEX}]",
             "1.3.6.1.2.1.43.11.1.1.10.1.{#SUPPLYINDEX}",
             None, "units",
             "prtMarkerSuppliesLevel - Current level (-1=unlimited -2=unknown -3=no restriction)",
             "5m"),
            ("Supply {#SUPPLYDESCR}: Level %",
             "printer.supply.pct[{#SUPPLYINDEX}]",
             "",   # calculated – no SNMP OID (set via preprocessing in advanced use)
             "FLOAT", "%",
             "Calculated supply level percentage. Enable only if -2/-3 not returned by device.",
             "5m"),
        ],
    },
    # ── Paper trays / input ───────────────────────────────────────────────
    {
        "name":  "Printer input trays discovery",
        "key":   "printer.trays.discovery",
        "discovery_oids": [
            ("{#TRAYINDEX}",     "1.3.6.1.2.1.43.8.2.1.1.1"),
            ("{#TRAYDESCR}",     "1.3.6.1.2.1.43.8.2.1.13.1"),
            ("{#TRAYMAXCAP}",    "1.3.6.1.2.1.43.8.2.1.9.1"),
        ],
        "filter": None,
        "prototypes": [
            ("Tray {#TRAYDESCR}: Max capacity",
             "printer.tray.max[{#TRAYINDEX}]",
             "1.3.6.1.2.1.43.8.2.1.9.1.{#TRAYINDEX}",
             None, "sheets",
             "prtInputMaxCapacity - Maximum media capacity of input sub-unit",
             "1h"),
            ("Tray {#TRAYDESCR}: Current level",
             "printer.tray.level[{#TRAYINDEX}]",
             "1.3.6.1.2.1.43.8.2.1.8.1.{#TRAYINDEX}",
             None, "sheets",
             "prtInputCurrentLevel - Current media level (-1=unlimited -2=unknown -3=available)",
             "5m"),
            ("Tray {#TRAYDESCR}: Media type",
             "printer.tray.media[{#TRAYINDEX}]",
             "1.3.6.1.2.1.43.8.2.1.11.1.{#TRAYINDEX}",
             "CHAR", "",
             "prtInputMediaName - Media type loaded in this tray",
             "1h"),
        ],
    },
    # ── Output bins ───────────────────────────────────────────────────────
    {
        "name":  "Printer output bins discovery",
        "key":   "printer.outputs.discovery",
        "discovery_oids": [
            ("{#OUTPUTINDEX}", "1.3.6.1.2.1.43.9.2.1.1.1"),
            ("{#OUTPUTDESCR}", "1.3.6.1.2.1.43.9.2.1.7.1"),
        ],
        "filter": None,
        "prototypes": [
            ("Output {#OUTPUTDESCR}: Max capacity",
             "printer.output.max[{#OUTPUTINDEX}]",
             "1.3.6.1.2.1.43.9.2.1.4.1.{#OUTPUTINDEX}",
             None, "sheets",
             "prtOutputMaxCapacity",
             "1h"),
            ("Output {#OUTPUTDESCR}: Remaining capacity",
             "printer.output.remaining[{#OUTPUTINDEX}]",
             "1.3.6.1.2.1.43.9.2.1.5.1.{#OUTPUTINDEX}",
             None, "sheets",
             "prtOutputRemainingCapacity",
             "5m"),
        ],
    },
    # ── Alert table ───────────────────────────────────────────────────────
    {
        "name":  "Printer alerts discovery",
        "key":   "printer.alerts.discovery",
        "discovery_oids": [
            ("{#ALERTINDEX}",    "1.3.6.1.2.1.43.18.1.1.1.1"),
            ("{#ALERTDESCR}",    "1.3.6.1.2.1.43.18.1.1.7.1"),
            ("{#ALERTSEVERITY}", "1.3.6.1.2.1.43.18.1.1.2.1"),
        ],
        "filter": None,
        "prototypes": [
            ("Alert {#ALERTINDEX}: Severity",
             "printer.alert.severity[{#ALERTINDEX}]",
             "1.3.6.1.2.1.43.18.1.1.2.1.{#ALERTINDEX}",
             None, "",
             "prtAlertSeverityLevel - 1=other 3=critical 4=warning 5=warningBinaryChangeEvent",
             "1m"),
            ("Alert {#ALERTINDEX}: Code",
             "printer.alert.code[{#ALERTINDEX}]",
             "1.3.6.1.2.1.43.18.1.1.5.1.{#ALERTINDEX}",
             None, "",
             "prtAlertCode - Numeric alert code from vendor MIB",
             "1m"),
            ("Alert {#ALERTINDEX}: Description",
             "printer.alert.descr[{#ALERTINDEX}]",
             "1.3.6.1.2.1.43.18.1.1.7.1.{#ALERTINDEX}",
             "CHAR", "",
             "prtAlertDescription - Textual description of alert",
             "1m"),
        ],
    },
]

# =============================================================================
# Trigger definitions
# (key_fragment, name, expression, severity, description)
# {HOST} → {HOST.HOST}  {KEY} → item key at render time
# =============================================================================
TRIGGER_DEFS = [
    # System
    ("sysUpTime",
     "Device has been restarted (uptime < 10 min)",
     "last(/{HOST}/{KEY})<600",
     "WARNING",
     "Device uptime is under 10 minutes. Possible reboot or power cycle."),
    # Interfaces
    ("ifOperStatus",
     "Interface {#IFDESCR}: Link down",
     "last(/{HOST}/{KEY})=2",
     "AVERAGE",
     "Interface operational status is DOWN."),
    ("ifInErrors",
     "Interface {#IFDESCR}: High inbound error rate",
     "avg(/{HOST}/{KEY},5m)>{$IF.ERRORS.WARN}",
     "WARNING",
     "Inbound errors exceed {$IF.ERRORS.WARN}/poll."),
    ("ifOutErrors",
     "Interface {#IFDESCR}: High outbound error rate",
     "avg(/{HOST}/{KEY},5m)>{$IF.ERRORS.WARN}",
     "WARNING",
     "Outbound errors exceed {$IF.ERRORS.WARN}/poll."),
    # Storage
    ("hrStorageUsed",
     "Storage {#HRDESCR}: Disk utilisation high",
     "last(/{HOST}/{KEY})/last(/{HOST}/hrStorageSize[{#SNMPINDEX}])*100>{$STORAGE.UTIL.WARN}",
     "WARNING",
     "Storage utilisation exceeds {$STORAGE.UTIL.WARN}%."),
    # CPU
    ("hrProcessorLoad",
     "CPU utilisation critical",
     "avg(/{HOST}/{KEY},5m)>{$CPU.UTIL.CRIT}",
     "HIGH",
     "CPU utilisation above {$CPU.UTIL.CRIT}% for 5 minutes."),
    # Printer – supplies
    # Expression simplified: level>=0 ensures we skip -1/-2/-3 special values,
    # level<{$TONER.WARN} works when printer returns 0-100 (percent) or raw units.
    # Adjust {$TONER.WARN} on the host/template to match what your printer returns.
    ("printer.supply.level",
     "Supply {#SUPPLYDESCR}: Toner/ink low",
     "last(/{HOST}/{KEY})>=0 and last(/{HOST}/{KEY})<{$TONER.WARN}",
     "WARNING",
     "Supply level is below {$TONER.WARN}. Replace toner/ink cartridge."),
    # Printer – trays
    ("printer.tray.level",
     "Tray {#TRAYDESCR}: Paper low",
     "last(/{HOST}/{KEY})>=0 and last(/{HOST}/{KEY})<{$PAPER.WARN}",
     "WARNING",
     "Paper tray level is below {$PAPER.WARN}. Refill required."),
    # Printer – cover
    ("printer.cover.status",
     "Printer cover/door is open",
     "last(/{HOST}/{KEY})=3",
     "AVERAGE",
     "Cover or door reported as open. Printing is interrupted."),
    # Printer – status
    ("printer.status",
     "Printer is not idle",
     "last(/{HOST}/{KEY})<>3",
     "INFO",
     "Printer status is not idle (3). Could be printing, warmup, or error."),
    # Printer – alerts
    ("printer.alert.severity",
     "Printer alert {#ALERTINDEX}: Critical",
     "last(/{HOST}/{KEY})=3",
     "HIGH",
     "Printer raised a critical-severity alert. Check alert description."),
]

# =============================================================================
# Macros
# =============================================================================
BASE_MACROS = [
    ("{$SNMP_COMMUNITY}",    "public", "SNMPv2c community string"),
    ("{$IF.ERRORS.WARN}",    "2",      "Interface error count warning threshold (per poll)"),
    ("{$STORAGE.UTIL.WARN}", "85",     "Storage utilisation warning (%)"),
    ("{$CPU.UTIL.CRIT}",     "90",     "CPU utilisation critical (%)"),
    ("{$MEMORY.UTIL.WARN}",  "85",     "Memory utilisation warning (%)"),
    ("{$TONER.WARN}",        "15",     "Toner / ink supply warning level (%)"),
    ("{$PAPER.WARN}",        "10",     "Paper tray warning level (%)"),
    ("{$SNMP.TIMEOUT}",      "5m",     "SNMP agent unavailability timeout"),
]

BASE_TAGS = [
    ("class",  "network"),
    ("source", "snmp"),
]

# =============================================================================
# Value maps (Zabbix 7.0 – included in template YAML/XML)
# =============================================================================
VALUE_MAPS = [
    {
        "name": "ifOperStatus",
        "uuid": None,   # filled at render time
        "mappings": [
            ("1", "up"),
            ("2", "down"),
            ("3", "testing"),
            ("4", "unknown"),
            ("5", "dormant"),
            ("6", "notPresent"),
            ("7", "lowerLayerDown"),
        ],
    },
    {
        "name": "hrPrinterStatus",
        "uuid": None,
        "mappings": [
            ("1", "other"),
            ("2", "unknown"),
            ("3", "idle"),
            ("4", "printing"),
            ("5", "warmup"),
        ],
    },
    {
        "name": "prtCoverStatus",
        "uuid": None,
        "mappings": [
            ("1", "other"),
            ("2", "coverOpen"),
            ("3", "coverClosed"),
            ("4", "interlockOpen"),
            ("5", "interlockClosed"),
        ],
    },
    {
        "name": "prtAlertSeverityLevel",
        "uuid": None,
        "mappings": [
            ("1", "other"),
            ("3", "critical"),
            ("4", "warning"),
            ("5", "warningBinaryChangeEvent"),
        ],
    },
]


# =============================================================================
# Utilities
# =============================================================================

def run(cmd: str, timeout: int = 20) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout
    except subprocess.TimeoutExpired:
        print(f"\n  [WARN] Timeout: {cmd[:80]}", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"\n  [WARN] {e}", file=sys.stderr)
        return ""


def uid() -> str:
    return uuid.uuid4().hex


def safe_key(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._\-\[\]{}#]", "_", s).strip("_")


def xml_esc(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def snmp_auth_flags(args) -> str:
    if args.v3:
        level = "noAuthNoPriv"
        auth  = ""
        priv  = ""
        if args.v3authkey:
            level = "authNoPriv"
            auth  = f"-a {args.v3auth} -A {args.v3authkey}"
        if args.v3privkey:
            level = "authPriv"
            priv  = f"-x {args.v3priv} -X {args.v3privkey}"
        return f"-v 3 -u {args.v3user} -l {level} {auth} {priv}"
    return f"-v {args.version} -c {args.community}"


def get_data_type(snmp_type: str):
    key = snmp_type.strip().upper()
    if key in DATATYPES:
        return DATATYPES[key]
    print(f"\n  [WARN] Unknown SNMP type [{snmp_type}] → TEXT", file=sys.stderr)
    return "TEXT"


def get_mib_description(oid: str) -> str:
    raw = run(f"snmptranslate -Td {oid}")
    m = re.search(r'DESCRIPTION\s*("[^"]*")', raw, re.DOTALL)
    if m:
        desc = m.group(1).replace('"', '').replace('\\n', ' ').replace('\n', ' ')
        return re.sub(r"\s{2,}", " ", desc).strip()
    return ""


def detect_device_type(sys_object_id: str, sys_descr: str) -> str:
    oid = sys_object_id.lstrip(".")
    for prefix, vendor in ENTERPRISE_PREFIX.items():
        if oid.startswith(prefix.lstrip(".")):
            return "printer" if vendor in PRINTER_VENDORS else vendor
    descr = sys_descr.lower()
    for pattern, hint in DESCR_HINTS:
        if re.search(pattern, descr):
            return hint
    return "generic"


def is_numeric(data_type) -> bool:
    return data_type in (None, "FLOAT")


def trends(data_type) -> str:
    return ZBX_TRENDS_NUM if is_numeric(data_type) else ZBX_TRENDS_STR


def check_dependencies():
    missing = [t for t in ("snmpwalk", "snmptranslate", "snmpget")
               if not run(f"which {t}").strip()]
    if missing:
        print(
            f"[ERROR] Missing: {', '.join(missing)}\n"
            "  Ubuntu/Debian: apt install snmp snmp-mibs-downloader\n"
            "  RHEL/Rocky:    dnf install net-snmp-utils",
            file=sys.stderr,
        )
        sys.exit(1)


# =============================================================================
# SNMP interaction
# =============================================================================

def get_system_info(args) -> dict:
    auth = snmp_auth_flags(args)
    result = {}
    scalars = {
        "sysDescr":    ".1.3.6.1.2.1.1.1.0",
        "sysObjectID": ".1.3.6.1.2.1.1.2.0",
        "sysName":     ".1.3.6.1.2.1.1.5.0",
        "sysContact":  ".1.3.6.1.2.1.1.4.0",
        "sysLocation": ".1.3.6.1.2.1.1.6.0",
    }
    for key, oid in scalars.items():
        out = run(f"snmpget -On {auth} {args.target}:{args.port} {oid}")
        m   = re.search(r"=\s*\S+:\s*(.*)", out)
        result[key] = m.group(1).strip().strip('"') if m else ""
    return result


def do_walk(args) -> list:
    auth = snmp_auth_flags(args)
    base = args.oid
    cmd  = f"snmpwalk -On {auth} {args.target}:{args.port} {base}"
    print(f"[*] {cmd}")
    raw  = run(cmd, timeout=300)
    lines = [
        l for l in raw.splitlines()
        if l.strip() and "NO MORE VARIABLES LEFT" not in l.upper()
    ]
    print(f"[*] {len(lines)} OID rows received")
    return lines


def parse_walk(lines: list, args) -> tuple:
    """Parse snmpwalk -On output → (scalar_items, disc_rules)."""
    items      = []
    disc_rules = {}
    total      = max(len(lines), 1)

    for idx, line in enumerate(lines):
        if "=" not in line:
            continue
        oid_part, rest = line.split("=", 1)
        oid_str = oid_part.strip()

        parts_rest = rest.split(":", 1)
        if len(parts_rest) < 2:
            continue
        snmp_type = parts_rest[0].strip().upper()
        data_type = get_data_type(snmp_type)

        # Resolve full dotted MIB name path (e.g. .iso.org...ifDescr.1)
        full_path = run(f"snmptranslate -Of {oid_str}").strip()
        if not full_path:
            continue
        path_parts = full_path.split(".")

        # Skip very deep / per-instance OIDs
        if len(path_parts) > 14:
            continue

        mib_str = run(f"snmptranslate -Tz {oid_str}").strip()
        if "::" not in mib_str:
            mib_str = oid_str

        description = get_mib_description(oid_str)

        pct = int((idx + 1) / total * 100)
        print(f"  [{pct:3d}%] {mib_str[:72]:<72}", end="\r", flush=True)

        is_table = (len(path_parts) >= 11
                    and path_parts[8].upper().endswith("TABLE"))

        if is_table:
            table_name = path_parts[8]
            col_name   = path_parts[10] if len(path_parts) > 10 else "value"
            rule_key   = mib_str.split("::")[0] + "::" + table_name

            if rule_key not in disc_rules:
                disc_rules[rule_key] = {"items": [], "seen_cols": set()}

            if col_name not in disc_rules[rule_key]["seen_cols"]:
                disc_rules[rule_key]["seen_cols"].add(col_name)
                proto_oid = ".".join(oid_str.split(".")[:-1])  # strip instance
                disc_rules[rule_key]["items"].append({
                    "col_name":    col_name,
                    "mib":         mib_str,
                    "oid":         proto_oid,
                    "data_type":   data_type,
                    "description": description,
                    "macro":       "{#" + col_name.upper() + "}",
                })
                print(f"\n  [LLD]  {rule_key} → {col_name}")
        else:
            if "::" in mib_str:
                item_name = mib_str.split("::")[1].split(".")[0]
            else:
                item_name = oid_str.rsplit(".", 2)[-2] if "." in oid_str else oid_str
            key = safe_key(mib_str.replace("::", "."))
            items.append({
                "name":        item_name,
                "mib":         mib_str,
                "key":         key,
                "oid":         oid_str,
                "data_type":   data_type,
                "description": description,
            })
            print(f"\n  [ITEM] {mib_str} ({data_type or 'UNSIGNED'})")

    print()
    return items, disc_rules


# =============================================================================
# Trigger builder
# =============================================================================

def triggers_for_key(item_key: str, tpl_name: str, is_prototype: bool = False) -> list:
    """
    Build trigger/trigger-prototype dicts for a given item key.
    tpl_name must be the exact template name – Zabbix 7.0 requires the template
    name (not {HOST.HOST}) inside function expressions in template definitions.
    """
    result = []
    for (fragment, name, expr_tmpl, severity, desc) in TRIGGER_DEFS:
        if fragment.lower() in item_key.lower():
            expr = (expr_tmpl
                    .replace("{HOST}", tpl_name)   # template name, NOT {HOST.HOST}
                    .replace("{KEY}",  item_key))
            result.append({
                "uuid":        uid(),
                "name":        name,
                "expression":  expr,
                "priority":    severity,
                "description": desc,
                "tags":        [{"tag": "scope", "value": "availability"}],
            })
    return result


# =============================================================================
# Preprocessing helper (Zabbix 7.0 – DISCARD_UNCHANGED_HEARTBEAT)
# =============================================================================

def preprocessing_heartbeat(heartbeat: str) -> list:
    """Returns a preprocessing step list with DISCARD_UNCHANGED_HEARTBEAT."""
    return [{
        "type":       "DISCARD_UNCHANGED_HEARTBEAT",
        "parameters": [heartbeat],
    }]


# =============================================================================
# Printer-specific injectors
# =============================================================================

def inject_printer_scalars(items: list):
    """Add well-known printer scalar items if not already walked."""
    existing_oids = {it["oid"] for it in items}
    for (name, key, oid, vtype, units, desc, interval, heartbeat) in PRINTER_SCALARS:
        if oid and oid not in existing_oids:
            items.append({
                "name":          name,
                "mib":           "Printer-MIB::" + name.replace(" ", ""),
                "key":           key,
                "oid":           oid,
                "data_type":     vtype,
                "description":   desc,
                "units":         units,
                "delay":         interval,
                "heartbeat":     heartbeat,
                "_printer_scalar": True,
            })


def inject_printer_lld(disc_rules: dict):
    """Add printer LLD rules if not already discovered by walk."""
    existing_keys = {v.get("_rule_key") for v in disc_rules.values()}
    for rule in PRINTER_LLD_RULES:
        rk = "Printer-MIB::" + rule["key"]
        if rk not in disc_rules:
            disc_rules[rk] = {
                "_rule_key":  rk,
                "_printer":   True,
                "rule_name":  rule["name"],
                "rule_key":   rule["key"],
                "disc_oids":  rule["discovery_oids"],
                "filter":     rule["filter"],
                "items":      [],
                "seen_cols":  set(),
                "prototypes": rule["prototypes"],
            }


# =============================================================================
# YAML builder – Zabbix 7.0 LTS native format
# =============================================================================

def build_yaml(sys_info: dict, items: list, disc_rules: dict,
               device_type: str, args) -> str:
    if not YAML_OK:
        print("[ERROR] PyYAML missing. pip3 install pyyaml --break-system-packages",
              file=sys.stderr)
        sys.exit(1)

    tpl_name      = (sys_info.get("sysName") or args.target).replace(" ", "-")
    full_tpl_name = f"{tpl_name} SNMP"   # full name used in trigger expressions
    now           = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Value maps ────────────────────────────────────────────────────────
    def render_valuemaps() -> list:
        out = []
        for vm in VALUE_MAPS:
            out.append({
                "uuid":     uid(),
                "name":     vm["name"],
                "mappings": [{"value": v, "newvalue": n} for v, n in vm["mappings"]],
            })
        return out

    # ── Preprocessing ─────────────────────────────────────────────────────
    def preproc(hb: str) -> list:
        return [{"type": "DISCARD_UNCHANGED_HEARTBEAT", "parameters": [hb]}]

    # ── Scalar items ──────────────────────────────────────────────────────
    def render_items() -> list:
        out = []
        for it in items:
            ikey  = it["key"]
            delay = it.get("delay", "5m")
            hb    = it.get("heartbeat", "1h")
            d = {
                "uuid":           uid(),
                "name":           it["name"],
                "type":           "SNMP_AGENT",
                "snmp_oid":       it["oid"],
                "key":            ikey,
                "delay":          delay,
                "history":        ZBX_HISTORY,
                "trends":         trends(it["data_type"]),
                "description":    it["description"],
                "status":         "DISABLED",
                "preprocessing":  preproc(hb),
            }
            if it["data_type"]:
                d["value_type"] = it["data_type"]
            units = it.get("units", "")
            if units:
                d["units"] = units
            trigs = triggers_for_key(ikey, full_tpl_name)
            if trigs:
                d["triggers"] = trigs
            out.append(d)
        return out

    # ── Discovery rules ───────────────────────────────────────────────────
    def render_rules() -> list:
        out = []
        for rule_key, rule_data in disc_rules.items():
            is_printer_rule = rule_data.get("_printer", False)

            if is_printer_rule:
                # Printer rules use the pre-defined prototype list
                rule_label = rule_data["rule_name"]
                lld_key    = rule_data["rule_key"]
                disc_oids  = rule_data["disc_oids"]
                protos_raw = rule_data["prototypes"]

                oid_parts = [macro + "," + oid for macro, oid in disc_oids]
                discovery_oid = "discovery[" + ",".join(oid_parts) + "]"
                while len(discovery_oid) > 512 and oid_parts:
                    oid_parts.pop()
                    discovery_oid = "discovery[" + ",".join(oid_parts) + "]"

                item_protos = []
                trig_protos = []
                graph_protos = []
                for (pname, pkey, poid, ptype, punits, pdesc, phb) in protos_raw:
                    if not poid:   # skip calculated-only entries
                        continue
                    pd = {
                        "uuid":          uid(),
                        "name":          pname,
                        "type":          "SNMP_AGENT",
                        "snmp_oid":      poid,
                        "key":           pkey,
                        "delay":         "5m",
                        "history":       ZBX_HISTORY,
                        "trends":        trends(ptype),
                        "description":   pdesc,
                        "status":        "DISABLED",
                        "preprocessing": preproc(phb),
                    }
                    if ptype:
                        pd["value_type"] = ptype
                    if punits:
                        pd["units"] = punits
                    trigs = triggers_for_key(pkey, full_tpl_name, is_prototype=True)
                    if trigs:
                        trig_protos.extend(trigs)
                    item_protos.append(pd)

                    # graph for numeric
                    if is_numeric(ptype):
                        graph_protos.append({
                            "uuid": uid(),
                            "name": rule_label + ": " + pname,
                            "graph_items": [{
                                "sortorder": "1",
                                "drawtype":  "BOLD_LINE",
                                "color":     "FF6600",
                                "item": {
                                    "host": f"{tpl_name} SNMP",
                                    "key":  pkey,
                                },
                            }],
                        })

                rd = {
                    "uuid":            uid(),
                    "name":            rule_label,
                    "type":            "SNMP_AGENT",
                    "snmp_oid":        discovery_oid,
                    "key":             lld_key,
                    "delay":           "1h",
                    "status":          "DISABLED",
                    "item_prototypes": item_protos,
                }
                if trig_protos:
                    rd["trigger_prototypes"] = trig_protos
                if graph_protos:
                    rd["graph_prototypes"] = graph_protos
                out.append(rd)

            else:
                # Walked rules
                protos     = rule_data["items"]
                rule_label = rule_key.split("::")[-1]
                lld_key    = safe_key(rule_key.replace("::", ".")) + "._discovery"

                oid_parts = ["{#" + p["col_name"].upper() + "}," + p["oid"]
                             for p in protos]
                discovery_oid = "discovery[" + ",".join(oid_parts) + "]"
                while len(discovery_oid) > 512 and oid_parts:
                    oid_parts.pop()
                    discovery_oid = "discovery[" + ",".join(oid_parts) + "]"

                item_protos  = []
                trig_protos  = []
                graph_protos = []

                for p in protos:
                    proto_key = safe_key(p["col_name"]) + "[{#SNMPINDEX}]"
                    pd = {
                        "uuid":          uid(),
                        "name":          p["col_name"] + ".{#SNMPINDEX}",
                        "type":          "SNMP_AGENT",
                        "snmp_oid":      p["oid"] + ".{#SNMPINDEX}",
                        "key":           proto_key,
                        "delay":         "5m",
                        "history":       ZBX_HISTORY,
                        "trends":        trends(p["data_type"]),
                        "description":   p["description"],
                        "status":        "DISABLED",
                        "preprocessing": preproc("1h"),
                    }
                    if p["data_type"]:
                        pd["value_type"] = p["data_type"]
                    trigs = triggers_for_key(proto_key, full_tpl_name, is_prototype=True)
                    if trigs:
                        trig_protos.extend(trigs)
                    item_protos.append(pd)

                    if is_numeric(p["data_type"]):
                        graph_protos.append({
                            "uuid": uid(),
                            "name": rule_label + ": " + p["col_name"] + ".{#SNMPINDEX}",
                            "graph_items": [{
                                "sortorder": "1",
                                "drawtype":  "BOLD_LINE",
                                "color":     "1A7C11",
                                "item": {
                                    "host": f"{tpl_name} SNMP",
                                    "key":  proto_key,
                                },
                            }],
                        })

                rd = {
                    "uuid":            uid(),
                    "name":            rule_label,
                    "type":            "SNMP_AGENT",
                    "snmp_oid":        discovery_oid,
                    "key":             lld_key,
                    "delay":           "1h",
                    "status":          "DISABLED",
                    "item_prototypes": item_protos,
                }
                # LLD filter for interface tables
                if "ifTable" in rule_label or "ifXTable" in rule_label:
                    rd["filter"] = {
                        "evaltype": "AND_OR",
                        "conditions": [{
                            "macro":     "{#IFTYPE}",
                            "value":     "^6$",
                            "operator":  "MATCHES_REGEX",
                            "formulaid": "A",
                        }],
                    }
                if trig_protos:
                    rd["trigger_prototypes"] = trig_protos
                if graph_protos:
                    rd["graph_prototypes"] = graph_protos[:4]
                out.append(rd)

        return out

    # ── Groups ────────────────────────────────────────────────────────────
    groups = [{"name": "Templates/Network devices"}]
    if device_type == "printer":
        groups = [
            {"name": "Templates/Network devices"},
            {"name": "Templates/Printers"},
        ]
    elif device_type == "linux":
        groups = [{"name": "Templates/Operating systems"}]

    # ── Full export dict ──────────────────────────────────────────────────
    export = {
        "zabbix_export": {
            "version": ZBX_YAML_VERSION,    # "7.0"  ← Zabbix 7.0 LTS
            # NOTE: "date" key intentionally omitted – Zabbix 7.0 rejects it on import
            # template_groups required at root level in 7.0
            "template_groups": [
                {"uuid": uid(), "name": g["name"]} for g in groups
            ],
            "templates": [{
                "uuid":        uid(),
                "template":    f"{tpl_name} SNMP",
                "name":        f"{tpl_name} SNMP",
                "description": (
                    f"Auto-generated by snmp2zabbix.py v{VERSION} for Zabbix 7.0 LTS\n"
                    f"Device : {sys_info.get('sysDescr','N/A')[:120]}\n"
                    f"Type   : {device_type}\n"
                    f"Date   : {now}"
                ),
                "groups":           groups,
                "macros":           [{"macro": m, "value": v, "description": d}
                                     for m, v, d in BASE_MACROS],
                "tags":             ([{"tag": t, "value": v} for t, v in BASE_TAGS]
                                     + [{"tag": "device-type", "value": device_type}]),
                "valuemaps":        render_valuemaps(),
                "items":            render_items(),
                "discovery_rules":  render_rules(),
            }],
        }
    }

    return yaml.dump(export, allow_unicode=True, sort_keys=False,
                     default_flow_style=False)


# =============================================================================
# XML builder – Zabbix 6.0 format (importable in 7.x)
# Note: XML uses <name> (not <n>) everywhere – critical for import success
# =============================================================================

def build_xml(sys_info: dict, items: list, disc_rules: dict,
              device_type: str, args) -> str:

    tpl_name      = xml_esc((sys_info.get("sysName") or args.target).replace(" ", "-"))
    full_tpl_name = f"{tpl_name} SNMP"   # full name used in trigger expressions
    now           = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    L = []
    w = L.append

    def preproc_xml(heartbeat: str):
        w('              <preprocessing>')
        w('                <step>')
        w('                  <type>DISCARD_UNCHANGED_HEARTBEAT</type>')
        w('                  <parameters>')
        w(f'                    <parameter>{heartbeat}</parameter>')
        w('                  </parameters>')
        w('                </step>')
        w('              </preprocessing>')

    w('<?xml version="1.0" encoding="UTF-8"?>')
    w('<zabbix_export>')
    w(f'  <version>{ZBX_XML_VERSION}</version>')
    # NOTE: <date> tag intentionally omitted – Zabbix 7.0 rejects it on import

    # Value maps at root level
    w('  <value_maps>')
    for vm in VALUE_MAPS:
        w('    <value_map>')
        w(f'      <uuid>{uid()}</uuid>')
        w(f'      <name>{vm["name"]}</name>')
        w('      <mappings>')
        for val, label in vm["mappings"]:
            w('        <mapping>')
            w(f'          <value>{val}</value>')
            w(f'          <newvalue>{label}</newvalue>')
            w('        </mapping>')
        w('      </mappings>')
        w('    </value_map>')
    w('  </value_maps>')

    w('  <templates>')
    w('    <template>')
    w(f'      <uuid>{uid()}</uuid>')
    w(f'      <template>{tpl_name} SNMP</template>')
    w(f'      <name>{tpl_name} SNMP</name>')   # <name> NOT <name>
    desc = xml_esc(
        f"Auto-generated by snmp2zabbix.py v{VERSION} for Zabbix 7.0 LTS | "
        f"Device: {sys_info.get('sysDescr','N/A')[:100]} | "
        f"Type: {device_type} | Generated: {now}"
    )
    w(f'      <description>{desc}</description>')

    # Groups
    w('      <groups>')
    w('        <group><name>Templates/Network devices</name></group>')
    if device_type == "printer":
        w('        <group><name>Templates/Printers</name></group>')
    elif device_type == "linux":
        w('        <group><name>Templates/Operating systems</name></group>')
    w('      </groups>')

    # Macros
    w('      <macros>')
    for macro, value, mdesc in BASE_MACROS:
        w('        <macro>')
        w(f'          <macro>{macro}</macro>')
        w(f'          <value>{xml_esc(value)}</value>')
        w(f'          <description>{xml_esc(mdesc)}</description>')
        w('        </macro>')
    w('      </macros>')

    # Tags
    w('      <tags>')
    for tag, val in BASE_TAGS:
        w(f'        <tag><tag>{tag}</tag><value>{val}</value></tag>')
    w(f'        <tag><tag>device-type</tag><value>{device_type}</value></tag>')
    w('      </tags>')

    # ── SCALAR ITEMS ──────────────────────────────────────────────────────
    w('      <items>')
    for item in items:
        ikey  = item["key"]
        delay = item.get("delay", "5m")
        hb    = item.get("heartbeat", "1h")
        units = item.get("units", "")
        trigs = triggers_for_key(ikey, full_tpl_name)
        w('        <item>')
        w(f'          <uuid>{uid()}</uuid>')
        w(f'          <name>{xml_esc(item["name"])}</name>')
        w(f'          <type>SNMP_AGENT</type>')
        w(f'          <snmp_oid>{item["oid"]}</snmp_oid>')
        w(f'          <key>{ikey}</key>')
        if item["data_type"]:
            w(f'          <value_type>{item["data_type"]}</value_type>')
        if units:
            w(f'          <units>{xml_esc(units)}</units>')
        w(f'          <delay>{delay}</delay>')
        w(f'          <history>{ZBX_HISTORY}</history>')
        w(f'          <trends>{trends(item["data_type"])}</trends>')
        w(f'          <description>{xml_esc(item["description"])}</description>')
        w(f'          <status>DISABLED</status>')
        preproc_xml(hb)
        if trigs:
            w('          <triggers>')
            for t in trigs:
                w('            <trigger>')
                w(f'              <uuid>{t["uuid"]}</uuid>')
                w(f'              <expression>{xml_esc(t["expression"])}</expression>')
                w(f'              <name>{xml_esc(t["name"])}</name>')
                w(f'              <priority>{t["priority"]}</priority>')
                w(f'              <description>{xml_esc(t["description"])}</description>')
                w('              <tags>')
                w('                <tag><tag>scope</tag><value>availability</value></tag>')
                w('              </tags>')
                w('            </trigger>')
            w('          </triggers>')
        w('        </item>')
    w('      </items>')

    # ── DISCOVERY RULES ───────────────────────────────────────────────────
    w('      <discovery_rules>')
    for rule_key, rule_data in disc_rules.items():
        is_printer_rule = rule_data.get("_printer", False)

        if is_printer_rule:
            rule_label    = rule_data["rule_name"]
            lld_key       = rule_data["rule_key"]
            disc_oids     = rule_data["disc_oids"]
            protos_raw    = rule_data["prototypes"]
            oid_parts     = [macro + "," + oid for macro, oid in disc_oids]
            discovery_oid = "discovery[" + ",".join(oid_parts) + "]"
            while len(discovery_oid) > 512 and oid_parts:
                oid_parts.pop()
                discovery_oid = "discovery[" + ",".join(oid_parts) + "]"

            w('        <discovery_rule>')
            w(f'          <uuid>{uid()}</uuid>')
            w(f'          <name>{xml_esc(rule_label)}</name>')
            w(f'          <type>SNMP_AGENT</type>')
            w(f'          <snmp_oid>{discovery_oid}</snmp_oid>')
            w(f'          <key>{lld_key}</key>')
            w(f'          <delay>1h</delay>')
            w(f'          <status>DISABLED</status>')
            w('          <item_prototypes>')
            for (pname, pkey, poid, ptype, punits, pdesc, phb) in protos_raw:
                if not poid:
                    continue
                w('            <item_prototype>')
                w(f'              <uuid>{uid()}</uuid>')
                w(f'              <name>{xml_esc(pname)}</name>')
                w(f'              <type>SNMP_AGENT</type>')
                w(f'              <snmp_oid>{poid}</snmp_oid>')
                w(f'              <key>{pkey}</key>')
                if ptype:
                    w(f'              <value_type>{ptype}</value_type>')
                if punits:
                    w(f'              <units>{xml_esc(punits)}</units>')
                w(f'              <delay>5m</delay>')
                w(f'              <history>{ZBX_HISTORY}</history>')
                w(f'              <trends>{trends(ptype)}</trends>')
                w(f'              <description>{xml_esc(pdesc)}</description>')
                w(f'              <status>DISABLED</status>')
                preproc_xml(phb)
                # trigger prototypes
                tprotos = triggers_for_key(pkey, full_tpl_name, True)
                if tprotos:
                    w('              <trigger_prototypes>')
                    for t in tprotos:
                        w('                <trigger_prototype>')
                        w(f'                  <uuid>{uid()}</uuid>')
                        w(f'                  <expression>{xml_esc(t["expression"])}</expression>')
                        w(f'                  <name>{xml_esc(t["name"])}</name>')
                        w(f'                  <priority>{t["priority"]}</priority>')
                        w(f'                  <description>{xml_esc(t["description"])}</description>')
                        w('                  <tags>')
                        w('                    <tag><tag>scope</tag><value>availability</value></tag>')
                        w('                  </tags>')
                        w('                </trigger_prototype>')
                    w('              </trigger_prototypes>')
                w('            </item_prototype>')
            w('          </item_prototypes>')
            # graph prototypes
            num_p = [(pname, pkey) for (pname, pkey, poid, ptype, punits, pdesc, phb) in protos_raw
                     if poid and is_numeric(ptype)][:4]
            if num_p:
                w('          <graph_prototypes>')
                for pname, pkey in num_p:
                    w('            <graph_prototype>')
                    w(f'              <uuid>{uid()}</uuid>')
                    w(f'              <name>{xml_esc(rule_label + ": " + pname)}</name>')
                    w('              <type>NORMAL</type>')
                    w('              <graph_items>')
                    w('                <graph_item>')
                    w('                  <sortorder>1</sortorder>')
                    w('                  <drawtype>BOLD_LINE</drawtype>')
                    w('                  <color>FF6600</color>')
                    w('                  <item>')
                    w(f'                    <host>{tpl_name} SNMP</host>')
                    w(f'                    <key>{pkey}</key>')
                    w('                  </item>')
                    w('                </graph_item>')
                    w('              </graph_items>')
                    w('            </graph_prototype>')
                w('          </graph_prototypes>')
            w('        </discovery_rule>')

        else:
            # Walked LLD rules
            protos     = rule_data["items"]
            rule_label = rule_key.split("::")[-1]
            lld_key    = safe_key(rule_key.replace("::", ".")) + "._discovery"
            oid_parts  = ["{#" + p["col_name"].upper() + "}," + p["oid"]
                          for p in protos]
            discovery_oid = "discovery[" + ",".join(oid_parts) + "]"
            while len(discovery_oid) > 512 and oid_parts:
                oid_parts.pop()
                discovery_oid = "discovery[" + ",".join(oid_parts) + "]"

            w('        <discovery_rule>')
            w(f'          <uuid>{uid()}</uuid>')
            w(f'          <name>{xml_esc(rule_label)}</name>')
            w(f'          <type>SNMP_AGENT</type>')
            w(f'          <snmp_oid>{discovery_oid}</snmp_oid>')
            w(f'          <key>{lld_key}</key>')
            w(f'          <delay>1h</delay>')
            w(f'          <status>DISABLED</status>')
            if "ifTable" in rule_label or "ifXTable" in rule_label:
                w('          <filter>')
                w('            <evaltype>AND_OR</evaltype>')
                w('            <conditions>')
                w('              <condition>')
                w('                <macro>{#IFTYPE}</macro>')
                w('                <value>^6$</value>')
                w('                <operator>MATCHES_REGEX</operator>')
                w('                <formulaid>A</formulaid>')
                w('              </condition>')
                w('            </conditions>')
                w('          </filter>')
            w('          <item_prototypes>')
            for p in protos:
                proto_key  = safe_key(p["col_name"]) + "[{#SNMPINDEX}]"
                proto_name = p["col_name"] + ".{#SNMPINDEX}"
                tprotos    = triggers_for_key(proto_key, full_tpl_name, True)
                w('            <item_prototype>')
                w(f'              <uuid>{uid()}</uuid>')
                w(f'              <name>{xml_esc(proto_name)}</name>')
                w(f'              <type>SNMP_AGENT</type>')
                w(f'              <snmp_oid>{p["oid"]}.{{#SNMPINDEX}}</snmp_oid>')
                w(f'              <key>{proto_key}</key>')
                if p["data_type"]:
                    w(f'              <value_type>{p["data_type"]}</value_type>')
                w(f'              <delay>5m</delay>')
                w(f'              <history>{ZBX_HISTORY}</history>')
                w(f'              <trends>{trends(p["data_type"])}</trends>')
                w(f'              <description>{xml_esc(p["description"])}</description>')
                w(f'              <status>DISABLED</status>')
                preproc_xml("1h")
                if tprotos:
                    w('              <trigger_prototypes>')
                    for t in tprotos:
                        w('                <trigger_prototype>')
                        w(f'                  <uuid>{uid()}</uuid>')
                        w(f'                  <expression>{xml_esc(t["expression"])}</expression>')
                        w(f'                  <name>{xml_esc(t["name"])}</name>')
                        w(f'                  <priority>{t["priority"]}</priority>')
                        w(f'                  <description>{xml_esc(t["description"])}</description>')
                        w('                  <tags>')
                        w('                    <tag><tag>scope</tag><value>availability</value></tag>')
                        w('                  </tags>')
                        w('                </trigger_prototype>')
                    w('              </trigger_prototypes>')
                w('            </item_prototype>')
            w('          </item_prototypes>')
            # graph prototypes – numeric columns only
            num_protos = [p for p in protos if is_numeric(p["data_type"])][:4]
            if num_protos:
                w('          <graph_prototypes>')
                for p in num_protos:
                    proto_key = safe_key(p["col_name"]) + "[{#SNMPINDEX}]"
                    g_name    = xml_esc(rule_label + ": " + p["col_name"] + ".{#SNMPINDEX}")
                    w('            <graph_prototype>')
                    w(f'              <uuid>{uid()}</uuid>')
                    w(f'              <name>{g_name}</name>')
                    w(f'              <type>NORMAL</type>')
                    w('              <graph_items>')
                    w('                <graph_item>')
                    w('                  <sortorder>1</sortorder>')
                    w('                  <drawtype>BOLD_LINE</drawtype>')
                    w('                  <color>1A7C11</color>')
                    w('                  <item>')
                    w(f'                    <host>{tpl_name} SNMP</host>')
                    w(f'                    <key>{proto_key}</key>')
                    w('                  </item>')
                    w('                </graph_item>')
                    w('              </graph_items>')
                    w('            </graph_prototype>')
                w('          </graph_prototypes>')
            w('        </discovery_rule>')

    w('      </discovery_rules>')
    w('    </template>')
    w('  </templates>')
    w('</zabbix_export>')

    return "\n".join(L)


# =============================================================================
# CLI
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="SNMPWALK -> Zabbix 7.0 LTS Template Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-t", "--target",    required=True,
                   help="Device IP or hostname")
    p.add_argument("-p", "--port",      default="161",
                   help="SNMP UDP port (default: 161)")
    p.add_argument("-o", "--oid",       default=".",
                   help="Base OID to walk (default: . = full tree). "
                        "Use 1.3.6.1.2.1.43 for Printer-MIB only.")
    p.add_argument("-c", "--community", default="public",
                   help="SNMPv1/v2c community string (default: public)")
    p.add_argument("--version",         default="2c", choices=["1", "2c"],
                   help="SNMP version (default: 2c)")

    v3 = p.add_argument_group("SNMPv3 options")
    v3.add_argument("--v3",         action="store_true", help="Use SNMPv3")
    v3.add_argument("--v3user",     default="monitor")
    v3.add_argument("--v3auth",     default="SHA",  choices=["SHA", "MD5"])
    v3.add_argument("--v3authkey",  default="",     help="Auth passphrase")
    v3.add_argument("--v3priv",     default="AES",  choices=["AES", "DES"])
    v3.add_argument("--v3privkey",  default="",     help="Priv passphrase")

    out = p.add_argument_group("Output options")
    out.add_argument("--format", default="yaml", choices=["xml", "yaml"],
                     help="yaml = Zabbix 7.0 native (default)  xml = 6.0 compat")
    out.add_argument("--type",   default=None,
                     choices=["cisco", "hp", "printer", "linux", "windows",
                              "juniper", "mikrotik", "fortinet", "generic"],
                     help="Force device type (skip auto-detection)")
    out.add_argument("--out",    default=None,
                     help="Output filename (default: template-<sysName>.<format>)")
    return p.parse_args()


# =============================================================================
# Entry point
# =============================================================================

def main():
    args = parse_args()
    sep  = "=" * 64

    print(f"\n{sep}")
    print(f"  snmp2zabbix.py  v{VERSION}  [Zabbix 7.0 LTS]")
    print(f"  Target : {args.target}:{args.port}")
    if args.v3:
        level = "noAuthNoPriv"
        if args.v3authkey: level = "authNoPriv"
        if args.v3privkey: level = "authPriv"
        print(f"  Auth   : SNMPv3  user={args.v3user}  level={level}")
    else:
        print(f"  Auth   : SNMPv{args.version}  community={args.community}")
    print(f"  Format : {args.format.upper()}  (Zabbix "
          + ("7.0 native" if args.format == "yaml" else "6.0 compat import") + ")")
    print(f"{sep}\n")

    check_dependencies()

    # 1 – System info
    print("[1/4] Fetching system info ...")
    sys_info = get_system_info(args)
    for k in ("sysName", "sysDescr", "sysObjectID", "sysContact", "sysLocation"):
        print(f"      {k:<15}: {sys_info.get(k,'<none>')[:78]}")

    # 2 – Device type
    device_type = args.type or detect_device_type(
        sys_info.get("sysObjectID", ""),
        sys_info.get("sysDescr",    ""),
    )
    src = "forced" if args.type else "auto-detected"
    print(f"\n[2/4] Device type ({src}): {device_type}")

    # 3 – Walk & parse
    print("\n[3/4] Walking SNMP tree ...")
    lines = do_walk(args)
    items, disc_rules = parse_walk(lines, args)

    # Inject printer-specific OIDs if device is a printer
    if device_type == "printer":
        before_i = len(items)
        before_r = len(disc_rules)
        inject_printer_scalars(items)
        inject_printer_lld(disc_rules)
        added_i = len(items) - before_i
        added_r = len(disc_rules) - before_r
        print(f"  [PRINTER] Injected {added_i} scalar items, "
              f"{added_r} LLD rules (Printer-MIB RFC 3805)")

    total_protos = sum(
        len(v.get("items", v.get("prototypes", [])))
        for v in disc_rules.values()
    )
    print(f"\n      Scalar items    : {len(items)}")
    print(f"      Discovery rules : {len(disc_rules)}")
    print(f"      Item prototypes : {total_protos}")

    # 4 – Generate
    print(f"\n[4/4] Generating {args.format.upper()} template ...")
    tpl_name = (sys_info.get("sysName") or args.target).replace(" ", "-")
    out_file  = args.out or f"template-{tpl_name}.{args.format}"

    content = (build_yaml(sys_info, items, disc_rules, device_type, args)
               if args.format == "yaml"
               else build_xml(sys_info, items, disc_rules, device_type, args))

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n{sep}")
    print(f"  OK  Written → {out_file}")
    print(f"      Scalar items    : {len(items)}")
    print(f"      Discovery rules : {len(disc_rules)}")
    print(f"      Item prototypes : {total_protos}")
    print(f"{sep}")
    print()
    print("  Import → Zabbix 7.0 LTS:")
    print("    Data collection → Templates → Import")
    print()
    print("  Post-import checklist:")
    print("    1. Link template to host (SNMP interface configured)")
    print("    2. Set {$SNMP_COMMUNITY} macro on host (or use SNMPv3)")
    print("    3. Enable items/rules ONE BY ONE – not all at once")
    print("    4. For printers: enable supplies + trays discovery first")
    print("    5. Review toner/paper triggers, adjust {$TONER.WARN} / {$PAPER.WARN}")
    print()


if __name__ == "__main__":
    main()
