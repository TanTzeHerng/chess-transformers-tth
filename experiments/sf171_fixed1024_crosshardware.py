#!/usr/bin/env python3
"""Same Stockfish 17.1 go depth 1 Threads=1024 on native cloud x86 and ARM."""
import json, os, platform, pathlib, random, re, statistics, subprocess, time
import chess, chess.engine
ROOT=pathlib.Path.cwd()
EXE=str(ROOT/"stockfish171/src/stockfish")
CPUS=sorted(os.sched_getaffinity(0))
REFERENCES=["d2d3","e2e3","f2f4","h2h3","a2a3","e4g3","f1g2","d2d4","e5d4","f1g2","g7g6","d2d4","h2g3","g4g5","d1d3","e2e3","a3b4","e6d8","b4b5","e4d5","e2e3","f4f5","b6c5","f3g5"]
arch=platform.machine()
def get_cpuinfo():
    p=pathlib.Path("/proc/cpuinfo")
    text=p.read_text(errors="replace") if p.exists() else ""
    return [x.strip() for x in text.splitlines() if x.startswith(("model name", "CPU implementer", "CPU part", "Hardware", "processor"))][:10]
DATA={
  "metadata":{"version":"Stockfish 17.1","uci":"go depth 1","threads":1024,
    "hash_mib":16,"multipv":1,"large_nnue":"nn-1c0000000000.nnue",
    "small_nnue":"nn-37f18f62d772.nnue",
    "machine":platform.platform(),"machine_arch":arch,"cpuinfo":get_cpuinfo(),
    "cpus_available":CPUS,"cgroup_cpu_max":pathlib.Path("/sys/fs/cgroup/cpu.max").read_text().strip() if pathlib.Path("/sys/fs/cgroup/cpu.max").exists() else "n/a",
    "remark":"Depth 11 moves are a diagnostic proxy, not a proven chess-strength score."
  },
  "samples":[], "modes":[], "reference_depth11":REFERENCES
}
name="fixed1024_"+arch+".json"
def save(): (ROOT/name).write_text(json.dumps(DATA,indent=2),encoding="utf8")
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
    positions=[]
    for s in lines:
        b=chess.Board()
        for san in s.split(): b.push_san(san)
        positions.append((s,b.fen()))
    random.seed(297421)
    for j in range(12):
        b=chess.Board()
        for ply in range(12+2*j):
            if b.is_game_over():break
            b.push(random.choice(list(b.legal_moves)))
        if not b.is_game_over():positions.append(("seed_"+str(j),b.fen()))
    return positions
POS=positions()
assert len(POS)==len(REFERENCES)==24
DATA["positions"]=[{"i":i,"id":name,"fen":fen} for i,(name,fen) in enumerate(POS)]
print("START",json.dumps(DATA["metadata"]),flush=True)
save()
for mode, cpus in [("one_cpu",CPUS[:1]),("all_cpus",CPUS)]:
    for repeat in range(3):
        print("BEGIN",mode,"repeat",repeat,"affinity",cpus,flush=True)
        cmd=["taskset","-c",",".join(map(str,cpus)),EXE]
        samples=[]
        try:
            t0=time.monotonic()
            with chess.engine.SimpleEngine.popen_uci(cmd,timeout=120) as eng:
                eng.configure({"Threads":1024,"Hash":16})
                for idx,(position_name,fen) in enumerate(POS):
                    b=chess.Board(fen)
                    start=time.monotonic()
                    result=eng.play(b,chess.engine.Limit(depth=1),game=f"{mode}-{repeat}-{idx}",info=chess.engine.INFO_ALL)
                    elapsed=round((time.monotonic()-start)*1000,3)
                    info=result.info or {}
                    output=info.get("string","")
                    match=re.search(r"helper_report\s+total=(\d+)\s+d2=(\d+)\s+d3=(\d+)\s+max=(\d+)",output)
                    chosen=result.move.uci() if result.move else None
                    item={"cpu_mode":mode,"repeat":repeat,"position":idx,"move":chosen,"reference_move":REFERENCES[idx],
                       "agree_depth11":chosen==REFERENCES[idx],"elapsed_ms":elapsed,"threads":1024,
                       "helpers_d2":int(match.group(2)) if match else None,
                       "helpers_d3":int(match.group(3)) if match else None,
                       "max_helper_depth":int(match.group(4)) if match else None}
                    DATA["samples"].append(item);samples.append(item);save()
                    print("SAMPLE",mode,repeat,idx,chosen,int(item["agree_depth11"]),elapsed,item["helpers_d2"],item["max_helper_depth"],flush=True)
            summary={"cpu_mode":mode,"repeat":repeat,"samples":len(samples),
                     "agreement":sum(x["agree_depth11"] for x in samples),
                     "avg_elapsed_ms":round(statistics.mean(x["elapsed_ms"] for x in samples),2),
                     "wall_seconds":round(time.monotonic()-t0,2)}
            DATA["modes"].append(summary);save()
            print("SUMMARY",json.dumps(summary),flush=True)
        except Exception as err:
            DATA.setdefault("errors",[]).append({"mode":mode,"repeat":repeat,"error":repr(err)})
            print("ERROR",repr(err),flush=True);save()
print("FINAL_SUMMARIES",json.dumps(DATA["modes"]),flush=True)
