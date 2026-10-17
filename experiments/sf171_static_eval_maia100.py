"""100 paired games: exact SF17.1 static NNUE successor evaluation vs Maia1100 nodes=1.
The adapter calls the original Stockfish Eval::evaluate() only. No Stockfish search.
Opening lines are sampled from the user-matching Jeroen Noomen Variety.bin book.
"""
import collections
import datetime
import hashlib
import json
import os
import pathlib
import random
import subprocess
import time
import chess
import chess.engine
import chess.pgn
import chess.polyglot

ROOT = pathlib.Path.cwd()
SF_STATIC = ROOT / "sf_static_eval"
BIG = ROOT / "sf17/src/nn-1c0000000000.nnue"
SMALL = ROOT / "sf17/src/nn-37f18f62d772.nnue"
LC0 = ROOT / "lc0/build/lc0"
MAIA = ROOT / "maia-1100.pb.gz"
BOOK = ROOT / "Variety.bin"
EXPECTED_BOOK = "5a30bc27b5e7b7d51694bf35e7d5dd2536b54e9b25ea3ea64ef4a2b982d5e21c"
EXPECTED_MAIA = "e1cf1cd0c96b8a4fa6a275f4b9fd54ed1ffebf9fe44641b9fceded310e9619c4"
MATCHES = 100
MAX_PLIES = 1500

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def save_json(obj):
    tmp = ROOT / "scoreboard.json.tmp"
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf8")
    tmp.replace(ROOT / "scoreboard.json")

def log(*x):
    msg = datetime.datetime.now(datetime.timezone.utc).isoformat() + " | " + " ".join(map(str, x))
    print(msg, flush=True)
    with (ROOT / "match.log").open("a", encoding="utf8") as f:
        f.write(msg+"\n")

