#!/usr/bin/env python3
"""Paired Stockfish 17.1 depth-1 EvalFile tournament. Technical games never count."""
import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

import chess
import chess.engine
import chess.pgn
import chess.polyglot

NETS = [
    "nn-1111cefa1111.nnue",
    "nn-1cedc0ffeeee.nnue",
    "nn-31337bea577c.nnue",
    "nn-e8bac1c07a5a.nnue",
    "nn-74f1d263ae9a.nnue",
    "nn-ddcfb9224cdb.nnue",
    "nn-c721dfca8cd3.nnue",
    "nn-808259761cca.nnue",
    "nn-a56cb8c3d477.nnue",
]
BASE = "nn-1c0000000000.nnue"
SMALL = "nn-37f18f62d772.nnue"

def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")

def initial_openings(bookpath, count, seed):
    rng = random.Random(seed)
    found = []
    seen = set()
    with chess.polyglot.open_reader(str(bookpath)) as book:
        for attempt in range(12000):
            if len(found) >= count:
                break
            board = chess.Board()
            for _ in range(12):
                entries = list(book.find_all(board))
                if not entries:
                    break
                entry = rng.choices(entries, [max(e.weight, 1) for e in entries])[0]
                if not board.is_legal(entry.move):
                    break
                board.push(entry.move)
                if board.is_game_over(claim_draw=True):
                    break
            key = tuple(m.uci() for m in board.move_stack)
            if len(key) >= 4 and key not in seen and not board.is_game_over(claim_draw=True):
                seen.add(key)
                found.append(key)
    if len(found) < count:
        raise RuntimeError("Variety book yielded only %d distinct valid openings, requested %d" % (len(found),count))
    return found

def start_engine(binary, evalfile, smallfile):
    eng = chess.engine.SimpleEngine.popen_uci(str(binary), timeout=45)
    try:
        if "Stockfish 17.1" not in eng.id.get("name", ""):
            raise RuntimeError("Wrong executable: %r" % eng.id)
        eng.configure({"Threads":1, "Hash":16, "EvalFile":str(evalfile), "EvalFileSmall":str(smallfile)})
        eng.ping()
        result = eng.play(chess.Board(), chess.engine.Limit(depth=1), game=object())
        if not result.move:
            raise RuntimeError("Network preflight returned no move")
        return eng
    except Exception:
        eng.quit()
        raise

