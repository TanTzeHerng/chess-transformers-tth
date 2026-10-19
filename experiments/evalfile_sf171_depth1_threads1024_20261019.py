#!/usr/bin/env python3
"""Stockfish 17.1 EvalFile comparison at depth 1 and precisely 1024 UCI threads.
Runs paired Variety openings; crashes and unfinished games are technical and excluded.
"""
import argparse
import hashlib
import json
import os
import random
import statistics
import time
from pathlib import Path

import chess
import chess.engine
import chess.pgn
import chess.polyglot

CANDIDATES=("nn-e8bac1c07a5a.nnue", "nn-ddcfb9224cdb.nnue")
BASE="nn-1c0000000000.nnue"
SMALL="nn-37f18f62d772.nnue"
THREADS=1024
DEPTH=1
HASH_MB=16

def save_json(path, value):
    temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(json.dumps(value,indent=2),encoding="utf-8")
    temp.replace(path)

def openings(bookpath,n,seed):
    rng=random.Random(seed)
    positions=[]
    seen=set()
    with chess.polyglot.open_reader(str(bookpath)) as book:
        for attempt in range(max(50000, n*80)):
            if len(positions)>=n:
                break
            board=chess.Board()
            for ply in range(12):
                entries=list(book.find_all(board))
                if not entries:break
                entry=rng.choices(entries,weights=[max(1,e.weight) for e in entries])[0]
                if not board.is_legal(entry.move):break
                board.push(entry.move)
                if board.is_game_over(claim_draw=True):break
            history=tuple(m.uci() for m in board.move_stack)
            if len(history)>=4 and history not in seen and not board.is_game_over(claim_draw=True):
                positions.append(history)
                seen.add(history)
    if len(positions)<n:
        raise RuntimeError(f"Variety book has {len(positions)} valid distinct openings, requested {n}")
    return positions

def start_engine(binary, large, small, label, logs):
    start=time.monotonic()
    print("ENGINE_START",label,"Threads="+str(THREADS),flush=True)
    engine=chess.engine.SimpleEngine.popen_uci(str(binary),timeout=240)
    try:
        if "Stockfish 17.1" not in engine.id.get("name",""):
            raise RuntimeError("Wrong Stockfish executable "+str(engine.id))
        if engine.options["Threads"].max<THREADS:
            raise RuntimeError("Engine doesn't support Threads=1024")
        engine.configure({"Threads":THREADS,"Hash":HASH_MB,"EvalFile":str(large),"EvalFileSmall":str(small)})
        engine.ping()
        result=engine.play(chess.Board(),chess.engine.Limit(depth=DEPTH),game=object(),info=chess.engine.INFO_NONE)
        if result.move is None:raise RuntimeError("preflight no move")
        record={"name":label,"elapsed_seconds":round(time.monotonic()-start,3),
                "preflight_move":result.move.uci(),
                "config":{"Threads":THREADS,"Hash":HASH_MB,"EvalFile":str(large),"EvalFileSmall":str(small)}}
        logs.append(record)
        print("ENGINE_READY",json.dumps(record),flush=True)
        return engine
    except Exception:
        try:engine.quit()
        except Exception:pass
        raise

def run_game(white,black,opening,round_id,game_index,candidate_name,maxplies):
    board=chess.Board()
    game=chess.pgn.Game()
    game.headers.update({"Event":"Stockfish 17.1 NNUE eval at depth1 Threads1024",
        "Site":"GitHub Actions Ubuntu runner",
        "White":white[0],"Black":black[0],"Round":str(round_id),
        "OpeningPair":str(round_id),"SearchDepth":str(DEPTH),
        "Threads":str(THREADS),"Hash":str(HASH_MB),
        "CandidateNet":candidate_name,
        "EvalFileSmall":SMALL})
    node=game
    for move_u in opening:
        move=chess.Move.from_uci(move_u)
        if not board.is_legal(move):
            return {"technical":"invalid_book_move","pgn":None,"plies":0}
        board.push(move);node=node.add_variation(move)
    game_token=object()
    elapsed=0.0
    max_move=0.0
    plies=0
    technical=None
    while not board.is_game_over(claim_draw=True) and plies<maxplies:
        engine=white[1] if board.turn==chess.WHITE else black[1]
        t=time.monotonic()
        try:
            result=engine.play(board,chess.engine.Limit(depth=DEPTH),game=game_token,info=chess.engine.INFO_NONE)
            if result.move is None or not board.is_legal(result.move):
                raise RuntimeError(f"Illegal/no move {result.move}")
            node=node.add_variation(result.move)
            board.push(result.move)
        except Exception as exc:
            technical=f"{type(exc).__name__}: {exc}"
            break
        delta=time.monotonic()-t
        elapsed+=delta;max_move=max(max_move,delta)
        plies+=1
    if technical is None and not board.is_game_over(claim_draw=True):
        technical="unfinished_at_ply_cap_"+str(maxplies)
    if technical:
        return {"technical":technical,"pgn":None,"plies":plies,
                "elapsed_seconds":round(elapsed,3),"longest_move_seconds":round(max_move,3)}
    outcome=board.outcome(claim_draw=True)
    if outcome is None:return {"technical":"no_outcome","pgn":None,"plies":plies}
    game.headers["Result"]=outcome.result()
    return {"technical":None,"pgn":str(game)+"\n\n","plies":plies,
            "elapsed_seconds":round(elapsed,3),"longest_move_seconds":round(max_move,3),
            "result":outcome.result()}

