#!/usr/bin/env python3
"""20-game ChessGPT 16L vs Maia 1900, with paired openings from user's exact Variety.bin.
2 minutes + 1-second increment, Maia nodes=1. No opening moves consume either clock.
"""
import csv
import hashlib
import random
import time
from pathlib import Path
import chess
import chess.engine
import chess.pgn
import chess.polyglot
from cloud_match import load_gpt, choose_gpt_move

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'results_maia'
OUT.mkdir(exist_ok=True)
BOOK=ROOT/'Variety.bin'
MAIA_WEIGHTS=ROOT/'maia-1900.pb.gz'
LC0=ROOT/'lc0'/'lc0.exe'
BOOK_SHA256='5a30bc27b5e7b7d51694bf35e7d5dd2536b54e9b25ea3ea64ef4a2b982d5e21c'
N_GAMES=20
OPENING_PLY=8
EXPECTED_HEAD=['e2e4','d2d4','c2c4','g1f3','b2b3','g2g3']

def opening_positions(n_pairs):
    payload=BOOK.read_bytes()
    assert hashlib.sha256(payload).hexdigest()==BOOK_SHA256, 'Wrong Variety.bin version'
    # Second independent sample from the SAME verified Variety book.
    # Paired openings and all engine/time-control settings remain unchanged.
    rng=random.Random(20261014)
    results=[]
    used=set()
    with chess.polyglot.open_reader(BOOK) as reader:
        first=list(reader.find_all(chess.Board()))
        assert all(any(e.move.uci()==uci for e in first) for uci in EXPECTED_HEAD)
        for pair in range(n_pairs):
            first_move=chess.Move.from_uci(EXPECTED_HEAD[pair%len(EXPECTED_HEAD)])
            candidate=None
            for attempt in range(60):
                b=chess.Board()
                b.push(first_move)
                for _ in range(OPENING_PLY-1):
                    try:
                        e=reader.weighted_choice(b,random=rng)
                    except IndexError:
                        break
                    b.push(e.move)
                if b.ply()<4: 
                    continue
                moves=tuple(m.uci() for m in b.move_stack)
                candidate=moves
                if moves not in used:
                    used.add(moves)
                    break
            if candidate is None:raise RuntimeError('Unable to sample opening')
            board=chess.Board()
            san=[]
            for u in candidate:
                move=chess.Move.from_uci(u)
                san.append(board.san(move))
                board.push(move)
            print(f'BOOK_PAIR {pair+1} plies={len(candidate)} moves={candidate} SAN={san}',flush=True)
            results.append(candidate)
    return results