def finish_game(white, black, opening, name, pair, reverse, maxplies, game_log):
    start = chess.Board()
    game = chess.pgn.Game()
    game.headers["Event"] = "Stockfish 17.1 EvalFile depth1 / Threads1"
    game.headers["Site"] = "GitHub Actions Ubuntu runner"
    game.headers["Round"] = "%s.%d.%d" % (name[:15], pair+1,reverse+1)
    game.headers["White"] = "SF17.1 " + white[0]
    game.headers["Black"] = "SF17.1 " + black[0]
    game.headers["OpeningPair"] = str(pair+1)
    game.headers["SearchDepth"] = "1"
    game.headers["Threads"] = "1"
    game.headers["Hash"] = "16"
    node = game
    board = start
    for move_str in opening:
        move = chess.Move.from_uci(move_str)
        node = node.add_variation(move)
        board.push(move)
    token = object()
    steps = 0
    error = None
    while not board.is_game_over(claim_draw=True) and steps < maxplies:
        player = white if board.turn == chess.WHITE else black
        try:
            result = player[1].play(board, chess.engine.Limit(depth=1), game=token, info=chess.engine.INFO_NONE)
            if result.move is None or not board.is_legal(result.move):
                raise RuntimeError("Engine produced an invalid move: %r" % result.move)
        except Exception as exc:
            error = "engine_error: %s: %s" % (type(exc).__name__, exc)
            break
        node = node.add_variation(result.move)
        board.push(result.move)
        steps += 1
    outcome = board.outcome(claim_draw=True)
    if error:
        return None, error, steps
    if outcome is None:
        return None, "unfinished_ply_cap_%d" % maxplies, steps
    game.headers["Result"] = outcome.result()
    game_log.write(str(game) + "\n\n")
    game_log.flush()
    return outcome.result(), None, steps

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, type=Path)
    ap.add_argument("--nets", required=True, type=Path)
    ap.add_argument("--book", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--pairs", type=int, default=20)
    ap.add_argument("--maxplies", type=int, default=260)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    allnets = [BASE, SMALL] + NETS
    headers = {}
    for name in allnets:
        path = args.nets/name
        if not path.is_file():
            raise RuntimeError("Missing network: "+str(path))
        with path.open("rb") as f:
            header = f.read(12)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest[:12] != name[3:15]:
            raise RuntimeError("NNUE sha256 mismatch "+name+" "+digest)
        headers[name] = {"bytes":path.stat().st_size, "header":header.hex(), "sha256":digest}
    save_json(args.out/"network_integrity.json", headers)
    openings = initial_openings(args.book, args.pairs, 20261019)
    save_json(args.out/"paired_variety_openings.json", openings)
    summary = {"engine":"Stockfish 17.1 (unmodified binary)", "threads":1,
               "depth":1, "hash_mb":16, "small_net":SMALL,
               "default_net":BASE, "opening_book":"Variety.bin",
               "pairs_per_opponent":args.pairs, "results":{}}
    errors = []
    print("PREFLIGHT: baseline network and all candidate files SHA-256 verified", flush=True)
    baseline = start_engine(args.engine, (args.nets/BASE).resolve(), (args.nets/SMALL).resolve())
    try:
        with (args.out/"all_games.pgn").open("w",encoding="utf8") as games, (args.out/"game_results.jsonl").open("w",encoding="utf8") as jsonl:
            for name in NETS:
                start = time.monotonic()
                result = {"wins":0, "draws":0, "losses":0, "technical":0,"completed":0,"network_loaded":False}
                summary["results"][name] = result
                save_json(args.out/"summary.json", summary)
                try:
                    candidate = start_engine(args.engine,(args.nets/name).resolve(),(args.nets/SMALL).resolve())
                    result["network_loaded"] = True
                except Exception as exc:
                    result["load_error"] = "%s: %s"%(type(exc).__name__,exc)
                    print("INCOMPATIBLE",name,result["load_error"],flush=True)
                    save_json(args.out/"summary.json",summary)
                    continue
                try:
                    for i,opening in enumerate(openings):
                        for reverse in (0,1):
                            is_white = (reverse==0)
                            white = (name,candidate) if is_white else (BASE,baseline)
                            black = (BASE,baseline) if is_white else (name,candidate)
                            score,technical,plies=finish_game(white,black,opening,name,i,reverse,args.maxplies,games)
                            record={"candidate":name,"pair":i+1,"candidate_white":is_white,"score":score,"technical_error":technical,"played_plies":plies}
                            if technical:
                                result["technical"] += 1
                                errors.append(record)
                                print("TECHNICAL",json.dumps(record),flush=True)
                            else:
                                result["completed"] += 1
                                if score=="1/2-1/2":result["draws"] += 1
                                elif (score=="1-0") == is_white:result["wins"] += 1
                                else:result["losses"] += 1
                            jsonl.write(json.dumps(record)+"\n")
                            jsonl.flush()
                            save_json(args.out/"summary.json",summary)
                        if (i+1)%5==0:
                            print("PROGRESS", name, i+1, "pairs",result,flush=True)
                finally:
                    candidate.quit()
                result["elapsed_seconds"] = round(time.monotonic()-start,2)
                print("FINAL",name,json.dumps(result),flush=True)
                save_json(args.out/"summary.json",summary)
    finally:
        baseline.quit()
    save_json(args.out/"technical_games.json",errors)
    print("TOURNAMENT_COMPLETED",json.dumps(summary["results"]),flush=True)

if __name__=="__main__":
    main()
