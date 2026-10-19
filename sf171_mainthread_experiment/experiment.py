import collections,json,os,pathlib,queue,re,statistics,subprocess,threading,time,traceback
ROOT=pathlib.Path(__file__).resolve().parent
SRC=ROOT/"Stockfish/src/search.cpp"
EXE=ROOT/"Stockfish/src/stockfish"
OUT=ROOT/"results";OUT.mkdir(exist_ok=True)
MOVES="e2e4 e7e5 g1f3 d7d6 f1c4 c8g4 b1c3 g7g6 f3e5 g4d1 c4f7 e8e7 c3d5".split()
# Complete historical Legal's Mate; analyze every pre-move position, excluding terminal mate.
POS=[(f"ply{n:02d}", "position startpos"+(" moves "+" ".join(MOVES[:n]) if n else "")) for n in range(len(MOVES))]
CONDITIONS=["baseline","nice5","nice12","nice19","sched_idle","affinity_one_core"]
REGISTER=re.compile(r"TRACE_REGISTER idx=0 tid=(\d+)")
DEPTH=re.compile(r"TRACE_DEPTH idx=(\d+) tid=(\d+) entered=(\d+) completed=(\d+) nodes=(\d+)")
def patch():
    s=SRC.read_text()
    def rep(a,b):
        nonlocal s
        if s.count(a)!=1:raise ValueError("Patch anchor count="+str(s.count(a))+" "+a[:100])
        s=s.replace(a,b,1)
    rep("#include <iostream>\n","#include <iostream>\n#include <mutex>\n#include <cstdio>\n#include <unistd.h>\n#include <sys/syscall.h>\n")
    rep("namespace TB = Tablebases;","namespace TB = Tablebases;\nstatic std::mutex trace_mutex;")
    a="    clear();\n}\n\nvoid Search::Worker::ensure_network_replicated() {"
    b="""    clear();
    if (threadIdx == 0) {
        std::lock_guard<std::mutex> g(trace_mutex);
        std::fprintf(stderr,"TRACE_REGISTER idx=0 tid=%ld\\n",(long)syscall(SYS_gettid));
        std::fflush(stderr);
    }
}

void Search::Worker::ensure_network_replicated() {"""
    rep(a,b)
    rep("    // Iterative deepening loop until requested to stop or the target depth is reached",
        "    Depth trace_max_entered = 0;\n    // Iterative deepening loop until requested to stop or the target depth is reached")
    a="    while (++rootDepth < MAX_PLY && !threads.stop\n           && !(limits.depth && mainThread && rootDepth > limits.depth))\n    {"
    rep(a,a+"\n        trace_max_entered = std::max(trace_max_entered,rootDepth);")
    a="    if (!mainThread)\n        return;\n\n    mainThread->previousTimeReduction = timeReduction;"
    b="""    {
        std::lock_guard<std::mutex> g(trace_mutex);
        std::fprintf(stderr,"TRACE_DEPTH idx=%zu tid=%ld entered=%d completed=%d nodes=%llu\\n",
          threadIdx,(long)syscall(SYS_gettid),int(trace_max_entered),int(completedDepth),
          (unsigned long long)nodes.load());
        std::fflush(stderr);
    }
"""+a
    rep(a,b)
    SRC.write_text(s)
    print("TRACE_ONLY_PATCH_APPLIED",flush=True)
