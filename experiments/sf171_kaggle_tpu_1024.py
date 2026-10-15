#!/usr/bin/env python3
"""Stockfish 17.1 depth1: fixed Threads=1024 on Kaggle TPU VM CPU host.
Compare CPU affinity 4 versus whole VM. Chess strength via paired opening games.
No TPU acceleration is claimed: Stockfish executes exclusively on CPU.
"""
import os,json,pathlib,platform,subprocess,sys,time,random,statistics,math,re
ROOT=pathlib.Path("/kaggle/working");ROOT.mkdir(exist_ok=True)
RESULT=ROOT/"sf171_fixed1024_kaggle_results.json"
PGN=ROOT/"sf171_fixed1024_kaggle_games.pgn"
CPUS=sorted(os.sched_getaffinity(0))
QUOTA=pathlib.Path("/sys/fs/cgroup/cpu.max")
q=QUOTA.read_text().strip() if QUOTA.exists() else "unavailable"
def cpu_quota():
    try:
        limit,period=q.split()
        return math.inf if limit=="max" else int(limit)/int(period)
    except Exception:return math.inf
ACTUAL_CPU=min(len(CPUS),cpu_quota())
DATA={"hardware":{"cpu_affinity_count":len(CPUS),"cpu_quota":q,"effective_cpu_estimate":ACTUAL_CPU,"architecture":platform.machine(),"reported_cpu_count":os.cpu_count(),"available_cpu_ids":CPUS,"tpu_name_exists":bool(os.getenv("TPU_NAME"))},
      "settings":{"version":"official Stockfish 17.1 source","Threads":1024,"depth":1,"Hash_MiB":16,"MultiPV":1,"net_big":"nn-1c0000000000.nnue","net_small":"nn-37f18f62d772.nnue"},
      "position_tests":[],"games":[],"w_d_l":{"all_cpu":0,"draw":0,"four_cpu":0,"unfinished":0},"errors":[]}
def save():RESULT.write_text(json.dumps(DATA,indent=2,default=str))
def note(*args):print("SF171_KAGGLE",*args,flush=True)
save();note("HARDWARE",json.dumps(DATA["hardware"]))
if ACTUAL_CPU < 8:
    DATA["status"]="not_enough_usable_cpus"
    DATA["errors"].append("Kaggle API did not provide the high-core TPU host; 1024-thread 4-v-4 test not run")
    save();note("STOP insufficient real CPU",ACTUAL_CPU)
    sys.exit(0)
DATA["status"]="building_stockfish";save()
subprocess.run([sys.executable,"-m","pip","install","-q","python-chess"],check=True)
import chess,chess.engine,chess.pgn
code=ROOT/"stockfish171"
subprocess.run(["git","clone","--depth","1","--branch","sf_17.1","https://github.com/official-stockfish/Stockfish.git",str(code)],check=True)
p=code/"src/search.cpp"
s=p.read_text()
anchor="    // When playing in 'nodes as time' mode, subtract the searched nodes from"
assert s.count(anchor)==1
extra="""    // Telemetry only. Read after all helper workers have stopped.
              size_t helperD2=0, helperD3=0;
              int maxHelperDepth=0;
              for (const auto& thread:threads) {
                  if (thread->worker.get()!=this) {
                      const int d=thread->worker->completedDepth;
                      helperD2+=d>=2; helperD3+=d>=3;
                      maxHelperDepth=std::max(maxHelperDepth,d);
                  }
              }
              std::cout << "info string helper_report total=" << (threads.size()-1)
                        << " d2=" << helperD2 << " d3=" << helperD3
                        << " max=" << maxHelperDepth << std::endl;
"""
p.write_text(s.replace(anchor,extra+"\n"+anchor,1))
arch="x86-64" if platform.machine()=="x86_64" else "armv8"
with (ROOT/"sf171_kaggle_build.log").open("w") as build:
    subprocess.run(["make","-C",str(code/"src"),"-j8","build","ARCH="+arch],stdout=build,stderr=subprocess.STDOUT,check=True)
