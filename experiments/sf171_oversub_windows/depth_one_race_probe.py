"""Test Stockfish depth-one 1024-thread search node collapse and repeated unchanged searches.
OFF-LAPTOP: Windows GitHub Action only. Hash=64, 1024T, Syzygy not relevant >5 men.
No chess moves committed, separate scratch engine, no tournament impact.
"""
import chess,chess.engine,psutil,json,pathlib,os,time,traceback,statistics
ROOT=pathlib.Path.cwd()
PATH=ROOT/"results"
PATH.mkdir(exist_ok=True)
STOCKFISH=os.environ["STOCKFISH_BINARY"]
MOVES="e2e4 e7e5 g1f3 b8c6 f1b5 g8f6 e1g1 f6e4 d2d4 a7a6 b5c6 d7c6 d1e2 e4f6 d4e5 f6d5 c2c4 d5b6 f1d1 d8e7 c1g5"
def test_board():
 b=chess.Board()
 for m in MOVES.split():b.push_uci(m)
 return b
b=test_board()
assert b.turn==chess.BLACK and b.ply()==21
bad=chess.Move.from_uci("e7e6")
assert bad in b.legal_moves
t=b.copy();t.push(bad)
assert any(t.gives_check(m) and t.is_checkmate() for m in t.legal_moves if m==chess.Move.from_uci("d1d8"))
print("TACTICAL_REPRO_FEN",b.fen(),flush=True)
out=[]
for trial in range(1,4):
 engine=chess.engine.SimpleEngine.popen_uci(STOCKFISH,timeout=240)
 try:
  engine.configure({"Threads":1024,"Hash":64})
  engine.ping()
  proc=psutil.Process(engine.transport.get_pid())
  print("ENGINE_READY",trial,"pid",proc.pid,"workers",proc.num_threads(),flush=True)
  token=object()
  for step in range(1,49):
   before=time.monotonic()
   r=engine.play(b,chess.engine.Limit(depth=1),game=token,info=chess.engine.INFO_ALL)
   elapsed=time.monotonic()-before
   choice=r.move
   m=b.copy();m.push(choice)
   mates=[]
   for reply in m.legal_moves:
    if m.gives_check(reply):
     c=m.copy();c.push(reply)
     if c.is_checkmate():mates.append(reply.uci())
   row={"trial":trial,"step":step,"move":choice.uci(),"mate_in_one_for_opponent":mates,
        "nodes":r.info.get("nodes"),"depth":r.info.get("depth"),
        "seldepth":r.info.get("seldepth"),"native_milliseconds":r.info.get("time"),
        "elapsed_s":round(elapsed,3),"hashfull":r.info.get("hashfull"),
        "cpu_seconds":round(sum(proc.cpu_times()[:2]),2),
        "rss_mb":round(proc.memory_info().rss/2**20),"threads":proc.num_threads()}
   out.append(row)
   if step<=3 or step%8==0 or mates:print("PASS",json.dumps(row),flush=True)
   if step%8==0:(PATH/"passes.json").write_text(json.dumps(out,indent=2))
 finally:
  try:engine.quit()
  except Exception:pass
(PATH/"passes.json").write_text(json.dumps(out,indent=2))
summary={"total":len(out),"mate_in_one_bad_move_passes":sum(bool(x["mate_in_one_for_opponent"]) for x in out),
"first_passes":[x for x in out if x["step"]==1],
"first_median_nodes":statistics.median(x["nodes"] for x in out if x["step"]==1),
"late_median_nodes":statistics.median(x["nodes"] for x in out if x["step"]>=16),
"distinct_moves":sorted(set(x["move"] for x in out)),
"settings":{"Threads":1024,"Hash":64,"go":"depth 1"}}
(PATH/"summary.json").write_text(json.dumps(summary,indent=2))
print("FINISHED",json.dumps(summary),flush=True)
