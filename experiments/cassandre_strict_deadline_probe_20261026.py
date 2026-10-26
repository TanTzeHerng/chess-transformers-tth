#!/usr/bin/env python3
"""Unscored diagnostic: sample Cassandre's native XBoard PV at a strict 30s wall deadline.
The original executable, its search and settings are unchanged. No puzzle results are graded.
"""
import hashlib
import json
import os
import pathlib
import queue
import re
import subprocess
import threading
import time

import chess

PV_RE = re.compile(r"^\s*(\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)\s+([a-h][1-8][a-h][1-8][qrbn]?)(?:\s|$)")
MOVE_RE = re.compile(r"^move\s+([a-h][1-8][a-h][1-8][qrbn]?)", re.I)
EXPECTED_SHA = "17d69f321244ab1f6530341b9207fe32fd415e80e017e6c33a827c84755839b7"
POSITIONS = [
    ("first_move", "rnbqkb1r/p2p1ppp/8/2n1p3/2B5/N1R5/2PP1PPP/2BQK1NR w Kkq - 0 12"),
    ("second_solver_move", "rnbqkb1r/p2p2pp/5p2/2n1p3/2B5/N1R2Q2/2PP1PPP/2B1K1NR w Kkq - 0 13"),
    ("starting_position", chess.STARTING_FEN),
]

def run_one(exe, label, fen, limit=30.0):
    board = chess.Board(fen)
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    out = queue.Queue()
    def reader():
        for line in proc.stdout:
            out.put((time.monotonic(), line.strip()))
    threading.Thread(target=reader, daemon=True).start()
    start = time.monotonic()
    samples = []
    native = None
    native_at = None
    try:
        for cmd in ["xboard", "protover 2", "new", "force",
                    "setboard " + fen, "post", "st 30", "go"]:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
        deadline = start + limit
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                received_at, line = out.get(timeout=min(0.1, remaining))
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if received_at > deadline:
                continue
            m = PV_RE.match(line)
            if m:
                move = chess.Move.from_uci(m.group(5))
                if move in board.legal_moves:
                    samples.append({"elapsed": round(received_at-start, 3),
                                    "depth": int(m.group(1)), "score": int(m.group(2)),
                                    "pv_move": move.uci(), "pv_line": line[:200]})
            m = MOVE_RE.match(line)
            if m:
                move = chess.Move.from_uci(m.group(1).lower())
                if move in board.legal_moves:
                    native, native_at = move.uci(), round(received_at-start, 3)
                    break
        elapsed = time.monotonic() - start
        last = samples[-1] if samples else None
        selected = native if native is not None else (last["pv_move"] if last else None)
        result = {"case": label, "fen": fen, "limit_seconds": limit,
                  "wall_elapsed": round(elapsed, 3), "native_move": native,
                  "native_elapsed": native_at, "latest_native_pv": last,
                  "native_pv_count": len(samples), "selected_at_deadline": selected,
                  "decision_source": "native_move" if native else
                    ("last_engine_reported_pv" if last else "no_legal_move_technical"),
                  "technical": selected is None,
                  "scored": False, "last_five_pv": samples[-5:]}
        print("STRICT_DEADLINE_PROBE", json.dumps(result), flush=True)
        return result
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        for f in (proc.stdin, proc.stdout):
            try:
                f.close()
            except Exception:
                pass

def main():
    exe = pathlib.Path(os.environ["CASSANDRE_EXE"])
    actual = hashlib.sha256(exe.read_bytes()).hexdigest()
    if actual != EXPECTED_SHA:
        raise RuntimeError("Cassandre executable SHA-256 mismatch")
    results = [run_one(exe, label, fen) for label, fen in POSITIONS]
    report = {"original_binary_sha256": actual, "time_control": "st 30",
              "deadline": "strict monotonic wall time, no extra search",
              "method": "latest native XBoard PV before deadline, never external evaluation",
              "unscored": True, "cases": results}
    pathlib.Path("cassandre-strict-deadline-probe.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    assert all(r["wall_elapsed"] < 32.5 for r in results)
    assert all(not r["technical"] for r in results)

if __name__ == "__main__":
    main()
