"""100 games SF17.1 depth=1 vs nodes=1, exactly 1024 search threads, Variety book.
Memory workaround: two independent UCI instances on one 16GiB cloud runner, each
reconfigured to 1024 Threads only during its move and returned to one idle thread.
This retains separate hash tables and process identities, but changing thread
counts each turn resets thread-local heuristics. Record this methodological caveat.
"""
import argparse,collections,datetime,hashlib,json,os,pathlib,psutil,random,time,traceback
import chess,chess.engine,chess.pgn,chess.polyglot,chess.syzygy
P=argparse.ArgumentParser()
P.add_argument("--engine",required=True)
P.add_argument("--shard",required=True,type=int)
P.add_argument("--shard-count",default=10,type=int)
P.add_argument("--games",default=100,type=int)
P.add_argument("--book",default="Variety.bin")
P.add_argument("--tb",default="syzygy")
args=P.parse_args()
assert args.games==100 and args.shard_count==10 and 0<=args.shard<10
ROOT=pathlib.Path.cwd()
DIR=ROOT / ("match_shard_%02d"%args.shard);DIR.mkdir(exist_ok=True)
BOOK_SHA="5a30bc27b5e7b7d51694bf35e7d5dd2536b54e9b25ea3ea64ef4a2b982d5e21c"
if hashlib.sha256(pathlib.Path(args.book).read_bytes()).hexdigest()!=BOOK_SHA:raise RuntimeError("Wrong Variety opening book")
tb=pathlib.Path(args.tb).resolve()
wdl=list(tb.glob("*.rtbw"));dtz=list(tb.glob("*.rtbz"))
if len(wdl)!=145 or len(dtz)!=145:raise RuntimeError("5-piece TB incomplete "+str((len(wdl),len(dtz))))
with chess.syzygy.open_tablebase(str(tb)) as t:
    probe=chess.Board("8/pp4k1/8/8/8/8/8/3QK3 w - - 0 1")
    if t.probe_wdl(probe) is None:raise RuntimeError("Syzygy probe failed")
rng=random.Random(20261018)
openings=[];seen=set()
with chess.polyglot.open_reader(args.book) as reader:
    for pair in range(50):
        for try_no in range(3000):
            b=chess.Board()
            for ix in range(rng.randint(8,24)):
                choices=list(reader.find_all(b))
                if not choices:break
                entry=rng.choices(choices,weights=[max(1,c.weight) for c in choices],k=1)[0]
                if entry.move not in b.legal_moves:raise RuntimeError("Corrupt book")
                b.push(entry.move)
            key=tuple(x.uci() for x in b.move_stack)
            if b.ply()>=8 and key not in seen and b.outcome(claim_draw=False) is None:
                seen.add(key);openings.append(key);break
        else:raise RuntimeError("Variety lacks 50 distinct sampled openings")
assert len(openings)==50
def write_json(p,data):
    tmp=p.with_suffix(p.suffix+".tmp");tmp.write_text(json.dumps(data,indent=2,default=str),encoding="utf8");tmp.replace(p)
def log(*parts):print(datetime.datetime.now(datetime.timezone.utc).isoformat(),*parts,flush=True)
summary={"phase":"running","completed":0,"shard":args.shard,"games_total":100,"games_this_shard":10,
 "threads_during_search":1024,"idle_threads_between_searches":1,
 "stockfish_default_hash_mb":16,"depth_engine":"go depth 1","nodes_engine":"go nodes 1",
 "five_men_syzygy":True,"five_men_files":[len(wdl),len(dtz)],
 "book_sha256":BOOK_SHA,"results":[],"score":{"depth_wins":0,"nodes_wins":0,"draws":0},
 "caveat":"Independent engine instances and TT, but reconfiguring Threads from 1 to 1024 before every search resets per-thread search state. Necessary for 16 GiB runner."}
