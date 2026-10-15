#!/usr/bin/env python3
"""Stockfish 17.1 depth-one paired NNUE comparisons with game-by-game public scoreboard."""
import base64
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import chess
import chess.engine
import chess.pgn

ROOT = Path.cwd()
NETS = ROOT / "nets"
NETS.mkdir(exist_ok=True)
ENGINE = str((ROOT / "sf17/src/stockfish").resolve())
SMALL = str((ROOT / "sf17/src/nn-37f18f62d772.nnue").resolve())
REFERENCE = "nn-1c0000000000.nnue"
REPO = os.environ.get("GITHUB_REPOSITORY", "TanTzeHerng/chess-transformers-tth")
BRANCH = "results/sf171-depth1-nnue-20261016"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
RUN = os.environ.get("GITHUB_RUN_ID", "")
SOURCE = "https://raw.githubusercontent.com/"
CANDIDATES = [
    ("nn-6df63d0990-20251019.nnue", SOURCE+"Matthies/NN/master/nn-6df63d0990-20251019.nnue", "2025-10-19", "requested_window"),
    ("nn-be4dcd7c83-20251031.nnue", SOURCE+"Matthies/NN/master/nn-be4dcd7c83-20251031.nnue", "2025-10-31", "requested_window"),
    ("nn-1cedc0ffeeee.nnue", SOURCE+"official-stockfish/networks/master/nn-1cedc0ffeeee.nnue", "2024-10-31", "historical_control"),
    ("nn-1111cefa1111.nnue", SOURCE+"official-stockfish/networks/master/nn-1111cefa1111.nnue", "2024-08-20", "historical_control"),
]
OPENINGS = [
    "e4 e5 Nf3 Nc6 Bb5 a6",
    "d4 d5 c4 e6 Nc3 Nf6",
    "e4 c5 Nf3 d6 d4 cxd4",
    "d4 Nf6 c4 g6 Nc3 Bg7",
]
META = {
    "reference": REFERENCE, "engine": "official Stockfish 17.1", "depth": 1,
    "threads": 1, "hash_MiB": 16, "MultiPV": 1,
    "small_net": Path(SMALL).name, "opening_pairs": len(OPENINGS),
    "pairing": "same openings with colors reversed",
    "result_counting": "Only normal chess results; unfinished games excluded",
    "github_run": f"https://github.com/{REPO}/actions/runs/{RUN}",
    "caveat": "Control nets predate the requested post-November-2024 upload window.",
}


