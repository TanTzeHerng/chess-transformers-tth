"""Fast cloud-only match validation. Never touches the user's local computer."""
import json,os,pathlib,psutil,time,traceback,hashlib
import chess,chess.engine,chess.syzygy,chess.polyglot
root=pathlib.Path.cwd()
stats={"phase":"starting","engines":[],"ram_total_mb":round(psutil.virtual_memory().total/(1024**2)),"tests":[]}
def save():pathlib.Path("match_preflight.json").write_text(json.dumps(stats,indent=2),encoding="utf8")
save()
t=pathlib.Path("syzygy").resolve()
assert len(list(t.glob("*.rtbw")))==145 and len(list(t.glob("*.rtbz")))==145,"Missing five-men TB at expected path: "+str(t)
with chess.syzygy.open_tablebase(t) as tb:
    b=chess.Board("8/pp4k1/8/8/8/8/8/3QK3 w - - 0 1")
    stats["tablebase_probe_wdl"]=tb.probe_wdl(b)
book=pathlib.Path("Variety.bin")
expected="5a30bc27b5e7b7d51694bf35e7d5dd2536b54e9b25ea3ea64ef4a2b982d5e21c"
assert hashlib.sha256(book.read_bytes()).hexdigest()==expected
with chess.polyglot.open_reader(book) as reader:
    assert list(reader.find_all(chess.Board())),"Variety has no start position entries"
engines=[]
try:
    for side in ("depth","nodes"):
        p=chess.engine.SimpleEngine.popen_uci(os.environ["STOCKFISH_BINARY"],timeout=150)
        engines.append(p)
        assert "Stockfish 17.1" in p.id["name"],p.id
        assert p.options["Hash"].default==16,"Stockfish default hash unexpectedly changed"
        p.configure({"Threads":1,"SyzygyPath":str(t),"SyzygyProbeLimit":5,"Syzygy50MoveRule":True})
        p.ping()
        obj=psutil.Process(p.transport.get_pid())
        stats["engines"].append({"side":side,"pid":obj.pid,"idle_threads":obj.num_threads()})
        print("READY",side,obj.pid,flush=True)
    for side,p in zip(("depth","nodes"),engines):
        p.configure({"Threads":1024})
        n=psutil.Process(p.transport.get_pid()).num_threads()
        if n<1024:raise RuntimeError("Stockfish did not start 1024 threads: "+str(n))
        start=time.monotonic()
        cmd=chess.engine.Limit(depth=1) if side=="depth" else chess.engine.Limit(nodes=1)
        out=p.play(chess.Board(),cmd,info=chess.engine.INFO_ALL)
        elapsed=time.monotonic()-start
        p.configure({"Threads":1})
        sample={"side":side,"threads_during_search":n,"elapsed":round(elapsed,3),
                "nodes":out.info.get("nodes"),"depth":out.info.get("depth"),
                "bestmove":out.move.uci(),"post_threads":psutil.Process(p.transport.get_pid()).num_threads()}
        stats["tests"].append(sample)
        print("SEARCH_RESULT",json.dumps(sample),flush=True)
        save()
    stats["phase"]="passed"
except BaseException as ex:
    stats["phase"]="failed";stats["error"]=repr(ex);stats["traceback"]=traceback.format_exc()
    print("PREFLIGHT_FAILURE",repr(ex),flush=True)
    raise
finally:
    save()
    for p in engines:
        try:p.quit()
        except Exception:pass
