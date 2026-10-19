#!/usr/bin/env python3
"""Single 1024-thread Stockfish instance, switch EvalFile for each turn.
The NNUE switches and resets hash before EVERY move, on BOTH sides.
This is a CPU-limit-safe method of testing 1024-thread depth-1 evaluation.
"""
import argparse,hashlib,json,random,statistics,time
from pathlib import Path
import chess,chess.engine,chess.pgn,chess.polyglot
from scipy.stats import t as tdist
BASE="nn-1c0000000000.nnue"
SMALL="nn-37f18f62d772.nnue"
CANDIDATES=("nn-e8bac1c07a5a.nnue","nn-ddcfb9224cdb.nnue")
THREADS=1024
HASH=16

def save(path,x):
    p=path.with_suffix(path.suffix+".tmp")
    p.write_text(json.dumps(x,indent=2),encoding="utf8");p.replace(path)

def make_openings(path,n,seed=20261021):
    rng=random.Random(seed);seen=set();out=[]
    with chess.polyglot.open_reader(str(path)) as bk:
        for k in range(max(50000,n*120)):
            if len(out)>=n:break
            board=chess.Board()
            for _ in range(12):
                entries=list(bk.find_all(board))
                if not entries:break
                entry=rng.choices(entries,weights=[max(e.weight,1) for e in entries])[0]
                if not board.is_legal(entry.move):break
                board.push(entry.move)
                if board.is_game_over(claim_draw=True):break
            moves=tuple(m.uci() for m in board.move_stack)
            if len(moves)>=4 and moves not in seen and not board.is_game_over(claim_draw=True):
                seen.add(moves);out.append(moves)
    if len(out)<n:raise RuntimeError(f"Only {len(out)} valid openings of {n}")
    return out

def lossafe(pairs):
    if len(pairs)<2:return None
    mu=statistics.mean(pairs);s=statistics.stdev(pairs)
    if s==0:return 1.0 if mu>.5 else 0.0 if mu<.5 else .5
    return float(tdist.cdf((mu-.5)/(s/len(pairs)**.5),df=len(pairs)-1))

def play_game(engine, nets, opening, candidate, white_net, black_net, pair, game_number, maxplies):
    board=chess.Board()
    game=chess.pgn.Game()
    game.headers.update({"Event":"SF17.1 depth1 1024threads Single Instance Switching EvalFile",
        "Site":"GitHub-hosted Ubuntu Runner",
        "White":white_net,"Black":black_net,
        "Pair":str(pair),"Round":str(pair)+"."+str(game_number),
        "Threads":str(THREADS),"Hash":str(HASH),"Depth":"1","EvalFileSmall":SMALL})
    node=game
    for s in opening:
        move=chess.Move.from_uci(s)
        if not board.is_legal(move):
            return {"technical":"illegal opening","plies":0}
        node=node.add_variation(move);board.push(move)
    plies=0
    elapsed=0.
    max_move=0.
    while not board.is_game_over(claim_draw=True) and plies<maxplies:
        key=white_net if board.turn==chess.WHITE else black_net
        try:
            before=time.monotonic()
            # Important: both sides receive identical cache/reset treatment.
            engine.configure({"EvalFile":str((nets/key).resolve()),"Clear Hash":None})
            # A NEW token forces ucinewgame, so no residual search state from
            # the other NNUE remains. Depth remains exactly 1.
            item=engine.play(board,chess.engine.Limit(depth=1),
                game=object(),info=chess.engine.INFO_NONE)
            cost=time.monotonic()-before
            elapsed+=cost;max_move=max(max_move,cost)
            if not item.move or not board.is_legal(item.move):
                raise RuntimeError("Engine did not return a legal move: "+str(item.move))
            node=node.add_variation(item.move);board.push(item.move);plies+=1
        except Exception as exc:
            return {"technical":repr(exc),"plies":plies,
                    "seconds":round(elapsed,3),"longest_move_seconds":round(max_move,3)}
    outcome=board.outcome(claim_draw=True)
    if outcome is None:
        return {"technical":"unfinished_at_maxplies_"+str(maxplies),"plies":plies,
                "seconds":round(elapsed,3),"longest_move_seconds":round(max_move,3)}
    game.headers["Result"]=outcome.result()
    return {"technical":None,"result":outcome.result(),"pgn":str(game)+"\n\n",
            "plies":plies,"seconds":round(elapsed,3),"longest_move_seconds":round(max_move,3)}

def pts(result,as_white):
    if result=="1/2-1/2":return .5
    return float((result=="1-0")==as_white)

