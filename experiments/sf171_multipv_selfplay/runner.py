#!/usr/bin/env python3
"""Stockfish 17.1 depth1/t1 Syzygy5 MultiPV=1 versus MultiPV=256; 100 legal games."""
import datetime, json, os, pathlib, time
import chess, chess.engine, chess.pgn, chess.syzygy

OPENINGS = """
e4 e5
e4 c5
e4 e6
e4 c6
e4 d5
e4 d6
e4 g6
e4 Nf6
e4 b6
e4 Nc6
d4 d5
d4 Nf6
d4 e6
d4 f5
d4 g6
d4 d6
d4 c5
d4 b6
c4 e5
c4 c5
c4 Nf6
c4 e6
c4 c6
c4 g6
Nf3 d5
Nf3 Nf6
Nf3 c5
Nf3 g6
Nf3 d6
g3 d5
g3 Nf6
b3 e5
b3 d5
f4 d5
f4 e5
b4 e5
b4 d5
Nc3 d5
Nc3 e5
e3 d5
e3 Nf6
d3 d5
d3 e5
c3 d5
c3 e5
e4 e5 Nf3 Nc6
e4 e5 Nf3 Nf6
e4 c5 Nf3 d6
d4 d5 c4 e6
d4 Nf6 c4 g6
""".strip().splitlines()
LABELS={1:"Stockfish 17.1 (depth 1, MultiPV 1)",256:"Stockfish 17.1 (depth 1, MultiPV 256)"}
ENGINE="stockfish/stockfish-ubuntu-x86-64-avx2"
TB="tablebases"
OUT=pathlib.Path("results")
OUT.mkdir(exist_ok=True)

def save_json(path, value):
    p=pathlib.Path(path); q=p.with_suffix(".tmp")
    with q.open("w") as f:
        json.dump(value,f,indent=2);f.flush();os.fsync(f.fileno())
    q.replace(p)

def opening(text):
    b=chess.Board()
    for san in text.split(): b.push_san(san)
    if b.is_game_over(claim_draw=True):raise RuntimeError("Bad opening "+text)
    return b

def verify_tb():
    w=list(pathlib.Path(TB).glob("*.rtbw"))
    z=list(pathlib.Path(TB).glob("*.rtbz"))
    if len(w)!=145 or len(z)!=145: raise RuntimeError(f"Missing five-man Syzygy files WDL={len(w)} DTZ={len(z)}")
    with chess.syzygy.open_tablebase(TB) as t:
        for fen in ["7k/8/8/8/8/8/2Q5/K7 w - - 0 1",
                    "7k/6r1/8/8/8/8/2Q5/K7 w - - 0 1",
                    "7k/6r1/8/8/8/8/RR6/K7 w - - 0 1"]:
            b=chess.Board(fen)
            assert b.is_valid()
            print("PROBE",b.fen(),t.probe_wdl(b),t.probe_dtz(b),flush=True)
    print("SYZYGY COMPLETE",len(w),len(z),flush=True)

def run():
    verify_tb()
    opens=[opening(line) for line in OPENINGS]
    if len(opens)!=50 or len(set(b.fen() for b in opens))!=50:
        raise RuntimeError("Exactly 50 unique openings required")
    score={"multipv1_wins":0,"multipv256_wins":0,"draws":0}
    started=time.monotonic()
    with chess.engine.SimpleEngine.popen_uci(ENGINE,timeout=180) as e1, chess.engine.SimpleEngine.popen_uci(ENGINE,timeout=180) as e256:
        for e in (e1,e256):
            e.configure({"Threads":1,"Hash":64,"SyzygyPath":str(pathlib.Path(TB).resolve()),"SyzygyProbeLimit":5})
            if e.options["MultiPV"].max<256:raise RuntimeError("MultiPV 256 unsupported")
            if "17.1" not in e.id.get("name",""):raise RuntimeError("Incorrect Stockfish: "+str(e.id))
            print("ENGINE VERIFIED",e.id,flush=True)
        with (OUT/"all_100_games.pgn").open("w",encoding="utf-8",buffering=1) as pgn:
            for rnd in range(1,101):
                idx=(rnd-1)//2
                white_pv=1 if rnd%2 else 256
                black_pv=256 if white_pv==1 else 1
                board=opens[idx].copy(stack=True)
                g=chess.pgn.Game()
                g.headers.update({"Event":"Stockfish17.1 d1 t1 MPV1 vs MPV256",
                  "Site":"GitHub-hosted Linux runner", 
                  "Date":datetime.datetime.now(datetime.timezone.utc).strftime("%Y.%m.%d"),
                  "Round":str(rnd),"White":LABELS[white_pv],"Black":LABELS[black_pv],
                  "OpeningPair":str(idx+1),"OpeningSeed":OPENINGS[idx],
                  "Engine":"Stockfish 17.1","Depth":"1","Threads":"1",
                  "MultiPVWhite":str(white_pv),"MultiPVBlack":str(black_pv),
                  "SyzygyProbeLimit":"5","Hash":"64"})
                node=g
                for mv in board.move_stack:node=node.add_variation(mv)
                for counter in range(600):
                    outcome=board.outcome(claim_draw=True)
                    if outcome is not None:break
                    pv=white_pv if board.turn else black_pv
                    eng=e1 if pv==1 else e256
                    lines=eng.analyse(board,chess.engine.Limit(depth=1),multipv=pv,game=rnd,info=chess.engine.INFO_ALL)
                    if not lines or not lines[0].get("pv"):raise RuntimeError(f"No line round={rnd} ply={board.ply()}")
                    line=lines[0];move=line["pv"][0]
                    if line.get("depth")!=1:raise RuntimeError(f"Depth mismatch {line.get('depth')}")
                    if move not in board.legal_moves:raise RuntimeError("Illegal UCI move")
                    san=board.san(move)
                    board.push(move);node=node.add_variation(move)
                    if board.ply()%30==0:print("GAME",rnd,"PLY",board.ply(),"PV",pv,"LAST",san,flush=True)
                else:raise RuntimeError(f"Unfinished round {rnd}; no invented result")
                result=outcome.result()
                if outcome.winner is None:score["draws"]+=1
                else:
                    winner=white_pv if outcome.winner==chess.WHITE else black_pv
                    score[f"multipv{winner}_wins"]+=1
                g.headers["Result"]=result
                g.headers["Termination"]=outcome.termination.name
                print(g,file=pgn,end="\n\n",flush=True);os.fsync(pgn.fileno())
                save_json(OUT/"status.json",{"completed":rnd,"total":100,"scoreboard":score.copy(),
                  "last_result":result,"termination":outcome.termination.name,"elapsed_s":round(time.monotonic()-started,2)})
                print("VERIFIED",rnd,result,outcome.termination.name,score,flush=True)
    print("FINAL",score,flush=True)

if __name__=="__main__":run()
