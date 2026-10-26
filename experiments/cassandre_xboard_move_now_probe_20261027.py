#!/usr/bin/env python3
"""Unscored diagnostic of XBoard '?' move-now behavior in original Cassandre 0.24.
Does not grade puzzles, change engine code, or accept moves after 30 seconds.
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

SHA256 = "17d69f321244ab1f6530341b9207fe32fd415e80e017e6c33a827c84755839b7"
PV = re.compile(r"^\s*(\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)\s+([a-h][1-8][a-h][1-8][qrbn]?)(?:\s|$)")
MOVE = re.compile(r"^move\s+([a-h][1-8][a-h][1-8][qrbn]?)", re.I)
POSITIONS = [
    ("first_move", "rnbqkb1r/p2p1ppp/8/2n1p3/2B5/N1R5/2PP1PPP/2BQK1NR w Kkq - 0 12"),
    ("second_solver_move", "rnbqkb1r/p2p2pp/5p2/2n1p3/2B5/N1R2Q2/2PP1PPP/2B1K1NR w Kkq - 0 13"),
    ("starting_position", chess.STARTING_FEN),
]

def probe(exe, label, fen, send_question_at=28.5):
    board = chess.Board(fen)
    p = subprocess.Popen([str(exe)], cwd=str(exe.parent),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    q = queue.Queue()
    def reader():
        for line in p.stdout:
            q.put((time.monotonic(), line.strip()))
    threading.Thread(target=reader, daemon=True).start()
    last_pv = None
    native = None
    question_at = None
    late_native = None
    try:
        for cmd in ("xboard", "protover 2", "new", "force",
                    "setboard "+fen, "post", "st 30"):
            p.stdin.write(cmd+"\n")
            p.stdin.flush()
        start = time.monotonic()
        p.stdin.write("go\n")
        p.stdin.flush()
        deadline = start+30.0
        # After the hard deadline, listen briefly for diagnostic purposes only.
        # Late moves are never eligible and never graded.
        until = deadline+0.5
        while time.monotonic() < until:
            now = time.monotonic()
            if question_at is None and native is None and now-start >= send_question_at and now < deadline:
                p.stdin.write("?\n")
                p.stdin.flush()
                question_at = round(time.monotonic()-start, 4)
            try:
                t,line=q.get(timeout=min(0.04,max(0.001,until-time.monotonic())))
            except queue.Empty:
                if p.poll() is not None:
                    break
                continue
            if t < start:
                continue
            m=PV.match(line)
            if m:
                try:
                    mv=chess.Move.from_uci(m.group(5))
                    if mv in board.legal_moves and t <= deadline:
                        last_pv={"move":mv.uci(),"at":round(t-start,4),"depth":int(m.group(1))}
                except ValueError:
                    pass
            m=MOVE.match(line)
            if m:
                try:
                    mv=chess.Move.from_uci(m.group(1).lower())
                    if mv not in board.legal_moves:
                        continue
                    record={"move":mv.uci(),"at":round(t-start,4)}
                    if t <= deadline:
                        native=record
                        break
                    late_native=record
                    break
                except ValueError:
                    pass
        result={"case":label,"question_sent_at":question_at,"native_before_deadline":native,
                "native_after_deadline_diagnostic_only":late_native,"last_legal_pv_before_deadline":last_pv,
                "hard_deadline_seconds":30,"scored":False,
                "decision_eligible":native is not None}
        print("MOVE_NOW_PROBE "+json.dumps(result),flush=True)
        return result
    finally:
        if p.poll() is None:
            p.terminate()
            try:p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)
        for stream in (p.stdin,p.stdout):
            try:stream.close()
            except Exception:pass

def main():
    exe=pathlib.Path(os.environ["CASSANDRE_EXE"])
    digest=hashlib.sha256(exe.read_bytes()).hexdigest()
    if digest != SHA256:
        raise RuntimeError("Original executable SHA256 mismatch")
    results=[probe(exe,name,fen) for name,fen in POSITIONS]
    report={"sha256":digest,"protocol":"XBoard","question":"? at 28.5 seconds if still searching",
            "engine_modified":False,"unscored":True,"results":results}
    pathlib.Path("cassandre-move-now-unscored.json").write_text(json.dumps(report,indent=2),encoding="utf8")
    print("SUMMARY "+json.dumps({"cases":len(results),
        "native_before_deadline":sum(bool(x["native_before_deadline"]) for x in results),
        "question_sent":sum(x["question_sent_at"] is not None for x in results)}),flush=True)

if __name__=="__main__":
    main()
