# Cloud recorder runbook

You are keeping a data recorder running in this cloud session. That is the whole job.

## Rules
- The recorder makes public GET requests to Kalshi's market-data API. It uses no account and no credentials, and it cannot trade. Never place, change or cancel an order, never read `.env` or any key, and never call an authenticated Kalshi endpoint.
- Touch nothing outside `lip-rec/`. Do not analyse the data, edit `lip_cloud.py`, open a pull request, or push to any branch other than `claude/lip-recordings` (the recorder pushes there by itself).
- Never use em dashes in anything you write. Keep every reply to a few lines.

## What it does
`lip-rec/lip_cloud.py` saves the order book and public trades of the 150 markets in `lip-rec/tickers.json` every 30 seconds, until 2026-11-16 21:19 UTC. It runs in pieces of 1 hour 50 minutes, because a background command here has a time limit. Each piece exits by itself and prints one line, and you start the next piece. Every 15 minutes it commits the finished files and pushes them to the branch `claude/lip-recordings`.

## First time in this session
1. Run `python3 lip-rec/lip_cloud.py lip-rec --check`. It must print `KALSHI OK`. If it does not, run `curl -sS -m 20 -o /dev/null -D - https://api.elections.kalshi.com/trade-api/v2/exchange/status | head -12`, report both outputs, say that this environment's network access has to allow `api.elections.kalshi.com`, and stop.
2. Run `echo "session started $(date -u +%FT%TZ)" > lip-rec/log-session-$(date +%s).txt`.
3. Start a piece (step 1 of the loop below).
4. Run `python3 lip-rec/lip_cloud.py lip-rec --push ; python3 lip-rec/lip_cloud.py lip-rec --status`.
5. Report, one line each: the check result, the time the piece started, and whether the last line of the status says `push ok`. If it says `push FAILED`, quote the error. Keep the recorder running either way.

## The loop
1. Start a piece: run `sh lip-rec/run.sh` with the Bash tool, `run_in_background` true and `timeout` 7200000. If the tool refuses that timeout, use the largest it accepts and start the piece with `LIP_CHUNK=<that timeout in seconds, minus 300> sh lip-rec/run.sh` instead.
2. End your turn with one line. Do not wait, sleep or poll.
3. When you are told the command ended, look at the last line it printed:
   - `RESTART NOW`: do step 1 at once, before anything else, then end your turn with one line.
   - `RUN COMPLETE`: run `python3 lip-rec/lip_cloud.py lip-rec --status`, report it, and stop for good.
   - `STOPPED`: report it and stop.
   - `ALREADY RUNNING`: a piece is already going. Do nothing and end your turn.
   - anything else (it crashed, was killed or timed out): do step 1 at once, then run `tail -n 5 lip-rec/log-*.txt | tail -n 8` and report one line. If three pieces in a row die within ten minutes of starting, stop and report the error.
4. If anyone writes "status": run `python3 lip-rec/lip_cloud.py lip-rec --status` and paste the output. If it says NOT RUNNING and the run has not ended, do step 1.

If the machine was reset (`lip-rec/` is missing, or it is there but nothing is running), check out the branch `claude/lip-recordings` again and start from "First time in this session".

To stop the recorder early, someone will ask you to run `touch lip-rec/STOP`.
