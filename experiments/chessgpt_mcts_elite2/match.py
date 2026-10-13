#!/usr/bin/env python3
"""20 genuine rules-completed games ChessGPT MCTS vs Elite v2, both 120+1.
ChessGPT controls its search entirely from wtime/btime/winc/binc, without
depth/node limits; Elite is restricted to Lc0 go nodes 1.
Ten paired 8-ply openings from verified Variety.bin.
"""
import csv
import hashlib
import random
import time
import sys
from pathlib import Path
import chess
import chess.engine
import chess.pgn
import chess.polyglot
from cloud_match import make_prompt
from chessgpt_mcts_uci import parse_go,parse_position

DIR=Path(__file__).resolve().parent
OUT=DIR/'results'
OUT.mkdir(exist_ok=True)
BOOK=DIR/'Variety.bin'
ELITE=DIR/'lc0'/'lc0.exe'
WEIGHTS=DIR/'Elite-Leela-v2-128x10x8h-1704000.pb.gz'
CHESSGPT=DIR/'chessgpt_mcts_uci.py'
BOOK_HASH='5a30bc27b5e7b7d51694bf35e7d5dd2536b54e9b25ea3ea64ef4a2b982d5e21c'
FIRST=['e2e4','d2d4','c2c4','g1f3','b2b3','g2g3']

def tests():
    b=chess.Board()
    assert make_prompt(b)==';1.'
    for san in ['e4','e5','Nf3','Nc6']:
        b.push_san(san)
    assert make_prompt(b)==';1.e4 e5 2.Nf3 Nc6 3.'
    assert parse_position('position startpos moves e2e4 e7e5').fen()==chess.Board('rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2').fen()
    assert parse_position('position startpos moves e2e4 e7e5').fullmove_number==2
    assert parse_go('go movetime 5000')['movetime']==5000
    print('UNIT_TESTS_PASS UCI time and corrected ChessGPT PGN',flush=True)

def openings():
    assert hashlib.sha256(BOOK.read_bytes()).hexdigest()==BOOK_HASH
    rng=random.Random(20261015)
    used=set()
    positions=[]
    with chess.polyglot.open_reader(BOOK) as reader:
        for i in range(10):
            first=chess.Move.from_uci(FIRST[i%len(FIRST)])
            candidate=None
            for attempt in range(150):
                b=chess.Board();b.push(first)
                for j in range(7):
                    try:e=reader.weighted_choice(b,random=rng)
                    except IndexError:break
                    b.push(e.move)
                if b.ply()<6:continue
                t=tuple(m.uci() for m in b.move_stack)
                if t in used:continue
                candidate=t;used.add(t);break
            if candidate is None:raise RuntimeError('Variety book failed pair '+str(i))
            print('BOOK_PAIR',i+1,candidate,flush=True)
            positions.append(candidate)
    return positions

