#!/usr/bin/env python3
"""100 chess-rule-verified Stockfish 17.1 depth1 t1 MPV1 hash64 vs hash131072."""
import datetime
import json
import os
from pathlib import Path
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

OUT=Path("results")
OUT.mkdir(exist_ok=True)
ENGINE="stockfish/stockfish-ubuntu-x86-64-avx2"
TB=Path("tablebases").resolve()
NAMES={64:"Stockfish 17.1 Hash 64",131072:"Stockfish 17.1 Hash 131072"}
SCORE={"hash64_wins":0,"hash131072_wins":0,"draws":0}

def persist(state):
    q=OUT/"status.tmp"
    with q.open("w",encoding="utf8") as f:
        json.dump(state,f,indent=2)
        f.flush()
        os.fsync(f.fileno())
    q.replace(OUT/"status.json")

def verify_tb():
    w=list(TB.glob("*.rtbw")); z=list(TB.glob("*.rtbz"))
    assert len(w)==145 and len(z)==145,(len(w),len(z))
    with chess.syzygy.open_tablebase(str(TB)) as t:
        for fen in ["7k/8/8/8/8/8/2Q5/K7 w - - 0 1","7k/6r1/8/8/8/8/2Q5/K7 w - - 0 1","7k/6r1/8/8/8/8/RR6/K7 w - - 0 1"]:
            b=chess.Board(fen)
            assert b.is_valid()
            print("SYZYGY_PROBE",b.occupied.bit_count(),"WDL",t.probe_wdl(b),flush=True)
    print("VERIFIED_FULL_3_4_5_SYZYGY",len(w)+len(z),flush=True)

def make_boards():
    out=[]
    for opening in OPENINGS:
        b=chess.Board()
        for san in opening.split(): b.push_san(san)
        if b.is_game_over(claim_draw=True): raise ValueError("Invalid opening "+opening)
        out.append(b)
    if len(out)!=50 or len({b.fen() for b in out})!=50:
        raise ValueError("Need 50 distinct opening positions")
    return out

def main():
    verify_tb()
    boards=make_boards()
    import subprocess
    subprocess.run(["free","-h"],check=False)
    subprocess.run(["swapon","--show"],check=False)
    start=time.monotonic()
    print("START",datetime.datetime.now(datetime.timezone.utc).isoformat(),flush=True)
    print("ENGINE",ENGINE,"Hash MB",64,131072,"Threads",1,"Depth",1,"MultiPV",1,flush=True)
    # Spawn exactly two engines and retain both TT caches between games.
    # A fresh 128 GiB table per game would require 12.8 TiB of avoidable swap writes.
    with chess.engine.SimpleEngine.popen_uci(ENGINE,timeout=3600) as small:
      with chess.engine.SimpleEngine.popen_uci(ENGINE,timeout=3600) as big:
        for engine,h in ((small,64),(big,131072)):
            if "17.1" not in engine.id.get("name",""):
                raise RuntimeError("Wrong engine "+str(engine.id))
            if engine.options["Hash"].max < h:
                raise RuntimeError("Hash value unsupported "+str(h))
            engine.configure({"Threads":1,"MultiPV":1,"Hash":h,
                              "SyzygyPath":str(TB),"SyzygyProbeLimit":5})
            print("ENGINE_CONFIGURED",h,str(engine.id),flush=True)
        with (OUT/"all_100_games.pgn").open("w",encoding="utf-8",buffering=1) as out:
            for rnd in range(1,101):
                small_white=rnd%2==1
                white_hash=64 if small_white else 131072
                black_hash=131072 if small_white else 64
                board=boards[(rnd-1)//2].copy(stack=True)
                game=chess.pgn.Game()
                game.headers.update({
                  "Event":"Stockfish 17.1 d1 t1 MultiPV1 Hash64 vs Hash131072",
                  "Site":"GitHub-hosted Linux",
                  "Round":str(rnd),
                  "Date":datetime.datetime.now(datetime.timezone.utc).strftime("%Y.%m.%d"),
                  "White":NAMES[white_hash],"Black":NAMES[black_hash],
                  "WhiteHashMB":str(white_hash),"BlackHashMB":str(black_hash),
                  "Opening":OPENINGS[(rnd-1)//2],
                  "Depth":"1","Threads":"1","MultiPV":"1",
                  "SyzygyProbeLimit":"5",
                  "HashPersistAcrossGames":"True"})
                node=game
                for move in board.move_stack:
                    node=node.add_variation(move)
                for i in range(800):
                    result=board.outcome(claim_draw=True)
                    if result is not None:
                        break
                    h=(white_hash if board.turn==chess.WHITE else black_hash)
                    e=small if h==64 else big
                    answer=e.play(board,chess.engine.Limit(depth=1),
                                  info=chess.engine.INFO_ALL)
                    if answer.move is None or answer.move not in board.legal_moves:
                        raise RuntimeError(f"Missing/illegal move at game {rnd} ply {board.ply()}")
                    # The UCI "go depth 1" command, not node limit 1.
                    if answer.info.get("depth") not in (None,1):
                        raise RuntimeError(f"Depth violation: {answer.info.get('depth')}")
                    board.push(answer.move)
                    node=node.add_variation(answer.move)
                    if i%20==19:
                        print("PROGRESS",rnd,"ply",board.ply(),"hash",h,
                              "elapsed",round(time.monotonic()-start,1),flush=True)
                else:
                    raise RuntimeError(f"Game {rnd} has no valid terminal result after 800 halfmoves")
                if result is None:
                    raise RuntimeError(f"Unverified result in game {rnd}")
                if result.winner is None: SCORE["draws"]+=1
                else:
                    winner_hash=white_hash if result.winner==chess.WHITE else black_hash
                    SCORE[f"hash{winner_hash}_wins"]+=1
                game.headers["Result"]=result.result()
                game.headers["Termination"]=result.termination.name
                print(game,file=out,end="\n\n",flush=True)
                os.fsync(out.fileno())
                state={"completed":rnd,"target":100,"score":SCORE.copy(),
                       "last_result":result.result(),"termination":result.termination.name,
                       "elapsed_seconds":round(time.monotonic()-start,1)}
                persist(state)
                print("VERIFIED_GAME",rnd,result.result(),result.termination.name,
                      SCORE,"elapsed",state["elapsed_seconds"],flush=True)
        print("FINAL_SCORE",json.dumps(SCORE),"100 LEGITIMATE CHESS RESULTS",flush=True)

if __name__=="__main__": main()
