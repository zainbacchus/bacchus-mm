#!/usr/bin/env python3
"""Cloud copy of the Kalshi liquidity-incentive recorder. NO orders, NO auth.

Writes the same records as research/lip/lip_record.py, so lip_score.py replays
them unchanged, but needs only the standard library and runs in pieces,
because a cloud background command has a time limit. After LIP_CHUNK seconds a
piece closes its file, saves its trade cursors and exits 0, and the caller
starts the next piece. Files rotate every LIP_ROTATE seconds and each opens
with a full snapshot of every book, so any file can be read on its own.

Every 30 s, for the tickers in OUTDIR/tickers.json: order books, market
status, and public trades for markets whose volume moved. One JSON object per
poll: t, b (changed books), tr (new trades), st (status changes).

Finished files are committed and pushed to BRANCH once pushing is on: either
OUTDIR/PUSH_ON exists or BRANCH already exists on origin.

usage: python3 lip_cloud.py OUTDIR            record one piece
       python3 lip_cloud.py OUTDIR --check    can this machine reach Kalshi?
       python3 lip_cloud.py OUTDIR --push     commit and push finished files
       python3 lip_cloud.py OUTDIR --status   one-screen health report
       python3 lip_cloud.py OUTDIR --fresh    is another session already recording?
env:   LIP_END (epoch seconds, end of the whole run), LIP_CHUNK (6600),
       LIP_ROTATE (900)
"""
import calendar
import collections
import fcntl
import json
import os
import signal
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

OUT = Path(sys.argv[1]).resolve()
MODE = sys.argv[2] if len(sys.argv) > 2 else "record"
BASE = "https://api.elections.kalshi.com/trade-api/v2"
BRANCH = "claude/lip-recordings"
KIT = ("lip_cloud.py", "run.sh", "RUNBOOK.md", "tickers.json")
POLL = 30.0
END = float(os.environ.get("LIP_END", "0"))
CHUNK = float(os.environ.get("LIP_CHUNK", "6600"))
ROTATE = float(os.environ.get("LIP_ROTATE", "900"))
T0 = time.time()
CTX = None                                      # None = the machine's own certificate store
MYLOG = f"log-{int(T0)}.txt"                    # one log per piece, so pushed logs never change


def iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def log(msg, name=MYLOG):
    with (OUT / name).open("a") as fh:
        fh.write(iso() + " " + msg + "\n")


def get(path, params=None, tries=6):
    global CTX
    url = BASE + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    err = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "lip-research/1.0"})
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            err = e
            if e.code == 429:
                time.sleep(2.0 * (a + 1))
                continue
            if e.code in (400, 404):
                raise
            time.sleep(1.0 * (a + 1))
        except Exception as e:  # noqa: BLE001
            err = e
            if CTX is None and isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                try:                            # a Python built without a certificate store: borrow certifi's if present
                    import certifi
                    CTX = ssl.create_default_context(cafile=certifi.where())
                    continue
                except ImportError:
                    pass
            time.sleep(1.0 * (a + 1))
    raise err


def ts(s):
    frac = s[19:].split("+")[0].rstrip("Z")
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) + (float("0" + frac) if frac[:1] == "." else 0.0)


def books_of(chunk, tries=4):
    d = get("/markets/orderbooks", [("tickers", t) for t in chunk], tries=tries)
    return {ob["ticker"]: ob.get("orderbook_fp") or {} for ob in d.get("orderbooks") or []}


def meta_of(chunk, tries=4):
    d = get("/markets", [("tickers", ",".join(chunk)), ("limit", "1000")], tries=tries)
    return {m["ticker"]: m for m in d.get("markets") or []}


BAD = collections.Counter()


def resilient(fn, tks):
    """Batch fetch in chunks of 100. If a chunk fails, try its tickers one by
    one so a single delisted ticker cannot blank the whole poll. A ticker only
    counts as bad when others in the same pass succeeded, so an exchange
    outage never thins the universe."""
    out = {}
    for i in range(0, len(tks), 100):
        chunk = tks[i:i + 100]
        try:
            out.update(fn(chunk))
            continue
        except Exception as e:  # noqa: BLE001
            log(f"{fn.__name__} chunk failed: {e!r}; trying tickers one by one")
        ok, failed = 0, []
        for tk in chunk:
            if ok == 0 and len(failed) >= 3:
                break                           # three straight failures and no success: an outage, not a bad ticker
            try:
                out.update(fn([tk], tries=1))
                ok += 1
            except Exception:  # noqa: BLE001
                failed.append(tk)
        if ok:
            for tk in failed:
                BAD[tk] += 1
    return out


