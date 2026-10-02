import subprocess, sys, time, chess, os

EXE = sys.argv[1]

def start():
    return subprocess.Popen(
        [EXE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

def send(p, s):
    print(">>", s, flush=True)
    p.stdin.write(s + "\n")
    p.stdin.flush()

def read_until(p, pred, timeout=40):
    end = time.time() + timeout
    lines = []
    while time.time() < end:
        line = p.stdout.readline()
        if not line:
            if p.poll() is not None:
                raise RuntimeError("engine exited " + repr(p.returncode))
            continue
        line = line.rstrip()
        lines.append(line)
        print(line, flush=True)
        if pred(line):
            return lines
    raise TimeoutError("timeout waiting for engine; tail=" + repr(lines[-30:]))

def bestmove(lines):
    x = [l for l in lines if l.startswith("bestmove ")]
    if not x:
        raise AssertionError("no bestmove")
    return x[-1].split()[1]

p = start()
try:
    send(p, "uci")
    lines = read_until(p, lambda x: x == "uciok", 20)
    assert any("YoloChess-NNUE-UCI 2.0" in x for x in lines)
    assert any("Stockfish dev 49ea5ded" in x for x in lines)

    send(p, "isready")
    read_until(p, lambda x: x == "readyok", 10)

    # Pure live YOLOChess policy compatibility.
    send(p, "position startpos")
    send(p, "go nodes 1")
    lines = read_until(p, lambda x: x.startswith("bestmove "), 30)
    bm = bestmove(lines)
    assert chess.Move.from_uci(bm) in chess.Board().legal_moves
    print("PASS nodes1 live policy", bm, flush=True)

    # Search using NNUE.
    send(p, "position startpos moves e2e4 e7e5")
    send(p, "go depth 2")
    lines = read_until(p, lambda x: x.startswith("bestmove "), 40)
    bm = bestmove(lines)
    b = chess.Board()
    b.push_uci("e2e4"); b.push_uci("e7e5")
    assert chess.Move.from_uci(bm) in b.legal_moves
    assert any("depth 2" in x for x in lines if x.startswith("info "))
    print("PASS depth2 NNUE search", bm, flush=True)

    # Hard node budget.
    send(p, "position startpos moves d2d4 g8f6 c2c4")
    send(p, "go nodes 256")
    lines = read_until(p, lambda x: x.startswith("bestmove "), 40)
    print("PASS nodes256", bestmove(lines), flush=True)

    # Movetime.
    send(p, "position fen 8/8/8/8/8/2k5/4K3/7R w - - 0 1")
    t0 = time.time()
    send(p, "go movetime 1000")
    lines = read_until(p, lambda x: x.startswith("bestmove "), 15)
    elapsed = time.time() - t0
    assert elapsed < 3.0, elapsed
    print("PASS movetime1000", bestmove(lines), "elapsed", elapsed, flush=True)

    # stop.
    send(p, "setoption name OnlinePolicy value false")
    send(p, "position startpos")
    send(p, "go infinite")
    time.sleep(0.35)
    send(p, "stop")
    lines = read_until(p, lambda x: x.startswith("bestmove "), 10)
    print("PASS infinite-stop", bestmove(lines), flush=True)

    print("ALL UCI SMOKE TESTS PASS", flush=True)
finally:
    try:
        send(p, "quit")
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()
