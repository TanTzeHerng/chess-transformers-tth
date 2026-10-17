"""Cloud-only Stockfish 17.1 1024-thread 8192 MiB TT Variety self-play.
One persistent original Stockfish process plays both colors. One fresh process/game.
Never executes code on the user's PC. Requires a recent existing PC resource profile.
"""
import argparse, collections, ctypes, datetime, hashlib, json, mmap, os, pathlib, psutil, random, sys, time, traceback, urllib.request
from ctypes import wintypes
import chess, chess.engine, chess.pgn, chess.polyglot
P=argparse.ArgumentParser()
P.add_argument("--engine",required=True)
P.add_argument("--book",default="Variety.bin")
P.add_argument("--game",type=int,required=True)
P.add_argument("--max-plies",type=int,default=300)
args=P.parse_args()
assert 1<=args.game<=4
ROOT=pathlib.Path.cwd()
MiB=2**20
G=GiB=2**30
HASH_MB=8192
THREADS=1024
DEPTH=1
SHA="5a30bc27b5e7b7d51694bf35e7d5dd2536b54e9b25ea3ea64ef4a2b982d5e21c"
out=ROOT / ("game_%02d"%args.game)
out.mkdir(exist_ok=True)
def write(name,value):
    tmp=out/(name+".tmp")
    tmp.write_text(json.dumps(value,indent=2,default=str),encoding="utf8")
    tmp.replace(out/name)
def emit(label,data):
    print(label,json.dumps(data,separators=(",",":"),default=str),flush=True)
def pc_profile():
    raw=os.environ.get("PC_RESOURCE_PROFILE","")
    if not raw:raise RuntimeError("PC resource profile unavailable; refusing stale simulation")
    o=json.loads(raw)
    age=time.time()-float(o["timestamp"])
    if not -30 <= age <= 300:raise RuntimeError("PC measurement age %.1fs not sufficiently live"%age)
    if int(o["pc_logical_processors"])!=14:raise RuntimeError("Unexpected PC CPU topology")
    o["age_at_cloud_start_seconds"]=round(age,1)
    return o
pc=pc_profile()
vm=psutil.virtual_memory()
idle=max(.05,min(1.,float(pc["cpu_idle_fraction"])))
rate=max(5,min(100,round(idle*100)))
meta={"game":args.game,"hash_mb":HASH_MB,"threads":THREADS,"multipv":1,
      "depth":DEPTH,"cpu_rate_jobobject_percent":rate,
      "pc_profile":pc,"pc_sample_updated_during_game":False,
      "cloud_logical_processors":psutil.cpu_count(logical=True),
      "cloud_ram_mb":round(vm.total/MiB),
      "cloud_available_before_engine_mb":round(vm.available/MiB),
      "book_sha256":SHA,"book_name":"Variety.bin / Jeroen Noomen",
      "shared_tt_between_colors":True,"single_persistent_engine":True,
      "independent_process_every_game":True,
      "syzygy_tables_in_engine":False,
      "syzygy_policy":"Lichess WDL/DTZ 5-piece adjudication when available; no SyzygyPath in engine",
      "cpu_caveat":"Job Object cap is relative to four cloud logical CPUs, not 14 laptop logical CPUs",
      "memory_caveat":"Physical free RAM is not a safe Stockfish process memory cap. No hard memory cap imposed; modest balloon only when enough headroom is available.",
      "started_utc":datetime.datetime.now(datetime.timezone.utc).isoformat()}
assert hashlib.sha256(pathlib.Path(args.book).read_bytes()).hexdigest()==SHA,"Variety.bin checksum mismatch"
rnd=random.Random(20261017)
seen=set()
openings=[]
with chess.polyglot.open_reader(args.book) as book:
    for book_idx in range(4):
        for trial in range(1500):
            board=chess.Board()
            target=rnd.randint(8,24)
            for _ in range(target):
                options=list(book.find_all(board))
                if not options:break
                e=rnd.choices(options,weights=[max(1,v.weight) for v in options],k=1)[0]
                if e.move not in board.legal_moves:raise RuntimeError("Invalid Variety book entry")
                board.push(e.move)
            key=tuple(m.uci() for m in board.move_stack)
            if len(key)>=8 and key not in seen and board.outcome(claim_draw=False) is None:
                seen.add(key)
                openings.append(key)
                break
        else:raise RuntimeError("Variety cannot produce four distinct openings")
bookline=openings[args.game-1]
board=chess.Board()
for move in bookline: board.push_uci(move)
meta["opening_uci"]=bookline
meta["opening_fen"]=board.fen()
write("metadata.json",meta)

