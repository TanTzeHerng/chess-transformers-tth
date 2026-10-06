#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
import urllib.request

import chess
import chess.pgn
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parent
TOBY = ROOT / "toby"
MODEL = ROOT / "model.onnx"
MODEL_URL = "https://huggingface.co/cstr/searchless-chess-onnx/resolve/main/270M/model_fp16.onnx"

FEN_CHARS = list("0123456789abcdefghpnrkqPBNRQKw.")
FEN_INDEX = {c:i for i,c in enumerate(FEN_CHARS)}
SPACE_CHARS = set("12345678")
BUCKET_VALUES = (np.arange(128, dtype=np.float64) + 0.5) / 128.0

def action_table():
    moves=[]
    b=chess.BaseBoard.empty()
    for sq in range(64):
        nxt=[]
        b.set_piece_at(sq, chess.Piece.from_symbol("Q"))
        nxt += list(b.attacks(sq))
        b.set_piece_at(sq, chess.Piece.from_symbol("N"))
        nxt += list(b.attacks(sq))
        b.remove_piece_at(sq)
        for t in nxt:
            moves.append(chess.square_name(sq)+chess.square_name(t))
    files=list("abcdefgh")
    for rank,next_rank in [("2","1"),("7","8")]:
        for i,f in enumerate(files):
            base=f+rank+f+next_rank
            moves += [base+p for p in "qrbn"]
            if f>"a":
                base=f+rank+files[i-1]+next_rank
                moves += [base+p for p in "qrbn"]
            if f<"h":
                base=f+rank+files[i+1]+next_rank
                moves += [base+p for p in "qrbn"]
    assert len(moves)==1968 and len(set(moves))==1968
    return {m:i for i,m in enumerate(moves)}

MOVE_TO_ACTION=action_table()

def tok(fen):
    board,side,castling,ep,half,full=fen.split()
    s=side+board.replace("/","")
    out=[]
    for c in s:
        if c in SPACE_CHARS:
            out += [FEN_INDEX["."]]*int(c)
        else:
            out.append(FEN_INDEX[c])
    if castling=="-":
        out += [FEN_INDEX["."]]*4
    else:
        out += [FEN_INDEX[c] for c in castling]
        out += [FEN_INDEX["."]]*(4-len(castling))
    if ep=="-":
        out += [FEN_INDEX["."]]*2
    else:
        out += [FEN_INDEX[c] for c in ep]
    for field in (half,full):
        field = field + "."*(3-len(field))
        out += [FEN_INDEX[c] for c in field]
    assert len(out)==77
    return np.asarray(out,dtype=np.int64)

def fen_for_model(board):
    try:
        return board.fen(en_passant="legal")
    except TypeError:
        return board.fen()

class Searchless270M:
    def __init__(self):
        if not MODEL.exists():
            print("downloading 270M ONNX", flush=True)
            urllib.request.urlretrieve(MODEL_URL, MODEL)
        so=ort.SessionOptions()
        so.intra_op_num_threads=max(1, min(2, os.cpu_count() or 1))
        so.inter_op_num_threads=1
        so.graph_optimization_level=ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.s=ort.InferenceSession(str(MODEL), sess_options=so, providers=["CPUExecutionProvider"])
    def move(self, board):
        legal=sorted(list(board.legal_moves), key=lambda m: MOVE_TO_ACTION[m.uci()])
        if not legal:
            return None
        f=tok(fen_for_model(board))
        x=np.zeros((len(legal),79),dtype=np.int64)
        x[:,:77]=f
        x[:,77]=[MOVE_TO_ACTION[m.uci()] for m in legal]
        lp=self.s.run(None,{"tokens":x})[0].astype(np.float64)
        probs=np.exp(lp) @ BUCKET_VALUES
        for i,m in enumerate(legal):
            board.push(m)
            if board.is_fivefold_repetition() or board.can_claim_threefold_repetition():
                probs[i]=0.5
            board.pop()
        i=int(np.argmax(probs))
        return legal[i], float(probs[i])

def load_toby():
    sys.path.insert(0,str(TOBY))
    spec=importlib.util.spec_from_file_location("toby_agent", TOBY/"agent.py")
    mod=importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod

def result_for_flag(flagger):
    return "0-1" if flagger==chess.WHITE else "1-0"

