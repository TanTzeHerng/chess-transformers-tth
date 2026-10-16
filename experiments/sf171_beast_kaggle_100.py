# %% [code]
"""Stockfish 17.1 depth=1 Threads=1024 5-man Syzygy vs Lc0 Beast nodes=1.
Never silently downgrade CPU, change network, or forfeit engine crashes.
"""
import os,sys,time,json,math,random,hashlib,pathlib,platform,subprocess,shutil,collections
ROOT=pathlib.Path("/kaggle/working");ROOT.mkdir(exist_ok=True,parents=True)
STATE=ROOT/"sf171_beast_100_score.json"; PGN=ROOT/"sf171_beast_100_games.pgn"
NET_SHA="394d1ca773b41e9f88f3993befd4a5c3a45eb6d7bcc5320a6ecca441625298cf"
affinity=sorted(os.sched_getaffinity(0));quota_path=pathlib.Path("/sys/fs/cgroup/cpu.max")
quota=quota_path.read_text().strip() if quota_path.exists() else "unavailable"
try:
    amount, period=quota.split()
    quota_cpus=float("inf") if amount=="max" else int(amount)/int(period)
except Exception: quota_cpus=float("inf")
effective_cpus=min(len(affinity),quota_cpus)
DATA={"status":"preflight","hardware":{"machine":platform.platform(),"architecture":platform.machine(),"os_cpu_count":os.cpu_count(),"affinity_count":len(affinity),"cpu_quota":quota,"usable_cores":effective_cpus,"tpu_name_present":bool(os.getenv("TPU_NAME"))},
      "settings":{"Stockfish":"official v17.1","go":"depth 1","Threads":1024,"Hash":64,"NNUE_big":"nn-1c0000000000.nnue","NNUE_small":"nn-37f18f62d772.nnue",
        "SyzygyProbeLimit":5,"Syzygy50MoveRule":True,"Beast":"Lc0","Beast_limit":"go nodes 1","Beast_expected_sha256":NET_SHA,
        "opening_pairs":50,"expected_games":100,"results_only_chess_rules":True},
      "score":{"Stockfish_wins":0,"draws":0,"Beast_wins":0,"unfinished":0,"games_completed":0},
      "games":[],"errors":[]}
def save(): STATE.write_text(json.dumps(DATA,indent=2,default=str))
def report(*args): print("BEAST_REMATCH",*args,flush=True)
def block(reason):
    DATA["status"]="blocked";DATA["errors"].append(reason);save();report("BLOCKED",reason);raise SystemExit(0)
save();report("HARDWARE",json.dumps(DATA["hardware"]))
if effective_cpus<16: block("Fewer than 16 usable CPUs; not a high-core comparison. No games started.")
# Input sources are mounted read-only under /kaggle/input. Do not upload private weights to a public dataset.
root_input=pathlib.Path("/kaggle/input")
nets=[];tbdirs=collections.defaultdict(lambda:[0,0])
if root_input.exists():
    for root,dirs,files in os.walk(root_input):
        dirs[:]=[d for d in dirs if d not in (".cache", ".git")]
        for f in files:
            if f.lower()=="beast.pb.gz": nets.append(pathlib.Path(root)/f)
            if f.lower().endswith(".rtbw"):tbdirs[root][0]+=1
            if f.lower().endswith(".rtbz"):tbdirs[root][1]+=1
report("SYZYGY_CANDIDATE_DIRS",dict(tbdirs))
# A complete 3-5-man collection has 145 WDL and 145 DTZ files; files may be in separate directories.
wdl=sum(v[0] for v in tbdirs.values());dtz=sum(v[1] for v in tbdirs.values())
DATA["tablebase_inventory"]={"wdl_files":wdl,"dtz_files":dtz}
save()
if wdl<145 or dtz<145: block("Complete 3-5-piece Syzygy WDL+DTZ tables not mounted: "+str(DATA["tablebase_inventory"]))
if not nets: block("Beast.pb.gz not found among Kaggle input datasets; not substituting a different net.")
BEAST=None
for p in nets:
    digest=hashlib.sha256(p.read_bytes()).hexdigest()
    if digest==NET_SHA: BEAST=p;break
