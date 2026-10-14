#!/usr/bin/env python3
"""Two real Stockfish 17.1 depth-1 1024-thread self-play games with UCI node telemetry."""
import csv
import datetime as dt
import json
import os
from pathlib import Path
import time
import chess
import chess.engine
import chess.pgn
import chess.syzygy

ENGINE = "stockfish/stockfish-ubuntu-x86-64-avx2"
TB = Path("tablebases").resolve()
OUT = Path("results")
OUT.mkdir(exist_ok=True)
THREADS = 1024
HASH_MB = 16
OPENINGS = ("e4 e5 Nf3 Nc6", "d4 d5 c4 e6")
FIELDS = ["game","ply","side","fen_before","uci","san","nodes","nps","search_time_seconds",
          "wall_time_s","hashfull_permille","tbhits","reported_depth","seldepth"]

def write_json_atomic(path, obj):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf8") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)

def save_pgn(game, index):
    path = OUT / f"game_{index:02d}.pgn"
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf8") as f:
        print(game, file=f)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)

def main():
    w = list(TB.glob("*.rtbw"))
    z = list(TB.glob("*.rtbz"))
    assert len(w) == len(z) == 145, (len(w), len(z))
    with chess.syzygy.open_tablebase(str(TB)) as tablebase:
        for fen in ("7k/8/8/8/8/8/2Q5/K7 w - - 0 1",
                    "7k/6r1/8/8/8/8/RR6/K7 w - - 0 1"):
            board = chess.Board(fen)
            assert board.is_valid()
            print("VALIDATED_TABLEBASE", tablebase.probe_wdl(board), flush=True)

    completed = 0
    grand_nodes = 0
    start = time.monotonic()
    with (OUT/"per_move_nodes.csv").open("w", encoding="utf8", newline="", buffering=1) as f:
        csvout = csv.DictWriter(f, fieldnames=FIELDS)
        csvout.writeheader()
        f.flush()
        for index, opening in enumerate(OPENINGS, start=1):
            game = chess.pgn.Game()
            game.headers.update({
                "Event": "Stockfish 17.1 1024 Threads Hash 16 Node Telemetry",
                "Site": "Free GitHub Actions cloud runner",
                "Round": str(index),
                "Date": dt.datetime.now(dt.timezone.utc).strftime("%Y.%m.%d"),
                "White": "Stockfish 17.1 depth1 t1024 h16",
                "Black": "Stockfish 17.1 depth1 t1024 h16",
                "Depth": "1", "Threads": str(THREADS),
                "HashMB": str(HASH_MB), "MultiPV": "1",
                "SyzygyProbeLimit": "5", "Opening": opening,
                "Result": "*", "Termination": "IN_PROGRESS"})
            board = chess.Board()
            node = game
            for san in opening.split():
                move = board.parse_san(san)
                node = node.add_variation(move)
                board.push(move)
            save_pgn(game, index)
            game_nodes = 0
            max_hashfull = None
            with chess.engine.SimpleEngine.popen_uci(ENGINE, timeout=330) as engine:
                assert "17.1" in engine.id.get("name", ""), engine.id
                assert engine.options["Threads"].max >= 1024
                assert engine.options["Hash"].max >= HASH_MB
                engine.configure({
                    "Threads": THREADS, "Hash": HASH_MB,
                    "SyzygyPath": str(TB), "SyzygyProbeLimit": 5,
                })
                print("ENGINE_CONFIGURED", index, "Threads", THREADS,
                      "Hash", HASH_MB, "Depth", 1, "MultiPV", 1,
                      "Syzygy", len(w)+len(z), flush=True)
                for i in range(400):
                    outcome = board.outcome(claim_draw=True)
                    if outcome is not None:
                        break
                    before = board.fen()
                    turn = "White" if board.turn else "Black"
                    tick = time.monotonic()
                    result = engine.play(board, chess.engine.Limit(depth=1),
                                         info=chess.engine.INFO_ALL)
                    wall = time.monotonic() - tick
                    assert result.move is not None and result.move in board.legal_moves
                    info = result.info
                    assert info.get("depth") in (None, 1), info.get("depth")
                    nodes = info.get("nodes")
                    if nodes is None:
                        raise RuntimeError(f"Missing node telemetry game {index} ply {board.ply()}")
                    nodes = int(nodes)
                    h = info.get("hashfull")
                    h = int(h) if h is not None else None
                    if h is not None:
                        max_hashfull = h if max_hashfull is None else max(h, max_hashfull)
                    ply = board.ply()+1
                    move_san = board.san(result.move)
                    csvout.writerow({
                        "game": index, "ply": ply, "side": turn,
                        "fen_before": before, "uci": result.move.uci(),
                        "san": move_san, "nodes": nodes,
                        "nps": info.get("nps"), "search_time_seconds": info.get("time"),
                        "wall_time_s": round(wall, 4),
                        "hashfull_permille": h, "tbhits": info.get("tbhits"),
                        "reported_depth": info.get("depth"),
                        "seldepth": info.get("seldepth")})
                    f.flush()
                    os.fsync(f.fileno())
                    game_nodes += nodes
                    grand_nodes += nodes
                    board.push(result.move)
                    node = node.add_variation(result.move)
                    node.comment = (f"nodes={nodes} hashfull={h if h is not None else 'NA'} "
                                    f"wall_s={wall:.3f}")
                    save_pgn(game, index)
                    status = {
                        "target_games": len(OPENINGS), "completed_games": completed,
                        "current_game": index, "current_ply": ply,
                        "current_game_nodes": game_nodes, "total_nodes": grand_nodes,
                        "current_hashfull_peak_permille": max_hashfull,
                        "last_move": move_san, "last_move_nodes": nodes,
                        "last_move_wall_seconds": round(wall,3),
                        "elapsed_seconds": round(time.monotonic()-start,1),
                        "current_result": "*",
                    }
                    write_json_atomic(OUT/"status.json", status)
                    print("ACTUAL_MOVE", "game",index,"ply",ply,"san",move_san,
                          "nodes",nodes,"hashfull_permille",h,
                          "game_nodes",game_nodes,"wall_s",round(wall,3),
                          flush=True)
                else:
                    print("STOPPED_AT_400_PLIES_GAME_NOT_TERMINAL",index,flush=True)
                    continue
                game.headers["Result"] = outcome.result()
                game.headers["Termination"] = outcome.termination.name
                save_pgn(game, index)
                completed += 1
                status.update({
                    "completed_games": completed,
                    "current_result": outcome.result(),
                    "current_termination": outcome.termination.name,
                    "game_total_nodes": game_nodes,
                    "game_peak_hashfull_permille": max_hashfull,
                    "elapsed_seconds": round(time.monotonic()-start,1),
                })
                write_json_atomic(OUT/"status.json", status)
                print("VERIFIED_GAME",index,outcome.result(),outcome.termination.name,
                      "nodes",game_nodes,"peak_hashfull_permille",max_hashfull,
                      flush=True)
    print("FINAL", "completed_games",completed,"target_games",len(OPENINGS),
          "aggregate_nodes",grand_nodes,flush=True)

if __name__ == "__main__":
    main()
