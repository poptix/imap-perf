#!/usr/bin/env python3
"""
IMAP Disk I/O Performance Tester
Connects to an IMAP server, runs a suite of commands that exercise disk I/O,
and reports latency statistics to measure user-visible responsiveness.
"""

import argparse
import imaplib
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from getpass import getpass
from statistics import mean, median, stdev


# ── timing helpers ────────────────────────────────────────────────────────────

class Timer:
    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_):
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000


def run_timed(fn, *args, repeat=1):
    """Run fn(*args) `repeat` times, return (results_list, samples_ms)."""
    samples = []
    results = []
    for _ in range(repeat):
        with Timer() as t:
            r = fn(*args)
        samples.append(t.elapsed_ms)
        results.append(r)
    return results, samples


# ── display helpers ───────────────────────────────────────────────────────────

COL_W = 42
_results: list = []   # [(label, avg_ms_or_None, n, status_str)]


def header(title):
    print(f"\n{'─' * 70}")
    print(f"  {title}")
    print(f"{'─' * 70}")

def row(label, samples, extra=""):
    avg = mean(samples)
    lo  = min(samples)
    hi  = max(samples)
    sd  = stdev(samples) if len(samples) > 1 else 0.0
    n   = len(samples)
    label_str = f"  {label:<{COL_W}}"
    if n == 1:
        stat = f"{avg:>8.1f} ms"
    else:
        stat = f"avg {avg:>7.1f} ms  min {lo:>7.1f}  max {hi:>7.1f}  σ {sd:>6.1f}  n={n}"
    print(f"{label_str} {stat}  {extra}")

def ok(label, samples, extra=""):
    row(f"[OK]  {label}", samples, extra)
    _results.append((label, mean(samples), len(samples), "ok"))

def fail(label, err):
    print(f"  {'[FAIL]':<6} {label:<{COL_W}} {err}")
    _results.append((label, None, 0, f"FAIL: {err}"))

def info(msg):
    print(f"  {msg}")


def print_csv_summary(host, port, tls):
    import csv, os
    ts        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta_cols = ["timestamp", "host", "port", "tls"]
    cmd_cols  = [label for label, *_ in _results]
    row_data  = [ts, host, port, tls] + [
        f"{avg:.3f}" if avg is not None else "FAIL"
        for _, avg, _, _ in _results
    ]

    filename   = f"{host}.csv"
    write_hdr  = not os.path.exists(filename)

    with open(filename, "a", newline="") as f:
        w = csv.writer(f)
        if write_hdr:
            w.writerow(meta_cols + cmd_cols)
        w.writerow(row_data)

    print(f"\n{'─' * 70}")
    print(f"  CSV → {filename}  ({'header + ' if write_hdr else ''}1 row appended)")
    print(f"{'─' * 70}")


# ── connection ────────────────────────────────────────────────────────────────

def connect(host, port, tls_mode, timeout):
    ctx = ssl.create_default_context()

    if tls_mode == "ssl":
        imap = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
    elif tls_mode == "starttls":
        imap = imaplib.IMAP4(host, port)
        imap.starttls(ssl_context=ctx)
    else:  # plain
        imap = imaplib.IMAP4(host, port)

    imap.sock.settimeout(timeout)
    return imap


# ── individual test functions ─────────────────────────────────────────────────

def test_connect(host, port, tls_mode, timeout):
    with Timer() as t:
        imap = connect(host, port, tls_mode, timeout)
    return imap, t.elapsed_ms


def test_login(imap, user, password):
    _, samples = run_timed(lambda: imap.login(user, password))
    return samples


def test_capability(imap, repeat):
    _, samples = run_timed(imap.capability, repeat=repeat)
    return samples


def test_list(imap, repeat):
    _, samples = run_timed(imap.list, repeat=repeat)
    return samples


def test_select(imap, mailbox, repeat):
    results, samples = run_timed(imap.select, mailbox, repeat=repeat)
    # last result carries message count
    last = results[-1]
    count = int(last[1][0]) if last[0] == "OK" and last[1] and last[1][0] else 0
    return samples, count


def test_status(imap, mailbox, repeat):
    _, samples = run_timed(
        imap.status, mailbox,
        "(MESSAGES RECENT UIDNEXT UIDVALIDITY UNSEEN)",
        repeat=repeat,
    )
    return samples


def test_search(imap, criterion, label, repeat):
    _, samples = run_timed(imap.search, None, criterion, repeat=repeat)
    return samples


def test_fetch_headers(imap, uid_list, repeat):
    """FETCH first N UIDs — RFC822.HEADER only (metadata, moderate I/O)."""
    if not uid_list:
        return None
    fetch_set = ",".join(uid_list)
    _, samples = run_timed(
        imap.fetch, fetch_set, "(RFC822.HEADER)",
        repeat=repeat,
    )
    return samples