if BEAST is None: block("Beast files present, but none matches the exact earlier-match Beast.pb.gz SHA256.")
TBPATH=os.pathsep.join(sorted(str(path) for path,counts in tbdirs.items() if any(counts)))
DATA["beast_network"]=str(BEAST);DATA["syzygy_path"]=TBPATH
report("INPUTS_VERIFIED","Beast_sha",NET_SHA,"WDL",wdl,"DTZ",dtz,"path",TBPATH)
save()
# Build engines only once prerequisites have been verified.
def run(cmd,**kwargs):
    report("RUN",cmd)
    return subprocess.run(cmd,check=True,**kwargs)
DATA["status"]="compiling";save()
run([sys.executable,"-m","pip","install","-q","python-chess","meson","ninja"])
run(["apt-get","update","-qq"],stdout=subprocess.DEVNULL)
run(["apt-get","install","-y","-qq","git","g++","libopenblas-dev","libprotobuf-dev","protobuf-compiler","zlib1g-dev","libeigen3-dev","libgtest-dev","pkg-config"],stdout=subprocess.DEVNULL)
import chess,chess.engine,chess.pgn,chess.syzygy
# Verify an actual 5-man tablebase probe, not merely existence of filenames.
with chess.syzygy.open_tablebase(str(next(iter(tbdirs))),load_wdl=True,load_dtz=True) as table:
    board=chess.Board("8/8/8/8/8/6k1/4K3/3Q4 w - - 0 1")
    tb_result=table.get_wdl(board)
    if tb_result is None: block("Syzygy sample endgame did not probe successfully")
report("SYZYGY_PROBE_SUCCESS",tb_result)
sf_src=ROOT/"Stockfish17.1"
run(["git","clone","-q","--depth","1","--branch","sf_17.1","https://github.com/official-stockfish/Stockfish.git",str(sf_src)])
arch="x86-64" if platform.machine()=="x86_64" else "armv8"
with (ROOT/"stockfish_build.log").open("w") as out:
    run(["make","-C",str(sf_src/"src"),"-j12","build","ARCH="+arch],stdout=out,stderr=subprocess.STDOUT)
sf_bin=str(sf_src/"src"/"stockfish")
lc_src=ROOT/"lc0-src"
run(["git","clone","-q","--recurse-submodules","--depth","1","--branch","v0.32.1","https://github.com/LeelaChessZero/lc0.git",str(lc_src)])
with (ROOT/"lc0_build.log").open("w") as out:
    run(["bash","build.sh","-Dgtest=false"],cwd=lc_src,stdout=out,stderr=subprocess.STDOUT)
