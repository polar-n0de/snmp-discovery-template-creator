# SNMP Discovery Template Creator

**`snmp2zabbix.py`** — walk any SNMP-capable device and auto-generate a ready-to-import **Zabbix 7.0 LTS** monitoring template (YAML or XML), complete with items, low-level discovery rules, triggers, graphs, value maps, and macros.

Built for the common real-world problem: a device has SNMP but **no existing Zabbix template**. Point this at it and get a working starting template in seconds.

---

## Credits

Based on the original concept by **[Sean Bradley](https://github.com/Sean-Bradley/SNMPWALK2ZABBIX)** (SNMPWALK2ZABBIX, AGPL-3.0). This is an independent rewrite and substantial extension targeting Zabbix 7.0 LTS, with a new template builder, hardcoded Printer-MIB (RFC 3805) support, automatic device-type detection, trigger/graph generation, and value maps.

Licensed under **AGPL-3.0** (inherited from the original work — see [LICENSE](LICENSE)).

---

## Features

- **Two output formats** — native YAML for Zabbix 7.0, or 6.0-compatible XML (both import into 7.x).
- **Automatic device-type detection** — via `sysObjectID` enterprise prefix and `sysDescr` heuristics (Cisco, HP, Juniper, MikroTik, Fortinet, printers, Linux, Windows, and more).
- **Low-level discovery (LLD)** — SNMP tables are detected during the walk and turned into discovery rules with item prototypes.
- **Built-in Printer-MIB (RFC 3805) support** — toner/ink supplies, paper trays, output bins, alerts, page counters, cover status — injected automatically for printer-class devices.
- **Triggers** — sensible defaults for link state, interface errors, storage/CPU utilisation, printer supplies, paper, covers, and alerts.
- **Graphs** — auto-generated for numeric item prototypes.
- **Value maps** — `ifOperStatus`, printer status, cover status, alert severity.
- **Macros** — pre-seeded thresholds (`{$TONER.WARN}`, `{$CPU.UTIL.CRIT}`, etc.).
- **SNMP v1 / v2c / v3** — full authPriv support.
- **Safe by default** — every item, rule, and trigger is imported **disabled**, so nothing starts polling until you review and enable it.

---

## Requirements

Run this **on your Zabbix server** (or any host with SNMP tools and MIBs), pointing at the target device.

Ubuntu / Debian:
```
apt install snmp snmp-mibs-downloader python3-yaml
```

RHEL / Rocky / Alma:
```
dnf install net-snmp-utils
pip3 install pyyaml --break-system-packages
```

---

## Usage

Full SNMPv2c walk (switch, router, printer, anything):
```
python3 snmp2zabbix.py -c public -t 192.168.1.100
```

Restrict the walk to a subtree (faster):
```
python3 snmp2zabbix.py -c public -t 192.168.1.100 -o 1.3.6.1.2.1
```

Printer-MIB only:
```
python3 snmp2zabbix.py -c public -t 192.168.1.100 -o 1.3.6.1.2.1.43
```

Force device type and emit YAML:
```
python3 snmp2zabbix.py -c public -t 192.168.1.100 --type printer --format yaml
```

SNMPv3 (authPriv):
```
python3 snmp2zabbix.py -t 192.168.1.100 --v3 \
    --v3user monitor --v3auth SHA --v3authkey MyAuthPass \
    --v3priv AES --v3privkey MyPrivPass
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `-t, --target` | Device IP or hostname (required) | — |
| `-p, --port` | SNMP UDP port | `161` |
| `-o, --oid` | Base OID to walk | `.` (full tree) |
| `-c, --community` | SNMP v1/v2c community | `public` |
| `--version` | SNMP version (`1`, `2c`) | `2c` |
| `--v3` | Use SNMPv3 | off |
| `--v3user` / `--v3auth` / `--v3authkey` / `--v3priv` / `--v3privkey` | SNMPv3 credentials | — |
| `--format` | `yaml` (7.0 native) or `xml` (6.0 compat) | `yaml` |
| `--type` | Force device type (skip auto-detect) | auto |
| `--out` | Output filename | `template-<sysName>.<format>` |

---

## Importing into Zabbix 7.0 LTS

1. **Data collection → Templates → Import**
2. Select the generated YAML or XML file.
3. Link the template to a host that has an SNMP interface configured.
4. Set `{$SNMP_COMMUNITY}` on the host (or use SNMPv3).
5. **Enable items / rules one at a time** — not all at once. Review first.
6. For printers: enable supplies and trays discovery first.
7. Tune thresholds (`{$TONER.WARN}`, `{$PAPER.WARN}`, etc.) to match what your device reports.

> All items, discovery rules, and triggers import **disabled** by design. This is intentional — it prevents a flood of polling and false alerts before you've reviewed the template.

---

## How it works

1. **System info** — queries `sysDescr`, `sysObjectID`, `sysName`, etc.
2. **Device type** — detected from enterprise OID prefix and description (or forced with `--type`).
3. **Walk & parse** — `snmpwalk` output is parsed; scalar OIDs become items, table OIDs become LLD rules with prototypes.
4. **Printer injection** — for printer-class devices, RFC 3805 OIDs are added automatically.
5. **Generate** — a complete template is rendered with items, discovery rules, triggers, graphs, value maps, macros, and tags.

---

## Notes & caveats

- Item keys for discovered table columns are derived from MIB column names (e.g. `ifOperStatus[{#SNMPINDEX}]`). These are unique within a table; collisions across different tables sharing a column name are rare but possible.
- Trigger thresholds are starting points — review them against your environment.
- MIB resolution depends on MIBs being installed locally. Without them, names fall back to numeric OIDs.

---

## License

**GNU Affero General Public License v3.0** — see [LICENSE](LICENSE).

This project is a derivative of an AGPL-3.0 work and therefore remains AGPL-3.0. If you run a modified version as a network service, the AGPL requires you to make your source available to its users.