class Writer:
    """gzip JSONL, sync-flushed every record so a kill loses at most the
    record being written. A file is named *.part until it is complete."""

    def __init__(self):
        self.fh, self.z, self.path, self.t_open = None, None, None, 0.0

    def open(self, t):
        self.close()
        self.path = OUT / f"rec-{time.strftime('%Y%m%d', time.gmtime(t))}-{int(t)}.jsonl.gz.part"
        self.fh = self.path.open("wb")
        self.z = zlib.compressobj(6, zlib.DEFLATED, 31)
        self.t_open = t

    def write(self, obj):
        self.fh.write(self.z.compress((json.dumps(obj, separators=(",", ":")) + "\n").encode()))
        self.fh.write(self.z.flush(zlib.Z_SYNC_FLUSH))
        self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.write(self.z.flush(zlib.Z_FINISH))
            self.fh.close()
            self.path.rename(self.path.with_suffix(""))
            self.fh = None


def load_state():
    try:
        return json.loads((OUT / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def save_state(t, cursor, seen, vol, dropped):
    tmp = OUT / "state.json.tmp"
    tmp.write_text(json.dumps({"t": t, "cursor": cursor, "vol": vol, "dropped": sorted(dropped),
                               "seen": {k: list(v)[-100:] for k, v in seen.items() if v}}))
    tmp.replace(OUT / "state.json")


def push(final=False):
    cmd = [sys.executable, str(Path(__file__).resolve()), str(OUT), "--push-all" if final else "--push"]
    try:
        if final:
            subprocess.run(cmd, timeout=600)
        else:                                   # detached, so the next piece can start without waiting for git
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:  # noqa: BLE001
        log(f"push launch error: {e!r}")


def record():
    if END <= 0:
        sys.exit("LIP_END is not set")
    all_tks = json.loads((OUT / "tickers.json").read_text())
    old = "".join(tail(OUT / "recorder.pid", 1)).strip()
    try:                                        # two pieces at once would write overlapping files
        if old and "lip_cloud.py" in subprocess.run(["ps", "-p", old, "-o", "command="],
                                                    capture_output=True, text=True).stdout:
            print(f"ALREADY RUNNING | pid {old} | nothing started")
            return
    except OSError:
        pass
    for p in OUT.glob("rec-*.jsonl.gz.part"):    # a killed piece leaves its file unfinished: keep what it wrote
        p.rename(p.with_suffix(""))
    (OUT / "recorder.pid").write_text(str(os.getpid()))
    (OUT / "current_log.txt").write_text(MYLOG)
    s = load_state()
    fresh = bool(s) and T0 - s.get("t", 0) < 900 # the last piece ended moments ago: carry its trade cursors over
    dropped = set(s.get("dropped", []))
    tks = [tk for tk in all_tks if tk not in dropped]
    cursor = {tk: (s["cursor"].get(tk, T0) if fresh else T0) for tk in tks}
    vol = dict(s.get("vol", {})) if fresh else {}
    seen = collections.defaultdict(dict)
    if fresh:
        for tk, ids in s.get("seen", {}).items():
            seen[tk] = dict.fromkeys(ids)
    t_stop = min(END, T0 + CHUNK)
    flag = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda n, f: flag.setdefault("sig", n))
    log(f"piece start: {len(tks)} markets, until {iso(t_stop)}, run ends {iso(END)}, "
        f"trade cursors {'carried over' if fresh else 'new'}")
    push()                                      # catch up on files an earlier piece left behind
    w = Writer()
    last_book, last_status = {}, {}
    n_poll = n_tr = 0
    t_last = None
    while time.time() < t_stop and not flag and not (OUT / "STOP").exists():
        t_loop = time.time()
        if w.fh is None or t_loop - w.t_open >= ROTATE:
            rotated = w.fh is not None
            w.open(t_loop)
            last_book, last_status = {}, {}     # every file opens with a full snapshot
            if rotated:
                save_state(t_last or t_loop, cursor, seen, vol, dropped)
                push()
        rec = {"t": round(t_loop, 3)}
        try:
            books = resilient(books_of, tks)
            meta = resilient(meta_of, tks)
        except Exception as e:  # noqa: BLE001
            log(f"poll error: {e!r}")
            time.sleep(10)
            continue
        if not books and not meta:
            log("poll error: nothing returned")
            time.sleep(10)
            continue
        gone = [tk for tk in tks if BAD[tk] >= 5]
        if gone:
            dropped.update(gone)
            tks = [tk for tk in tks if tk not in dropped]
            log(f"dropped unreachable tickers: {gone}")
        ch = {}
        for tk in tks:
            ob = books.get(tk)
            if ob is None:
                continue
            key = json.dumps([ob.get("yes_dollars") or [], ob.get("no_dollars") or []], separators=(",", ":"))
            if last_book.get(tk) != key:
                last_book[tk] = key
                ch[tk] = [ob.get("yes_dollars") or [], ob.get("no_dollars") or []]
        if ch:
            rec["b"] = ch
        trs, st = [], {}
        for idx, tk in enumerate(tks):
            m = meta.get(tk)
            if not m:
                continue
            sr = [m.get("status"), m.get("result")]
            if last_status.get(tk) != sr:
                last_status[tk] = sr
                st[tk] = sr
            v = m.get("volume_fp")
            if vol.get(tk) is not None and v == vol[tk] and (n_poll + idx) % 20:
                continue                        # no new volume: skip the trade call (each market re-checked every 10 min)
            try:
                d = get("/markets/trades", [("ticker", tk), ("min_ts", str(int(cursor[tk]) - 2)), ("limit", "1000")],
                        tries=3)
            except Exception as e:  # noqa: BLE001
                log(f"trade error {tk}: {e!r}")
                continue
            vol[tk] = v                         # only after a successful fetch, so a failed one is retried next poll
            for t in sorted(d.get("trades") or [], key=lambda t: t.get("created_time") or ""):
                tid = t.get("trade_id")
                if tid in seen[tk] or not t.get("created_time"):
                    continue
                seen[tk][tid] = None
                tt = ts(t["created_time"])
                cursor[tk] = max(cursor[tk], tt)
                trs.append([tk, round(tt, 3), t.get("yes_price_dollars"), t.get("count_fp") or t.get("count"),
                            t.get("taker_side"), 1 if t.get("is_block_trade") else 0])
            if len(seen[tk]) > 2000:
                seen[tk] = dict.fromkeys(list(seen[tk])[-500:])
        if trs:
            rec["tr"] = trs
        if st:
            rec["st"] = st
        try:
            w.write(rec)
        except Exception:  # noqa: BLE001
            log("write error: " + traceback.format_exc()[-300:])
        n_poll += 1
        n_tr += len(trs)
        t_last = t_loop
        if n_poll % 60 == 1:
            log(f"poll {n_poll}: markets {len(tks)}, books changed {len(ch)}, trades {len(trs)}, "
                f"loop {time.time() - t_loop:.1f}s")
        t_next = time.time() + max(1.0, POLL - (time.time() - t_loop))
        if t_next >= t_stop:
            break                               # hand over straight after a poll, so the next piece leaves no hole
        while time.time() < t_next and not flag:
            time.sleep(min(1.0, max(0.05, t_next - time.time())))
    w.close()
    save_state(t_last or time.time(), cursor, seen, vol, dropped)
    done = time.time() >= END - 1
    stopped = (OUT / "STOP").exists()
    why = "run complete" if done else ("STOP file" if stopped else ("signal" if flag else "piece complete"))
    log(f"piece end ({why}): {n_poll} polls, {n_tr} trades")
    (OUT / "current_log.txt").unlink()
    push(final=done or stopped)
    word = "RUN COMPLETE" if done else ("STOPPED" if stopped else "RESTART NOW")
    line = f"{word} | {why} | polls {n_poll} | last poll {iso(t_last) if t_last else '-'}"
    (OUT / "last_exit.txt").write_text(line + "\n")
    print(line)


def git(top, *args, timeout=240):
    return subprocess.run(["git", "-C", top, "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, timeout=timeout)


def toplevel():
    return subprocess.run(["git", "-C", str(OUT), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True).stdout.strip()


def push_on(top):
    if (OUT / "PUSH_ON").exists():
        return True
    r = git(top, "ls-remote", "--heads", "origin", BRANCH, timeout=60)
    return r.returncode == 0 and bool(r.stdout.strip())


def do_push(everything=False):
    with (OUT / "push.lock").open("w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)          # one push at a time
        top = toplevel()
        if not top:
            return log("push skipped: not inside a git checkout", "push.log")
        if not push_on(top):
            return log(f"pushing is off (no PUSH_ON file and no {BRANCH} on origin)", "push.log")
        if not git(top, "config", "user.email").stdout.strip():
            git(top, "config", "user.email", "lip-recorder@users.noreply.github.com")
            git(top, "config", "user.name", "lip-recorder")
        cur = OUT / "current_log.txt"
        live = cur.read_text().strip() if cur.exists() and not everything else ""
        files = [str(OUT / k) for k in KIT if (OUT / k).exists()]
        files += sorted(str(p) for p in OUT.glob("rec-*.jsonl.gz"))
        files += sorted(str(p) for p in OUT.glob("log-*.txt") if p.name != live)
        git(top, "add", "-f", "--", *files)
        if git(top, "diff", "--cached", "--quiet").returncode:
            r = git(top, "commit", "-q", "-m", "lip recorder: data through " + iso())
            if r.returncode:
                return log("commit FAILED: " + (r.stderr or r.stdout)[-300:].strip(), "push.log")
        r = git(top, "push", "-q", "origin", "HEAD:refs/heads/" + BRANCH)
        if r.returncode:                        # the branch moved, or is new to us: put our commits on top of it
            git(top, "-c", "rebase.autoStash=true", "pull", "-q", "--rebase", "origin", BRANCH)
            r = git(top, "push", "-q", "origin", "HEAD:refs/heads/" + BRANCH)
        log("push ok" if r.returncode == 0 else "push FAILED: " + (r.stderr or r.stdout)[-300:].strip(), "push.log")


def fresh():
    """A session that starts while another one is recording must stand down:
    two recorders would push the same minutes twice."""
    top = toplevel()
    r = git(top, "fetch", "-q", "origin", BRANCH, timeout=120)
    if r.returncode:
        print("NO FRESH DATA | could not fetch the branch: " + (r.stderr or r.stdout).strip()[-200:])
        return
    ct, _, subj = git(top, "log", "-1", "--format=%ct %s", "FETCH_HEAD").stdout.strip().partition(" ")
    age = (time.time() - float(ct)) / 60 if ct else 1e9
    if subj.startswith("lip recorder: data") and age < 20:
        print(f"ANOTHER SESSION IS RECORDING | it pushed {age:.0f} minutes ago | do not start")
    else:
        print(f"NO FRESH DATA | last push {age:.0f} minutes ago | go ahead")


def check():
    try:
        d = get("/exchange/status", tries=2)
        tks = json.loads((OUT / "tickers.json").read_text())
        b = books_of(tks[:3], tries=2)
        print(f"KALSHI OK | exchange_active {d.get('exchange_active')} | books for {len(b)} of 3 tickers | "
              f"{len(tks)} tickers | python {sys.version.split()[0]}")
    except Exception as e:  # noqa: BLE001
        print(f"KALSHI NOT REACHABLE | {e!r}")
        sys.exit(2)


def tail(path, n):
    try:
        return path.read_text().splitlines()[-n:]
    except OSError:
        return []


def status():
    now = time.time()
    print("now", iso(now), "| run ends", iso(END) if END else "(LIP_END not set)")
    pid = "".join(tail(OUT / "recorder.pid", 1)).strip()
    alive = False
    try:
        os.kill(int(pid), 0)
        alive = True
    except (OSError, ValueError):
        pass
    print("recorder pid", pid or "-", "RUNNING" if alive else "NOT RUNNING")
    done, part = sorted(OUT.glob("rec-*.jsonl.gz")), sorted(OUT.glob("rec-*.jsonl.gz.part"))
    print(f"finished files {len(done)} ({sum(p.stat().st_size for p in done) / 1e6:.1f} MB)"
          + (f" | first {done[0].name} | last {done[-1].name}" if done else ""))
    if part:
        print(f"open file {part[-1].name}, last written {now - part[-1].stat().st_mtime:.0f} s ago")
    s = load_state()
    if s:
        print("last saved poll", iso(s.get("t")), "| dropped tickers", len(s.get("dropped", [])))
    top = toplevel()
    if top:
        print("pushing", "ON" if push_on(top) else "OFF", "| branch", BRANCH,
              "| tracked rec files", len(git(top, "ls-files", "--", str(OUT / "rec-*")).stdout.split()))
    logs = sorted(OUT.glob("log-*.txt"))
    for line in (tail(logs[-1], 4) if logs else []) + tail(OUT / "push.log", 3):
        print("  " + line)


if __name__ == "__main__":
    if MODE == "record":
        record()
    elif MODE == "--check":
        check()
    elif MODE in ("--push", "--push-all"):
        do_push(MODE == "--push-all")
    elif MODE == "--status":
        status()
    elif MODE == "--fresh":
        fresh()
    else:
        sys.exit(__doc__)