# This separate cloud engine process is assigned a Windows Job Object CPU hard cap.
# No second engine is launched on the PC, nor on this runner.
class CpuRate(ctypes.Structure):
    _fields_=[("ControlFlags",wintypes.DWORD),("CpuRate",wintypes.DWORD)]
k=ctypes.WinDLL("kernel32",use_last_error=True)
k.CreateJobObjectW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p]
k.CreateJobObjectW.restype=wintypes.HANDLE
k.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,ctypes.c_uint]
k.SetInformationJobObject.restype=wintypes.BOOL
k.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
k.AssignProcessToJobObject.restype=wintypes.BOOL
k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
k.OpenProcess.restype=wintypes.HANDLE
k.CloseHandle.argtypes=[wintypes.HANDLE]
k.CloseHandle.restype=wintypes.BOOL
job=None
eng=None
balloon=None
moves=[]
result={"phase":"initializing","game":args.game,"moves":[],"metadata":meta}
write("game_result.json",result)
def apply_cpu(pid):
    global job
    if rate>=100:return True
    job=k.CreateJobObjectW(None,None)
    if not job:raise OSError(ctypes.get_last_error(),"CreateJobObject failed")
    c=CpuRate(0x1|0x4,rate*100)
    if not k.SetInformationJobObject(job,15,ctypes.byref(c),ctypes.sizeof(c)):
        raise OSError(ctypes.get_last_error(),"CPU rate Job Object failed")
    # Windows needs a PROCESS HANDLE, not the integer PID.
    h=k.OpenProcess(0x0001|0x0100|0x0400,False,pid)
    if not h:raise OSError(ctypes.get_last_error(),"OpenProcess failed")
    try:
        if not k.AssignProcessToJobObject(job,h):
            raise OSError(ctypes.get_last_error(),"AssignProcessToJobObject failed")
    finally:
        k.CloseHandle(h)
    return True
def mem_sample():
    v=psutil.virtual_memory()
    x={"available_mb":round(v.available/MiB),"used_pct":v.percent}
    if eng is not None:
        try:
            m=psutil.Process(eng.transport.get_pid()).memory_info()
            x.update(engine_rss_mb=round(m.rss/MiB),engine_private_mb=round(m.vms/MiB))
        except Exception as e: x["process_error"]=str(e)[:150]
    return x
def tablebase_adjudication(b):
    if len(b.piece_map())>5:return None
    url="https://tablebase.lichess.ovh/standard?fen="+urllib.parse.quote(b.fen())
    try:
        with urllib.request.urlopen(url,timeout=9) as response: data=json.load(response)
        category=data.get("category")
        if category in ("win","loss","draw","cursed-win","blessed-loss"):
            winner=None
            if category=="win":winner=b.turn
            if category=="loss":winner=not b.turn
            res=("1-0" if winner is chess.WHITE else "0-1") if winner is not None else "1/2-1/2"
            return {"result":res,"termination":"SYZYGY_DTz","category":category,
                    "dtz":data.get("dtz"),"fen":b.fen()}
    except Exception as e:
        emit("TABLEBASE_WARNING",{"error":str(e)[:160]})
    return None