def openings():
    r = random.Random(20261017)
    seen = set()
    result = []
    with chess.polyglot.open_reader(BOOK) as reader:
        for i in range(MATCHES // 2):
            for trial in range(2000):
                board = chess.Board()
                target = r.randint(8, 24)
                for k in range(target):
                    entries = list(reader.find_all(board))
                    if not entries:
                        break
                    # Polyglot book weights, no engine decisions.
                    entry = r.choices(entries, weights=[max(1, q.weight) for q in entries], k=1)[0]
                    if entry.move not in board.legal_moves:
                        raise RuntimeError("Corrupt Variety book move: "+entry.move.uci())
                    board.push(entry.move)
                key = tuple(m.uci() for m in board.move_stack)
                if board.ply() >= 8 and key not in seen and not board.outcome(claim_draw=False):
                    seen.add(key)
                    result.append((board.copy(),key))
                    break
            else:
                raise RuntimeError("Could not sample 50 distinct opening lines")
    return result

class OriginalStockfishNNUE:
    def __init__(self):
        self.p = subprocess.Popen(
            [str(SF_STATIC),str(BIG),str(SMALL)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=(ROOT/"static_eval_stderr.log").open("w"),
            text=True,bufsize=1
        )
        if self.p.poll() is not None:
            raise RuntimeError("Static evaluator exited immediately")

    def score(self, board):
        self.p.stdin.write(board.fen()+"\n")
        self.p.stdin.flush()
        s=self.p.stdout.readline()
        if not s:
            raise RuntimeError("Stockfish static evaluation adapter failed, exit="+str(self.p.poll()))
        return int(s.strip())

    def move(self,board):
        side=board.turn
        scored=[]
        # All legal moves, no quiescence, no search, no mate prioritization.
        for move in sorted(board.legal_moves, key=lambda m:m.uci()):
            board.push(move)
            score=self.score(board)
            board.pop()
            scored.append((score,move))
        if not scored:
            raise RuntimeError("No legal successor in ongoing position")
        return (max(scored,key=lambda q:q[0]) if side == chess.WHITE
                else min(scored,key=lambda q:q[0]))

    def close(self):
        if self.p.poll() is None:
            self.p.stdin.close()
            self.p.wait(timeout=20)

def main():
    assert sha(BOOK) == EXPECTED_BOOK, "Variety mismatch "+sha(BOOK)
    assert sha(MAIA) == EXPECTED_MAIA, "Maia mismatch "+sha(MAIA)
    assert SF_STATIC.is_file() and LC0.is_file() and BIG.is_file() and SMALL.is_file()
    start=time.monotonic()
    score=collections.Counter()
    status={"phase":"running","completed":0,"total":MATCHES,"maia_wins":0,
            "static_nnue_wins":0,"draws":0,"current_round":1,
            "book_sha256":EXPECTED_BOOK,"maia_sha256":EXPECTED_MAIA,
            "method":"Every legal successor, Stockfish 17.1 original Eval::evaluate() with optimism=0; no search",
            "max_plies":MAX_PLIES}
    save_json(status)
    lines=openings()
    (ROOT/"sampled_openings.json").write_text(json.dumps([{"pair":i+1,"uci":list(moves),"fen":b.fen()} for i,(b,moves) in enumerate(lines)], indent=2))
    log("PREFLIGHT_OK","Variety",sha(BOOK),"Maia",sha(MAIA),"openings",len(lines))
    static=OriginalStockfishNNUE()
    try:
        probe=chess.Board()
        start_eval=static.score(probe)
        probe.push_san("e4"); probe.push_san("e5"); probe.push_san("Qh5")
        check_eval=static.score(probe)   # Tests static NNUE on an unsearched checked successor if checking.
        log("STATIC_EVAL_READY","startpos",start_eval,"probe",check_eval,"no_search")
        with chess.engine.SimpleEngine.popen_uci([str(LC0),"--backend=eigen","--weights="+str(MAIA)], timeout=120) as maia:
            log("MAIA_READY",maia.id)
            for pair_idx,(initial,bookline) in enumerate(lines):
                for reverse in (False,True):
                    gameid=pair_idx*2+int(reverse)+1
                    maia_white=reverse
                    board=initial.copy(stack=True)
                    game=chess.pgn.Game()
                    game.headers.update({
                        "Event":"Stockfish 17.1 Static NNUE vs Maia 1100 Nodes 1",
                        "Site":"GitHub Actions Ubuntu",
                        "Date":datetime.date.today().strftime("%Y.%m.%d"),
                        "Round":str(gameid),
                        "White":"Maia 1100 (Lc0 nodes=1)" if maia_white else "Stockfish 17.1 Static NNUE",
                        "Black":"Stockfish 17.1 Static NNUE" if maia_white else "Maia 1100 (Lc0 nodes=1)",
                        "OpeningBook":"Variety.bin Jeroen Noomen",
                        "OpeningBookSHA256":EXPECTED_BOOK,
                        "MaiaNodes":"1",
                        "StaticEvaluation":"Original SF17.1 NNUE evaluate; no search"
                    })
                    node=game
                    staging=chess.Board()
                    for opening_move in initial.move_stack:
                        node=node.add_variation(opening_move)
                        staging.push(opening_move)
                    assert staging.fen()==board.fen()
                    status["current_round"]=gameid
                    save_json(status)
                    for step in range(MAX_PLIES):
                        outcome=board.outcome(claim_draw=False)
                        if outcome is not None:break
                        whose_maia=(board.turn==chess.WHITE)==maia_white
                        if whose_maia:
                            result=maia.play(board,chess.engine.Limit(nodes=1),game=gameid)
                            move=result.move
                        else:
                            _,move=static.move(board)
                        if move not in board.legal_moves:
                            raise RuntimeError("Illegal move round %s: %s" % (gameid, move))
                        node=node.add_variation(move)
                        board.push(move)
                    else:
                        raise RuntimeError("A game exceeded "+str(MAX_PLIES)+" plies without a chess-rule result. Not scored.")
                    outcome=board.outcome(claim_draw=False)
                    if outcome is None:
                        raise RuntimeError("No chess-rule outcome in round "+str(gameid))
                    label="draw"
                    if outcome.winner is not None:
                        maiawon=(outcome.winner==chess.WHITE)==maia_white
                        label="maia" if maiawon else "static"
                    score[label]+=1
                    game.headers["Result"]=board.result(claim_draw=False)
                    game.headers["Termination"]=str(outcome.termination.name)
                    game.headers["BookPlies"]=str(len(bookline))
                    with (ROOT/"games.pgn").open("a",encoding="utf8") as f:
                        print(game,file=f,end="\n\n")
                    status.update({"completed":gameid,"maia_wins":score["maia"],
                        "static_nnue_wins":score["static"],"draws":score["draw"],
                        "last_result":game.headers["Result"],
                        "last_termination":game.headers["Termination"],
                        "elapsed_seconds":round(time.monotonic()-start,2)})
                    save_json(status)
                    log("GAME_RESULT",gameid,"out_of",MATCHES,game.headers["Result"],
                        game.headers["Termination"],"maia",score["maia"],
                        "static",score["static"],"draws",score["draw"],"plies",board.ply())
            status["phase"]="finished"
            save_json(status)
            log("MATCH_COMPLETED",json.dumps(status))
    finally:
        static.close()

if __name__ == "__main__":
    main()
