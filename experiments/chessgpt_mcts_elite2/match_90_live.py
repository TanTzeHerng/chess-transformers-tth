#!/usr/bin/env python3
"""One genuine 90'+30" ChessGPT vs Elite v2 (1 node) game per GitHub Actions shard.

Each independent job gets its own originals: untempered Adam Karvonen 16-layer
ChessGPT, Stockfish 16 classical static value, and Lc0 Elite v2 (nodes=1).
Variety.bin supplies one of ten deterministically paired 8-ply openings.
Each ChessGPT reply commits observed chess progress, clock, nodes, depth,
seldepth, candidate PV and commentary to a unique GitHub JSON path.
No artificial node/depth limits, adjudicated wins or fictitious results.
"""
import argparse
import base64
import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import chess
import chess.engine
import chess.pgn

from match import DIR, ELITE, WEIGHTS, CHESSGPT, openings, tests

INITIAL_SECONDS = 90 * 60
INCREMENT_SECONDS = 30
BRANCH = "experiment/chessgpt-90plus30-parallel20-20261014"
PROGRESS_DIR = "live_matches/chessgpt_90plus30_20261014"
REPO = "TanTzeHerng/chess-transformers-tth"


def publish(game_id, status):
    """Write one game's status, without branch checkout or overwriting rivals."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("LIVE_PUBLISH_UNAVAILABLE no GITHUB_TOKEN", flush=True)
        return False
    path = "%s/game_%02d.json" % (PROGRESS_DIR, game_id)
    uri = "https://api.github.com/repos/%s/contents/%s" % (
        REPO, urllib.parse.quote(path, safe="/"))
    payload = json.dumps(status, indent=2, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": "Bearer " + token,
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "chessgpt-90-30-match"}
    for attempt in range(10):
        sha = None
        try:
            req = urllib.request.Request(
                uri + "?ref=" + urllib.parse.quote(BRANCH), headers=headers)
            with urllib.request.urlopen(req, timeout=35) as response:
                sha = json.load(response)["sha"]
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                print("LIVE_GET_RETRY", game_id, exc.code, attempt, flush=True)
                time.sleep(min(10, 0.3 * (2 ** min(attempt, 5))) + random.random())
                continue
        except Exception as exc:
            print("LIVE_GET_RETRY", game_id, repr(exc), attempt, flush=True)
            time.sleep(min(10, 0.3 * (2 ** min(attempt, 5))) + random.random())
            continue
        data = {"message": "90+30 chess match game %02d: ply %d %s" % (
                    game_id, status["current_ply"], status["state"]),
                "branch": BRANCH,
                "content": base64.b64encode(payload).decode("ascii")}
        if sha:
            data["sha"] = sha
        req = urllib.request.Request(uri, method="PUT", headers={
            **headers, "Content-Type": "application/json"},
            data=json.dumps(data).encode("utf-8"))
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                result = json.load(response)
            print("LIVE_PUBLISHED", game_id, status["current_ply"],
                  status["state"], result["commit"]["sha"][:10], flush=True)
            return True
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode("utf-8", errors="replace")
            print("LIVE_PUT_RETRY", game_id, exc.code, attempt, detail[:100], flush=True)
        except Exception as exc:
            print("LIVE_PUT_RETRY", game_id, repr(exc), attempt, flush=True)
        time.sleep(min(12, 0.4 * (2 ** min(attempt, 5))) + random.random())
    print("LIVE_PUBLISH_FAILED", game_id, "games continue without fake logs", flush=True)
    return False


def score_for_gpt(result, gpt_white):
    white_points = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}[result]
    return white_points if gpt_white else 1.0 - white_points


def format_pv(board, info):
    pv = info.get("pv") or []
    try:
        return board.variation_san(pv[:12])
    except (ValueError, AssertionError):
        return ""


def play(game_id):
    tests()
    positions = openings()
    opening = positions[(game_id - 1) // 2]
    board = chess.Board()
    for u in opening:
        board.push_uci(u)
    gpt_white = game_id % 2 == 1
    clocks = {True: float(INITIAL_SECONDS), False: float(INITIAL_SECONDS)}
    out = DIR / "results_90" / ("game_%02d" % game_id)
    out.mkdir(parents=True, exist_ok=True)
    timeline = []
    startwall = time.time()
    state = {
        "event": "20 games, 90+30, ChessGPT T=1 versus Elite v2 nodes=1",
        "run_id": os.getenv("GITHUB_RUN_ID", "unknown"),
        "game_id": game_id,
        "opening_pair": (game_id + 1) // 2,
        "opening_moves_uci": list(opening),
        "chessgpt_color": "white" if gpt_white else "black",
        "white_player": "ChessGPT MCTS" if gpt_white else "Elite v2 (1 node)",
        "black_player": "Elite v2 (1 node)" if gpt_white else "ChessGPT MCTS",
        "time_control": "5400+30",
        "mcts_policy": "original ChessGPT SAN probabilities, temperature 1 (no option)",
        "mcts_configuration": {
            "TorchThreads": 4, "SANBatchSize": 64, "CPuct": 900,
            "RootPolicyBeam": 64, "ChildPolicyBeam": 64, "ExpandAfterVisits": 24},
        "notes": "MCTS nodes are PUCT simulations; depth and seldepth are search-tree plies. Lc0 uses go nodes 1.",
        "state": "starting",
        "current_ply": board.ply(),
        "board_fen": board.fen(),
        "clocks_seconds": {"white": clocks[True], "black": clocks[False]},
        "history": timeline,
        "started_unix": startwall,
        "last_updated_unix": startwall,
        "last_commentary": "Game %d starting from Variety opening pair %d." %
                          (game_id, (game_id + 1) // 2),
        "result": None,
        "termination": None,
    }
    publish(game_id, state)
    print("GAME_START", game_id, state["chessgpt_color"],
          "opening", opening, flush=True)

    def update(now=None):
        state["last_updated_unix"] = time.time() if now is None else now
        state["board_fen"] = board.fen()
        state["current_ply"] = board.ply()
        state["clocks_seconds"] = {
            "white": round(clocks[True], 3),
            "black": round(clocks[False], 3)}
        publish(game_id, state)

    try:
        with chess.engine.SimpleEngine.popen_uci(
                [sys.executable, "-u", str(CHESSGPT)],
                timeout=400, cwd=str(DIR)) as gpt, \
             chess.engine.SimpleEngine.popen_uci(
                str(ELITE), timeout=400, cwd=str(ELITE.parent)) as elite:
            assert gpt.id.get("name", "").startswith("ChessGPT-MCTS"), gpt.id
            expected = state["mcts_configuration"]
            for name, value in expected.items():
                assert name in gpt.options and int(gpt.options[name].default) == value, name
            assert "PolicyTemperature" not in gpt.options
            assert not gpt.options["FreshLeafBenchmark"].default
            gpt.configure(expected)
            elite_options = {
                "WeightsFile": str(WEIGHTS),
                "Threads": 1, "NNCacheSize": 1024,
                "MinibatchSize": 1, "MaxPrefetch": 0,
                "RamLimitMb": 256}
            elite.configure({k: v for k, v in elite_options.items()
                             if k in elite.options})
            probe = elite.play(chess.Board(), chess.engine.Limit(nodes=1))
            assert probe.move in chess.Board().legal_moves, "Elite nodes1 preflight failed"
            print("PRE_FLIGHT_PASS", game_id, "Elite", probe.move, flush=True)
            game_token = object()
            state["state"] = "running"
            update()
            result = None
            termination = None
            for _ in range(800):
                outcome = board.outcome(claim_draw=True)
                if outcome:
                    result = outcome.result()
                    termination = outcome.termination.name
                    break
                mover = board.turn
                gpt_turn = (mover == chess.WHITE) == gpt_white
                who = "ChessGPT MCTS" if gpt_turn else "Elite v2 (1 node)"
                engine = gpt if gpt_turn else elite
                limit = chess.engine.Limit(
                    white_clock=clocks[chess.WHITE],
                    black_clock=clocks[chess.BLACK],
                    white_inc=INCREMENT_SECONDS,
                    black_inc=INCREMENT_SECONDS,
                    **({} if gpt_turn else {"nodes": 1}))
                tic = time.monotonic()
                response = engine.play(
                    board, limit, game=game_token, info=chess.engine.INFO_ALL)
                elapsed = time.monotonic() - tic
                clocks[mover] -= elapsed
                if clocks[mover] <= 0:
                    result = "0-1" if mover else "1-0"
                    termination = who + " TIME_FORFEIT"
                    state["last_commentary"] = "%s lost on time." % who
                    print("FLAG", game_id, who, elapsed, flush=True)
                    break
                move = response.move
                if move not in board.legal_moves:
                    raise RuntimeError(
                        "Engine gave illegal move %r from %s, FEN=%s" %
                        (move, who, board.fen()))
                info = response.info
                san = board.san(move)
                capture = board.is_capture(move)
                castle = board.is_castling(move)
                check = board.gives_check(move)
                pv = format_pv(board, info)
                nodes = info.get("nodes")
                depth = info.get("depth")
                seldepth = info.get("seldepth")
                score = info.get("score")
                cp = None
                if score is not None:
                    try:
                        cp = score.pov(mover).score(mate_score=100000)
                    except (AttributeError, ValueError):
                        pass
                board.push(move)
                clocks[mover] += INCREMENT_SECONDS
                flags = []
                if capture:
                    flags.append("capture")
                if castle:
                    flags.append("castles")
                if check:
                    flags.append("check")
                outcome = board.outcome(claim_draw=True)
                if outcome:
                    flags.append("game finished by " + outcome.termination.name)
                annotation = (
                    "Game %02d, %d... %s by %s. " % (
                        game_id, board.ply(), san, who)
                    + ("; ".join(flags) + ". " if flags else "")
                    + ("Search: %s nodes, depth %s, seldepth %s; %.2f s. " %
                       (nodes if nodes is not None else "unreported",
                        depth if depth is not None else "unreported",
                        seldepth if seldepth is not None else "unreported",
                        elapsed))
                    + ("PV: %s" % pv if pv else ""))
                move_record = {
                    "ply": board.ply(), "san": san, "uci": move.uci(),
                    "player": who, "search_seconds": round(elapsed, 3),
                    "nodes": nodes, "depth": depth, "seldepth": seldepth,
                    "pv_san": pv, "cp_pov_mover": cp,
                    "white_clock": round(clocks[True], 3),
                    "black_clock": round(clocks[False], 3),
                    "commentary": annotation}
                timeline.append(move_record)
                state["last_move"] = move_record
                state["last_commentary"] = annotation
                print("PLY", game_id, board.ply(), who, san,
                      "elapsed", round(elapsed, 2), "nodes", nodes,
                      "depth", depth, "seldepth", seldepth, "pv", pv,
                      "white", round(clocks[True], 1),
                      "black", round(clocks[False], 1), flush=True)
                # Per-ChessGPT-move push: logs are readable even though
                # GitHub Actions API hides live job stdout until completion.
                if gpt_turn or outcome is not None:
                    update()
                if outcome:
                    result = outcome.result()
                    termination = outcome.termination.name
                    break
            if result is None:
                raise RuntimeError("No rules-based outcome after 800 plies")
        g = chess.pgn.Game.from_board(board)
        g.headers.update({
            "Event": "ChessGPT T1 MCTS vs Elite v2 nodes1, 90+30",
            "Site": "GitHub Actions 20 concurrent games",
            "Round": str(game_id),
            "White": state["white_player"], "Black": state["black_player"],
            "Result": result, "TimeControl": "5400+30",
            "Termination": termination,
            "OpeningBook": "Variety.bin verified, paired 8-ply",
            "EngineBuild": "ChessGPT 16L T1, classical SF16 values, PUCT"})
        pgnpath = out / ("game_%02d.pgn" % game_id)
        pgnpath.write_text(str(g) + "\n\n", encoding="utf-8")
        assert not g.errors
        assert g.headers["Result"] in {"1-0", "0-1", "1/2-1/2"}
        gpt_points = score_for_gpt(result, gpt_white)
        state.update({
            "state": "completed", "result": result,
            "termination": termination,
            "chessgpt_points": gpt_points, "elite_points": 1 - gpt_points,
            "game_duration_seconds": round(time.time() - startwall, 2),
            "last_commentary": "Game %d completed: %s (%s); ChessGPT %.1f, Elite %.1f." %
                               (game_id, result, termination, gpt_points, 1 - gpt_points)})
        (out / "status.json").write_text(
            json.dumps(state, indent=2), encoding="utf-8")
        print("GAME_RESULT", game_id, result, termination,
              "CHESSGPT_POINTS", gpt_points, "plies", board.ply(), flush=True)
        update()
        print("GAME_PGN_VERIFIED", game_id, pgnpath.name, flush=True)
    except Exception as exc:
        state["state"] = "failed"
        state["error"] = repr(exc)
        state["last_commentary"] = "Game %d interrupted by technical failure: %s. No chess result." % (
            game_id, repr(exc))
        update()
        print("GAME_TECHNICAL_FAILURE", game_id, repr(exc), flush=True)
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", type=int, required=True)
    args = ap.parse_args()
    assert 1 <= args.game <= 20
    play(args.game)


if __name__ == "__main__":
    main()
