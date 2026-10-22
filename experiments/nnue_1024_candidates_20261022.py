#!/usr/bin/env python3
"""Stockfish 17.1 NNUE paired Variety study, with 1024 UCI threads.
Run only on free public-repository GitHub-hosted runners, never on user's PC.
Chess-rule terminations only; engine failures and timeouts are unscored.
"""
import argparse, datetime, hashlib, json, os, pathlib, queue, subprocess, sys, threading, time, urllib.request
import chess, chess.pgn

OPENINGS=[
["g1f3","d7d5","d2d4","g8f6","c1f4","e7e6","e2e3","c7c5","b1d2","d8b6"],
["g1f3","d7d5","g2g3","g7g6","f1g2","f8g7","d2d4","g8f6","c2c4","c7c6","c4d5","c6d5"],
["b2b3","e7e5","c1b2","b8c6","e2e3","g8f6","g1f3","e5e4","f3d4","h7h5","c2c4","h8h6"],
["e2e4","c7c6","g1f3","d7d5","e4e5","c8g4","f1e2","c6c5","c2c4"],
["d2d4","g8f6","c1g5","c7c5","d4d5","f6e4"],
["e2e4","c7c6","d2d4","d7d5","e4e5","c8f5","g1f3","e7e6","f1e2","b8d7"],
["b2b3","d7d5","c1b2","g8f6","e2e3","c7c5","g1f3","g7g6","c2c4"],
["d2d4","g8f6","c2c4","e7e6","g1f3","b7b6","b1c3","f8b4","c1g5","h7h6","g5h4","c8b7"],
]
BASE="nn-1c0000000000"
SMALL="nn-37f18f62d772"
CANDIDATES=("nn-e8bac1c07a5a","nn-ddcfb9224cdb")
THREADS=1024
HASH=16

def emit(path,obj):
    path=pathlib.Path(path)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(obj,indent=2,default=str),encoding="utf-8")
    os.replace(tmp,path)

def get_nnue(name,directory):
    path=directory/(name+".nnue")
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest().startswith(name[3:]):return path
    url="https://tests.stockfishchess.org/api/nn/"+name+".nnue"
    print("DOWNLOADING",url,flush=True)
    with urllib.request.urlopen(url,timeout=180) as stream,path.open("wb") as target:
        while True:
            chunk=stream.read(1024*1024)
            if not chunk:break
            target.write(chunk)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if not digest.startswith(name[3:]):
        path.unlink(missing_ok=True)
        raise RuntimeError("NNUE integrity mismatch "+name+" "+digest)
    print("NET_VERIFIED",name,path.stat().st_size,digest,flush=True)
    return path