class SF:
    def __init__(self):
        self.p=subprocess.Popen([str(EXE)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,bufsize=1,start_new_session=True)
        self.oq=queue.Queue();self.eq=queue.Queue();self.ol=[];self.el=[]
        for st,q,l in [(self.p.stdout,self.oq,self.ol),(self.p.stderr,self.eq,self.el)]:
            threading.Thread(target=self.reader,args=(st,q,l),daemon=True).start()
    @staticmethod
    def reader(st,q,l):
        for x in st:
            x=x.rstrip("\n");l.append(x);q.put(x)
        q.put(None)
    def send(self,s):self.p.stdin.write(s+"\n");self.p.stdin.flush()
    def wait(self,fn,secs=90,q=None):
        q=q or self.oq
        t=time.monotonic()+secs
        while time.monotonic()<t:
            try:x=q.get(timeout=min(1,max(.01,t-time.monotonic())))
            except queue.Empty:continue
            if x is None:raise EOFError()
            if fn(x):return x
        raise TimeoutError(f"search timeout last={self.ol[-4:]}")
    def close(self):
        try:self.send("quit");self.p.wait(timeout=5)
        except Exception:self.p.kill();self.p.wait(timeout=5)
def apply(tid,mode):
    before={"nice":os.getpriority(os.PRIO_PROCESS,tid),"scheduler":os.sched_getscheduler(tid),
      "affinity":sorted(os.sched_getaffinity(tid))}
    if mode.startswith("nice"):os.setpriority(os.PRIO_PROCESS,tid,int(mode[4:]))
    elif mode=="sched_idle":os.sched_setscheduler(tid,os.SCHED_IDLE,os.sched_param(0))
    elif mode=="affinity_one_core":os.sched_setaffinity(tid,{min(os.sched_getaffinity(tid))})
    elif mode!="baseline":raise ValueError(mode)
    return {"before":before,"after":{"nice":os.getpriority(os.PRIO_PROCESS,tid),
      "scheduler":os.sched_getscheduler(tid),"affinity":sorted(os.sched_getaffinity(tid))}}
def one_condition(mode):
    print("CONDITION",mode,flush=True)
    e=SF();measurements=[]
    try:
        e.send("uci");e.wait(lambda x:x=="uciok")
        for command in ["setoption name Threads value 1024","setoption name MultiPV value 1",
             "setoption name Hash value 16","setoption name UCI_LimitStrength value false","isready"]:
            e.send(command)
        e.wait(lambda x:x=="readyok",150)
        # setoption Threads destroys the original default worker and creates a new main worker.
        # Pick the LAST registration after isready, not the obsolete startup thread.
        registrations=[int(REGISTER.search(x).group(1)) for x in e.el if REGISTER.search(x)]
        if not registrations:raise RuntimeError("No registered main search worker")
        tid=registrations[-1]
        print("MAIN_THREAD_REGISTRATIONS",registrations,"SELECTED",tid,flush=True)
        schedule=apply(tid,mode)
        print("SCHEDULER",mode,"TID",tid,json.dumps(schedule),flush=True)
        for number,(label,fencommand) in enumerate(POS,1):
            e.send("ucinewgame");e.send("isready");e.wait(lambda x:x=="readyok",100)
            before=len(e.el);e.send(fencommand);start=time.monotonic();e.send("go depth 1")
            timedout=False
            try:bm=e.wait(lambda x:x.startswith("bestmove "),35)
            except TimeoutError:
                timedout=True;e.send("stop")
                try:bm=e.wait(lambda x:x.startswith("bestmove "),40)
                except TimeoutError:bm="MISSING_BESTMOVE"
            dt=round(time.monotonic()-start,3)
            # Wait for the stderr reader to drain all worker records before taking a snapshot.
            # bestmove arrives only AFTER helper threads have exited, but their pipe may lag.
            drain_deadline=time.monotonic()+12
            while time.monotonic()<drain_deadline:
                seen={int(m.group(1)) for line in e.el[before:] if (m:=DEPTH.search(line))}
                if len(seen)>=1024:break
                time.sleep(.025)
            byidx={}
            for line in e.el[before:]:
                m=DEPTH.search(line)
                if m:
                    idx,tid_,entered,completed,nodes=map(int,m.groups())
                    byidx[idx]={"tid":tid_,"entered":entered,"completed":completed,"nodes":nodes}
            row={"position":label,"game_number":number,"seconds":dt,
                 "bestmove":bm,"timedout":timedout,"thread_data":byidx}
            measurements.append(row)
            (OUT/(mode+".json")).write_text(json.dumps({"mode":mode,"os_thread_setting":schedule,
                   "positions":measurements},indent=2))
            print("RESULT",mode,label,"workers",len(byidx),"max_entered",
                max((z["entered"] for z in byidx.values()),default=-1),
                "max_completed",max((z["completed"] for z in byidx.values()),default=-1),
                "seconds",dt,"timeout",timedout,"best",bm,flush=True)
    finally:
        (OUT/(mode+".stderr.txt")).write_text("\n".join(e.el))
        (OUT/(mode+".stdout.txt")).write_text("\n".join(e.ol))
        e.close()
def summarize():
    out=[]
    for mode in CONDITIONS:
        p=OUT/(mode+".json")
        if not p.exists():continue
        d=json.loads(p.read_text());vals=[];com=[];worker_max=collections.defaultdict(list)
        for row in d["positions"]:
            for idx,v in row["thread_data"].items():
                vals.append(v["entered"]);com.append(v["completed"]);worker_max[idx].append(v["entered"])
        out.append({"condition":mode,"positions":len(d["positions"]),"samples":len(vals),
         "workers":len(worker_max),"mean_max_entered":round(statistics.mean(vals),3) if vals else None,
         "mean_max_completed":round(statistics.mean(com),3) if com else None,
         "max_entered":max(vals,default=None),"max_completed":max(com,default=None),
         "entered_hist":dict(sorted(collections.Counter(vals).items())),
         "completed_hist":dict(sorted(collections.Counter(com).items())),
         "worker_across_positions_max_hist":dict(sorted(collections.Counter(max(v) for v in worker_max.values()).items())),
         "mean_seconds":round(statistics.mean(x["seconds"] for x in d["positions"]),3)})
    (OUT/"summary.json").write_text(json.dumps(out,indent=2))
    print("SUMMARY",json.dumps(out),flush=True)
if __name__=="__main__":
    patch()
    print("BUILD_START",flush=True)
    subprocess.run(["make","-j4","build","ARCH=x86-64-avx2"],cwd=str(ROOT/"Stockfish/src"),check=True)
    try:
        for mode in CONDITIONS:
            try:one_condition(mode)
            except Exception as ex:
                print("ERROR",mode,str(ex),traceback.format_exc(),flush=True)
                (OUT/(mode+".error.txt")).write_text(traceback.format_exc())
    finally:summarize()
