#!/usr/bin/env python3
"""Stockfish 17.1 go-depth-one Lazy SMP: server CPU and thread count experiment."""
import json, os, random, re, statistics, time, subprocess, pathlib
import chess, chess.engine, chess.pgn

ROOT=pathlib.Path.cwd()
BIN=str(ROOT/"stockfish171/src/stockfish")
CPUS=sorted(os.sched_getaffinity(0))
DATA={"machine":{"cpu_affinity":CPUS,"visible_cpu_count":os.cpu_count(),"uname":os.uname().sysname+" "+os.uname().machine,"cgroup_cpu_max":pathlib.Path("/sys/fs/cgroup/cpu.max").read_text().strip() if pathlib.Path("/sys/fs/cgroup/cpu.max").exists() else None}, "samples":[], "games":[], "settings":{"depth":1,"hash_MB":16,"NNUE":"official SF17.1","Tablebases":"none","Skill":"max","MultiPV":1}}
print("HOST",DATA["machine"],flush=True)
def save():
    (ROOT/"thread1_results.json").write_text(json.dumps(DATA,indent=2),encoding="utf-8")
def engine(n,cpus=None):
    cmd=[BIN] if cpus is None else ["taskset","-c",",".join(map(str,cpus)),BIN]
    e=chess.engine.SimpleEngine.popen_uci(cmd,timeout=120)
    e.configure({"Threads":n,"Hash":16})
    return e
def positions():
    lines=[
       "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7",
       "d4 d5 c4 e6 Nc3 Nf6 Bg5 Be7",
       "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6",
       "d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 Nf3 O-O",
       "e4 e6 d4 d5 Nc3 Bb4 e5 c5",
       "e4 c6 d4 d5 Nc3 dxe4 Nxe4 Bf5",
       "c4 e5 Nc3 Nf6 g3 d5 cxd5 Nxd5",
       "Nf3 d5 g3 Nf6 Bg2 g6 O-O Bg7",
       "e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6 d4",
       "d4 Nf6 c4 e6 Nf3 b6 g3 Bb7",
       "e4 c5 Nf3 Nc6 d4 cxd4 Nxd4",
       "e4 d5 exd5 Qxd5 Nc3 Qa5",
    ]
    result=[]
    for s in lines:
        b=chess.Board()
        for san in s.split(): b.push_san(san)
        if not b.is_game_over():result.append((s,b.fen()))
    random.seed(297421)
    for j in range(12):
        b=chess.Board()
        for ply in range(12+2*j):
            if b.is_game_over():break
            b.push(random.choice(list(b.legal_moves)))
        if not b.is_game_over():result.append(("seed_"+str(j),b.fen()))
    return result
POS=positions()
DATA["positions"]=[{"id":i,"source":opening,"fen":fen} for i,(opening,fen) in enumerate(POS)]
print("POSITION_COUNT",len(POS),flush=True)
save()
reference=[]
with engine(1) as e:
    for i,(opening,fen) in enumerate(POS):
        b=chess.Board(fen)
        inf=e.analyse(b,chess.engine.Limit(depth=11),game=f"reference_{i}")
        reference.append(inf["pv"][0].uci() if inf.get("pv") else None)