def main():
    parser=argparse.ArgumentParser()
    for name in ("engine","nets","book","out"):
        parser.add_argument("--"+name,required=True,type=Path)
    parser.add_argument("--pairs",type=int,default=8)
    parser.add_argument("--maxplies",type=int,default=260)
    parser.add_argument("--seconds",type=int,default=12000)
    args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    checks={}
    for name in (BASE,SMALL)+CANDIDATES:
        path=args.nets/name
        with path.open("rb") as f:d=hashlib.file_digest(f,"sha256").hexdigest()
        if d[:12]!=name[3:15]:raise RuntimeError("bad net sha for "+name)
        checks[name]={"bytes":path.stat().st_size,"sha256":d}
    save(args.out/"network_integrity.json",checks)
    openings=make_openings(args.book,args.pairs)
    save(args.out/"paired_variety_openings.json",openings)
    total={"engine":"Stockfish 17.1 unmodified","threads":THREADS,
        "depth":1,"hash_mb":HASH,"baseline":BASE,"small":SMALL,
        "method":"one 1024-thread process switches NNUE + clears hash before every move, both sides",
        "pair_count_requested":args.pairs,"results":{},"technical_pairs":[]}
    save(args.out/"summary.json",total)
    deadline=time.monotonic()+args.seconds
    engine=None
    try:
        t=time.monotonic()
        engine=chess.engine.SimpleEngine.popen_uci(str(args.engine),timeout=240)
        if "Stockfish 17.1" not in engine.id.get("name",""):
            raise RuntimeError("Wrong executable "+str(engine.id))
        if engine.options["Threads"].max<THREADS:raise RuntimeError("1024 threads unsupported")
        engine.configure({"Threads":THREADS,"Hash":HASH,
            "EvalFile":str((args.nets/BASE).resolve()),
            "EvalFileSmall":str((args.nets/SMALL).resolve())})
        engine.ping()
        probe=engine.play(chess.Board(),chess.engine.Limit(depth=1),
            game=object(),info=chess.engine.INFO_NONE)
        if probe.move is None:raise RuntimeError("1024-thread baseline didn't move")
        print("PREFLIGHT_SUCCESS_BASE",probe.move.uci(),
              "seconds",round(time.monotonic()-t,2),flush=True)
        for contender in CANDIDATES:
            engine.configure({"EvalFile":str((args.nets/contender).resolve()),"Clear Hash":None})
            probe=engine.play(chess.Board(),chess.engine.Limit(depth=1),game=object(),info=chess.engine.INFO_NONE)
            if probe.move is None:raise RuntimeError("candidate didn't move: "+contender)
            print("PREFLIGHT_SUCCESS_CANDIDATE",contender,probe.move.uci(),flush=True)
        with (args.out/"games.pgn").open("w",encoding="utf8") as pgn, (args.out/"individual_games.jsonl").open("w",encoding="utf8") as jl:
            for contender in CANDIDATES:
                row={"wins":0,"draws":0,"losses":0,
                    "valid_pairs":0,"technical_pairs":0,"los":None,
                    "paired_scores":[],"completed":0}
                total["results"][contender]=row
                save(args.out/"summary.json",total)
                for i,opening in enumerate(openings):
                    if time.monotonic()>deadline:
                        print("TIME_BUDGET_EXCEEDED",flush=True);break
                    records=[]
                    for j in (0,1):
                        white=contender if j==0 else BASE
                        black=BASE if j==0 else contender
                        r=play_game(engine,args.nets,opening,contender,white,black,
                            i+1,j+1,args.maxplies)
                        records.append({"candidate_white":j==0,**r})
                    if any(r.get("technical") for r in records):
                        row["technical_pairs"]+=1
                        total["technical_pairs"].append({"network":contender,"pair":i+1,
                            "errors":[r.get("technical") for r in records]})
                    else:
                        scores=[pts(r["result"],r["candidate_white"]) for r in records]
                        row["paired_scores"].append(sum(scores)/2)
                        row["valid_pairs"]+=1
                        for val in scores:
                            if val==1:row["wins"]+=1
                            elif val==0:row["losses"]+=1
                            else:row["draws"]+=1
                        row["completed"]+=2
                        row["los"]=lossafe(row["paired_scores"])
                        for r in records:pgn.write(r.pop("pgn"))
                    jl.write(json.dumps({"candidate":contender,"pair":i+1,
                        "games":[{k:v for k,v in r.items() if k!="pgn"} for r in records]})+"\n")
                    jl.flush();pgn.flush()
                    print("PAIR",contender,i+1,"WDL",row["wins"],row["draws"],row["losses"],
                        "LOS",row["los"],
                        "wall_seconds",round(sum(r.get("seconds",0) for r in records),2),
                        "technical_pairs",row["technical_pairs"],flush=True)
                    save(args.out/"summary.json",total)
                if row["valid_pairs"]:
                    row["score_pct"]=100*(row["wins"]+.5*row["draws"])/(2*row["valid_pairs"])
                print("FINAL",contender,json.dumps({k:v for k,v in row.items() if k!="paired_scores"}),flush=True)
                save(args.out/"summary.json",total)
    finally:
        if engine is not None:
            try:engine.quit()
            except Exception:pass
    print("ALL_DONE",json.dumps({name:{k:v for k,v in row.items() if k!="paired_scores"}
            for name,row in total["results"].items()}),flush=True)

if __name__=="__main__":main()
