"""Consume the live, minimally identifying PC headroom profile on a Windows cloud runner."""
import argparse, json, os, pathlib, time
import psutil
P=argparse.ArgumentParser()
P.add_argument("--mode",choices=["profile_only","run_fixed","run_scaled","run_fixed_with_memory_pressure","run_scaled_with_memory_pressure"],default="profile_only")
args=P.parse_args()
text=os.environ.get("PC_RESOURCE_PROFILE","")
if not text.strip():
    raise SystemExit("PC resource variable not found. PC publisher must upload SF171_PC_RESOURCE_ENVELOPE first.")
data=json.loads(text)
age=time.time()-float(data["timestamp"])
if not (-30 <= age <= 240):
    raise SystemExit("Refusing stale PC resource snapshot; age=%.1fs. Cloud tests must reflect recent conditions."%age)
physical=psutil.virtual_memory()
cloud_cpus=psutil.cpu_count(logical=True) or 1
pc_cpus=int(data["pc_logical_processors"])
pc_idle=max(0.05,min(1.0,float(data["cpu_idle_fraction"])))
ram_mb=int(data["ram_available_before_new_process_bytes"]//(1024*1024))
commit_mb=(data["commit_headroom_bytes"]//(1024*1024) if data.get("commit_headroom_bytes") is not None else None)
cpu_rate_percent=max(5,min(100,round(pc_idle*100)))
scaled_threads=max(1,min(1024,round(1024*cloud_cpus/pc_cpus)))
# Memory balloon is optional because one new 1024-thread Stockfish often exceeds
# the laptop's available physical RAM. The purpose is to recreate paging pressure,
# not claim that available RAM is a guaranteed per-process quota.
balloon_mb=max(0,int(physical.total//(1024*1024))-ram_mb)
plan={
 "mode":args.mode,"pc_sample_age_seconds":round(age,1),
 "pc_snapshot":data,
 "cloud_total_ram_mb":int(physical.total//(1024*1024)),
 "cloud_logical_cpus":cloud_cpus,
 "cloud_available_ram_mb_before_test":int(physical.available//(1024*1024)),
 "target_pc_headroom_ram_mb":ram_mb,
 "target_pc_commit_headroom_mb":commit_mb,
 "target_pc_cpu_idle_fraction":pc_idle,
 "cloud_cpu_jobobject_rate_percent":cpu_rate_percent,
 "cloud_threads_to_preserve_1024":1024,
 "cloud_threads_for_equal_thread_to_cpu_ratio":scaled_threads,
 "cloud_memory_balloon_mb_for_target_headroom":balloon_mb,
 "memory_pressure_enabled":args.mode.endswith("_with_memory_pressure"),
 "cpu_rate_jobobject_required":args.mode!="profile_only",
 "warning":"CPU cap is a fraction of cloud CPU, not 14 laptop CPUs; physical RAM is not a process limit. " 
           "Balloon mode can cause paging or process failure and is only experimental.",
}
pathlib.Path("cloud_resource_plan.json").write_text(json.dumps(plan,indent=2))
print("CLOUD_RESOURCE_PLAN",json.dumps({k:v for k,v in plan.items() if k!="pc_snapshot"}),flush=True)
envfile=os.environ.get("GITHUB_ENV")
if envfile:
    with open(envfile,"a",encoding="utf-8") as f:
        f.write("SF_CLOUD_CPU_RATE="+str(cpu_rate_percent)+"\n")
        f.write("SF_CLOUD_THREADS="+str(scaled_threads if "scaled" in args.mode else 1024)+"\n")
        f.write("SF_CLOUD_BALLOON_MB="+str(balloon_mb if plan["memory_pressure_enabled"] else 0)+"\n")