savepath=DIR/"status.json"
write_json(savepath,summary)
pairs=[(i,openings[i//2]) for i in range(args.shard*10,(args.shard+1)*10)]
def new_engine(side):
    engine=chess.engine.SimpleEngine.popen_uci(args.engine,timeout=450)
    if "Stockfish 17.1" not in engine.id.get("name",""):raise RuntimeError("Wrong engine "+str(engine.id))
    if engine.options["Hash"].default!=16:raise RuntimeError("Default Hash changed")
    engine.configure({"Threads":1,"SyzygyPath":str(tb),"SyzygyProbeLimit":5,"Syzygy50MoveRule":True})
    engine.ping()
    log("ENGINE_READY",side,"pid",engine.transport.get_pid(),"hash_default",engine.options["Hash"].default)
    return engine
depth=nodes=None
try:
    depth=new_engine("depth");nodes=new_engine("nodes")
    for gameid,bookmoves in pairs:
        complete=False
        for attempt in range(1,5):
            b=chess.Board()
            for move in bookmoves:b.push_uci(move)
            depth_white=gameid%2==0
            plydata=[]
            token=object()
            started=time.monotonic()
            summary["current_game"]=gameid+1
            summary["current_attempt"]=attempt
            write_json(savepath,summary)
            log("GAME_START",gameid+1,"attempt",attempt,"depth_white",depth_white,"bookplies",len(bookmoves))
            try:
                for ply in range(1,1601):
                    ending=b.outcome(claim_draw=True)
                    if ending is not None:break
                    side="depth" if b.turn==depth_white else "nodes"
                    e=depth if side=="depth" else nodes
                    limit=chess.engine.Limit(depth=1) if side=="depth" else chess.engine.Limit(nodes=1)
                    # Set exactly 1024 threads while moving, independently for each engine.
                    configured=False
                    before=time.monotonic()
                    try:
                        e.configure({"Threads":1024})
                        e.ping()  # Wait for Stockfish UCI readyok before verifying worker count.
                        configured=True
                        try:
                            count=psutil.Process(e.transport.get_pid()).num_threads()
                            if count<1024:raise RuntimeError("Requested 1024 threads but only "+str(count)+" observed")
                        except psutil.NoSuchProcess:raise RuntimeError("Stockfish disappeared after thread setup")
                        t=time.monotonic()
                        played=e.play(b,limit,game=token,info=chess.engine.INFO_ALL)
                        search_time=time.monotonic()-t
                    finally:
                        if configured:
                            e.configure({"Threads":1})
                    if played.move is None or played.move not in b.legal_moves:raise RuntimeError("Engine returned invalid move")
                    score=played.info.get("score")
                    data={"game":gameid+1,"ply":b.ply()+1,"side":side,"san":b.san(played.move),"uci":played.move.uci(),
                          "reported_depth":played.info.get("depth"),"seldepth":played.info.get("seldepth"),
                          "nodes":played.info.get("nodes"),"hashfull":played.info.get("hashfull"),
                          "tbhits":played.info.get("tbhits"),"search_sec":round(search_time,3),
                          "total_turn_sec":round(time.monotonic()-before,3),
                          "score":str(score) if score is not None else None}
                    plydata.append(data);b.push(played.move)
                    if len(plydata)%10==0:
                        write_json(DIR/("live_game_%03d.json"%(gameid+1)),
                                   {"game":gameid+1,"round_ply":b.ply(),"last_search":data,"score":summary["score"],"attempt":attempt})
                        log("GAME_PROGRESS",gameid+1,"ply",b.ply(),"mode",side,"nodes",data["nodes"],"tbhits",data["tbhits"])
                ending=b.outcome(claim_draw=True)
                if ending is None:raise RuntimeError("Game exceeded 1600 plies without chess-rule result")
                result=ending.result()
                winner=(None if result=="1/2-1/2" else ("depth" if (result=="1-0")==depth_white else "nodes"))
                g=chess.pgn.Game.from_board(b)
                g.headers.update({"Event":"Stockfish 17.1 1024T Depth1 vs Nodes1","Round":str(gameid+1),
                      "White":"Stockfish17.1 depth1" if depth_white else "Stockfish17.1 nodes1",
                      "Black":"Stockfish17.1 nodes1" if depth_white else "Stockfish17.1 depth1",
                      "Result":result,"Termination":ending.termination.name,
                      "Book":"Variety.bin","BookSHA256":BOOK_SHA,
                      "Threads":"1024 during each search","Hash":"16 default",
                      "Syzygy":"Complete 5-men WDL/DTZ","BookPlies":str(len(bookmoves))})
                (DIR/("game_%03d.pgn"%(gameid+1))).write_text(str(g)+"\n",encoding="utf8")
                with (DIR/"all_moves.jsonl").open("a",encoding="utf8") as f:
                    for row in plydata:f.write(json.dumps(row)+"\n")
                row={"game":gameid+1,"result":result,"winner":winner,"termination":ending.termination.name,
                     "plies":b.ply(),"searched_plies":len(plydata),"book_plies":len(bookmoves),
                     "attempt":attempt,"elapsed_sec":round(time.monotonic()-started,2),
                     "total_nodes_depth":sum(v["nodes"] or 0 for v in plydata if v["side"]=="depth"),
                     "total_nodes_nodes":sum(v["nodes"] or 0 for v in plydata if v["side"]=="nodes")}
                summary["results"].append(row)
                summary["score"]["draws" if winner is None else winner+"_wins"]+=1
                summary["completed"]+=1
                write_json(savepath,summary)
                log("GAME_COMPLETE",json.dumps(row))
                complete=True
                break
            except BaseException as ex:
                log("TECHNICAL_RETRY",gameid+1,attempt,repr(ex))
                (DIR/("technical_error_%03d_%d.txt"%(gameid+1,attempt))).write_text(traceback.format_exc(),encoding="utf8")
                for e in (depth,nodes):
                    if e:
                        try:e.quit()
                        except Exception:pass
                if attempt<4:
                    depth=new_engine("depth");nodes=new_engine("nodes")
            if complete:break
        if not complete:raise RuntimeError("Game "+str(gameid+1)+" failed four technical retries; no adjudicated result")
    summary["phase"]="complete"
    write_json(savepath,summary)
    log("SHARD_COMPLETED",args.shard,summary["score"])
except BaseException:
    summary["phase"]="blocked"
    summary["last_error"]=traceback.format_exc()
    write_json(savepath,summary)
    raise
finally:
    for e in (depth,nodes):
        if e:
            try:e.quit()
            except Exception:pass
