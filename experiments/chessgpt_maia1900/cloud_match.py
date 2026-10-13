#!/usr/bin/env python3
"""Cloud ChessGPT 16-layer vs exact Elite Leela v2 network, Lc0 nodes=1.
Time control 120s + 1s increment; games alternate colors.
"""
import csv
import os
import sys
import time
from pathlib import Path
import chess
import chess.engine
import chess.pgn
import torch
import torch.nn.functional as F
from model import GPT, GPTConfig

DIR=Path(__file__).resolve().parent
OUT=DIR/'results'
OUT.mkdir(exist_ok=True)
WEIGHTS=DIR/'stockfish_16layers_ckpt_with_optimizer.pt'
ELITE_WEIGHTS=DIR/'Elite-Leela-v2-128x10x8h-1704000.pb.gz'
ELITE_EXE=DIR/'lc0'/'lc0.exe'
VOCAB=' #+-.0123456789;=BKNOQRabcdefghx'
STOI={c:i for i,c in enumerate(VOCAB)}
assert len(STOI)==32 and STOI[';']==15

def load_gpt():
    torch.set_num_threads(2)
    cp=torch.load(str(WEIGHTS),map_location='cpu',mmap=True,weights_only=True)
    print('CHECKPOINT',cp['model_args'],flush=True)
    with torch.device('meta'):
        model=GPT(GPTConfig(**cp['model_args']))
    state={k.removeprefix('_orig_mod.'):v for k,v in cp['model'].items()}
    model.load_state_dict(state,strict=True,assign=True)
    model.lm_head.weight=model.transformer.wte.weight
    del cp,state
    model.eval()
    return model

