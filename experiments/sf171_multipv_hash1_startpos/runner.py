#!/usr/bin/env python3
"""Stockfish 17.1 depth1 Threads1024 Hash1 MiB MultiPV1 versus MultiPV256; all games from standard start."""
import argparse
import datetime
import json
import os
from pathlib import Path
import time
import chess
import chess.engine
import chess.pgn
import chess.syzygy

EXE="stockfish/stockfish-ubuntu-x86-64-avx2"
TB=Path("tablebases").resolve()
MAX_PLIES=800

def verify_tablebases():
    w=list(TB.glob("*.rtbw")); z=list(TB.glob("*.rtbz"))
    if len(w)!=145 or len(z)!=145:
        raise RuntimeError("Missing five-piece WDL/DTZ Syzygy: "+repr((len(w),len(z))))
    with chess.syzygy.open_tablebase(str(TB)) as t:
        for fen in ("7k/8/8/8/8/8/2Q5/K7 w - - 0 1",
                    "7k/6r1/8/8/8/8/2Q5/K7 w - - 0 1",
                    "7k/6r1/8/8/8/8/RR6/K7 w - - 0 1"):
            b=chess.Board(fen)
            assert b.is_valid() and t.probe_wdl(b)==2
    print("FULL_5_PIECE_SYZYGY_VERIFIED",len(w)+len(z),flush=True)

def record_json(path,data):
    tmp=path.with_suffix(".tmp")
    with tmp.open("w",encoding="utf-8") as fh:
        json.dump(data,fh,indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)

def move_by_engine(engine,board,mpv):
    # Use one 1024-thread Stockfish process. Running two such processes at once
    # caused all cloud jobs to be terminated before a single legal move.
    # Explicit MultiPV per search and clear its 1 MiB TT before every move,
    # eliminating any cross-player TT leakage.
    engine.configure({"Clear Hash":None})
    answers=engine.analyse(board,chess.engine.Limit(depth=1),multipv=mpv,
                           info=chess.engine.INFO_ALL)
    if not isinstance(answers,list) or not answers or not answers[0].get("pv"):
        raise RuntimeError("MultiPV %d failed to return principal variation"%mpv)
    move=answers[0]["pv"][0]
    return move,answers[0].get("depth"),len(answers)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--start",type=int,required=True)
    parser.add_argument("--count",type=int,default=5)
    args=parser.parse_args()
    if args.start<1 or args.start>100 or args.count<1 or args.start+args.count>101:
        raise ValueError("Invalid game shard")
    print("START_SHARD",args.start,args.count,datetime.datetime.now(datetime.timezone.utc).isoformat(),flush=True)
    verify_tablebases()
    out=Path("results")/("games_%03d_%03d"%(args.start,args.start+args.count-1))
    out.mkdir(parents=True,exist_ok=True)
    totals={"multipv1_wins":0,"multipv256_wins":0,"draws":0}
    start=time.monotonic()
    engines=[]
    try:
        e=chess.engine.SimpleEngine.popen_uci(EXE,timeout=600)
        engines.append(e)
        assert "17.1" in e.id.get("name",""),e.id
        assert e.options["Hash"].min==1
        assert e.options["Threads"].max>=1024
        assert e.options["MultiPV"].max>=256
        e.configure({"Hash":1,"Threads":1024,"SyzygyPath":str(TB),
                     "SyzygyProbeLimit":5})
        print("CONFIGURED_SINGLE_PROCESS","Hash 1","Threads 1024","Depth 1",
              "Switch MultiPV 1 or 256 per move","Clear hash before each move",
              "Syzygy 5",str(e.id),flush=True)
        named={1:"Stockfish 17.1 MultiPV 1",256:"Stockfish 17.1 MultiPV 256"}
        pgn=out/"games.pgn"
        with pgn.open("w",encoding="utf-8",buffering=1) as output:
            for rnd in range(args.start,args.start+args.count):
                mpv_white=1 if rnd%2==1 else 256
                mpv_black=256 if rnd%2==1 else 1
                b=chess.Board()
                assert b.fen()==chess.STARTING_FEN
                g=chess.pgn.Game()
                g.headers.update({
                  "Event":"Stockfish 17.1 depth1 Threads1024 Hash1 MultiPV1 vs MultiPV256",
                  "Site":"Cloud Linux",
                  "Round":str(rnd),
                  "Date":datetime.datetime.now(datetime.timezone.utc).strftime("%Y.%m.%d"),
                  "White":named[mpv_white],"Black":named[mpv_black],
                  "HashWhiteMB":"1","HashBlackMB":"1",
                  "MultiPVWhite":str(mpv_white),"MultiPVBlack":str(mpv_black),
                  "Threads":"1024","Depth":"1","SyzygyProbeLimit":"5",
                  "InitialPosition":"standard chess starting position",
                  "HashPersistAcrossGames":"False",
                  "HashClearedBeforeEachMove":"True"
                })
                node=g
                for ply in range(MAX_PLIES):
                    result=b.outcome(claim_draw=True)
                    if result is not None: break
                    mpv=mpv_white if b.turn==chess.WHITE else mpv_black
                    engine=e
                    move,depth,lines=move_by_engine(engine,b,mpv)
                    if move is None or move not in b.legal_moves:
                        raise RuntimeError("Illegal/null move game=%d ply=%d mpv=%d"%(rnd,ply,mpv))
                    if depth not in (None,1):
                        raise RuntimeError("Wrong depth %r at game %d ply %d"%(depth,rnd,ply))
                    b.push(move)
                    node=node.add_variation(move)
                    if (ply+1)%10==0:
                        print("PROGRESS","game",rnd,"ply",ply+1,"mpv",mpv,
                              "pvlines",lines,"elapsed",round(time.monotonic()-start,1),flush=True)
                else:
                    raise RuntimeError("Game %d exceeded %d halfmoves without legal result"%(rnd,MAX_PLIES))
                if result is None: raise RuntimeError("Unverified end game %d"%rnd)
                g.headers["Result"]=result.result()
                g.headers["Termination"]=result.termination.name
                if result.winner is None: totals["draws"]+=1
                else:
                    winner=mpv_white if result.winner==chess.WHITE else mpv_black
                    totals["multipv%d_wins"%winner]+=1
                print(g,file=output,end="\n\n",flush=True)
                output.flush()
                os.fsync(output.fileno())
                status={"completed":sum(totals.values()),"from_round":args.start,
                        "through_round":rnd,"target":args.count,"score":totals,
                        "last_result":result.result(),"termination":result.termination.name,
                        "elapsed_seconds":round(time.monotonic()-start,1)}
                record_json(out/"status.json",status)
                print("VERIFIED_GAME",rnd,status,flush=True)
        print("SHARD_COMPLETE",args.start,args.count,totals,flush=True)
    finally:
        for e in engines:
            try: e.quit()
            except Exception: pass

if __name__=="__main__": main()
