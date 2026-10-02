import sys, time
from pathlib import Path
import chess, chess.engine, chess.pgn

yolo=Path(sys.argv[1]).resolve()
lc0=Path(sys.argv[2]).resolve()
net=Path(sys.argv[3]).resolve()
outp=Path("YOLOChess-NNUE-v2-vs-Elite-v2.pgn")

white=chess.engine.SimpleEngine.popen_uci(str(yolo))
black=chess.engine.SimpleEngine.popen_uci(str(lc0))
try:
    white.configure({"OnlinePolicy": True, "PolicyTimeout": 5000, "Hash": 128})
    try:
        black.configure({"WeightsFile": str(net), "Backend": "blas"})
    except Exception:
        black.configure({"WeightsFile": str(net)})

    board=chess.Board()
    game=chess.pgn.Game()
    game.headers["Event"]="YOLOChess-NNUE v2 sanity match"
    game.headers["White"]="YOLOChess-NNUE v2 (16383 nodes)"
    game.headers["Black"]="Elite Leela v2 (1 node)"
    game.headers["Result"]="*"
    node=game

    for ply in range(1,181):
        if board.is_game_over(claim_draw=True):
            break
        eng=white if board.turn else black
        limit=chess.engine.Limit(nodes=16383 if board.turn else 1)
        t=time.perf_counter()
        res=eng.play(board,limit,info=chess.engine.INFO_ALL)
        dt=time.perf_counter()-t
        if res.move is None or res.move not in board.legal_moves:
            game.headers["Termination"]="engine error"
            game.headers["Result"]="0-1" if board.turn else "1-0"
            break
        san=board.san(res.move)
        board.push(res.move)
        node=node.add_variation(res.move)
        node.comment=f"elapsed {dt:.3f}s; nodes {res.info.get('nodes','?')}; depth {res.info.get('depth','?')}; score {res.info.get('score','?')}"
        print(f"{ply:03d} {san} {res.move.uci()} {dt:.3f}s",flush=True)

    if game.headers["Result"]=="*":
        if board.is_game_over(claim_draw=True):
            game.headers["Result"]=board.result(claim_draw=True)
            game.headers["Termination"]=str(board.outcome(claim_draw=True).termination)
        else:
            game.headers["Result"]="1/2-1/2"
            game.headers["Termination"]="180-ply safety limit"

    with outp.open("w",encoding="utf-8") as f:
        print(game,file=f,end="\n\n")
    print("FINAL",game.headers["Result"],game.headers.get("Termination",""),flush=True)
finally:
    try:white.quit()
    except:pass
    try:black.quit()
    except:pass
