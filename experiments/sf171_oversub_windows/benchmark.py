"""Read-only Windows Stockfish 17.1 oversubscription/hash latency calibration.
Never accesses the user's machine. Runs solely on the isolated GitHub Actions runner.
This is NOT an Elo or playing-strength test and is not a replacement for 14-CPU hardware.
"""
import argparse, datetime, json, os, pathlib, platform, queue, re, subprocess, sys, threading, time
import psutil

P=argparse.ArgumentParser()
P.add_argument("--engine",required=True)
P.add_argument("--hash",type=int,default=64)
P.add_argument("--threads",type=int,default=1024)
P.add_argument("--per-position-timeout",type=int,default=150)
P.add_argument("--cpu-rate",type=int,default=100,help="Windows Job Object CPU hard cap 5..100 percent")
args=P.parse_args()
assert 1<=args.hash<=8192 and 1<=args.threads<=1024 and 5<=args.cpu_rate<=100
root=pathlib.Path.cwd()
logfile=(root/"uci_output.log").open("w",encoding="utf8",buffering=1)
p=subprocess.Popen([args.engine],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT,encoding="utf8",errors="replace",bufsize=1)

# On Windows this is a real Job Object CPU cap applied directly to the cloud
# Stockfish process, not a claim to recreate the laptop's CPU instruction throughput.
cpu_cap_applied=False
job_handle=None
if args.cpu_rate<100:
    import ctypes
    from ctypes import wintypes
    class CpuRate(ctypes.Structure):
        _fields_=[("ControlFlags",wintypes.DWORD),("CpuRate",wintypes.DWORD)]
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.CreateJobObjectW.restype=wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p]
    kernel.SetInformationJobObject.restype=wintypes.BOOL
    kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,ctypes.c_uint]
    kernel.AssignProcessToJobObject.restype=wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
    job_handle=kernel.CreateJobObjectW(None,None)
    if not job_handle:raise OSError(ctypes.get_last_error(),"CreateJobObjectW failed")
    rate=CpuRate(0x1|0x4,args.cpu_rate*100)
    if not kernel.SetInformationJobObject(job_handle,15,ctypes.byref(rate),ctypes.sizeof(rate)):
        raise OSError(ctypes.get_last_error(),"Cannot establish Job Object CPU cap")
    if not kernel.AssignProcessToJobObject(job_handle,int(p._handle)):
        raise OSError(ctypes.get_last_error(),"Cannot assign Stockfish to Job Object")
    cpu_cap_applied=True
    print("STOCKFISH_CLOUD_CPU_HARD_CAP_APPLIED",args.cpu_rate,flush=True)
proc=psutil.Process(p.pid)
q=queue.Queue()
def reader():
    for line in p.stdout:
        logfile.write(line)
        q.put(line.rstrip("\r\n"))
    q.put(None)
threading.Thread(target=reader,daemon=True).start()
def send(line):
    if p.poll() is not None:raise RuntimeError("Stockfish exited code "+str(p.returncode))
    p.stdin.write(line+"\n")
    p.stdin.flush()
def wait_for(predicate,timeout):
    until=time.monotonic()+timeout
    recent=[]
    while time.monotonic()<until:
        try: item=q.get(timeout=min(0.5,max(.01,until-time.monotonic())))
        except queue.Empty:
            if p.poll() is not None:raise RuntimeError("Stockfish exited unexpectedly")
            continue
        if item is None: raise RuntimeError("Stockfish stdout closed: "+repr(recent[-10:]))
        recent.append(item)
        if predicate(item):return item,recent
    raise TimeoutError("No matching UCI response within "+str(timeout)+" seconds; recent="+repr(recent[-8:]))
def metrics():
    if not proc.is_running():return None
    try:
        m=proc.memory_info()
        t=proc.cpu_times()
        return {"timestamp":round(time.monotonic(),3),"threads":proc.num_threads(),
                "rss_mb":round(m.rss/1048576,2),"private_mb":round(m.vms/1048576,2),
                "cpu_seconds":round(t.user+t.system,3)}
    except(psutil.NoSuchProcess,psutil.AccessDenied):return None