@torch.inference_mode()
def neural_step(model,ids,cache=None):
    xids=torch.tensor([ids],dtype=torch.long)
    b,t=xids.shape
    prev=cache[0][0].shape[2] if cache is not None else 0
    if prev+t>=model.config.block_size:
        raise ValueError('prompt too long')
    pos=torch.arange(prev,prev+t)
    x=model.transformer.wte(xids)+model.transformer.wpe(pos)
    new=[]
    for i,h in enumerate(model.transformer.h):
        z=h.ln_1(x)
        c=z.shape[-1]
        q,k,v=h.attn.c_attn(z).split(c,dim=-1)
        def heads(y):return y.reshape(b,t,h.attn.n_head,c//h.attn.n_head).transpose(1,2)
        q,k,v=heads(q),heads(k),heads(v)
        if cache is not None:
            k=torch.cat([cache[i][0],k],dim=2)
            v=torch.cat([cache[i][1],v],dim=2)
        y=F.scaled_dot_product_attention(q,k,v,is_causal=cache is None)
        x=x+h.attn.c_proj(y.transpose(1,2).contiguous().reshape(b,t,c))
        x=x+h.mlp.c_proj(F.gelu(h.mlp.c_fc(h.ln_2(x))))
        new.append((k,v))
    logits=model.lm_head(model.transformer.ln_f(x[:,-1:,:]))[0,-1]
    return logits,new

def make_prompt(board):
    b=board.root()
    s=';'
    for move in board.move_stack:
        s+=(str(b.fullmove_number)+'.') if b.turn==chess.WHITE else ' '
        s+=b.san(move)
        b.push(move)
    s+=(' '+str(b.fullmove_number)+'.') if b.turn==chess.WHITE and len(board.move_stack)>0 else (
        '1.' if b.turn==chess.WHITE else ' ')
    return s

@torch.inference_mode()
def choose_gpt_move(model,board,remaining):
    if not any(board.legal_moves):return None
    prompt=make_prompt(board)[-(model.config.block_size-20):]
    root={}
    for move in board.legal_moves:
        node=root
        for c in board.san(move):
            node=node.setdefault(c,{})
        node['_end']=move
    fallback=next(iter(board.legal_moves))
    if chess.Move.from_uci('e2e4') in board.legal_moves:
        fallback=chess.Move.from_uci('e2e4')
    budget=max(0.5,min(8.0,(remaining-4.0)/20.0+1.0))
    deadline=time.monotonic()+budget
    logits,cache=neural_step(model,[STOI[c] for c in prompt])
    node=root
    for _ in range(14):
        if time.monotonic()>deadline:return node.get('_end',fallback)
        possibilities=[x for x in node if x!='_end']
        if '_end' in node: possibilities.append(' ')
        if not possibilities: return node.get('_end',fallback)
        pick=max(possibilities,key=lambda ch:float(logits[STOI[ch]]))
        if pick==' ':return node['_end']
        node=node[pick]
        if node.get('_end') is not None:
            fallback=node['_end']
        logits,cache=neural_step(model,[STOI[pick]],cache)
    return node.get('_end',fallback)

def game_play(number,model,elite,pgnfile,scorefile):
    board=chess.Board()
    gptwhite=number%2==1
    clock={True:120.0,False:120.0}
    game_id=object()
    print('GAME',number,'START','ChessGPT White' if gptwhite else 'Elite White',flush=True)
    for ply in range(500):
        outcome=board.outcome(claim_draw=True)
        if outcome:
            result=outcome.result()
            reason=outcome.termination.name
            break
        turn=board.turn
        gpt=(turn==chess.WHITE)==gptwhite
        name='ChessGPT' if gpt else 'Elite v2'
        start=time.monotonic()
        if gpt:
            move=choose_gpt_move(model,board,clock[turn])
        else:
            limit=chess.engine.Limit(nodes=1,white_clock=clock[chess.WHITE],
                black_clock=clock[chess.BLACK],white_inc=1.0,black_inc=1.0)
            move=elite.play(board,limit,game=game_id,info=chess.engine.INFO_NONE).move
        used=time.monotonic()-start
        clock[turn]-=used
        if clock[turn]<=0:
            result='0-1' if turn==chess.WHITE else '1-0'
            reason=name+' FLAG'
            print('TIME_FORFEIT',number,name,used,flush=True)
            break
        if move not in board.legal_moves:
            raise RuntimeError(f'{name}: invalid move {move} at {board.fen()}')
        san=board.san(move)
        board.push(move)
        clock[turn]+=1.0
        print('PLY',number,board.ply(),name,san,'elapsed',round(used,2),
            'W',round(clock[chess.WHITE],1),'B',round(clock[chess.BLACK],1),flush=True)
    else:
        raise RuntimeError('Game exceeded 500 plies without chess termination')
    pgn=chess.pgn.Game.from_board(board)
    pgn.headers.update({'Event':'ChessGPT 16L v Elite Leela v2 (Lc0 nodes=1)',
        'Round':str(number),'TimeControl':'120+1',
        'White':'ChessGPT 16L' if gptwhite else 'Elite v2 (1 node)',
        'Black':'Elite v2 (1 node)' if gptwhite else 'ChessGPT 16L',
        'Result':result,'Termination':reason})
    with pgnfile.open('a',encoding='utf-8') as f: print(pgn,file=f,end='\n\n')
    with scorefile.open('a',newline='',encoding='utf-8') as f:
        csv.writer(f).writerow([number,'white' if gptwhite else 'black',result,
            reason,board.ply(),round(clock[chess.WHITE],3),round(clock[chess.BLACK],3)])
    return result,gptwhite

def main():
    model=load_gpt()
    assert ELITE_EXE.is_file() and ELITE_WEIGHTS.is_file()
    pgn=OUT/'ChessGPT_vs_Elite_v2.pgn'
    scorefile=OUT/'score.csv'
    with scorefile.open('w',newline='',encoding='utf-8') as f:
        csv.writer(f).writerow(['round','gpt_color','result','termination','plies','white_clock','black_clock'])
    points={'ChessGPT':0.0,'Elite v2':0.0}
    with chess.engine.SimpleEngine.popen_uci(str(ELITE_EXE),timeout=120,cwd=str(DIR/'lc0')) as elite:
        opts={'WeightsFile':str(ELITE_WEIGHTS),'Threads':1,'NNCacheSize':1024,
              'MinibatchSize':1,'MaxPrefetch':0,'RamLimitMb':256}
        elite.configure({k:v for k,v in opts.items() if k in elite.options})
        print('ELITE_BACKEND',elite.id,'OPTIONS',opts,flush=True)
        b=chess.Board()
        probe=elite.play(b,chess.engine.Limit(nodes=1))
        assert probe.move in b.legal_moves, 'Elite did not produce legal one-node move'
        gptprobe=choose_gpt_move(model,b,120.0)
        assert gptprobe in b.legal_moves, 'ChessGPT model could not produce legal move'
        print('PREFLIGHT_PASS',gptprobe,probe.move,flush=True)
        for n in range(1,11):
            result,gptwhite=game_play(n,model,elite,pgn,scorefile)
            whitepoints={'1-0':1.0,'0-1':0.0,'1/2-1/2':0.5}[result]
            gp=whitepoints if gptwhite else 1-whitepoints
            points['ChessGPT']+=gp
            points['Elite v2']+=1-gp
            print('SCORE',n,points,flush=True)
    print('MATCH_COMPLETE',points,flush=True)

if __name__=='__main__':main()
