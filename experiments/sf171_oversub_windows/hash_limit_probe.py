"""Cloud-only Stockfish 17.1 1024-thread hash-doubling launch limit experiment.
A successful launch = official engine loads, accepts Threads/Hash, and responds readyok.
One go depth 1 after each successful launch verifies depth setting, but search timeout is
NOT classified as a failed launch. Failures are retried at the same Hash up to ten times.
"""
import argparse,datetime,json,os,pathlib,psutil,queue,re,subprocess,sys,threading,time,traceback
from collections import Counter
P=argparse.ArgumentParser()
P.add_argument("--engine",required=True)
P.add_argument("--initial-hash-mb",type=int,default=64)
P.add_argument("--threads",type=int,default=1024)
P.add_argument("--ready-timeout",type=int,default=75)
P.add_argument("--search-timeout",type=int,default=90)
P.add_argument("--max-hash-mb",type=int,default=33554432)
args=P.parse_args()
assert args.threads==1024 and args.initial_hash_mb>=1 and args.initial_hash_mb&(args.initial_hash_mb-1)==0
assert args.ready_timeout>=20 and args.search_timeout>=15
ROOT=pathlib.Path.cwd()
RESULT=ROOT/"hash_doubling_results.json"
LINES=ROOT/"hash_attempts.jsonl"
FEN="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
metadata={"engine":"Official Stockfish 17.1 Windows x86-64 AVX2","threads":args.threads,
          "depth":1,"multipv":1,"starting_hash_mb":args.initial_hash_mb,
          "probe_definition":"uciok then setoption Threads=1024, Hash=X, MultiPV=1, isready -> readyok",
          "failure_rule":"Ten consecutive unsuccessful fresh-process UCI initialization attempts at the same Hash; any success moves to double Hash",
          "search_validation":"On each successful launch, go depth 1; search timeouts NOT counted as failed launches",
          "system_total_ram_mb":round(psutil.virtual_memory().total/1048576),
          "logical_cpus":psutil.cpu_count(logical=True),
          "start_utc":datetime.datetime.now(datetime.timezone.utc).isoformat()}
state={"metadata":metadata,"levels":[],"attempts":[],"status":"running",
       "failed_hash_mb":None,"final_halved_hash_mb":None,"last_successful_hash_mb":None}
def persist():
    tmp=RESULT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state,indent=2),encoding="utf-8")
    os.replace(tmp,RESULT)
def emit(tag,rec):
    s=json.dumps(rec,separators=(",",":"),default=str)
    print(tag,s,flush=True)
    with LINES.open("a",encoding="utf-8") as f:f.write(tag+" "+s+"\n")
def hardware_snapshot(proc=None):
    vm=psutil.virtual_memory()
    out={"sys_avail_mb":round(vm.available/1048576),"sys_used_pct":vm.percent,
         "sys_commit_used_mb":None}
    try:
        if proc is not None and proc.is_running():
            m=proc.memory_info()
            out.update(process_pid=proc.pid,process_rss_mb=round(m.rss/1048576),
                       process_private_mb=round(m.vms/1048576),process_threads=proc.num_threads())
    except Exception as ex:out["process_sampling_error"]=str(ex)
    return out