lc_bin=next((str(p) for p in [lc_src/"build/release/lc0",lc_src/"build/lc0",lc_src/"build/release/lc0.exe"] if p.is_file()),None)
if not lc_bin:block("Lc0 build produced no executable")
# UCI checks must pass before first tournament game.
DATA["status"]="engine_smoke_tests";save()
sf=chess.engine.SimpleEngine.popen_uci(sf_bin,timeout=180)
beast=chess.engine.SimpleEngine.popen_uci([lc_bin,"--backend=blas","--weights="+str(BEAST)],timeout=180)
try:
    if "Stockfish" not in sf.id.get("name",""):block("Wrong Stockfish binary: "+str(sf.id))
    if "Lc0" not in beast.id.get("name","") and "Leela" not in beast.id.get("name",""):block("Wrong Beast engine executable: "+str(beast.id))
    sf.configure({"Threads":1024,"Hash":64,"SyzygyPath":TBPATH,"SyzygyProbeLimit":5,"Syzygy50MoveRule":True})
    if "SyzygyProbeDepth" in sf.options:sf.configure({"SyzygyProbeDepth":1})
    # Most lc0 builds accept backend & weights on command line.
    for label,engine,limit in [("Stockfish",sf,chess.engine.Limit(depth=1)),("Beast",beast,chess.engine.Limit(nodes=1))]:
        mv=engine.play(chess.Board(),limit,game="smoke")
        if mv.move not in chess.Board().legal_moves:block(label+" did not return a legal start-position move")
        report("ENGINE_SMOKE_OK",label,mv.move.uci())
    DATA["status"]="playing";save()
    # 25 varied legal openings, color-swapped twice each: 100 games.
    openings=[
      "e4 e5 Nf3 Nc6 Bb5 a6","d4 d5 c4 e6 Nc3 Nf6","e4 c5 Nf3 d6 d4 cxd4",
      "d4 Nf6 c4 g6 Nc3 Bg7","e4 e6 d4 d5 Nc3 Bb4","e4 c6 d4 d5 Nc3 dxe4",
      "c4 e5 Nc3 Nf6","Nf3 d5 g3 Nf6 Bg2 g6","e4 e5 Nf3 Nc6 Bc4 Bc5",
      "d4 Nf6 c4 e6 Nf3 b6","e4 c5 Nf3 Nc6 d4 cxd4","e4 d5 exd5 Qxd5",
      "e4 e5 Nf3 Nf6","d4 d5 c4 c6 Nf3 Nf6","c4 c5 Nc3 Nc6",
      "Nf3 Nf6 g3 g6 Bg2 Bg7","e4 c5 Nc3 Nc6 g3 g6","d4 Nf6 Nf3 d5",
      "e4 e5 Nc3 Nf6","b3 e5 Bb2 Nc6","e4 c6 Nf3 d5",
      "d4 Nf6 c4 c5 d5 b5","e4 e5 f4 exf4","c4 e6 Nc3 d5","e4 d6 d4 Nf6 Nc3 g6"]
    pairings=[(opening,w) for opening in openings for w in (True,False)]*2
    assert len(pairings)==100
    start=time.monotonic()
    for index,(opening,sf_white) in enumerate(pairings,1):
        board=chess.Board(); game=chess.pgn.Game();node=game
        game.headers.update({"Event":"SF17.1 depth1 1024 threads + Syzygy5 vs Beast nodes1 on Kaggle highcore",
          "Site":"Kaggle TPU 1VM CPU","Round":str(index),"White":"Stockfish 17.1" if sf_white else "Beast Lc0","Black":"Beast Lc0" if sf_white else "Stockfish 17.1"})
        for san in opening.split():
            m=board.parse_san(san);node=node.add_variation(m);board.push(m)
        try:
            for ply in range(450):
                if board.is_game_over(claim_draw=True):break
                playing_sf=(board.turn==chess.WHITE)==sf_white
                eng=sf if playing_sf else beast
                limit=chess.engine.Limit(depth=1) if playing_sf else chess.engine.Limit(nodes=1)
                move=eng.play(board,limit,game="game"+str(index))
                if move.move is None or move.move not in board.legal_moves:raise RuntimeError("UCI returned missing or illegal move: "+str(move.move))
                node=node.add_variation(move.move);board.push(move.move)
            result=board.result(claim_draw=True) if board.is_game_over(claim_draw=True) else "*"
            if result=="*":
                DATA["score"]["unfinished"]+=1
            else:
                DATA["score"]["games_completed"]+=1
                if result=="1/2-1/2":DATA["score"]["draws"]+=1
                elif (result=="1-0" and sf_white) or (result=="0-1" and not sf_white):DATA["score"]["Stockfish_wins"]+=1
                else:DATA["score"]["Beast_wins"]+=1
            game.headers["Result"]=result
            game.headers["Termination"]="Normal" if result!="*" else "Unfinished; not counted"
            DATA["games"].append({"number":index,"opening":opening,"Stockfish_as_white":sf_white,"result":result,"ply":board.ply()})
            with PGN.open("a",encoding="utf8") as f:f.write(str(game)+"\n\n")
            save();report("GAME",index,"result",result,"SCORE",json.dumps(DATA["score"]))
        except Exception as exc:
            # A process crash is NOT a chess result. Stop and publish records for forensic analysis.
            DATA["errors"].append({"game":index,"error":repr(exc),"fen":board.fen()})
            DATA["status"]="engine_error";save();report("ENGINE_ERROR",repr(exc));break
        if time.monotonic()-start>7.9*3600:
            DATA["status"]="session_time_limit_partial";save();report("TIME_BUDGET_STOP",len(DATA["games"]));break
    else:
        DATA["status"]="completed";save()
    report("FINAL_STATUS",DATA["status"],"SCORE",json.dumps(DATA["score"]))
finally:
    sf.quit()
    beast.quit()