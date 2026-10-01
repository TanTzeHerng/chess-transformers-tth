import subprocess, time, sys, urllib.request, json, chess

EXE = sys.argv[1]

def read_until(p, pred, timeout=30):
    end = time.time() + timeout
    out=[]
    while time.time() < end:
        line = p.stdout.readline()
        if not line:
            if p.poll() is not None:
                raise RuntimeError("engine exited: " + repr(p.returncode))
            continue
        line=line.strip()
        out.append(line)
        print(line, flush=True)
        if pred(line):
            return out
    raise TimeoutError("timeout; lines=" + repr(out[-20:]))

p = subprocess.Popen([EXE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True, bufsize=1)

def send(s):
    print(">>", s, flush=True)
    p.stdin.write(s+"\n"); p.stdin.flush()

try:
    send("uci")
    lines=read_until(p, lambda x:x=="uciok", 10)
    assert any("id name YoloChess-UCI" in x for x in lines)
    send("isready"); read_until(p, lambda x:x=="readyok", 5)

    tests = [
        ("position startpos", "go nodes 1", 30),
        ("position startpos moves e2e4 e7e5", "go depth 2", 30),
        ("position startpos moves d2d4 g8f6 c2c4", "go nodes 128", 30),
        ("position fen 8/8/8/8/8/2k5/4K3/7R w - - 0 1", "go movetime 1000", 30),
    ]
    for pos, go, timeout in tests:
        send(pos); send(go)
        lines=read_until(p, lambda x:x.startswith("bestmove "), timeout)
        bm=[x for x in lines if x.startswith("bestmove ")][-1].split()[1]
        b=chess.Board() if pos=="position startpos" else None
        # Full legality validation only for the cases we reconstruct easily.
        if pos=="position startpos":
            assert chess.Move.from_uci(bm) in b.legal_moves
        print("PASS", go, bm, flush=True)

    # stop/infinite
    send("position startpos")
    send("setoption name OnlinePolicy value false")
    send("go infinite")
    time.sleep(0.25)
    send("stop")
    read_until(p, lambda x:x.startswith("bestmove "), 10)
    print("UCI SMOKE PASS", flush=True)
finally:
    try:
        send("quit")
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()
