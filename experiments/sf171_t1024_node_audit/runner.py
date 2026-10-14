#!/usr/bin/env python3
"""Empirical UCI nodes and hashfull instrumentation: Stockfish 17.1, depth1, 1024 threads."""
from collections import defaultdict
from pathlib import Path
import datetime
import json
import os
import time
import chess
import chess.engine
import chess.pgn
import chess.syzygy

OUT=Path("node_audit_results")
OUT.mkdir(exist_ok=True)
ENGINE="stockfish/stockfish-ubuntu-x86-64-avx2"
TB=Path("tablebases").resolve()
H=(1,256)
THREADS=1024
GAMES=2
MAX_PLY=300

def store(name,obj):
    p=OUT/name
    with p.open("w",encoding="utf8") as f:
        json.dump(obj,f,indent=2)
        f.flush()
        os.fsync(f.fileno())

def main():
    wdls=list(TB.glob("*.rtbw")); dtzs=list(TB.glob("*.rtbz"))
    assert len(wdls)==145 and len(dtzs)==145, (len(wdls),len(dtzs))
    with chess.syzygy.open_tablebase(str(TB)) as t:
        board=chess.Board("7k/8/8/8/8/8/2Q5/K7 w - - 0 1")
        print("SYZYGY_3MAN",t.probe_wdl(board),flush=True)
    elapsed_start=time.monotonic()
    print("SETTINGS",dict(engine="Stockfish 17.1",threads=THREADS,depth=1,hash_mb=H,tablebases=5,multipv=1,games=GAMES),flush=True)
    summaries=[]
    with chess.engine.SimpleEngine.popen_uci(ENGINE,timeout=900) as small, chess.engine.SimpleEngine.popen_uci(ENGINE,timeout=900) as large:
        engines={1:small,256:large}
        for h,e in engines.items():
            assert "17.1" in e.id.get("name","")
            assert e.options["Threads"].max >= THREADS
            e.configure({"Threads":THREADS,"Hash":h,"SyzygyPath":str(TB),"SyzygyProbeLimit":5})
            print("CONFIGURED",h,"Threads",THREADS,flush=True)
        with (OUT/"nodes_per_move.jsonl").open("w",encoding="utf8",buffering=1) as log, (OUT/"sample_games.pgn").open("w",encoding="utf8",buffering=1) as pg:
            for idx in range(1,GAMES+1):
                wh,bh=(1,256) if idx%2 else (256,1)
                board=chess.Board()
                game=chess.pgn.Game()
                game.headers.update({"Event":"Stockfish 17.1 t1024 depth1 empirical node audit","Site":"GitHub free runner","Round":str(idx),"White":f"SF17.1 Hash{wh} t1024","Black":f"SF17.1 Hash{bh} t1024","WhiteHashMB":str(wh),"BlackHashMB":str(bh),"Threads":"1024","Depth":"1","MultiPV":"1","SyzygyProbeLimit":"5"})
                for e in engines.values():
                    e.configure({"Clear Hash":None})
                sums={1:0,256:0}
                maximum_hashfull={1:0,256:0}
                node=game
                for ply in range(1,MAX_PLY+1):
                    outcome=board.outcome(claim_draw=True)
                    if outcome is not None:
                        break
                    side=board.turn
                    h=wh if side==chess.WHITE else bh
                    t0=time.monotonic()
                    reply=engines[h].play(board,chess.engine.Limit(depth=1),info=chess.engine.INFO_ALL)
                    assert reply.move is not None and reply.move in board.legal_moves,(idx,ply,reply.move)
                    n=reply.info.get("nodes")
                    assert isinstance(n,int) and n>0,("MISSING_NODES",idx,ply,reply.info)
                    assert reply.info.get("depth") in (1,None),reply.info
                    hf=reply.info.get("hashfull")
                    sums[h]+=n
                    if isinstance(hf,int):maximum_hashfull[h]=max(maximum_hashfull[h],hf)
                    board.push(reply.move)
                    node=node.add_variation(reply.move)
                    m={"game":idx,"ply":ply,"hash_mb":h,"nodes":n,"hashfull_permille":hf,"depth":reply.info.get("depth"),"seconds":round(time.monotonic()-t0,3),"move":reply.move.uci()}
                    print("MOVE",json.dumps(m,separators=(",",":")),flush=True)
                    log.write(json.dumps(m)+"\n")
                outcome=board.outcome(claim_draw=True)
                if outcome is not None:
                    game.headers["Result"]=outcome.result()
                    game.headers["Termination"]=outcome.termination.name
                else:
                    game.headers["Result"]="*"
                    game.headers["Termination"]="MAX_PLY_INCOMPLETE"
                print(game,file=pg,end="\n\n",flush=True)
                summ={"game":idx,"completed":outcome is not None,"plies":board.ply(),"result":game.headers["Result"],"hash_nodes":sums,"nodes_total":sum(sums.values()),"max_hashfull":maximum_hashfull,"elapsed":round(time.monotonic()-elapsed_start,2)}
                summaries.append(summ)
                store("summary.json",summaries)
                print("GAME_NODES",json.dumps(summ),flush=True)
    print("BENCHMARK_FINAL",json.dumps(summaries),flush=True)

if __name__=="__main__": main()
