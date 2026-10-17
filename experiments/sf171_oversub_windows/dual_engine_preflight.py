"""Preflight only: two *independent* unmodified Stockfish 17.1 instances on a cloud runner.
Does not touch the user's PC or modify Stockfish's settings except Threads.
"""
import json,os,pathlib,psutil,time,traceback,urllib.request,re,sys
import chess,chess.engine
P=pathlib.Path.cwd()
res={"phase":"starting","engines":[],"mem_total_mb":round(psutil.virtual_memory().total/2**20),
     "mem_before_mb":round(psutil.virtual_memory().available/2**20)}
def save(): (P/"dual_engine_preflight.json").write_text(json.dumps(res,indent=2,default=str))
save()
e=[]
try:
 for side in ("depth1","nodes1"):
  p=chess.engine.SimpleEngine.popen_uci(os.environ["STOCKFISH_BINARY"],timeout=110)
  e.append(p)
  if "Stockfish 17.1" not in p.id.get("name",""):raise RuntimeError("Unexpected SF "+str(p.id))
  p.configure({"Threads":1024})
  p.ping()
  proc=psutil.Process(p.transport.get_pid())
  res["engines"].append({"side":side,"pid":proc.pid,"threads":proc.num_threads(),
    "private_mb":round(proc.memory_info().vms/2**20),"rss_mb":round(proc.memory_info().rss/2**20),
    "hash_option_default_mb":p.options["Hash"].default})
  print("ENGINE_READY",res["engines"][-1],flush=True)
  save()
 print("MEMORY_BOTH_READY",json.dumps({"available_mb":round(psutil.virtual_memory().available/2**20),
                                        "swap_used_mb":round(psutil.swap_memory().used/2**20)}),flush=True)
 for side,p in zip(("depth1","nodes1"),e):
  b=chess.Board()
  limit=chess.engine.Limit(depth=1) if side=="depth1" else chess.engine.Limit(nodes=1)
  start=time.monotonic()
  out=p.play(b,limit,info=chess.engine.INFO_ALL,timeout=110) if False else p.play(b,limit,info=chess.engine.INFO_ALL)
  elapsed=time.monotonic()-start
  r={"side":side,"bestmove":out.move.uci(),"elapsed_s":round(elapsed,3),
     "reported_depth":out.info.get("depth"),"nodes":out.info.get("nodes"),"hashfull":out.info.get("hashfull")}
  res["searches"]=res.get("searches",[])+[r]
  print("SEARCH",json.dumps(r),flush=True)
  save()
 # Check real Syzygy directory listing only; don't download 940MB in preflight.
 urls=["https://tablebases.sesse.net/syzygy/3-4-5/","https://tablebase.lichess.ovh/tables/standard/3-4-5/"]
 for url in urls:
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"StockfishStudy/1.0"}),timeout=22) as v:
    page=v.read(500000).decode("utf8","replace")
   names=re.findall(r'href="([^"]+\.rtb[zw])"',page,re.I)
   print("SYZYGY_INDEX",url,"entries",len(names),"first",names[:4],flush=True)
   if len(names)>200:
    res["syzygy_source"]={"url":url,"entries":len(names),"examples":names[:3]}
    break
  except Exception as ex:print("SYZYGY_SOURCE_FAILURE",url,repr(ex),flush=True)
 if not res.get("syzygy_source"):raise RuntimeError("No complete 5-men Syzygy manifest source")
 res["phase"]="passed"
except BaseException as ex:
 res["phase"]="failed";res["error"]=repr(ex);res["traceback"]=traceback.format_exc()
 print("PREFLIGHT_FAILURE",repr(ex),flush=True)
 raise
finally:
 save()
 for p in e:
  try:p.quit()
  except Exception:pass
