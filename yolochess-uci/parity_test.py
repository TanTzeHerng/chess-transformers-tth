import json, subprocess, sys, time, urllib.request
import chess

EXE = sys.argv[1]
SPACE = "https://jrahn-yolochess.hf.space"

def space_move(board):
    fen = board.fen()
    body = json.dumps({"data":[fen, "", "UCI", 1]}).encode()
    req = urllib.request.Request(SPACE + "/call/btn_play", data=body, method="POST",
                                 headers={"Content-Type":"application/json","Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        event_id = json.loads(r.read().decode())["event_id"]
    req2 = urllib.request.Request(SPACE + "/call/btn_play/" + event_id,
                                  headers={"Accept":"text/event-stream"})
    result = None
    with urllib.request.urlopen(req2, timeout=20) as r:
        end = time.time() + 20
        while time.time() < end:
            raw = r.readline()
            if not raw:
                break
            line = raw.decode("utf-8","replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                val = json.loads(line[5:].strip())
            except Exception:
                continue
            if isinstance(val, list) and len(val) >= 2 and isinstance(val[1], str):
                result = val[1]
                break
    if result is None:
        raise RuntimeError("No YOLOChess result for " + fen)
    target4 = " ".join(result.split()[:4])
    for mv in board.legal_moves:
        b=board.copy(stack=False); b.push(mv)
        if " ".join(b.fen().split()[:4]) == target4:
            return mv.uci()
    raise RuntimeError("Could not infer Space move from returned FEN: " + result)

def engine_move(p, board):
    p.stdin.write("position fen " + board.fen() + "\n")
    p.stdin.write("go nodes 1\n")
    p.stdin.flush()
    end=time.time()+30
    while time.time()<end:
        line=p.stdout.readline()
        if not line:
            if p.poll() is not None:
                raise RuntimeError("engine exited")
            continue
        line=line.strip()
        print("ENGINE", line, flush=True)
        if line.startswith("bestmove "):
            return line.split()[1]
    raise TimeoutError("engine bestmove timeout")

positions = []
b=chess.Board(); positions.append(b.copy())
for moves in [
    ["e2e4","e7e5","g1f3","b8c6"],
    ["d2d4","g8f6","c2c4","g7g6","b1c3","f8g7"],
    ["c2c4","e7e5","b1c3","g8f6","g2g3","d7d5"],
]:
    b=chess.Board()
    for u in moves: b.push_uci(u)
    positions.append(b.copy())

p=subprocess.Popen([EXE],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                   text=True,bufsize=1)
try:
    p.stdin.write("uci\n"); p.stdin.flush()
    while True:
        x=p.stdout.readline().strip()
        if x=="uciok": break
    p.stdin.write("isready\n"); p.stdin.flush()
    while True:
        x=p.stdout.readline().strip()
        if x=="readyok": break

    for i,b in enumerate(positions,1):
        expected=space_move(b)
        actual=engine_move(p,b)
        print(f"PARITY {i}: space={expected} exe={actual}", flush=True)
        if expected != actual:
            raise AssertionError(f"Parity mismatch {i}: {expected} != {actual}")
    print("YOLOCHESS LIVE POLICY PARITY PASS", flush=True)
finally:
    try:
        p.stdin.write("quit\n"); p.stdin.flush()
    except Exception:
        pass