try:
    # Python-chess owns a single long-lived Windows UCI process.
    eng=chess.engine.SimpleEngine.popen_uci([args.engine],timeout=190)
    proc=psutil.Process(eng.transport.get_pid())
    apply_cpu(proc.pid)
    t0=time.monotonic()
    eng.configure({"Hash":HASH_MB,"Threads":THREADS})
    meta["configuration_seconds"]=round(time.monotonic()-t0,3)
    meta["engine_options"]={"Threads":THREADS,"Hash":HASH_MB,"MultiPV":1}
    # Memory pressure can be added only if cloud free RAM exceeds measured laptop headroom,
    # and only up to 512 MiB, preserving cloud capacity for 8192 MiB Stockfish Hash.
    free=psutil.virtual_memory().available
    pc_free=int(pc["ram_available_before_new_process_bytes"])
    reserve=900*MiB
    extra=max(0,min(free-pc_free,free-reserve,512*MiB))
    if extra>=64*MiB:
        try:
            balloon=mmap.mmap(-1,int(extra),access=mmap.ACCESS_WRITE)
            for ix in range(0,int(extra),4096):balloon[ix:ix+1]=b"\0"
            meta["cloud_added_memory_pressure_mb"]=round(extra/MiB)
        except (MemoryError,OSError) as e:
            balloon=None
            meta["cloud_added_memory_pressure_error"]=str(e)
    else:meta["cloud_added_memory_pressure_mb"]=0
    meta["cloud_memory_after_start"]=mem_sample()
    write("metadata.json",meta)
    emit("SELFPLAY_STARTED",{"game":args.game,"pid":proc.pid,"cpu_cap_percent":rate,
          "hash_mb":HASH_MB,"threads":THREADS,
          "book_plies":len(bookline),"pc_age_s":pc["age_at_cloud_start_seconds"],
          "memory":meta["cloud_memory_after_start"]})
    result.update(phase="playing",metadata=meta)
    write("game_result.json",result)
    # TT must persist across all moves. Do NOT issue ucinewgame during the game.
    # A separate GitHub job/process supplies the fresh TT at the next game.
    for ply in range(1,args.max_plies+1):
        if board.outcome(claim_draw=True) is not None:break
        tb=tablebase_adjudication(board)
        if tb:
            result["syzygy"]=tb
            break
        start=time.monotonic()
        play=eng.play(board,chess.engine.Limit(depth=DEPTH),
                      info=chess.engine.INFO_ALL,game=args.game)
        dt=time.monotonic()-start
        info=play.info or {}
        if play.move not in board.legal_moves:raise RuntimeError("Engine returned illegal move")
        sample={"ply":ply,"game":args.game,"move":board.san(play.move),
                "uci":play.move.uci(),"color":"white" if board.turn else "black",
                "search_seconds":round(dt,3),
                "hashfull_per_mille":info.get("hashfull"),
                "hashfull_percent":round(info["hashfull"]/10,2) if isinstance(info.get("hashfull"),int) else None,
                "nodes":info.get("nodes"),"depth":info.get("depth"),
                "seldepth":info.get("seldepth"),"nps":info.get("nps"),
                "score":str(info.get("score")) if info.get("score") is not None else None,
                "mem":mem_sample()}
        moves.append(sample)
        board.push(play.move)
        result["moves"]=moves
        if ply%5==0 or ply<=2:
            write("game_result.json",result)
        emit("MOVE",{"game":args.game,"ply":ply,"hashfull_per_mille":sample["hashfull_per_mille"],
                     "search_seconds":sample["search_seconds"],"move":sample["move"],
                     "available_ram_mb":sample["mem"]["available_mb"]})
    game=chess.pgn.Game.from_board(board)
    game.headers.update({"Event":"Stockfish17.1 8192MiB Hash Cloud Selfplay",
                         "White":"Stockfish 17.1 (1024 threads, Hash 8192)",
                         "Black":"Stockfish 17.1 (1024 threads, Hash 8192)",
                         "Round":str(args.game),"OpeningBook":"Variety.bin",
                         "OpeningBookSHA256":SHA,"SharedTT":"true","Depth":"1"})
    if "syzygy" in result:
        res=result["syzygy"]["result"]
        term=result["syzygy"]["termination"]
    else:
        outcome=board.outcome(claim_draw=True)
        if outcome is None:
            res="*"
            term="MAX_PLIES_INCOMPLETE"
        else:
            res=outcome.result()
            term=str(outcome.termination)
    game.headers["Result"]=res
    game.headers["Termination"]=term
    (out/"game.pgn").write_text(str(game)+"\n",encoding="utf8")
    hf=[x["hashfull_per_mille"] for x in moves if isinstance(x.get("hashfull_per_mille"),int)]
    result.update(phase="finished" if res!="*" else "incomplete",
                  result=res,termination=term,game_plies=len(board.move_stack),
                  searched_plies=len(moves),
                  max_hashfull_per_mille=max(hf) if hf else None,
                  last_hashfull_per_mille=hf[-1] if hf else None,
                  hashfull_reported_moves=len(hf),
                  total_engine_seconds=round(sum(x["search_seconds"] for x in moves),3),
                  metadata=meta)
    write("game_result.json",result)
    emit("GAME_FINISHED",{"game":args.game,"result":res,"termination":term,
          "searched_plies":len(moves),"max_hashfull":result["max_hashfull_per_mille"],
          "last_hashfull":result["last_hashfull_per_mille"]})
except BaseException as ex:
    result.update(phase="technical_failure",error=repr(ex),traceback=traceback.format_exc())
    write("game_result.json",result)
    emit("GAME_TECHNICAL_ERROR",{"game":args.game,"error":repr(ex)})
    raise
finally:
    if eng is not None:
        try:eng.quit()
        except Exception:pass
    if balloon is not None:balloon.close()