def report(name, data):
    target = "net-results/" + name.replace(".nnue", "") + ".json"
    document = json.dumps(data, indent=2) + "\n"
    (ROOT / "results.json").write_text(document, encoding="utf8")
    print("LIVE_RESULT", name, document.replace("\n", " "), flush=True)
    if not TOKEN:
        print("LIVE_PUBLISH_WARNING: GitHub token unavailable", flush=True)
        return
    url = "https://api.github.com/repos/" + REPO + "/contents/" + target
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + TOKEN,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "stockfish-nnue-live-scoreboard",
    }
    for attempt in range(6):
        sha = None
        try:
            req = urllib.request.Request(url + "?ref=" + BRANCH, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                sha = json.loads(resp.read().decode("utf8"))["sha"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print("LIVE_READ_WARNING", repr(e), flush=True)
        body = {
            "message": "Update live SF17 depth1 results for " + name,
            "branch": BRANCH,
            "content": base64.b64encode(document.encode("utf8")).decode("ascii"),
        }
        if sha:
            body["sha"] = sha
        try:
            payload = json.dumps(body).encode("utf8")
            req = urllib.request.Request(url, data=payload, headers={**headers, "Content-Type": "application/json"}, method="PUT")
            with urllib.request.urlopen(req, timeout=30) as resp:
                print("LIVE_PUBLISHED", resp.status,
                      f"https://github.com/{REPO}/blob/{BRANCH}/{target}", flush=True)
            return
        except Exception as e:
            print("LIVE_WRITE_RETRY", attempt, repr(e), flush=True)
            time.sleep(1 + attempt)
    print("LIVE_PUBLISH_FAILED", target, flush=True)


def download(name, url):
    filename = NETS / name
    if not filename.exists():
        print("DOWNLOAD", name, flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": "stockfish-nettest-experiment"})
        with urllib.request.urlopen(req, timeout=100) as src, filename.open("wb") as dst:
            import shutil
            shutil.copyfileobj(src, dst)
    size = filename.stat().st_size
    print("DOWNLOADED", name, size, flush=True)
    return filename


def compatible(filename):
    cmd = "\n".join([
        "uci",
        "setoption name Threads value 1",
        "setoption name EvalFileSmall value " + SMALL,
        "setoption name EvalFile value " + str(filename.resolve()),
        "position startpos",
        "eval",
        "quit",
    ]) + "\n"
    p = subprocess.run([ENGINE], input=cmd, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=60)
    output = p.stdout
    with (ROOT / "compatibility.log").open("a", encoding="utf8") as out:
        out.write(filename.name + "\n" + output[-3500:] + "\n")
    if "Final evaluation" not in output or "not loaded" in output or p.returncode:
        print("REJECT_INCOMPATIBLE", filename.name, output[-1300:], flush=True)
        return False
    print("ACCEPT_COMPATIBLE", filename.name, output[-350:], flush=True)
    return True


def start_engine(net):
    e = chess.engine.SimpleEngine.popen_uci(ENGINE, timeout=100)
    e.configure({
        "Threads": 1, "Hash": 16,
        "EvalFileSmall": SMALL, "EvalFile": str((NETS / net).resolve()),
    })
    return e


def run_game(candidate, white_candidate, opening, index):
    white = candidate if white_candidate else REFERENCE
    black = REFERENCE if white_candidate else candidate
    b = chess.Board()
    g = chess.pgn.Game()
    g.headers.update({
        "Event": "Stockfish17.1 NNUE depth1",
        "Site": "GitHub Actions",
        "Round": str(index), "White": white, "Black": black,
        "TimeControl": "fixed depth 1"
    })
    node = g
    for san in opening.split():
        move = b.parse_san(san)
        b.push(move)
        node = node.add_variation(move)
    ewhite = start_engine(white)
    eblack = start_engine(black)
    try:
        for _ in range(300):
            if b.is_game_over(claim_draw=True):
                break
            engine = ewhite if b.turn == chess.WHITE else eblack
            result = engine.play(b, chess.engine.Limit(depth=1), game=str(index))
            if result.move is None or result.move not in b.legal_moves:
                raise ValueError("No legal bestmove: " + str(result.move))
            node = node.add_variation(result.move)
            b.push(result.move)
        outcome = b.result(claim_draw=True) if b.is_game_over(claim_draw=True) else "*"
        g.headers["Result"] = outcome
        g.headers["Termination"] = "Normal" if outcome != "*" else "Incomplete"
        with (ROOT / "games.pgn").open("a", encoding="utf8") as dest:
            dest.write(str(g) + "\n\n")
        print("GAME_DONE", candidate, index, outcome, "ply", b.ply(), flush=True)
        return outcome
    finally:
        ewhite.quit()
        eblack.quit()


def main():
    import shutil
    ref_src = ROOT / "sf17/src" / REFERENCE
    assert ref_src.exists(), "Official reference was not fetched by build"
    shutil.copy2(ref_src, NETS / REFERENCE)
    print("REFERENCE_READY", ref_src.stat().st_size, flush=True)
    assert compatible(NETS / REFERENCE), "Reference network is not loadable"
    for name, url, date, group in CANDIDATES:
        data = {
            "metadata": META, "network": name, "network_date": date,
            "group": group, "status": "checking_compatibility",
            "complete_games": 0, "wins": 0, "draws": 0, "losses": 0,
            "unfinished": 0, "errors": [], "games": [],
            "strength_conclusion": "insufficient_evidence",
        }
        report(name, data)
        try:
            network_file = download(name, url)
            if not compatible(network_file):
                data["status"] = "incompatible_with_stockfish_17_1"
                report(name, data)
                continue
            data["status"] = "playing"
            report(name, data)
            for j, opening in enumerate(OPENINGS):
                for side in (True, False):
                    gid = j * 2 + int(not side) + 1
                    try:
                        outcome = run_game(name, side, opening, gid)
                        if outcome == "*":
                            data["unfinished"] += 1
                        else:
                            data["complete_games"] += 1
                            if outcome == "1/2-1/2":
                                data["draws"] += 1
                            elif (outcome == "1-0" and side) or (outcome == "0-1" and not side):
                                data["wins"] += 1
                            else:
                                data["losses"] += 1
                        data["games"].append({
                            "game": gid, "opening": opening, "network_as_white": side, "result": outcome
                        })
                    except Exception as exc:
                        print("GAME_ERROR", repr(exc), flush=True)
                        data["errors"].append(repr(exc))
                    report(name, data)
            data["status"] = "completed"
        except Exception as exc:
            data["status"] = "error"
            data["errors"].append(repr(exc))
            print("NET_ERROR", name, repr(exc), flush=True)
        report(name, data)
    print("ALL_NETS_CHECKED", flush=True)


if __name__ == "__main__":
    main()