def launch(hash_mb,number):
    t0=time.monotonic()
    p=None
    q=queue.Queue()
    recent=[]
    record={"hash_mb":hash_mb,"attempt":number,
            "started_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "preflight":hardware_snapshot(),"launch_ok":False,"search_ok":False,
            "failure_kind":None,"failure_message":None}
    logfile=ROOT/f"uci_hash_{hash_mb}_attempt_{number}.log"
    try:
        p=subprocess.Popen([args.engine],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT,encoding="utf-8",errors="replace",
                           bufsize=1,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        proc=psutil.Process(p.pid)
        record["pid"]=p.pid
        def read():
            try:
                with logfile.open("w",encoding="utf-8",buffering=1) as f:
                    for line in p.stdout:
                        f.write(line)
                        q.put(line.rstrip("\r\n"))
            finally:q.put(None)
        threading.Thread(target=read,daemon=True).start()
        def send(s):
            if p.poll() is not None:raise RuntimeError(f"engine exited code {p.returncode}")
            p.stdin.write(s+"\n");p.stdin.flush()
        def wait(target,timeout):
            end=time.monotonic()+timeout
            while time.monotonic()<end:
                try:
                    s=q.get(timeout=min(.5,max(.01,end-time.monotonic())))
                except queue.Empty:
                    if p.poll() is not None:
                        raise RuntimeError(f"engine died while awaiting {target}; rc={p.returncode}")
                    continue
                if s is None:
                    raise RuntimeError("engine stdout closed; rc="+str(p.poll()))
                recent.append(s)
                if len(recent)>80:recent.pop(0)
                if target(s):return s
            raise TimeoutError(f"awaiting UCI response after {timeout}s: "+str(recent[-6:]))
        send("uci")
        wait(lambda s:s=="uciok",30)
        if not any("Stockfish 17.1" in line for line in recent if line.startswith("id name")):
            raise RuntimeError("unexpected executable version")
        send("setoption name Threads value 1024")
        send(f"setoption name Hash value {hash_mb}")
        send("setoption name MultiPV value 1")
        send("isready")
        wait(lambda s:s=="readyok",args.ready_timeout)
        record["launch_ok"]=True
        record["setup_seconds"]=round(time.monotonic()-t0,3)
        record["after_ready"]=hardware_snapshot(proc)
        emit("LAUNCH_SUCCESS",{"hash_mb":hash_mb,"attempt":number,"setup_seconds":record["setup_seconds"],"after_ready":record["after_ready"]})
        # Validate depth=1, but don't confuse search performance with launchability.
        try:
            send("position fen "+FEN)
            send("go depth 1")
            search_t=time.monotonic()
            reply=wait(lambda s:s.startswith("bestmove "),args.search_timeout)
            record["search_ok"]=True
            record["bestmove"]=reply.split()[1]
            record["search_seconds"]=round(time.monotonic()-search_t,3)
        except Exception as ex:
            record["search_failure_kind"]=type(ex).__name__
            record["search_failure_message"]=str(ex)[:800]
            emit("SEARCH_WARNING",{"hash_mb":hash_mb,"attempt":number,"warning":str(ex)[:250]})
    except Exception as ex:
        record["failure_kind"]=type(ex).__name__
        record["failure_message"]=str(ex)[:800]
        record["recent_output"]=recent[-12:]
        emit("LAUNCH_FAILED",{"hash_mb":hash_mb,"attempt":number,
                                "reason":record["failure_kind"],"message":record["failure_message"]})
    finally:
        record["elapsed_seconds"]=round(time.monotonic()-t0,3)
        if p:
            try:
                if p.poll() is None:
                    p.kill()
                p.wait(timeout=8)
            except Exception as ex:
                record["cleanup_error"]=str(ex)
        record["after_cleanup"]=hardware_snapshot()
        state["attempts"].append(record)
        persist()
    return record

persist()
size=args.initial_hash_mb
try:
    while True:
        if size>args.max_hash_mb:
            state.update(status="stockfish_hash_option_maximum_reached",unresolved_next_hash_mb=size,
                         note="Option's documented 32 TiB maximum reached without observing 10 consecutive failures")
            break
        level={"hash_mb":size,"successes":0,"failures":0,"last_consecutive_failures":0}
        state["levels"].append(level)
        persist()
        for n in range(1,11):
            r=launch(size,n)
            if r["launch_ok"]:
                level["successes"]+=1
                level["last_consecutive_failures"]=0
                state["last_successful_hash_mb"]=size
                emit("HASH_LEVEL_PASS",{"hash_mb":size,"attempt":n,"search_ok":r["search_ok"]})
                persist()
                break
            else:
                level["failures"]+=1
                level["last_consecutive_failures"]+=1
                persist()
                if n==10:
                    state["failed_hash_mb"]=size
                    state["final_halved_hash_mb"]=size//2
                    state["status"]="ten_consecutive_launch_failures"
                    emit("STOP_TEN_FAILURES",{"failed_hash_mb":size,
                             "failures":10,"halved_hash_mb":size//2,
                             "last_successful_hash_mb":state["last_successful_hash_mb"]})
                    break
        if state["status"]!="running":break
        size*=2
    # Actually launch with half the repeatedly failing Hash, not merely print it.
    if state["status"]=="ten_consecutive_launch_failures":
        target=state["final_halved_hash_mb"]
        if target>=1:
            verification=launch(target,"final_half_confirmation")
            state["final_halved_launch_confirmed"]=verification["launch_ok"]
            emit("FINAL_HALF_CONFIRMATION",{"hash_mb":target,
                 "launch_ok":verification["launch_ok"],
                 "search_ok":verification["search_ok"]})
    emit("FINAL_RESULT",{"status":state["status"],
          "first_repeatedly_failed_hash_mb":state["failed_hash_mb"],
          "halved_hash_mb":state["final_halved_hash_mb"],
          "last_successful_hash_mb":state["last_successful_hash_mb"],
          "levels":len(state["levels"]),"attempts":len(state["attempts"])})
except BaseException as ex:
    state["status"]="unhandled_error"
    state["fatal_error"]=repr(ex)
    state["traceback"]=traceback.format_exc()
    emit("FATAL",{"error":repr(ex)})
    raise
finally:persist()
