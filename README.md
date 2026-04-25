# imap_perf

Measures IMAP server latency across a suite of commands that exercise disk I/O. Each command is run multiple times and reported with avg / min / max / σ. Results are appended to a per-host CSV file for trending over time.

Built with [Claude Code](https://claude.ai/code).

## Requirements

Python 3.8+, no external dependencies.

## Usage

```
python3 imap_perf.py <host> -u <user> [options]
```

Password is prompted if not supplied via `-p`.

### Examples

```bash
# Basic — IMAPS (port 993), 3 reps per command
python3 imap_perf.py mail.example.com -u alice@example.com

# STARTTLS on port 143
python3 imap_perf.py mail.example.com -u alice@example.com --tls starttls

# More repetitions, larger fetch sample
python3 imap_perf.py mail.example.com -u alice@example.com --repeat 10 --fetch-count 50

# Include full-body fetch and write path (APPEND + EXPUNGE)
python3 imap_perf.py mail.example.com -u alice@example.com --full-body --write-test

# Non-default mailbox
python3 imap_perf.py mail.example.com -u alice@example.com --mailbox Sent
```

## Options

| Flag | Default | Description |
|---|---|---|
| `host` | — | IMAP server hostname or IP |
| `-u / --user` | — | Login username |
| `-p / --password` | prompted | Password |
| `--port` | 993 (ssl) / 143 | Override port |
| `--tls` | `ssl` | `ssl`, `starttls`, or `plain` |
| `--mailbox` | `INBOX` | Mailbox to test against |
| `--repeat` | `3` | Repetitions per command (for avg/min/max) |
| `--fetch-count` | `10` | Number of messages used in FETCH tests |
| `--full-body` | off | Include `FETCH RFC822` (full message body) |
| `--write-test` | off | Run `APPEND` + `EXPUNGE` write-path test |
| `--timeout` | `30.0` | Socket timeout in seconds |

## What gets tested

| Section | Commands | I/O profile |
|---|---|---|
| Connection | TCP + TLS handshake | network |
| Server | CAPABILITY, NOOP | minimal |
| Mailbox listing | LIST | directory read |
| Mailbox open | SELECT, STATUS | index read |
| Search | ALL, UNSEEN, SEEN, FLAGGED, date ranges, TEXT, BODY, SUBJECT | index + optional full-text scan |
| Fetch | FLAGS, ENVELOPE, RFC822.HEADER, BODYSTRUCTURE, RFC822\* | sequential message reads |
| Write path\* | APPEND, EXPUNGE | write + journal flush |

\* Disabled by default — use `--full-body` / `--write-test` to enable.

## Output

Live results print to stdout with timing stats per command:

```
IMAP Perf Test — mail.example.com:993 (SSL) — 2026-04-24 18:45:00

──────────────────────────────────────────────────────────────────────
  Connection
──────────────────────────────────────────────────────────────────────
  [OK]  TCP + TLS handshake                          44.8 ms
  [OK]  LOGIN (auth + session init)                 112.3 ms

──────────────────────────────────────────────────────────────────────
  SEARCH  (index / full scan I/O)
──────────────────────────────────────────────────────────────────────
  [OK]  ALL                             avg    18.4 ms  min   17.1  max   20.2  σ   1.6  n=3
  [OK]  UNSEEN                          avg     4.1 ms  min    3.9  max    4.4  σ   0.3  n=3
  ...
```

At the end of each run, one row is appended to `<host>.csv` in the current directory. The file is created with a header on first run; subsequent runs append data only.

```
timestamp,host,port,tls,TCP + TLS handshake,LOGIN (auth + session init),CAPABILITY,...
2026-04-24 18:45:00,mail.example.com,993,ssl,44.821,112.340,3.201,...
2026-04-24 19:00:00,mail.example.com,993,ssl,43.100,109.820,3.180,...
```

## Trending / Grafana

The CSV accumulates one row per run, making it easy to track latency over time. Two options for Grafana:

**Grafana CSV data source** — install the [CSV data source plugin](https://grafana.com/grafana/plugins/marcusolsson-csv-datasource/) and point it at the file. Works well for periodic manual runs or cron jobs on the same host as Grafana.

**InfluxDB** — for continuous monitoring, write results directly to InfluxDB and use Grafana's native InfluxDB data source. InfluxDB can be started with:

```bash
docker run -d -p 8086:8086 influxdb:2
```

## Running on a schedule

```bash
# cron — run every 5 minutes
*/5 * * * * cd /opt/imap_perf && python3 imap_perf.py mail.example.com -u probe@example.com -p "$IMAP_PASS" --repeat 5
```
