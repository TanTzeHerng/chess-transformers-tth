#!/usr/bin/env python3
"""True Lc0-backend service: ChessGPT 16-layer policy + Stockfish 16 classical Q.
Never calls Stockfish 'go'; no non-ChessGPT move ordering. Requires backend C++.
"""
import functools
import math
import os
import socketserver
import sys
import threading
from pathlib import Path
import chess
import torch
from cloud_match import load_gpt, neural_step, make_prompt, STOI
from chessgpt_mcts_uci import ClassicalValue, STOCKFISH

PORT=int(os.environ.get('CHESSGPT_LC0_PORT','19217'))
M=load_gpt()
E=ClassicalValue(STOCKFISH)
LOCK=threading.Lock()
P_CACHE={}
Q_CACHE={}
QUERY_COUNT=0

def reconstruct(fens):
    if not fens:raise ValueError('Bridge history empty')
    b=chess.Board(fens[0])
    for fen in fens[1:]:
        target=chess.Board(fen)
        target_tuple=(target.board_fen(),target.turn,target.castling_rights)
        found=False
        for move in b.legal_moves:
            b.push(move)
            same=(b.board_fen(),b.turn,b.castling_rights)==target_tuple
            if same:
                found=True
                break
            b.pop()
        if not found:
            raise ValueError('Cannot derive Lc0 move from recorded positions: '+b.fen()+' -> '+fen)
    target=chess.Board(fens[-1])
    if b.board_fen()!=target.board_fen() or b.turn!=target.turn:
        raise AssertionError('Lc0 position history reconstruction mismatch')
    return b

@torch.inference_mode()
def chessgpt_policy(board,legal_moves):
    prompt=make_prompt(board)
    key=(prompt,tuple(m.uci() for m in legal_moves))
    if key in P_CACHE:return P_CACHE[key]
    prompt=prompt[-(M.config.block_size-20):]
    # Shared-prefix SAN trie produces logprobability of every legal SAN.
    trie={}
    for ix,m in enumerate(legal_moves):
        node=trie
        for c in board.san(m):
            node=node.setdefault(c,{})
        node.setdefault('$',[]).append(ix)
    out=[float('-inf')]*len(legal_moves)
    init_logits,init_cache=neural_step(M,[STOI[c] for c in prompt])
    def visit(node,logits,cache,logprob):
        z=torch.log_softmax(logits,dim=-1)
        for char,child in node.items():
            if char=='$':
                score=logprob+float(z[STOI[' ']])
                for ix in child:out[ix]=score
            else:
                if char not in STOI:
                    raise ValueError('Unknown ChessGPT SAN character: '+repr(char))
                logits2,cache2=neural_step(M,[STOI[char]],cache)
                visit(child,logits2,cache2,logprob+float(z[STOI[char]]))
    visit(trie,init_logits,init_cache,0.0)
    if not all(math.isfinite(p) for p in out):
        raise RuntimeError('ChessGPT did not score every legal SAN move')
    peak=max(out)
    probs=[math.exp(x-peak) for x in out]
    denom=sum(probs)
    probs=[p/denom for p in probs]
    if len(P_CACHE)>4096:P_CACHE.clear()
    P_CACHE[key]=probs
    return probs

def classical_q(board,checks=0):
    outcome=board.outcome(claim_draw=True)
    if outcome:
        if outcome.winner is None:return 0.0
        return 1.0 if board.turn==outcome.winner else -1.0
    key=board.fen()
    if key in Q_CACHE:return Q_CACHE[key]
    cp=E.static_white_cp(board)
    if cp is None:
        if checks>=8:raise RuntimeError('SF16 classical eval not available after eight policy-led check resolutions')
        legal=list(board.legal_moves)
        prob=chessgpt_policy(board,legal)
        move=legal[max(range(len(legal)),key=lambda i:prob[i])]
        board.push(move)
        q=-classical_q(board,checks+1)
        board.pop()
    else:
        q=math.tanh((cp if board.turn else -cp)/320.0)
    if len(Q_CACHE)>4096:Q_CACHE.clear()
    Q_CACHE[key]=q
    return q

def evaluate(fens,moves):
    b=reconstruct(fens)
    legal=[]
    for u in moves:
        m=chess.Move.from_uci(u)
        if m not in b.legal_moves:
            raise RuntimeError('Lc0 and python-chess disagree on legal move: '+u+' FEN '+b.fen())
        legal.append(m)
    policy=chessgpt_policy(b,legal) if legal else []
    q=classical_q(b)
    return q,0.0,policy

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        header=self.rfile.readline()
        if not header:return
        n=int(header)
        if not 0<n<=64:raise ValueError('Invalid batch size')
        inputs=[]
        for i in range(n):
            histories=int(self.rfile.readline())
            if not 0<histories<700:raise ValueError('Invalid history length')
            fens=[self.rfile.readline().decode().strip() for _ in range(histories)]
            m=int(self.rfile.readline())
            if not 0<=m<=256:raise ValueError('Invalid move count')
            moves=[self.rfile.readline().decode().strip() for _ in range(m)]
            inputs.append((fens,moves))
        with LOCK:
            global QUERY_COUNT
            for fens,moves in inputs:
                q,d,p=evaluate(fens,moves)
                data=' '.join([format(q,'.9g'),format(d,'.9g')]+[format(x,'.9g') for x in p])
                self.wfile.write((data+'\n').encode())
                QUERY_COUNT+=1
                if QUERY_COUNT%20==0:
                    print('BACKEND_EVALUATIONS',QUERY_COUNT,'unique policies',len(P_CACHE),'unique values',len(Q_CACHE),flush=True)
            self.wfile.flush()

class Service(socketserver.TCPServer):
    allow_reuse_address=True
    request_queue_size=32

def main():
    assert E.static_white_cp(chess.Board()) is not None
    b=chess.Board()
    r=evaluate([b.fen()],[m.uci() for m in b.legal_moves])
    assert len(r[2])==20 and abs(sum(r[2])-1)<1e-5
    b.push_san('e4');b.push_san('e5')
    b2=reconstruct([chess.Board().fen(),chess.Board().variation_san([chess.Move.from_uci('e2e4')])]) if False else None
    print('CHESSGPT_LC0_SERVICE_PREFLIGHT_PASS',format(r[0],'.4f'),flush=True)
    with Service(('127.0.0.1',PORT),Handler) as server:
        print('CHESSGPT_LC0_SERVICE_READY',PORT,flush=True)
        server.serve_forever()

if __name__=='__main__':main()