DATA["reference_depth11_bestmoves"]=reference
save()
print("REFERENCE_READY",len(reference),flush=True)
modes=[("t1",1,None),("t4",4,None),("t16",16,None),("t64",64,None),("t256",256,None),("t1024",1024,None)]
if len(CPUS)>1:modes.append(("t1024_one_cpu",1024,CPUS[:1]))
for mode,threadnum,affinity in modes:
    print("MODE_BEGIN",mode,flush=True)
    t0=time.monotonic()
    samples=[]
    try:
        with engine(threadnum,affinity) as eng:
            for i,(source,fen) in enumerate(POS):
                b=chess.Board(fen)
                start=time.monotonic()
                result=eng.play(b,chess.engine.Limit(depth=1),game=f"{mode}-{i}",info=chess.engine.INFO_ALL)
                elapsed_ms=round((time.monotonic()-start)*1000,3)
                info=result.info or {}
                report=info.get("string","")
                m=re.search(r"helper_report\s+total=(\d+)\s+d2=(\d+)\s+d3=(\d+)\s+max=(\d+)",report)
                s={"mode":mode,"position":i,"threads":threadnum,"cpu_restriction":len(affinity) if affinity else len(CPUS),"move":result.move.uci() if result.move else None,"depth11_agree":bool(result.move and result.move.uci()==reference[i]),"duration_ms":elapsed_ms,"helper_report":report,"helpers_depth2":int(m.group(2)) if m else None,"helpers_depth3":int(m.group(3)) if m else None,"max_helper_depth":int(m.group(4)) if m else None}
                samples.append(s);DATA["samples"].append(s)
                print("SAMPLE",mode,i,s["move"],"deep_agreement",int(s["depth11_agree"]),"milliseconds",elapsed_ms,"helpers_d2",s["helpers_depth2"],"helpers_d3",s["helpers_depth3"],"max_depth",s["max_helper_depth"],flush=True)
                save()
        print("MODE_SUMMARY",mode,"agree",sum(v["depth11_agree"] for v in samples),"/",len(samples),"avg_ms",round(statistics.mean(v["duration_ms"] for v in samples),2),"wall_s",round(time.monotonic()-t0,2),flush=True)
    except Exception as exc:
        print("MODE_ERROR",mode,repr(exc),flush=True)
        DATA.setdefault("errors",[]).append({"mode":mode,"error":repr(exc)})
        save()
# Eight real games as an independent strength check.
games=chess.pgn.Game()
starts=[
 "e4 e5 Nf3 Nc6 Bb5 a6",
 "d4 d5 c4 e6 Nc3 Nf6",
 "e4 c5 Nf3 d6 d4 cxd4",
 "d4 Nf6 c4 g6 Nc3 Bg7",
]
try:
    with engine(1) as low, engine(1024) as high:
        for opening_idx,opening in enumerate(starts):
            for high_white in (True,False):
                b=chess.Board();g=chess.pgn.Game();node=g
                w="SF17.1 depth1 1024 threads" if high_white else "SF17.1 depth1 1 thread"
                bl="SF17.1 depth1 1 thread" if high_white else "SF17.1 depth1 1024 threads"
                g.headers.update({"Event":"SF17.1 depth1 thread comparison","White":w,"Black":bl,"Round":str(2*opening_idx+1+(not high_white)),"TimeControl":"fixed depth 1"})
                for san in opening.split():
                    m=b.parse_san(san);b.push(m);node=node.add_variation(m)
                for ply in range(200):
                    if b.is_game_over(claim_draw=True):break
                    ee=high if (b.turn==chess.WHITE)==high_white else low
                    result=ee.play(b,chess.engine.Limit(depth=1),game=f"game{opening_idx}:{high_white}")
                    if result.move is None: raise RuntimeError("no legal move")
                    node=node.add_variation(result.move);b.push(result.move)
                outcome=b.result(claim_draw=True) if b.is_game_over(claim_draw=True) else "*"
                g.headers["Result"]=outcome
                rec={"round":g.headers["Round"],"opening":opening,"threads1024_white":high_white,"result":outcome,"ply":b.ply(),"valid_chess_result":outcome!="*"}
                DATA["games"].append(rec);save()
                with (ROOT/"thread1_games.pgn").open("a",encoding="utf8") as f:f.write(str(g)+"\n\n")
                print("GAME_RESULT",json.dumps(rec),flush=True)
except Exception as exc:
    print("GAMES_ERROR",repr(exc),flush=True);DATA.setdefault("errors",[]).append({"stage":"games","error":repr(exc)});save()
print("FINAL",json.dumps({"modes":list(dict.fromkeys(s["mode"] for s in DATA["samples"])),"samples":len(DATA["samples"]),"games":len(DATA["games"]),"errors":DATA.get("errors",[])}),flush=True)