def play(game_no, toby_color):
    # Fresh Toby module/process per game is provided by workflow invoking this script separately.
    toby=load_toby()
    sl=Searchless270M()
    board=chess.Board()
    # Asymmetric clock: TobyCoad alone plays 2'+1". Searchless is untimed.
    clocks={toby_color:120000.0}
    inc=1000.0
    game=chess.pgn.Game()
    game.headers["Event"]="Searchless 270M (1 node) vs TobyCoad"
    game.headers["Site"]="GitHub Actions Ubuntu runner"
    game.headers["Date"]="2026.10.07"
    game.headers["Round"]=str(game_no)
    game.headers["White"]="TobyCoad" if toby_color==chess.WHITE else "DeepMind Searchless 270M (1 node)"
    game.headers["Black"]="TobyCoad" if toby_color==chess.BLACK else "DeepMind Searchless 270M (1 node)"
    game.headers["TimeControl"]="-"
    game.headers["TobyCoadTimeControl"]="120+1"
    game.headers["SearchlessTimeControl"]="unlimited"
    node=game
    ply=0
    timings=[]
    result="*"
    termination=""
    while True:
        outcome=board.outcome(claim_draw=True)
        if outcome is not None:
            result=outcome.result()
            termination=str(outcome.termination)
            break
        side=board.turn
        before=clocks[toby_color] if side==toby_color else None
        t0=time.perf_counter()
        try:
            if side==toby_color:
                uci=toby.get_move(board.fen(), int(max(0.0,before)))
                mv=chess.Move.from_uci(uci)
                detail=None
            else:
                got=sl.move(board)
                if got is None:
                    result="1/2-1/2"; termination="no legal moves"; break
                mv,detail=got
        except Exception as exc:
            elapsed=(time.perf_counter()-t0)*1000.0
            result="0-1" if side==chess.WHITE else "1-0"
            termination=f"engine exception: {type(exc).__name__}: {exc}"
            timings.append({"ply":ply+1,"side":"w" if side else "b","elapsed_ms":elapsed,"clock_before_ms":before,"error":repr(exc)})
            break
        elapsed=(time.perf_counter()-t0)*1000.0
        ply+=1
        if side==toby_color:
            clocks[toby_color]-=elapsed
            if clocks[toby_color] < 0:
                result=result_for_flag(side)
                termination="TobyCoad time forfeit"
                timings.append({"ply":ply,"side":"w" if side else "b","elapsed_ms":elapsed,"clock_before_ms":before,"clock_after_ms":clocks[toby_color],"move":mv.uci()})
                break
        if mv not in board.legal_moves:
            result="0-1" if side==chess.WHITE else "1-0"
            termination=f"illegal move {mv.uci()}"
            break
        board.push(mv)
        if side==toby_color:
            clocks[toby_color]+=inc
        node=node.add_variation(mv)
        timings.append({
            "ply":ply,
            "side":"w" if side else "b",
            "engine":"TobyCoad" if side==toby_color else "Searchless270M",
            "move":mv.uci(),
            "elapsed_ms":round(elapsed,3),
            "clock_after_ms":round(clocks[toby_color],3) if side==toby_color else None,
            **({"searchless_win_prob":round(detail,6)} if detail is not None else {})
        })
        if ply>=500:
            result="1/2-1/2"
            termination="500-ply safety cap"
            break
    game.headers["Result"]=result
    game.headers["Termination"]=termination
    outdir=ROOT/"results"
    outdir.mkdir(exist_ok=True)
    pgn_path=outdir/f"game{game_no}.pgn"
    with pgn_path.open("w",encoding="utf-8") as f:
        print(game, file=f, end="\n\n")
    summary={
        "game":game_no,
        "white":game.headers["White"],
        "black":game.headers["Black"],
        "result":result,
        "termination":termination,
        "plies":ply,
        "final_fen":board.fen(),
        "white_clock_ms":round(clocks[toby_color],3) if toby_color==chess.WHITE else None,
        "black_clock_ms":round(clocks[toby_color],3) if toby_color==chess.BLACK else None,
        "toby_clock_ms":round(clocks[toby_color],3),
        "searchless_clock":"unlimited",
        "searchless_inference_ms_total":round(sum(t["elapsed_ms"] for t in timings if t.get("engine")=="Searchless270M"),3),
        "timings":timings,
    }
    (outdir/f"game{game_no}.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in summary.items() if k!="timings"},indent=2), flush=True)
    return result

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--game",type=int,required=True)
    ap.add_argument("--toby-color",choices=["white","black"],required=True)
    a=ap.parse_args()
    play(a.game, chess.WHITE if a.toby_color=="white" else chess.BLACK)