BIN=str(code/"src/stockfish")
OPENINGS=[
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
POSITIONS=[]
for op in OPENINGS:
    b=chess.Board()
    for move in op.split():b.push_san(move)
    POSITIONS.append((op,b.fen()))
random.seed(297421)
for j in range(12):
    b=chess.Board()
    for n in range(12+j*2):
        if b.is_game_over(): break
        b.push(random.choice(list(b.legal_moves)))
    if not b.is_game_over():POSITIONS.append(("seed_"+str(j),b.fen()))
def run_engine(available):
    return chess.engine.SimpleEngine.popen_uci(["taskset","-c",",".join(map(str,available)),BIN],timeout=180)
def configure(e):e.configure({"Threads":1024,"Hash":16})
DATA["status"]="measuring_fixed_1024";save()
for mode,avail in [("four_cpu",CPUS[:4]),("all_cpu",CPUS)]:
    for repeat in range(3):
        with run_engine(avail) as engine:
            configure(engine)
            for idx,(_,fen) in enumerate(POSITIONS):
                st=time.monotonic()
                result=engine.play(chess.Board(fen),chess.engine.Limit(depth=1),
                                   game=f"{mode}-{repeat}-{idx}",info=chess.engine.INFO_ALL)
                duration=round(1000*(time.monotonic()-st),3)
                report=result.info.get("string","") if result.info else ""
                m=re.search(r"helper_report\s+total=(\d+)\s+d2=(\d+)\s+d3=(\d+)\s+max=(\d+)",report)
                row={"mode":mode,"repeat":repeat,"position":idx,"move":result.move.uci() if result.move else None,
                     "elapsed_ms":duration,
                     "helper_d2":int(m.group(2)) if m else None,
                     "helper_d3":int(m.group(3)) if m else None,
                     "helper_max_depth":int(m.group(4)) if m else None}
                DATA["position_tests"].append(row);save()
                note("POSITION",mode,repeat,idx,row["move"],duration,row["helper_d2"],row["helper_max_depth"])
DATA["status"]="playing_games";save()
with run_engine(CPUS) as full,run_engine(CPUS[:4]) as four:
    configure(full);configure(four)
    for opening_index,opening in enumerate(OPENINGS):
        for all_is_white in (True,False):
            board=chess.Board();game=chess.pgn.Game();node=game
            game.headers.update({"Event":"SF17.1 fixed 1024 threads on 4 vs many CPUs",
                "Site":"Kaggle TPU VM CPU host",
                "White":"All usable CPUs" if all_is_white else "4 CPU affinity",
                "Black":"4 CPU affinity" if all_is_white else "All usable CPUs",
                "Round":str(2*opening_index+int(not all_is_white)+1),"TimeControl":"go depth 1"})
            for san in opening.split():
                mv=board.parse_san(san);node=node.add_variation(mv);board.push(mv)
            try:
                for _ in range(260):
                    if board.is_game_over(claim_draw=True):break
                    player=full if (board.turn==chess.WHITE)==all_is_white else four
                    response=player.play(board,chess.engine.Limit(depth=1),
                                         game=f"match-{opening_index}-{all_is_white}")
                    if response.move is None or response.move not in board.legal_moves:
                        raise RuntimeError("Invalid Stockfish move "+str(response.move))
                    node=node.add_variation(response.move);board.push(response.move)
                result=board.result(claim_draw=True) if board.is_game_over(claim_draw=True) else "*"
                game.headers["Result"]=result
                game.headers["Termination"]="Normal" if result!="*" else "Unfinished"
                if result=="*":DATA["w_d_l"]["unfinished"]+=1
                elif result=="1/2-1/2":DATA["w_d_l"]["draw"]+=1
                elif (result=="1-0" and all_is_white) or (result=="0-1" and not all_is_white):
                    DATA["w_d_l"]["all_cpu"]+=1
                else:DATA["w_d_l"]["four_cpu"]+=1
                DATA["games"].append({"round":game.headers["Round"],"opening":opening,
                                       "all_cpu_white":all_is_white,"result":result,"ply":board.ply()})
                with PGN.open("a") as f:f.write(str(game)+"\n\n")
                save();note("GAME",len(DATA["games"]),result,DATA["w_d_l"])
            except Exception as e:
                DATA["errors"].append({"opening":opening,"error":repr(e)});save();note("ERROR",repr(e))
DATA["status"]="completed";save()
note("FINAL",DATA["w_d_l"])
