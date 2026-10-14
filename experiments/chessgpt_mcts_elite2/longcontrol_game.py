#!/usr/bin/env python3
"""One 90'+30" game per runner, independent public progress files."""
import base64,csv,json,os,re,sys,time,urllib.request,urllib.error
from pathlib import Path
import chess,chess.engine,chess.pgn
from match import DIR,OUT,ELITE,WEIGHTS,CHESSGPT,openings,tests

N=int(os.environ["MATCH_GAME_NUMBER"])
assert 1<=N<=20
TOKEN=os.environ.get("GITHUB_TOKEN","")
REPO=os.environ.get("GITHUB_REPOSITORY","")
BRANCH=os.environ.get("GITHUB_REF_NAME","")
PROGRESS=f"experiments/chessgpt_mcts_elite2/live90/game_{N:02d}.md"
OUT.mkdir(exist_ok=True,parents=True)

def publish(markdown):
    if not (TOKEN and REPO and BRANCH):
        print("PUBLIC_CHECKPOINT_UNAVAILABLE missing token/context",flush=True)
        return
    url=f"https://api.github.com/repos/{REPO}/contents/{PROGRESS}"
    headers={"Authorization":f"Bearer {TOKEN}","Accept":"application/vnd.github+json",
             "X-GitHub-Api-Version":"2022-11-28","User-Agent":"ChessGPT-90min-monitor"}
    for attempt in range(3):
        try:
            sha=None
            req=urllib.request.Request(url,headers=headers)
            try:
                with urllib.request.urlopen(req,timeout=15) as response:
                    sha=json.load(response).get("sha")
            except urllib.error.HTTPError as exc:
                if exc.code!=404:raise
            payload={"message":f"Live 90+30 chess game {N} checkpoint",
                     "branch":BRANCH,
                     "content":base64.b64encode(markdown.encode()).decode()}
            if sha:payload["sha"]=sha
            req=urllib.request.Request(url,data=json.dumps(payload).encode(),
                                       headers=headers,method="PUT")
            with urllib.request.urlopen(req,timeout=25) as response:
                response.read()
            print("PUBLIC_CHECKPOINT_UPDATED",N,flush=True)
            return
        except Exception as exc:
            print("PUBLIC_CHECKPOINT_RETRY",N,attempt,str(exc)[:140],flush=True)
            time.sleep(attempt+1)

def points(result,gptwhite):
    w={"1-0":1.0,"0-1":0.0,"1/2-1/2":.5}[result]
    return w if gptwhite else 1-w

