import subprocess, sys, time, chess, os

EXE = sys.argv[1]

def start():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.Popen([EXE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            creationflags=flags)

def send(p, s):
    print(">>", s, flush=True)
    p.stdin.write(s + "\n"); p.stdin.flush()

def read_until(p, pred, timeout=30):
    end = time.time() + timeout
    lines = []
    while time.time() < end:
        line = p.stdout.readline()
        if not line:
            if p.poll() is not None:
                raise RuntimeError("engine exited " + repr(p.returncode))
            continue
        line = line.strip()
        lines.append(line)
        print(line, flush=True)
        if pred(line):
            return lines
    raise TimeoutError("tail=" + repr(lines[-30:]))

p = start()
try:
    send(p, "uci")
    lines = read_until(p, lambda x: x == "uciok", 20)
    assert any("YOLOChess-NNUE 2.0" in x for x in lines)
    send(p, "isready")
    read_until(p, lambda x: x == "readyok", 10)

    # Pure transformer compatibility at exactly one node.
    send(p, "position startpos")
    send(p, "go nodes 1")
    lines = read_until(p, lambda x: x.startswith("bestmove "), 20)
    assert any(x.startswith("info string YOLOChess policy ") and "unavailable" not in x for x in lines)
    bm = [x for x in lines if x.startswith("bestmove ")][-1].split()[1]
    assert chess.Move.from_uci(bm) in chess.Board().legal_moves
    print("PASS live-policy nodes=1", bm, flush=True)

    # Local NNUE search tests.
    send(p, "setoption name OnlinePolicy value false")
    cases = [
        ("position startpos moves e2e4 e7e5", "go depth 2", 20),
        ("position startpos moves d2d4 g8f6 c2c4", "go nodes 3000", 20),
        ("position fen 8/8/8/8/8/2k5/4K3/7R w - - 0 1", "go movetime 700", 20),
    ]
    for pos, go, timeout in cases:
        send(p, pos); send(p, go)
        lines = read_until(p, lambda x: x.startswith("bestmove "), timeout)
        assert any(x.startswith("info depth ") for x in lines)
        print("PASS", go, flush=True)

    send(p, "position startpos")
    send(p, "go infinite")
    time.sleep(0.25)
    send(p, "stop")
    read_until(p, lambda x: x.startswith("bestmove "), 10)
    print("PASS stop", flush=True)

    print("ALL YOLOCHESS-NNUE V2 TESTS PASS", flush=True)
finally:
    try: send(p, "quit")
    except Exception: pass
    try: p.wait(timeout=3)
    except Exception: p.kill()
