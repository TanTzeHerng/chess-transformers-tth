"""Optional resource-replay launcher. Runs ONLY on the cloud Windows runner."""
import argparse,json,mmap,os,pathlib,subprocess,sys,time
P=argparse.ArgumentParser()
P.add_argument("--engine")
P.add_argument("--hash",type=int,default=64)
P.add_argument("--balloon-child",action="store_true")
P.add_argument("--balloon-mb",type=int,default=0)
args=P.parse_args()
plan=json.loads(pathlib.Path("cloud_resource_plan.json").read_text())
if args.balloon_child:
    if args.balloon_mb<1:raise SystemExit("Invalid balloon size")
    count=args.balloon_mb*1024*1024
    print("BALLOON_COMMITTING_MB",args.balloon_mb,flush=True)
    mem=mmap.mmap(-1,count,access=mmap.ACCESS_WRITE)
    for offset in range(0,count,4096):
        mem[offset:offset+1]=b"x"
    pathlib.Path("balloon_ready.txt").write_text(str(args.balloon_mb),encoding="ascii")
    print("BALLOON_READY",args.balloon_mb,flush=True)
    while True:time.sleep(60)
else:
    if not args.engine:raise SystemExit("--engine required")
    if plan["mode"]=="profile_only":raise SystemExit("Nothing to benchmark in profile-only mode")
    fixed="fixed" in plan["mode"]
    threads=1024 if fixed else plan["cloud_threads_for_equal_thread_to_cpu_ratio"]
    cpu_rate=plan["cloud_cpu_jobobject_rate_percent"]
    balloon_mb=plan["cloud_memory_balloon_mb_for_target_headroom"] if plan["memory_pressure_enabled"] else 0
    if balloon_mb and balloon_mb>0.88*plan["cloud_total_ram_mb"]:
        raise SystemExit("Memory balloon exceeds safety ceiling")
    subprocess.run([sys.executable,"-m","pip","--version"],check=True,stdout=subprocess.DEVNULL)
    child=None
    try:
        if balloon_mb:
            ready=pathlib.Path("balloon_ready.txt")
            ready.unlink(missing_ok=True)
            log=open("cloud_balloon.log","w",buffering=1,encoding="utf-8")
            child=subprocess.Popen([sys.executable,__file__,"--balloon-child","--balloon-mb",str(balloon_mb)],
                                   stdout=log,stderr=subprocess.STDOUT)
            until=time.monotonic()+100
            while time.monotonic()<until and not ready.exists() and child.poll() is None:
                time.sleep(.25)
            if not ready.exists():raise RuntimeError("Cloud memory balloon failed to initialize")
        print("START_CLOUD_STOCKFISH",json.dumps({"threads":threads,"hash_mb":args.hash,
              "cpu_rate":cpu_rate,"memory_pressure_mb":balloon_mb}),flush=True)
        result=subprocess.run([sys.executable,"-u","experiments/sf171_oversub_windows/benchmark.py",
                "--engine",args.engine,"--threads",str(threads),"--hash",str(args.hash),
                "--cpu-rate",str(cpu_rate)])
        if result.returncode:raise SystemExit(result.returncode)
    finally:
        if child:
            child.terminate()
            try:child.wait(timeout=12)
            except subprocess.TimeoutExpired:child.kill()
