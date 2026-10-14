#!/usr/bin/env python3
"""100 verified chess results: Stockfish 17.1 depth=1, Threads=1024, MultiPV=1, Hash 1 vs 131072 MiB."""
import csv
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import time

import chess
import chess.engine
import chess.pgn
import chess.syzygy

OPENINGS = """
e4 e5
e4 c5
e4 e6
e4 c6
e4 d5
e4 d6
e4 g6
e4 Nf6
e4 b6
e4 Nc6
d4 d5
d4 Nf6
d4 e6
d4 f5
d4 g6
d4 d6
d4 c5
d4 b6
c4 e5
c4 c5
c4 Nf6
c4 e6
c4 c6
c4 g6
Nf3 d5
Nf3 Nf6
Nf3 c5
Nf3 g6
Nf3 d6
g3 d5
g3 Nf6
b3 e5
b3 d5
f4 d5
f4 e5
b4 e5
b4 d5
Nc3 d5
Nc3 e5
e3 d5
e3 Nf6
d3 d5
d3 e5
c3 d5
c3 e5
e4 e5 Nf3 Nc6
e4 e5 Nf3 Nf6
e4 c5 Nf3 d6
d4 d5 c4 e6
d4 Nf6 c4 g6
""".strip().splitlines()
assert len(OPENINGS) == 50

OUT = Path("results")
OUT.mkdir(parents=True, exist_ok=True)
ENGINE = "stockfish/stockfish-ubuntu-x86-64-avx2"
TB = Path("tablebases").resolve()
THREADS = 1024
DEPTH = 1
NAMES = {1:"Stockfish 17.1 Hash 1", 131072:"Stockfish 17.1 Hash 131072"}
SCORE = {"hash1_wins":0, "hash131072_wins":0, "draws":0}
FIELDS = ["game","ply","hash_mb","side","uci","san","nodes","nps",
          "time_seconds","hashfull_permille","tbhits","reported_depth","seldepth"]