def points(score,as_white):
    if score=="1/2-1/2":return .5
    return float((score=="1-0")==as_white)

def los(paired):
    if len(paired)<2:return None
    # Paired-point scores [0,0.25,0.5,0.75,1.0], using Student-t approximation.
    from scipy.stats import t
    mu=statistics.mean(paired)
    sd=statistics.stdev(paired)
    if sd==0:return 1 if mu>.5 else 0 if mu<.5 else .5
    return float(t.cdf((mu-.5)/(sd/(len(paired)**.5)),df=len(paired)-1))

def main():
    parser=argparse.ArgumentParser()
    for name in ("engine","nets","book","out"):
        parser.add_argument("--"+name,required=True,type=Path)
    parser.add_argument("--pairs",type=int,default=12)
    parser.add_argument("--maxplies",type=int,default=260)
    parser.add_argument("--seconds",type=int,default=11400)
    args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    integrity={}
    for name in [BASE,SMALL,*CANDIDATES]:
        file=args.nets/name
        with file.open("rb") as f:h=hashlib.file_digest(f,"sha256").hexdigest()
        if h[:12]!=name[3:15]:raise RuntimeError("SHA mismatch "+name)
        integrity[name]={"bytes":file.stat().st_size,"sha256":h}
    save_json(args.out/"network_integrity.json",integrity)
    opening_pool=openings(args.book,args.pairs,20261021)
    save_json(args.out/"paired_openings.json",opening_pool)
    summary={"engine":"unmodified Stockfish 17.1","depth":DEPTH,"threads":THREADS,
        "hash_mb":HASH_MB,"opening_book":"Variety.bin","small_net":SMALL,
        "baseline":BASE,"candidates":list(CANDIDATES),"pairs_requested":args.pairs,
        "completed_games":0,"technical_games":0,"results":{},"engine_startup":[]}
    save_json(args.out/"summary.json",summary)
    deadline=time.monotonic()+args.seconds
    base=None
    with (args.out/"games.pgn").open("w",encoding="utf-8") as fpg, (args.out/"games.jsonl").open("w",encoding="utf-8") as flog:
        try:
            base=start_engine(args.engine,(args.nets/BASE).resolve(),(args.nets/SMALL).resolve(),
                BASE,summary["engine_startup"])
            for candidate in CANDIDATES:
                row={"wins":0,"draws":0,"losses":0,"complete_pairs":0,
                     "technical_pairs":0,"los":None,"paired_scores":[]}
                summary["results"][candidate]=row
                save_json(args.out/"summary.json",summary)
                champ=None
                try:
                    champ=start_engine(args.engine,(args.nets/candidate).resolve(),
                        (args.nets/SMALL).resolve(),candidate,summary["engine_startup"])
                    for i,opening in enumerate(opening_pool):
                        if time.monotonic()>deadline:
                            print("TIME_BUDGET_REACHED",candidate,i,flush=True)
                            break
                        group=[]
                        for reverse in (False,True):
                            as_white=not reverse
                            white=(candidate,champ) if as_white else (BASE,base)
                            black=(BASE,base) if as_white else (candidate,champ)
                            game=run_game(white,black,opening,i+1,int(reverse),candidate,args.maxplies)
                            group.append({"candidate_white":as_white,**game})
                        if any(g["technical"] for g in group):
                            row["technical_pairs"]+=1
                            summary["technical_games"]+=sum(bool(g["technical"]) for g in group)
                            print("TECHNICAL_PAIR",candidate,i+1,
                                  [g["technical"] for g in group],flush=True)
                        else:
                            paired_score=sum(points(g["result"],g["candidate_white"]) for g in group)/2
                            row["paired_scores"].append(paired_score)
                            row["complete_pairs"]+=1
                            for g in group:
                                score=points(g["result"],g["candidate_white"])
                                if score==1:row["wins"]+=1
                                elif score==0:row["losses"]+=1
                                else:row["draws"]+=1
                                fpg.write(g.pop("pgn"))
                            row["los"]=los(row["paired_scores"])
                            summary["completed_games"]+=2
                        flog.write(json.dumps({"candidate":candidate,"pair":i+1,"games":[{
                            k:v for k,v in g.items() if k!="pgn"} for g in group]})+"\n")
                        flog.flush();fpg.flush()
                        row["score_pct"]=round(100*(row["wins"]+.5*row["draws"])/max(1,2*row["complete_pairs"]),4)
                        print("PAIR",candidate,i+1,"WDL",row["wins"],row["draws"],row["losses"],
                            "LOS",row["los"],"seconds",sum(g.get("elapsed_seconds",0) for g in group),flush=True)
                        save_json(args.out/"summary.json",summary)
                except Exception as exc:
                    row["engine_error"]=repr(exc)
                    print("CANDIDATE_ERROR",candidate,repr(exc),flush=True)
                    save_json(args.out/"summary.json",summary)
                finally:
                    if champ is not None:
                        try:champ.quit()
                        except Exception:pass
        finally:
            if base is not None:
                try:base.quit()
                except Exception:pass
    print("TOURNAMENT_COMPLETE",json.dumps(summary),flush=True)

if __name__=="__main__":main()