def play_one(n,opening,gpt,elite,pgnpath,csvpath):
    b=chess.Board()
    for u in opening:b.push_uci(u)
    gptwhite= n%2==1
    clocks={True:120.0,False:120.0}
    game_token=object()
    print('GAME_START',n,'ChessGPT white' if gptwhite else 'Elite white',flush=True)
    for ply in range(600):
        outcome=b.outcome(claim_draw=True)
        if outcome:
            result=outcome.result();termination=outcome.termination.name
            break
        turn=b.turn
        gpt_to_move=(turn==chess.WHITE)==gptwhite
        engine=gpt if gpt_to_move else elite
        who='ChessGPT MCTS' if gpt_to_move else 'Elite v2 (nodes1)'
        limit=chess.engine.Limit(
            white_clock=clocks[chess.WHITE],black_clock=clocks[chess.BLACK],
            white_inc=1.0,black_inc=1.0,
            **({} if gpt_to_move else {'nodes':1}))
        start=time.monotonic()
        # Explicitly no time/depth/node cap for ChessGPT. UCI wtime/btime
        # will control its PUCT search independently.
        response=engine.play(b,limit,game=game_token,info=chess.engine.INFO_ALL)
        elapsed=time.monotonic()-start
        clocks[turn]-=elapsed
        if clocks[turn]<=0:
            result='0-1' if turn==chess.WHITE else '1-0'
            termination=who+' FLAG';print('FLAG',n,who,elapsed,flush=True)
            break
        m=response.move
        if m not in b.legal_moves:
            raise RuntimeError('Illegal move from '+who+': '+str(m)+' FEN='+b.fen())
        san=b.san(m);b.push(m);clocks[turn]+=1.0
        nodes=response.info.get('nodes','?')
        print('PLY',n,b.ply(),who,san,'elapsed',round(elapsed,2),
              'nodes',nodes,'white',round(clocks[True],1),'black',round(clocks[False],1),flush=True)
    else:
        raise RuntimeError('No chess result after 600 plies; aborting not fabricating a draw')
    g=chess.pgn.Game.from_board(b)
    g.headers.update({'Event':'ChessGPT 16L PUCT Stockfish16 classical vs Elite v2 nodes1',
        'Site':'GitHub Actions cloud','Round':str(n),
        'White':'ChessGPT MCTS' if gptwhite else 'Elite v2 nodes1',
        'Black':'Elite v2 nodes1' if gptwhite else 'ChessGPT MCTS',
        'Result':result,'TimeControl':'120+1','Termination':termination,
        'OpeningBook':'Variety.bin, eight free plies paired',
        'EngineBuild':'200-distinct-leaf UCI defaults, 24 visits, temp 8, 64-beam'})
    with pgnpath.open('a',encoding='utf-8') as f:print(g,file=f,end='\n\n')
    with csvpath.open('a',newline='',encoding='utf-8') as f:
        csv.writer(f).writerow([n,'white' if gptwhite else 'black',result,termination,b.ply(),round(clocks[True],2),round(clocks[False],2)])
    wp={'1-0':1.0,'0-1':0.0,'1/2-1/2':0.5}[result]
    return wp if gptwhite else 1.0-wp

def main():
    tests()
    positions=openings()
    csvpath=OUT/'score.csv';pgnpath=OUT/'ChessGPT_MCTS_vs_Elite_v2.pgn'
    if pgnpath.exists():pgnpath.unlink()
    with csvpath.open('w',newline='',encoding='utf-8') as f:
        csv.writer(f).writerow(['game','chessgpt_color','result','termination','plies','white_clock','black_clock'])
    pts={'ChessGPT MCTS':0.0,'Elite v2':0.0}
    # Internal Stockfish16 is spawned by ChessGPT's UCI engine only.
    with chess.engine.SimpleEngine.popen_uci([sys.executable,'-u',str(CHESSGPT)],timeout=180,cwd=str(DIR)) as gpt,\
         chess.engine.SimpleEngine.popen_uci(str(ELITE),timeout=180,cwd=str(ELITE.parent)) as elite:
        assert gpt.id.get('name','').startswith('ChessGPT-MCTS')
        opts={'WeightsFile':str(WEIGHTS),'Threads':1,'NNCacheSize':1024,
              'MinibatchSize':1,'MaxPrefetch':0,'RamLimitMb':256}
        elite.configure({k:v for k,v in opts.items() if k in elite.options})
        # Never silently use the 2.5/20 predecessor's old parameters.
        # These are the audited actual UCI defaults from the 200+
        # independently classical-evaluated leaf / 2.5s version.
        expected={'TorchThreads':4,'SANBatchSize':64,'CPuct':900,
                  'RootPolicyBeam':64,'ChildPolicyBeam':64,
                  'ExpandAfterVisits':24,'PolicyTemperature':800}
        for name,exp in expected.items():
            assert name in gpt.options,(name,'missing UCI option')
            actual=gpt.options[name].default
            assert actual==exp,(name,'expected',exp,'actual',actual)
        assert gpt.options['FreshLeafBenchmark'].default is False
        gpt.configure(expected)
        gpt.configure({'FreshLeafBenchmark':False})
        print('MATCH_ENGINE_PRECISE_200LEAF_DEFAULTS',expected,
              'FreshLeafBenchmark',False,'weights_original_16layer',flush=True)
        eprobe=elite.play(chess.Board(),chess.engine.Limit(nodes=1))
        assert eprobe.move in chess.Board().legal_moves
        print('MATCH_PREFLIGHT_OK',gpt.id,elite.id,eprobe.move,flush=True)
        for n in range(1,21):
            pt=play_one(n,positions[(n-1)//2],gpt,elite,pgnpath,csvpath)
            pts['ChessGPT MCTS']+=pt
            pts['Elite v2']+=1-pt
            print('SCORE_AFTER',n,pts,flush=True)
    print('MATCH_COMPLETE',pts,flush=True)

if __name__=='__main__':main()