def game(number,model,maia,opening,pgnpath,csvpath):
    board=chess.Board()
    for u in opening:
        move=chess.Move.from_uci(u)
        if move not in board.legal_moves:raise RuntimeError('Book produced illegal move '+u)
        board.push(move)
    gpt_is_white=number%2==1
    clocks={True:120.0, False:120.0}
    game_token=object()
    print('GAME_START',number,'ChessGPT_is_white',gpt_is_white,'start_ply',board.ply(),flush=True)
    for _ in range(600):
        outcome=board.outcome(claim_draw=True)
        if outcome:
            result=outcome.result()
            termination=outcome.termination.name
            break
        turn=board.turn
        gpt=(turn==chess.WHITE)==gpt_is_white
        engine_name='ChessGPT' if gpt else 'Maia 1900'
        t=time.monotonic()
        if gpt:
            nextmove=choose_gpt_move(model,board,clocks[turn])
        else:
            limit=chess.engine.Limit(nodes=1,white_clock=clocks[chess.WHITE],
                black_clock=clocks[chess.BLACK],white_inc=1.0,black_inc=1.0)
            nextmove=maia.play(board,limit,game=game_token,info=chess.engine.INFO_NONE).move
        elapsed=time.monotonic()-t
        clocks[turn]-=elapsed
        if clocks[turn]<=0:
            result='0-1' if turn==chess.WHITE else '1-0'
            termination=engine_name+' lost on time'
            print('FLAG',number,engine_name,flush=True)
            break
        if nextmove not in board.legal_moves:
            raise RuntimeError(f'Invalid move {nextmove} from {engine_name} at {board.fen()}')
        san=board.san(nextmove)
        board.push(nextmove)
        clocks[turn]+=1.0
        print(f'PLY game={number} ply={board.ply()} engine={engine_name} move={san} '
              f'think={elapsed:.2f} white={clocks[chess.WHITE]:.2f} black={clocks[chess.BLACK]:.2f}',flush=True)
    else:
        raise RuntimeError(f'Game {number} never achieved a chess result after 600 moves')
    pgn=chess.pgn.Game.from_board(board)
    pgn.headers.update({'Event':'ChessGPT 16L vs Maia 1900 nodes1 Variety paired',
                        'Site':'GitHub Actions Windows','Round':str(number),
                        'White':'ChessGPT' if gpt_is_white else 'Maia 1900 (1 node)',
                        'Black':'Maia 1900 (1 node)' if gpt_is_white else 'ChessGPT',
                        'Result':result,'Termination':termination,
                        'TimeControl':'120+1',
                        'OpeningBook':'Variety.bin (Noomen) paired first 8 plies'})
    with pgnpath.open('a',encoding='utf-8') as f: print(pgn,file=f,end='\n\n')
    with csvpath.open('a',newline='',encoding='utf-8') as f:
        csv.writer(f).writerow([number,(number+1)//2,
            'white' if gpt_is_white else 'black',result,termination,
            board.ply(),round(clocks[chess.WHITE],3),round(clocks[chess.BLACK],3)])
    points_white={'1-0':1.0,'0-1':0.0,'1/2-1/2':0.5}[result]
    return points_white if gpt_is_white else 1-points_white

def main():
    assert BOOK.is_file() and MAIA_WEIGHTS.is_file() and LC0.is_file()
    positions=opening_positions(N_GAMES//2)
    model=load_gpt()
    scorefile=OUT/'score.csv'
    pgnpath=OUT/'ChessGPT_vs_Maia1900_Variety.pgn'
    if pgnpath.exists():pgnpath.unlink()
    with scorefile.open('w',newline='',encoding='utf-8') as f:
        csv.writer(f).writerow(['round','opening_pair','chessgpt_color','result',
                                'termination','plies','white_clock','black_clock'])
    totals={'ChessGPT':0.0,'Maia 1900':0.0}
    with chess.engine.SimpleEngine.popen_uci(str(LC0),timeout=180,cwd=str(LC0.parent)) as maia:
        opts={'WeightsFile':str(MAIA_WEIGHTS),'Threads':1,'NNCacheSize':1024,
              'MinibatchSize':1,'MaxPrefetch':0,'RamLimitMb':256}
        maia.configure({k:v for k,v in opts.items() if k in maia.options})
        probe=maia.play(chess.Board(),chess.engine.Limit(nodes=1))
        assert probe.move in chess.Board().legal_moves
        gptprobe=choose_gpt_move(model,chess.Board(),120)
        assert gptprobe in chess.Board().legal_moves
        print('PREFLIGHT_PASS',probe.move,gptprobe,'maia',maia.id,flush=True)
        print('RERUN_ID','20261014','VARIETY_SEED',20261014,'GAMES',N_GAMES,'TC','120+1','MAIA_NODE_LIMIT',1,flush=True)
        for n in range(1,N_GAMES+1):
            point=game(n,model,maia,positions[(n-1)//2],pgnpath,scorefile)
            totals['ChessGPT']+=point
            totals['Maia 1900']+=1-point
            print('SCORE_AFTER',n,totals,flush=True)
    print('MATCH_COMPLETE',totals,flush=True)

if __name__=='__main__':main()