def test_fetch_full(imap, uid_list, repeat):
    """FETCH first N UIDs — RFC822 (full body, heavy I/O)."""
    if not uid_list:
        return None
    fetch_set = ",".join(uid_list)
    _, samples = run_timed(
        imap.fetch, fetch_set, "(RFC822)",
        repeat=repeat,
    )
    return samples


def test_fetch_envelope(imap, uid_list, repeat):
    """FETCH ENVELOPE — server-parsed structured headers."""
    if not uid_list:
        return None
    fetch_set = ",".join(uid_list)
    _, samples = run_timed(
        imap.fetch, fetch_set, "(ENVELOPE)",
        repeat=repeat,
    )
    return samples


def test_fetch_bodystructure(imap, uid_list, repeat):
    """FETCH BODYSTRUCTURE — MIME tree, forces full parse."""
    if not uid_list:
        return None
    fetch_set = ",".join(uid_list)
    _, samples = run_timed(
        imap.fetch, fetch_set, "(BODYSTRUCTURE)",
        repeat=repeat,
    )
    return samples


def test_append_expunge(imap, mailbox):
    """APPEND a small test message then EXPUNGE it — write I/O path."""
    now = imaplib.Time2Internaldate(time.time())
    msg = (
        f"Date: {datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')}\r\n"
        f"From: imap-perf-test@localhost\r\n"
        f"To: imap-perf-test@localhost\r\n"
        f"Subject: [imap-perf] probe message\r\n"
        f"Message-ID: <imap-perf-{int(time.time())}@localhost>\r\n"
        f"\r\nThis is an automated probe message from imap_perf.py.\r\n"
    ).encode()

    with Timer() as t_append:
        typ, data = imap.append(mailbox, r"(\Seen \Deleted)", now, msg)
    if typ != "OK":
        return None, None, f"APPEND failed: {data}"

    # Message was appended with \Deleted already set; just expunge.
    # Use "*" (last message) as a fallback store in case the server
    # ignored the flag on APPEND.
    imap.store("*", "+FLAGS", r"(\Deleted)")

    with Timer() as t_expunge:
        imap.expunge()

    return t_append.elapsed_ms, t_expunge.elapsed_ms, None


def test_noop(imap, repeat):
    _, samples = run_timed(imap.noop, repeat=repeat)
    return samples


# ── main test suite ───────────────────────────────────────────────────────────