fens=[
    ("startpos","rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"),
    ("open_game","rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"),
    ("middle_game","r1bq1rk1/ppp2ppp/2n1pn2/3p4/3P4/2PBPN2/PP3PPP/RNBQ1RK1 w - - 0 8"),
]
metadata={"platform":platform.platform(),"cpu_model":platform.processor(),
          "logical_cpus":psutil.cpu_count(logical=True),
          "physical_cpus":psutil.cpu_count(logical=False),
          "memory_total_gib":round(psutil.virtual_memory().total/(1024**3),3),
          "stockfish":pathlib.Path(args.engine).name,"threads_requested":args.threads,
          "hash_requested_mb":args.hash,"multipv":1,"depth":1,
          "jobobject_cpu_rate_percent":args.cpu_rate,"jobobject_cpu_cap_verified":cpu_cap_applied,
          "syzygy_probe_limit":5,"syzygy_path":"",
          "syzygy_explanation":"No local 5-man tables on calibration runner. All test FENs have >5 pieces; no Syzygy probes expected.",
          "cpu_topology_caveat":"Standard GitHub Windows runner has 4 vCPUs, user's Intel 225U has 14 logical CPUs.",
          "start_utc":datetime.datetime.now(datetime.timezone.utc).isoformat()}
(root/"runner_metadata.json").write_text(json.dumps(metadata,indent=2),encoding="utf8")
data=[]
try:
    send("uci")
    _,lines=wait_for(lambda s:s=="uciok",30)
    assert any("Stockfish 17.1" in s for s in lines if s.startswith("id name")), "Wrong Stockfish version"
    send("setoption name Threads value "+str(args.threads))
    send("setoption name Hash value "+str(args.hash))
    send("setoption name MultiPV value 1")
    send("setoption name SyzygyProbeLimit value 5")
    send("setoption name Syzygy50MoveRule value true")
    # Warn: no SyzygyPath supplied. All benchmark positions have many pieces.
    send("isready")
    setup_t0=time.monotonic()
    wait_for(lambda s:s=="readyok",180)
    meta0=metrics()
    metadata["engine_setup_seconds"]=round(time.monotonic()-setup_t0,3)
    metadata["post_setup_process"]=meta0
    (root/"runner_metadata.json").write_text(json.dumps(metadata,indent=2),encoding="utf8")
    print("UCI_SETUP",json.dumps({"setup_seconds":metadata["engine_setup_seconds"],"process":meta0}),flush=True)
    for idx,(label,fen) in enumerate(fens,1):
        send("ucinewgame")
        send("isready")
        wait_for(lambda s:s=="readyok",60)
        send("position fen "+fen)
        baseline=metrics()
        go_t=time.monotonic()
        send("go depth 1")
        samples=[]
        lines=[]
        move=None
        while time.monotonic()-go_t<args.per_position_timeout:
            m=metrics()
            if m:samples.append(m)
            try:line=q.get(timeout=0.12)
            except queue.Empty: continue
            if line is None: raise RuntimeError("Stockfish died mid-search")
            lines.append(line)
            if line.startswith("bestmove "):
                move=line.split()[1];break
        elapsed=time.monotonic()-go_t
        if move is None:
            print("POSITION_TIMEOUT",label,"elapsed",round(elapsed,3),flush=True)
            # For this isolated cloud worker only, stop its own engine on timeout.
            send("stop")
            try:wait_for(lambda s:s.startswith("bestmove "),30)
            except Exception:pass
        end=metrics()
        progress=[s for s in lines if s.startswith("info depth ")]
        result={"label":label,"fen":fen,"bestmove":move,"elapsed_seconds":round(elapsed,3),
                "timed_out":move is None,"search_info_last":progress[-1] if progress else None,
                "max_threads_seen":max([s["threads"] for s in samples] or [0]),
                "max_rss_mb":max([s["rss_mb"] for s in samples] or [0]),
                "max_private_mb":max([s["private_mb"] for s in samples] or [0]),
                "samples":samples,"before":baseline,"after":end,
                "search_output":lines[-20:]}
        data.append(result)
        (root/"results.json").write_text(json.dumps({"metadata":metadata,"positions":data},indent=2),encoding="utf8")
        print("POSITION_RESULT",json.dumps({k:v for k,v in result.items() if k not in ("samples","search_output")}),flush=True)
        if result["timed_out"]:break
finally:
    try:
        if p.poll() is None:
            send("quit")
            p.wait(timeout=12)
    except Exception:
        p.kill()
    logfile.close()
print("CALIBRATION_COMPLETE",len(data),"positions",flush=True)