def atomic_json(obj):
    tmp=OUT/"status.tmp"
    with tmp.open("w",encoding="utf8") as f:
        json.dump(obj,f,indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(OUT/"status.json")

def atomic_current_pgn(game):
    tmp=OUT/"current_game.tmp"
    with tmp.open("w",encoding="utf8") as f:
        print(game,file=f,end="\n\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(OUT/"current_game.pgn")

def verify_tb():
    w=list(TB.glob("*.rtbw")); z=list(TB.glob("*.rtbz"))
    assert len(w)==145 and len(z)==145,("Syzygy table count",len(w),len(z))
    with chess.syzygy.open_tablebase(str(TB)) as t:
        for fen in ("7k/8/8/8/8/8/2Q5/K7 w - - 0 1",
                    "7k/6r1/8/8/8/8/2Q5/K7 w - - 0 1",
                    "7k/6r1/8/8/8/8/RR6/K7 w - - 0 1"):
            b=chess.Board(fen)
            assert b.is_valid()
            print("SYZYGY_TEST",b.occupied.bit_count(),t.probe_wdl(b),flush=True)
    print("VERIFIED_SYZYGY_ALL_290",flush=True)

def main():
    verify_tb()
    boards=[]
    for sanline in OPENINGS:
        b=chess.Board()
        for san in sanline.split():
            b.push_san(san)
        assert not b.is_game_over(claim_draw=True)
        boards.append(b)
    assert len(set(b.fen() for b in boards)) == 50, "Openings must be unique."
    subprocess.run(["free","-h"],check=False)
    started=time.monotonic()
    total_nodes=0
    print("ENGINE_REQUESTED",ENGINE, "depth",DEPTH,"threads",THREADS,
          "hash1",1,"hash2",131072,"multipv",1,flush=True)

    with chess.engine.SimpleEngine.popen_uci(ENGINE,timeout=3600) as small:
      with chess.engine.SimpleEngine.popen_uci(ENGINE,timeout=3600) as big:
        for engine,h in ((small,1),(big,131072)):
            assert "17.1" in engine.id.get("name",""),engine.id
            assert engine.options["Threads"].max >= THREADS
            assert engine.options["Hash"].max >= h
            # python-chess reserves MultiPV for play()/analyse(). Stockfish's UCI default
            # is one principal variation, so retain MultiPV=1 without configuring it.
            assert engine.options["MultiPV"].default == 1, "Stockfish MultiPV default is not 1"
            engine.configure({
                "Threads": THREADS,
                "Hash": h,
                "SyzygyPath": str(TB),
                "SyzygyProbeLimit": 5,
            })
            print("ENGINE_CONFIGURED", "hash",h,"threads",THREADS,
                  "multipv",1,flush=True)
        with (OUT/"per_move_nodes.csv").open("w",encoding="utf8",newline="",buffering=1) as fc:
          writer=csv.DictWriter(fc,fieldnames=FIELDS)
          writer.writeheader()
          with (OUT/"all_100_games.pgn").open("w",encoding="utf8",buffering=1) as pgn:
            for rnd in range(1,101):
                small_white=rnd%2==1
                white_h=1 if small_white else 131072
                black_h=131072 if small_white else 1
                b=boards[(rnd-1)//2].copy(stack=True)
                game=chess.pgn.Game()
                game.headers.update({
                    "Event":"SF171 depth1 1024threads MPV1 Hash1 vs Hash131072",
                    "Site":"Google Cloud Free Trial",
                    "Round":str(rnd),
                    "Date":dt.datetime.now(dt.timezone.utc).strftime("%Y.%m.%d"),
                    "White":NAMES[white_h],"Black":NAMES[black_h],
                    "WhiteHashMB":str(white_h),"BlackHashMB":str(black_h),
                    "Depth":"1","Threads":"1024","MultiPV":"1",
                    "SyzygyProbeLimit":"5",
                    "Opening":OPENINGS[(rnd-1)//2],
                    "HashPersistsAcrossGames":"True",
                    "Result":"*",
                })
                node=game
                for move in b.move_stack:
                    node=node.add_variation(move)
                game_nodes=0
                max_hash={1:None,131072:None}
                atomic_current_pgn(game)

                for i in range(800):
                    outcome=b.outcome(claim_draw=True)
                    if outcome is not None:
                        break
                    h=white_h if b.turn else black_h
                    side="White" if b.turn else "Black"
                    engine=small if h==1 else big
                    start_move=time.monotonic()
                    response=engine.play(b,chess.engine.Limit(depth=1),
                                         info=chess.engine.INFO_ALL)
                    wall=time.monotonic()-start_move
                    if response.move is None or response.move not in b.legal_moves:
                        raise RuntimeError("Illegal/no move in game %s ply %s"%(rnd,b.ply()+1))
                    info=response.info
                    # The UCI go command requests depth=1. Under 1024-thread Lazy SMP,
                    # helper threads may report a higher completed depth while the
                    # root limit is still 1. Preserve this telemetry rather than abort.
                    if info.get("depth") not in (None, 1):
                        print("DEPTH1_HELPER_REPORTED",rnd,b.ply()+1,info.get("depth"),flush=True)
                    nodes=info.get("nodes")
                    if nodes is None:
                        raise RuntimeError("Missing UCI nodes telemetry at game %d ply %d"%(rnd,b.ply()+1))
                    nodes=int(nodes)
                    hfull=info.get("hashfull")
                    hfull=int(hfull) if hfull is not None else None
                    if hfull is not None:
                        max_hash[h]=max(hfull,max_hash[h] or 0)
                    san=b.san(response.move)
                    ply=b.ply()+1
                    writer.writerow({
                        "game":rnd,"ply":ply,"hash_mb":h,
                        "side":side,"uci":response.move.uci(),"san":san,
                        "nodes":nodes,"nps":info.get("nps"),
                        "time_seconds":info.get("time",wall),
                        "hashfull_permille":hfull,
                        "tbhits":info.get("tbhits"),
                        "reported_depth":info.get("depth"),
                        "seldepth":info.get("seldepth")})
                    fc.flush()
                    os.fsync(fc.fileno())
                    game_nodes+=nodes
                    total_nodes+=nodes
                    b.push(response.move)
                    node=node.add_variation(response.move)
                    node.comment="nodes=%d hashfull=%s"%(nodes,hfull if hfull is not None else "NA")
                    if i%10==9:
                        atomic_current_pgn(game)
                        atomic_json({
                            "completed":rnd-1,"target":100,"score":SCORE.copy(),
                            "active_game":rnd,"active_ply":b.ply(),
                            "last_move":san,"last_move_nodes":nodes,
                            "game_nodes":game_nodes,"total_nodes":total_nodes,
                            "hashfull_peaks":max_hash,
                            "elapsed_seconds":round(time.monotonic()-started,2)})
                        print("PROGRESS",rnd,"ply",b.ply(),"nodes",game_nodes,
                              "last",san,"hash",h,"hashfull",hfull,
                              "elapsed",round(time.monotonic()-started),flush=True)
                else:
                    # Exactly 800 plies may reach a legal terminal outcome.
                    outcome=b.outcome(claim_draw=True)
                    if outcome is None:
                        raise RuntimeError("Unfinished game %d after 800 halfmoves"%rnd)

                if outcome is None:
                    outcome=b.outcome(claim_draw=True)
                if outcome is None:
                    raise RuntimeError("No verified result game %d"%rnd)
                if outcome.winner is None:
                    SCORE["draws"]+=1
                else:
                    winning_h=white_h if outcome.winner == chess.WHITE else black_h
                    SCORE["hash%d_wins"%winning_h]+=1
                game.headers["Result"]=outcome.result()
                game.headers["Termination"]=outcome.termination.name
                print(game,file=pgn,end="\n\n")
                pgn.flush()
                os.fsync(pgn.fileno())
                atomic_current_pgn(game)
                atomic_json({
                    "completed":rnd,"target":100,"score":SCORE.copy(),
                    "last_result":outcome.result(),
                    "last_termination":outcome.termination.name,
                    "game_nodes":game_nodes,"total_nodes":total_nodes,
                    "hashfull_peaks":max_hash,
                    "elapsed_seconds":round(time.monotonic()-started,2)})
                print("VERIFIED_GAME",rnd,outcome.result(),outcome.termination.name,
                      "game_nodes",game_nodes,"total_nodes",total_nodes,
                      "peak_hashfull",max_hash,
                      "score",SCORE,"elapsed",round(time.monotonic()-started,1),
                      flush=True)
    print("FINAL_SCORE",json.dumps(SCORE),"AGGREGATE_NODES",total_nodes,flush=True)

if __name__=="__main__":
    main()