def run_suite(args):
    global _results
    _results = []
    repeat   = args.repeat
    n_fetch  = args.fetch_count

    # ── Connect ───────────────────────────────────────────────────────────────
    header("Connection")
    try:
        imap, conn_ms = test_connect(args.host, args.port, args.tls, args.timeout)
        ok("TCP + TLS handshake", [conn_ms])
    except Exception as e:
        fail("Connect", e)
        sys.exit(1)

    # ── Login ─────────────────────────────────────────────────────────────────
    try:
        login_samples = test_login(imap, args.user, args.password)
        ok("LOGIN (auth + session init)", login_samples)
    except Exception as e:
        fail("LOGIN", e)
        imap.logout()
        sys.exit(1)

    # ── CAPABILITY ────────────────────────────────────────────────────────────
    header("Server commands (low disk I/O)")
    try:
        ok("CAPABILITY", test_capability(imap, repeat))
    except Exception as e:
        fail("CAPABILITY", e)

    try:
        ok("NOOP", test_noop(imap, repeat))
    except Exception as e:
        fail("NOOP", e)

    # ── Mailbox listing ───────────────────────────────────────────────────────
    try:
        ok("LIST (all mailboxes)", test_list(imap, repeat))
    except Exception as e:
        fail("LIST", e)

    # ── SELECT / STATUS ───────────────────────────────────────────────────────
    header(f"Mailbox open  [{args.mailbox}]")
    try:
        sel_samples, msg_count = test_select(imap, args.mailbox, repeat)
        ok(f"SELECT {args.mailbox}", sel_samples, f"({msg_count} messages)")
    except Exception as e:
        fail(f"SELECT {args.mailbox}", e)
        msg_count = 0

    try:
        ok(f"STATUS {args.mailbox}", test_status(imap, args.mailbox, repeat))
    except Exception as e:
        fail(f"STATUS {args.mailbox}", e)

    # ── SEARCH ────────────────────────────────────────────────────────────────
    header("SEARCH  (index / full scan I/O)")
    uid_sample = []

    searches = [
        ("ALL",                      "SEARCH ALL"),
        ("UNSEEN",                   "SEARCH UNSEEN"),
        ("SEEN",                     "SEARCH SEEN"),
        ("FLAGGED",                  "SEARCH FLAGGED"),
        ("SINCE 1-Jan-2020",         "SEARCH SINCE 1-Jan-2020"),
        ("BEFORE 1-Jan-2030",        "SEARCH BEFORE 1-Jan-2030"),
        ("TEXT imap-perf",           "SEARCH TEXT imap-perf"),
        ("BODY imap-perf",           "SEARCH BODY imap-perf"),
        ("SUBJECT test",             "SEARCH SUBJECT test"),
    ]
    for label, criterion in searches:
        try:
            parts = criterion.split(" ", 1)
            samples = test_search(imap, *parts[1:], label=label, repeat=repeat)
            ok(label, samples)
        except Exception as e:
            fail(label, e)

    # Gather some sequence numbers for FETCH tests
    try:
        typ, data = imap.search(None, "ALL")
        if typ == "OK" and data[0]:
            all_ids = data[0].decode().split()
            uid_sample = all_ids[-n_fetch:] if len(all_ids) >= n_fetch else all_ids
    except Exception:
        pass

    # ── FETCH ─────────────────────────────────────────────────────────────────
    n_actual = len(uid_sample)
    header(f"FETCH  (disk read I/O — {n_actual} messages)")
    if not uid_sample:
        info("No messages to fetch — skipping FETCH tests.")
    else:
        tests = [
            ("FETCH FLAGS (flags only)",          test_fetch_headers,      False),
            ("FETCH ENVELOPE (parsed headers)",   test_fetch_envelope,     False),
            ("FETCH RFC822.HEADER (raw headers)", test_fetch_headers,      False),
            ("FETCH BODYSTRUCTURE (MIME tree)",   test_fetch_bodystructure,False),
            ("FETCH RFC822 (full body)",          test_fetch_full,         True),
        ]
        for label, fn, is_heavy in tests:
            if is_heavy and not args.full_body:
                info(f"  (skipped) {label}  — use --full-body to enable")
                continue
            try:
                samples = fn(imap, uid_sample, repeat)
                if samples is not None:
                    ok(label, samples, f"({n_actual} msgs)")
            except Exception as e:
                fail(label, e)

    # ── APPEND / EXPUNGE (write path) ─────────────────────────────────────────
    if args.write_test:
        header("Write path  (APPEND + EXPUNGE)")
        try:
            t_append, t_expunge, err = test_append_expunge(imap, args.mailbox)
            if err:
                fail("APPEND", err)
            else:
                ok("APPEND (write new message)", [t_append])
                ok("EXPUNGE (delete + compact)", [t_expunge])
        except Exception as e:
            fail("APPEND/EXPUNGE", e)
    else:
        info("\n  Write path skipped — use --write-test to enable APPEND/EXPUNGE.")

    # ── Done ──────────────────────────────────────────────────────────────────
    header("Teardown")
    try:
        with Timer() as t:
            imap.close()
        ok("CLOSE", [t.elapsed_ms])
    except Exception:
        pass
    try:
        with Timer() as t:
            imap.logout()
        ok("LOGOUT", [t.elapsed_ms])
    except Exception:
        pass

    print_csv_summary(args.host, args.port, args.tls)
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="IMAP disk I/O performance tester",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("host",              help="IMAP server hostname or IP")
    p.add_argument("-u", "--user",      required=True, help="Login username")
    p.add_argument("-p", "--password",  default=None,
                   help="Password (omit to be prompted)")
    p.add_argument("--port",            type=int, default=None,
                   help="Port (default: 993 for ssl, 143 otherwise)")
    p.add_argument("--tls",             choices=["ssl", "starttls", "plain"],
                   default="ssl", help="TLS mode")
    p.add_argument("--mailbox",         default="INBOX",
                   help="Mailbox to test against")
    p.add_argument("--repeat",          type=int, default=3,
                   help="Repeat each command N times for averaging")
    p.add_argument("--fetch-count",     type=int, default=10, dest="fetch_count",
                   help="Number of messages to use in FETCH tests")
    p.add_argument("--full-body",       action="store_true",
                   help="Include full RFC822 body fetch (can be large)")
    p.add_argument("--write-test",      action="store_true",
                   help="Run APPEND + EXPUNGE write-path test")
    p.add_argument("--timeout",         type=float, default=30.0,
                   help="Socket timeout in seconds")

    args = p.parse_args()

    if args.port is None:
        args.port = 993 if args.tls == "ssl" else 143

    if args.password is None:
        args.password = getpass(f"Password for {args.user}@{args.host}: ")

    return args


if __name__ == "__main__":
    args = parse_args()
    print(f"\nIMAP Perf Test — {args.host}:{args.port} ({args.tls.upper()}) — {datetime.now():%Y-%m-%d %H:%M:%S}")
    run_suite(args)
