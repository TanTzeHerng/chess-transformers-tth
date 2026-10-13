#!/usr/bin/env python3
"""Native LCZero search + ChessGPT policy + SF16 classical value
versus Elite v2 go nodes 1. 120+1, Variety.bin.
Strict: if Games 1 AND 2 are ChessGPT losses, abort immediately.
Every finished game recorded as PGN, no fabricated decisions.
"""
import csv
import hashlib
import os
import random
import time
import sys
from pathlib import Path
import chess
import chess.engine
import chess.pgn
import chess.polyglot
from cloud_match import make_prompt

D=Path(__file__).resolve().parent
OUT=D/'results'
OUT.mkdir(exist_ok=True)
BOOK=D/'Variety.bin'
NET=D/'Elite-Leela-v2-128x10x8h-1704000.pb.gz'
EXE=D/'lc0-src'/'build'/'lc0'
BH='5a30bc27b5e7b7d51694bf35e7d5dd2536b54e9b25ea3ea64ef4a2b982d5e21c'
N=20
OPENINGS=['e2e4','d2d4','c2c4','g1f3','b2b3','g2g3']

def samples():
    assert hashlib.sha256(BOOK.read_bytes()).hexdigest()==BH
    rng=random.Random(20261014)
    seen=set();positions=[]
    with chess.polyglot.open_reader(BOOK) as reader:
        for pair in range(N//2):
            base=chess.Move.from_uci(OPENINGS[pair%len(OPENINGS)])
            result=None
            for attempt in range(200):
                board=chess.Board();board.push(base)
                for j in range(7):
                    try: entry=reader.weighted_choice(board,random=rng)
                    except IndexError:break
                    board.push(entry.move)
                if board.ply()<6:continue
                t=tuple(m.uci() for m in board.move_stack)
                if t not in seen:seen.add(t);result=t;break
            if result is None:raise RuntimeError('Cannot sample Variety opening '+str(pair))
            print('BOOK_PAIR',pair+1,result,flush=True)
            positions.append(result)
    return positions

def match_game(n,start,gpt,elite,pgn,csvpath):
    board=chess.Board()
    for m in start:board.push_uci(m)
    gptwhite=n%2==1
    clock={True:120.0,False:120.0}
    token=object()
    print('GAME_START',n,'ChessGPT white' if gptwhite else 'ChessGPT black',flush=True)
    for _ in range(600):
        done=board.outcome(claim_draw=True)
        if done:
            result=done.result()
            why=done.termination.name
            break
        turn=board.turn
        gptturn= turn==gptwhite
        eng=gpt if gptturn else elite
        limit=chess.engine.Limit(white_clock=clock[True],black_clock=clock[False],
                                white_inc=1.0,black_inc=1.0,
                                **({} if gptturn else {'nodes':1}))
        t=time.monotonic()
        res=eng.play(board,limit,info=chess.engine.INFO_ALL,game=token)
        elapsed=time.monotonic()-t
        clock[turn]-=elapsed
        if clock[turn]<=0:
            result='0-1' if turn else '1-0'
            why=('ChessGPT' if gptturn else 'Elite v2')+' lost on time'
            break
        m=res.move
        if m not in board.legal_moves:
            raise RuntimeError('ILLEGAL ENGINE MOVE '+str(m)+' at '+board.fen())
        san=board.san(m)
        board.push(m)
        clock[turn]+=1
        print('PLY',n,board.ply(),'ChessGPT' if gptturn else 'Elite v2',
              san,'think',round(elapsed,2),'nodes',res.info.get('nodes','?'),
              'W',round(clock[True],1),'B',round(clock[False],1),flush=True)
    else:raise RuntimeError('No legitimate game termination within 600 plies')
    game=chess.pgn.Game.from_board(board)
    game.headers.update({
        'Event':'Genuine Lc0 search / ChessGPT policy SF16 classical vs Elite2 one node',
        'Round':str(n),'Site':'GitHub Actions Ubuntu','TimeControl':'120+1',
        'White':'Lc0-ChessGPT' if gptwhite else 'Elite v2 (nodes 1)',
        'Black':'Elite v2 (nodes 1)' if gptwhite else 'Lc0-ChessGPT',
        'Result':result,'Termination':why,
        'OpeningBook':'Variety.bin eight free halfmoves; paired colors'})
    with pgn.open('a',encoding='utf8') as f:print(game,file=f,end='\n\n')
    with csvpath.open('a',newline='',encoding='utf8') as f:
        csv.writer(f).writerow([n,'white' if gptwhite else 'black',
                                result,why,board.ply(),
                                round(clock[True],2),round(clock[False],2)])
    score={'1-0':1.,'0-1':0.,'1/2-1/2':.5}[result]
    return score if gptwhite else 1.-score

def main():
    openings=samples()
    f=OUT/'score.csv';p=OUT/'games.pgn'
    if p.exists():p.unlink()
    with f.open('w',newline='') as o:
        csv.writer(o).writerow(['game','gpt_color','result','termination','plies','white_remaining','black_remaining'])
    results=[]
    gcmd=[str(EXE),'--backend=chessgpt_socket','--weights=',
          '--threads=1','--minibatch-size=1','--max-prefetch=0']
    ecmd=[str(EXE),'--backend=blas','--weights='+str(NET),
          '--threads=1','--minibatch-size=1','--max-prefetch=0']
    with chess.engine.SimpleEngine.popen_uci(gcmd,timeout=180) as gpt,\
         chess.engine.SimpleEngine.popen_uci(ecmd,timeout=180) as elite:
        print('ENGINE_IDS',gpt.id,elite.id,flush=True)
        gpt.configure({'NoiseEpsilon':0} if 'NoiseEpsilon' in gpt.options else {})
        b=chess.Board()
        probe=elite.play(b,chess.engine.Limit(nodes=1))
        assert probe.move in b.legal_moves
        for n in range(1,N+1):
            points=match_game(n,openings[(n-1)//2],gpt,elite,p,f)
            results.append(points)
            print('SCORE_AFTER',n,'ChessGPT',sum(results),'Elite',len(results)-sum(results),flush=True)
            if n==2 and results==[0.0,0.0]:
                print('MATCH_STOPPED_EARLY first_two_games_both_losses',flush=True)
                return
    print('MATCH_COMPLETE','ChessGPT',sum(results),'Elite',len(results)-sum(results),flush=True)

if __name__=='__main__':main()
