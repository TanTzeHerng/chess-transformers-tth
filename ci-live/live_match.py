import os, sys, time, json, base64, urllib.request, urllib.error
from pathlib import Path
import chess
import chess.engine
import chess.pgn

REPO = os.environ["GITHUB_REPOSITORY"]
BRANCH = os.environ["GITHUB_REF_NAME"]
TOKEN = os.environ["GH_TOKEN"]
LIVE_PATH = "live-transformer-elite-2plus1.txt"
PGN_PATH = Path("Transformer-vs-Elite-v2-2plus1.pgn")

engine_path = Path(sys.argv[1]).resolve()
lc0_path = Path(sys.argv[2]).resolve()
elite_path = Path(sys.argv[3]).resolve()

lines = [
    "LIVE MATCH: Rahul Transformer UCI (search) vs Elite Leela v2 (1 node)",
    "Time control: 2 minutes + 1 second increment",
    "Transformer: White | Elite v2: Black",
    "Started from the standard initial position.",
    "",
]
content_sha = None

def publish():
    global content_sha
    data = ("\n".join(lines) + "\n").encode("utf-8")
    body = {
        "message": "Live match update",
        "content": base64.b64encode(data).decode("ascii"),
        "branch": BRANCH,
    }
    if content_sha:
        body["sha"] = content_sha
    url = f"https://api.github.com/repos/{REPO}/contents/{LIVE_PATH}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="PUT",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "live-chess-match",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            obj = json.load(r)
            content_sha = obj["content"]["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 422 and not content_sha:
            get = urllib.request.Request(
                url + f"?ref={BRANCH}",
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "live-chess-match",
                },
            )
            with urllib.request.urlopen(get, timeout=20) as r:
                obj = json.load(r)
                content_sha = obj["sha"]
            publish()
        else:
            print("LIVE_PUBLISH_ERROR", e.code, e.read().decode("utf-8", "ignore"), flush=True)

def score_text(info, pov):
    try:
        sc = info.get("score")
        if sc is None:
            return "?"
        p = sc.pov(pov)
        if p.is_mate():
            return f"M{p.mate()}"
        s = p.score()
        return f"{s/100:+.2f}" if s is not None else "?"
    except Exception:
        return "?"

def info_int(info, key):
    try:
        return int(info.get(key, 0) or 0)
    except Exception:
        return 0

publish()
white = chess.engine.SimpleEngine.popen_uci(str(engine_path))
black = chess.engine.SimpleEngine.popen_uci(str(lc0_path))
try:
    try:
        black.configure({"WeightsFile": str(elite_path), "Backend": "blas"})
    except Exception as e:
        print("ELITE_CONFIG_WARNING", repr(e), flush=True)

    board = chess.Board()
    game = chess.pgn.Game()
    game.headers["Event"] = "Rahul Transformer UCI vs Elite Leela v2"
    game.headers["Site"] = "GitHub Actions Windows runner"
    game.headers["Date"] = time.strftime("%Y.%m.%d")
    game.headers["Round"] = "1"
    game.headers["White"] = "Rahul Transformer UCI (search)"
    game.headers["Black"] = "Elite Leela v2 (1 node)"
    game.headers["TimeControl"] = "120+1"
    game.headers["Result"] = "*"
    node = game

    clocks = {chess.WHITE: 120.0, chess.BLACK: 120.0}
    inc = 1.0
    result = "*"
    termination = ""
    max_plies = 300

    for ply in range(1, max_plies + 1):
        if board.is_game_over(claim_draw=True):
            result = board.result(claim_draw=True)
            termination = str(board.outcome(claim_draw=True).termination)
            break

        color = board.turn
        eng = white if color == chess.WHITE else black
        if color == chess.WHITE:
            limit = chess.engine.Limit(
                white_clock=clocks[chess.WHITE],
                black_clock=clocks[chess.BLACK],
                white_inc=inc,
                black_inc=inc,
            )
        else:
            limit = chess.engine.Limit(
                white_clock=clocks[chess.WHITE],
                black_clock=clocks[chess.BLACK],
                white_inc=inc,
                black_inc=inc,
                nodes=1,
            )

        t0 = time.perf_counter()
        play = eng.play(board, limit, info=chess.engine.INFO_ALL)
        elapsed = time.perf_counter() - t0

        clocks[color] -= elapsed
        if clocks[color] < 0:
            result = "0-1" if color == chess.WHITE else "1-0"
            termination = "time forfeit"
            lines.append(f"FINAL {result} — {termination}")
            publish()
            break

        move = play.move
        if move is None or move not in board.legal_moves:
            result = "0-1" if color == chess.WHITE else "1-0"
            termination = "engine returned no legal move"
            lines.append(f"FINAL {result} — {termination}")
            publish()
            break

        san = board.san(move)
        uci = move.uci()
        mover = "Transformer" if color == chess.WHITE else "Elite v2"
        evaltxt = score_text(play.info, color)
        depth = info_int(play.info, "depth")
        nodes = info_int(play.info, "nodes")

        board.push(move)
        clocks[color] += inc
        node = node.add_variation(move)
        node.comment = (
            f"{mover}; elapsed {elapsed:.3f}s; clock {clocks[color]:.3f}s; "
            f"self-eval {evaltxt}; depth {depth}; nodes {nodes}"
        )

        move_no = (ply + 1) // 2
        prefix = f"{move_no}." if color == chess.WHITE else f"{move_no}..."
        line = (
            f"PLY {ply:03d} | {prefix} {san} ({uci}) | {mover} | "
            f"used {elapsed:.3f}s | clock W {clocks[chess.WHITE]:.1f}s / "
            f"B {clocks[chess.BLACK]:.1f}s | self-eval {evaltxt} | "
            f"depth {depth} | nodes {nodes} | FEN {board.fen()}"
        )
        print(line, flush=True)
        lines.append(line)
        publish()

    else:
        result = "1/2-1/2"
        termination = "300-ply safety limit"

    if result == "*" and board.is_game_over(claim_draw=True):
        result = board.result(claim_draw=True)
        termination = str(board.outcome(claim_draw=True).termination)

    game.headers["Result"] = result
    game.headers["Termination"] = termination or "normal"
    with PGN_PATH.open("w", encoding="utf-8") as f:
        print(game, file=f, end="\n\n")

    lines.append("")
    lines.append(f"FINAL {result} — {termination or 'normal'}")
    lines.append(f"PGN saved as {PGN_PATH.name}")
    publish()
    print(f"FINAL {result} {termination}", flush=True)
finally:
    try:
        white.quit()
    except Exception:
        pass
    try:
        black.quit()
    except Exception:
        pass