def markdown(board,gptwhite,clocks,records,result=None,termination=None):
    data=[
      f"# Live game {N:02d}: ChessGPT vs Elite v2, 90'+30\"",
      "",
      f"**ChessGPT:** {'White' if gptwhite else 'Black'}, original policy, no temperature.",
      "**Elite v2:** one Lc0 MCTS node per move.",
      f"**Status:** {result+' ('+termination+')' if result else 'PLAYING (no result yet)'}",
      f"**Played:** {board.ply()} plies; FEN: {board.fen()}",
      f"**Clocks:** White {clocks[True]:.2f}s, Black {clocks[False]:.2f}s.",
      "",
      "### Observed moves and search statistics",
      "",
      "ChessGPT nodes are MCTS simulations, not distinct leaves. Depth and seldepth are "
      "deepest selected MCTS paths (not full-width minimax depth). Fresh leaves are "
      "Stockfish 16 classical leaf values not served from its cache.",
      "",
      "|Ply|Engine|SAN|Think (s)|Nodes|Depth|SelDepth|Distinct leaves|Fresh values|Observation|",
      "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for e in records[-24:]:
        data.append("|{ply}|{engine}|{san}|{elapsed:.2f}|{nodes}|{depth}|{seldepth}|{distinct}|{fresh}|{comment}|".format(**e))
    data+=["","Only real legal moves are displayed. An incomplete game scores no points.",
           "Paired opening: eight-ply verified Variety.bin; game "+str(N),
           "Full PGN with per-move statistics is uploaded when the game ends."]
    return "\n".join(data)+"\n"

def main():
    tests()
    opening=openings()[(N-1)//2]
    b=chess.Board()
    for u in opening:b.push_uci(u)
    gptwhite=N%2==1
    clocks={True:5400.0,False:5400.0}
    records=[]
    PGN=OUT/f"game_{N:02d}.pgn"
    CSV=OUT/f"game_{N:02d}.csv"
    JS=OUT/f"game_{N:02d}.json"
    for p in (PGN,CSV,JS):p.unlink(missing_ok=True)
    publish(markdown(b,gptwhite,clocks,records))
    print("GAME_START",N,"5400+30","ChessGPT_white",gptwhite,
          "BOOK"," ".join(opening),flush=True)
    token=object()
    with chess.engine.SimpleEngine.popen_uci(
            [sys.executable,"-u",str(CHESSGPT)],timeout=360,cwd=str(DIR)) as gpt, \
         chess.engine.SimpleEngine.popen_uci(
            str(ELITE),timeout=360,cwd=str(ELITE.parent)) as elite:
        expected={"TorchThreads":4,"SANBatchSize":64,"CPuct":900,
                  "RootPolicyBeam":64,"ChildPolicyBeam":64,"ExpandAfterVisits":24}
        for k,v in expected.items():
            assert k in gpt.options and gpt.options[k].default==v,(k,v)
        assert "PolicyTemperature" not in gpt.options
        gpt.configure(expected)
        opts={"WeightsFile":str(WEIGHTS),"Threads":1,"NNCacheSize":1024,
              "MinibatchSize":1,"MaxPrefetch":0,"RamLimitMb":256}
        elite.configure({k:v for k,v in opts.items() if k in elite.options})
        probe=elite.play(chess.Board(),chess.engine.Limit(nodes=1))
        assert probe.move in chess.Board().legal_moves
        print("PREFLIGHT_90_30_PASS",N,flush=True)
        for turn in range(600):
            outcome=b.outcome(claim_draw=True)
            if outcome:
                result=outcome.result()
                termination=outcome.termination.name
                break
            color=b.turn
            isgpt=(color==chess.WHITE)==gptwhite
            engine=gpt if isgpt else elite
            who="ChessGPT" if isgpt else "Elite"
            lim=chess.engine.Limit(white_clock=max(.001,clocks[True]),
               black_clock=max(.001,clocks[False]),white_inc=30.0,black_inc=30.0,
               **({} if isgpt else {"nodes":1}))
            started=time.monotonic()
            response=engine.play(b,lim,game=token,info=chess.engine.INFO_ALL)
            elapsed=time.monotonic()-started
            clocks[color]-=elapsed
            if clocks[color]<=0:
                result="0-1" if color==chess.WHITE else "1-0"
                termination=who+"_FLAG"
                print("GAME_FLAG",N,who,round(elapsed,3),flush=True)
                break
            move=response.move
            if move not in b.legal_moves:
                raise RuntimeError(f"Illegal or absent move {move}, FEN {b.fen()}")
            capture=b.is_capture(move)
            castle=b.is_castling(move)
            san=b.san(move)
            b.push(move)
            clocks[color]+=30
            mcts=response.info.get("string","")
            def extract(name):
                x=re.search(r"(?<!\w)"+re.escape(name)+r" (\d+)",mcts)
                return int(x.group(1)) if x else "-"
            obs="capture" if capture else "castle" if castle else "quiet"
            if b.is_check():obs+=" check"
            if move.promotion:obs+=" promotion"
            ev=dict(ply=b.ply(),engine=who,san=san,elapsed=elapsed,
                nodes=response.info.get("nodes",0),
                depth=response.info.get("depth","-"),
                seldepth=response.info.get("seldepth","-"),
                distinct=extract("unique_leaf_positions") if isgpt else "-",
                fresh=extract("fresh_distinct_classical_leaf_evals") if isgpt else "-",
                comment=obs,
                pv=[m.uci() for m in (response.info.get("pv") or [])[:12]],
                white_clock=round(clocks[True],3),
                black_clock=round(clocks[False],3))
            records.append(ev)
            print("MOVE90",N,b.ply(),who,san,
               "search_seconds",round(elapsed,2),"nodes",ev["nodes"],
               "depth",ev["depth"],"seldepth",ev["seldepth"],
               "distinct",ev["distinct"],"fresh",ev["fresh"],
               "PV"," ".join(ev["pv"][:8]),"observation",obs,flush=True)
            publish(markdown(b,gptwhite,clocks,records))
        else:
            raise RuntimeError("Game exceeded 600 plies: not fabricating a result")
    if not termination.endswith("_FLAG"):
        assert b.outcome(claim_draw=True) and b.outcome(claim_draw=True).result()==result
    game=chess.pgn.Game.from_board(b)
    game.headers.update({"Event":"ChessGPT original policy vs Elite nodes1, 20-game 90+30",
         "Site":"GitHub Actions cloud","Round":str(N),
         "White":"ChessGPT" if gptwhite else "Elite v2 nodes1",
         "Black":"Elite v2 nodes1" if gptwhite else "ChessGPT",
         "Result":result,"TimeControl":"5400+30","Termination":termination,
         "OpeningBook":"Variety.bin paired eight-ply"})
    byply={x["ply"]:x for x in records}
    for node in game.mainline():
        x=byply.get(node.ply())
        if x:
            node.comment=f"{x['engine']} nodes {x['nodes']} depth {x['depth']} seldepth {x['seldepth']} distinct {x['distinct']} fresh {x['fresh']}"
    PGN.write_text(str(game)+"\n\n",encoding="utf-8")
    with CSV.open("w",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        w.writerow(["game","result","chessgpt_color","chessgpt_points","termination","plies"])
        w.writerow([N,result,"white" if gptwhite else "black",
                    points(result,gptwhite),termination,b.ply()])
    JS.write_text(json.dumps({"game":N,"result":result,"termination":termination,
                  "chessgpt_points":points(result,gptwhite),"moves":records},
                  indent=2),encoding="utf-8")
    publish(markdown(b,gptwhite,clocks,records,result=result,termination=termination))
    print("GAME90_COMPLETED",N,result,"points",points(result,gptwhite),
          "termination",termination,"plies",b.ply(),flush=True)

if __name__=="__main__":
    try:main()
    except Exception as e:
        print("GAME90_FAILED",N,repr(e),flush=True)
        raise
