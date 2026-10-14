import csv,os,pathlib,chess,chess.pgn
n=int(os.environ["MATCH_GAME_NUMBER"])
out=pathlib.Path("results")
pgn=out/f"game_{n:02d}.pgn"
csvpath=out/f"game_{n:02d}.csv"
jsonpath=out/f"game_{n:02d}.json"
assert pgn.is_file() and csvpath.is_file() and jsonpath.is_file(),(n,"missing game output")
with pgn.open(encoding="utf-8") as f:
    game=chess.pgn.read_game(f)
    assert game and chess.pgn.read_game(f) is None,(n,"not exactly one PGN")
assert not game.errors,(n,game.errors)
assert int(game.headers["Round"])==n
assert game.headers["TimeControl"]=="5400+30"
assert game.headers["Result"] in ("1-0","0-1","1/2-1/2")
board=game.end().board()
assert board.is_valid()
assert len(list(game.mainline_moves()))>8
term=game.headers["Termination"]
if not term.endswith("_FLAG"):
    outcome=board.outcome(claim_draw=True)
    assert outcome and outcome.result()==game.headers["Result"],(n,term,board.fen())
else:
    assert game.headers["Result"] in ("1-0","0-1"),(n,term)
rows=list(csv.DictReader(csvpath.open(encoding="utf-8")))
assert len(rows)==1 and int(rows[0]["game"])==n
assert rows[0]["result"]==game.headers["Result"] and rows[0]["termination"]==term
white=n%2==1
want={"1-0":1.0,"0-1":0.0,"1/2-1/2":0.5}[game.headers["Result"]]
want=want if white else 1-want
assert float(rows[0]["chessgpt_points"])==want
print("GAME90_RULES_VALIDATED",n,game.headers["Result"],term,
      "ChessGPT points",want,"plies",board.ply(),flush=True)