class UCI:
    def __init__(self,exe,small,log):
        self.stderr=open(log,"ab",buffering=0)
        self.p=subprocess.Popen([str(exe)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=self.stderr,text=True,bufsize=1)
        self.output=queue.Queue()
        def reader():
            for line in self.p.stdout:self.output.put(line.rstrip("\r\n"))
            self.output.put("<<EOF>>")
        threading.Thread(target=reader,daemon=True).start()
        self.cmd("uci")
        banner=self.until("uciok",45)
        if not any("Stockfish 17.1" in x for x in banner):
            raise RuntimeError("Stockfish version not 17.1: "+repr(banner[:5]))
        options="\n".join(banner)
        for option in ("Threads","Hash","EvalFile","EvalFileSmall","MultiPV"):
            if "option name "+option+" " not in options:
                raise RuntimeError("Missing required option "+option)
        self.cmd("setoption name Threads value 1024")
        self.cmd("setoption name Hash value 16")
        self.cmd("setoption name MultiPV value 1")
        self.cmd("setoption name EvalFileSmall value "+str(small))
        self.ready()
    def cmd(self,line):
        if self.p.poll() is not None:raise RuntimeError("Stockfish exited "+str(self.p.returncode))
        self.p.stdin.write(line+"\n");self.p.stdin.flush()
    def until(self,needle,seconds):
        deadline=time.monotonic()+seconds;out=[]
        while time.monotonic()<deadline:
            try:line=self.output.get(timeout=min(1,max(.001,deadline-time.monotonic())))
            except queue.Empty:continue
            if line=="<<EOF>>":raise RuntimeError("Stockfish EOF "+repr(out[-6:]))
            out.append(line)
            if line.startswith("info string ERROR") or line.startswith("Unknown command"):
                raise RuntimeError("Engine error "+line)
            if line.startswith(needle):return out
        raise TimeoutError("Timed out waiting for "+needle+"; tail "+repr(out[-6:]))
    def ready(self):
        self.cmd("isready");self.until("readyok",90)
    def move(self,board,net):
        self.cmd("setoption name EvalFile value "+str(net))
        self.cmd("setoption name Clear Hash")
        self.ready()
        self.cmd("position startpos moves "+ " ".join(x.uci() for x in board.move_stack))
        self.cmd("go depth 1")
        lines=self.until("bestmove ",150)
        mv=next(x for x in reversed(lines) if x.startswith("bestmove "))
        fields=mv.split()
        if len(fields)<2 or fields[1] in ("(none)","0000"):
            raise RuntimeError("No bestmove in nonterminal position")
        try:move=chess.Move.from_uci(fields[1])
        except ValueError:raise RuntimeError("Bad bestmove "+repr(fields[1]))
        if move not in board.legal_moves:raise RuntimeError("Illegal bestmove "+fields[1])
        info=[x for x in lines if x.startswith("info depth")]
        return move,(info[-1] if info else "")
    def close(self):
        if self.p.poll() is None:
            try:self.cmd("quit");self.p.wait(timeout=3)
            except Exception:self.p.kill()
        self.stderr.close()

def play(uci,opening,white_candidate,candidate,baseline,idx,outdir):
    game=chess.pgn.Game()
    game.headers.update({"Event":"NNUE 1024 threads Variety paired comparison",
        "White":candidate.name if white_candidate else baseline.name,
        "Black":baseline.name if white_candidate else candidate.name,
        "Round":str(idx+1),"Date":datetime.date.today().strftime("%Y.%m.%d")})
    board=chess.Board();cursor=game
    for value in opening:
        move=chess.Move.from_uci(value)
        if move not in board.legal_moves:raise RuntimeError("Invalid Variety opening: "+value)
        cursor=cursor.add_variation(move);board.push(move)
    uci.cmd("ucinewgame")
    uci.ready()
    telemetry=[]
    for ply in range(1,402):
        outcome=board.outcome(claim_draw=True)
        if outcome:
            result=outcome.result()
            game.headers["Result"]=result
            game.headers["Termination"]=str(outcome.termination.name)
            game.headers["Candidate"]=candidate.name
            game.headers["Baseline"]=baseline.name
            game.headers["UCIThreads"]="1024"
            game.headers["UCIDepth"]="1"
            game.headers["HashMB"]="16"
            score=1 if (result=="1-0")==white_candidate and result!="1/2-1/2" else 0
            if result=="1/2-1/2":score=.5
            return {"valid":True,"result":result,"candidate_score":score,
                    "candidate_white":white_candidate,"game_pgn":str(game),
                    "plies":len(board.move_stack),"termination":outcome.termination.name,
                    "telemetry":telemetry}
        is_candidate=(board.turn==chess.WHITE)==white_candidate
        net=candidate if is_candidate else baseline
        start=time.monotonic()
        move,info=uci.move(board,net)
        dur=round(time.monotonic()-start,3)
        entry={"ply":len(board.move_stack)+1,"candidate_to_move":is_candidate,
               "uci":move.uci(),"seconds":dur,"info":info[-300:]}
        telemetry.append(entry)
        cursor=cursor.add_variation(move);board.push(move)
        if ply%8==0:
            emit(outdir/"live.json",{"state":"playing","game":idx+1,"current_ply":len(board.move_stack),
                "candidate_white":white_candidate,"last_move":entry,"saved_at":time.time()})
    raise RuntimeError("Exceeded 400 searched plies without chess-rule termination; UNCOUNTED")

def main():
    pa=argparse.ArgumentParser()
    pa.add_argument("--exe",type=pathlib.Path,required=True)
    pa.add_argument("--candidate",choices=CANDIDATES,required=True)
    pa.add_argument("--opening",type=int,choices=range(len(OPENINGS)),required=True)
    pa.add_argument("--out",type=pathlib.Path,required=True)
    args=pa.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    emit(args.out/"status.json",{"state":"preparing","candidate":args.candidate,
        "opening_index":args.opening,"threads":THREADS,"depth":1,"hash_mb":HASH})
    # Download and validate all exact network bytes. No alternate candidate permitted.
    netdir=args.out/"nets";netdir.mkdir(exist_ok=True)
    baseline=get_nnue(BASE,netdir)
    candidate=get_nnue(args.candidate,netdir)
    small=get_nnue(SMALL,netdir)
    engine=UCI(args.exe.resolve(),small,args.out/"stockfish_stderr.log")
    games=[]
    try:
        for n in (0,1):
            white_candidate=n==0
            try:
                game=play(engine,OPENINGS[args.opening],white_candidate,candidate,baseline,
                          2*args.opening+n,args.out)
                games.append(game)
                print("VALID_GAME",args.opening,n,game["result"],game["candidate_score"],
                      game["plies"],flush=True)
                with (args.out/"completed_games.pgn").open("a",encoding="utf8") as f:
                    f.write(game["game_pgn"]+"\n\n")
                emit(args.out/"results.json",{"candidate":args.candidate,
                    "opening_index":args.opening,"finished_valid":len(games),
                    "games":[{k:v for k,v in item.items() if k not in ("game_pgn","telemetry")} for item in games]})
            except BaseException as exc:
                print("TECHNICAL_GAME_UNSCORED",repr(exc),flush=True)
                emit(args.out/"status.json",{"state":"technical_failure","reason":repr(exc),
                    "valid_games":len(games),"candidate":args.candidate,
                    "opening_index":args.opening,"failed_game":n+1})
                raise
        emit(args.out/"status.json",{"state":"completed","valid_games":2,
            "candidate":args.candidate,"opening_index":args.opening,
            "score":sum(x["candidate_score"] for x in games),
            "threads":1024,"depth":1,"hash_mb":16})
    finally:engine.close()
if __name__=="__main__":main()
