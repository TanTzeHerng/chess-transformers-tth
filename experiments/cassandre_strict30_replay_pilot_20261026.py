#!/usr/bin/env python3
"""Unscored strict-30-second Cassandre replay pilot. Original engine executable is unchanged.
Only a native final move or a native PV reported before the monotonic deadline is eligible.
Never convert a technical/no-move outcome into a chess loss.
"""
import argparse
import datetime
import hashlib
import json
import pathlib
import queue
import re
import subprocess
import threading
import time
import chess

SHA = "17d69f321244ab1f6530341b9207fe32fd415e80e017e6c33a827c84755839b7"
PV = re.compile(r"^\s*(\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)\s+([a-h][1-8][a-h][1-8][qrbn]?)(?:\s|$)")
MOVE = re.compile(r"^move\s+([a-h][1-8][a-h][1-8][qrbn]?)", re.I)

def attempt(exe, board, seconds=30.0):
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        errors="replace", bufsize=1)
    lines = queue.Queue()
    def reader():
        for line in proc.stdout:
            lines.put((time.monotonic(), line.strip()))
    threading.Thread(target=reader, daemon=True).start()
    started = None
    best = None
    final = None
    pv_count = 0
    try:
        for cmd in ("xboard", "protover 2", "new", "force",
                    "setboard " + board.fen(en_passant="fen"), "post", "st 30"):
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
        # The 30-second budget begins when the search is requested, not during setup.
        started = time.monotonic()
        proc.stdin.write("go\n")
        proc.stdin.flush()
        deadline = started + seconds
        while time.monotonic() < deadline:
            try:
                received, line = lines.get(timeout=min(0.08, max(0.001, deadline-time.monotonic())))
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if received < started or received > deadline:
                continue
            pv = PV.match(line)
            if pv:
                move = chess.Move.from_uci(pv.group(5))
                if move in board.legal_moves:
                    pv_count += 1
                    best = {"uci":move.uci(), "reported_at":round(received-started, 4),
                            "depth":int(pv.group(1)), "score":int(pv.group(2))}
            native = MOVE.match(line)
            if native:
                move = chess.Move.from_uci(native.group(1).lower())
                if move in board.legal_moves:
                    final = {"uci":move.uci(), "reported_at":round(received-started, 4)}
                    break
        chosen = final or best
        decision = "native_final" if final else ("native_pv" if best else "technical_no_legal_native_move")
        return {"chosen": chosen["uci"] if chosen else None, "source":decision,
                "selected_reported_at":chosen["reported_at"] if chosen else None,
                "last_pv":best, "native_final":final, "pv_count":pv_count,
                "deadline_seconds":seconds, "search_wall_elapsed":round(time.monotonic()-started,4),
                "technical":chosen is None}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        for stream in (proc.stdin, proc.stdout):
            try:stream.close()
            except Exception:pass

def run_puzzle(exe, row, index):
    board=chess.Board(row["initialFen"])
    board.push_uci(row["setupMoveUci"])
    if board.fen(en_passant="fen") != row["puzzleFen"]:
        raise ValueError("source FEN mismatch")
    events=[]
    solved=None
    for ply, expected in enumerate(row["solutionUci"]):
        if ply%2:
            board.push_uci(expected)
            continue
        r=attempt(exe, board)
        r.update({"fen":board.fen(en_passant="fen"),"expected":expected,"ply":ply})
        events.append(r)
        if r["technical"]:
            break
        if r["chosen"]!=expected:
            solved=False
            break
        board.push_uci(expected)
    else:
        solved=True
    return {"index":index,"id":row["id"],"puzzle_rating":row["rating"],
            "pilot_outcome":solved,"technical":solved is None,"moves":events,
            "scored":False,"timestamp_utc":datetime.datetime.now(datetime.timezone.utc).isoformat()}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--exe",type=pathlib.Path,required=True)
    p.add_argument("--dataset",type=pathlib.Path,required=True)
    p.add_argument("--manifest",type=pathlib.Path,required=True)
    p.add_argument("--out",type=pathlib.Path,default=pathlib.Path("strict_pilot"))
    p.add_argument("--max-puzzles",type=int,default=8)
    p.add_argument("--skip-puzzles",type=int,default=0)
    a=p.parse_args()
    if hashlib.sha256(a.exe.read_bytes()).hexdigest()!=SHA:
        raise RuntimeError("Original Cassandre SHA mismatch")
    data=[json.loads(x) for x in a.dataset.read_text(encoding="utf-8").splitlines() if x.strip()]
    manifest=[json.loads(x) for x in a.manifest.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(data)==33460 and len(manifest)==4151
    assert len({x["index"] for x in manifest})==len(manifest)
    assert 0 <= a.skip_puzzles < len(manifest) and 1 <= a.max_puzzles <= len(manifest)-a.skip_puzzles
    a.out.mkdir(parents=True,exist_ok=True)
    count=0
    with (a.out/"pilot.jsonl").open("w",encoding="utf-8",buffering=1) as f:
        for item in manifest[a.skip_puzzles:]:
            if count>=a.max_puzzles:break
            idx=item["index"]
            row=data[idx]
            assert item["id"]==row["id"]
            result=run_puzzle(a.exe,row,idx)
            f.write(json.dumps(result,separators=(",",":"))+"\n")
            f.flush()
            print("STRICT_PILOT",json.dumps(result),flush=True)
            count+=1
    assert count==a.max_puzzles
    results=[json.loads(x) for x in (a.out/"pilot.jsonl").read_text().splitlines()]
    summary={"unscored":True,"skip_puzzles":a.skip_puzzles,"attempted":len(results),
        "legal_decisions":sum(not r["technical"] for r in results),
        "technical":sum(r["technical"] for r in results),
        "native_final_moves":sum(m["source"]=="native_final" for r in results for m in r["moves"]),
        "native_pv_moves":sum(m["source"]=="native_pv" for r in results for m in r["moves"]),
        "max_selected_reported_at":max((m["selected_reported_at"] or 0 for r in results for m in r["moves"]),default=0),
        "note":"No puzzle outcomes integrated into the historical rating"}
    (a.out/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print("STRICT_PILOT_SUMMARY",json.dumps(summary),flush=True)

if __name__=="__main__":
    main()
