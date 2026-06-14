# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.0.0] - 2026-06-15
### Added
- Native Zabbix 7.0 LTS YAML export (version 7.0, template_groups at root)
- Printer-MIB (RFC 3805) built-in OID bank: supplies, trays, output bins, alerts, page counters, cover status
- Automatic device-type detection via sysObjectID enterprise prefix + sysDescr heuristics
- Trigger generation (link state, interface errors, storage/CPU, printer supplies/paper/cover/alerts)
- Graph prototype generation for numeric items
- Value maps (ifOperStatus, hrPrinterStatus, prtCoverStatus, prtAlertSeverityLevel)
- Pre-seeded macros for thresholds
- SNMPv3 authPriv support
- DISCARD_UNCHANGED_HEARTBEAT preprocessing on all items
- All items/rules/triggers imported DISABLED by default

### Changed
- Complete rewrite from original SNMPWALK2ZABBIX concept
- Default history 31d (Zabbix 7.0 default)
- Numeric OIDs enforced in snmp_oid (proxy-safe, no MIB text names)
- Discovery rule item keys derived from MIB column names

### Fixed
- Trigger expressions now reference real generated item keys (walked LLD rules)
- Storage utilisation trigger cross-reference to hrStorageSize resolves correctly

## [0.1.0] - <date>
### Added
- Initial fork/adaptation of Sean Bradley's SNMPWALK2ZABBIX
- Basic SNMP walk to Zabbix XML template conversion