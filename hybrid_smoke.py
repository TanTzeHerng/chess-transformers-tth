import math, os, re, subprocess, tarfile, urllib.request, pathlib
import numpy as np
import chess
import onnxruntime as ort

ROOT=pathlib.Path('.')
MODEL=ROOT/'model.onnx'
SF=next((ROOT/'sf').rglob('stockfish-ubuntu-x86-64'))

FEN_CHARS=list("0123456789abcdefghpnrkqPBNRQKw.")
IDX={c:i for i,c in enumerate(FEN_CHARS)}
SP=set("12345678")
BUCKET=(np.arange(128,dtype=np.float32)+0.5)/128

def actions():
    out=[]; b=chess.BaseBoard.empty()
    for sq in range(64):
        nxt=[]
        b.set_piece_at(sq,chess.Piece.from_symbol('Q')); nxt+=list(b.attacks(sq))
        b.set_piece_at(sq,chess.Piece.from_symbol('N')); nxt+=list(b.attacks(sq)); b.remove_piece_at(sq)
        for t in nxt: out.append(chess.square_name(sq)+chess.square_name(t))
    fs=list('abcdefgh')
    for r,nr in [('2','1'),('7','8')]:
        for i,f in enumerate(fs):
            base=f+r+f+nr; out += [base+p for p in 'qrbn']
            if f>'a':
                base=f+r+fs[i-1]+nr; out += [base+p for p in 'qrbn']
            if f<'h':
                base=f+r+fs[i+1]+nr; out += [base+p for p in 'qrbn']
    assert len(out)==1968 and len(set(out))==1968
    return {m:i for i,m in enumerate(out)}
MOVE=actions()

def tok(fen):
    board,side,cast,ep,hm,fm=fen.split()
    s=side+board.replace('/',''); o=[]
    for c in s:
        if c in SP: o += [IDX['.']]*int(c)
        else: o.append(IDX[c])
    if cast=='-': o += [IDX['.']]*4
    else:
        o += [IDX[c] for c in cast]; o += [IDX['.']]*(4-len(cast))
    if ep=='-': o += [IDX['.']]*2
    else: o += [IDX[c] for c in ep]
    hm += '.'*(3-len(hm)); fm += '.'*(3-len(fm))
    o += [IDX[c] for c in hm]; o += [IDX[c] for c in fm]
    assert len(o)==77
    return np.array(o,dtype=np.int64)

so=ort.SessionOptions(); so.intra_op_num_threads=2
sess=ort.InferenceSession(str(MODEL),so,providers=['CPUExecutionProvider'])

def policy(board):
    ms=sorted(list(board.legal_moves),key=lambda m:MOVE[m.uci()])
    ft=tok(board.fen(en_passant='legal'))
    x=np.zeros((len(ms),79),dtype=np.int64); x[:,:77]=ft; x[:,77]=[MOVE[m.uci()] for m in ms]
    lp=sess.run(None,{'tokens':x})[0]
    q=np.exp(lp.astype(np.float64))@BUCKET.astype(np.float64)
    z=sorted(zip(ms,q),key=lambda x:(-x[1],x[0].uci()))
    return z

p=subprocess.Popen([str(SF)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
def send(s): p.stdin.write(s+'\n'); p.stdin.flush()
def until(pred):
    lines=[]
    while True:
        s=p.stdout.readline()
        if s=='': raise RuntimeError('stockfish exited')
        s=s.rstrip(); lines.append(s)
        if pred(s): return lines
send('uci'); u=until(lambda s:s=='uciok'); assert any('Stockfish 16' in s for s in u)
send('setoption name Use NNUE value false'); send('setoption name Threads value 1'); send('setoption name Hash value 1')
send('isready'); until(lambda s:s=='readyok')
rx=re.compile(r'^Final evaluation\s+([+-]?\d+(?:\.\d+)?)\s+\(white side\)')
def hce(board):
    assert not board.is_check()
    send('position fen '+board.fen(en_passant='legal')); send('eval')
    lines=until(lambda s:s.startswith('Final evaluation'))
    assert not any('NNUE evaluation' in s for s in lines), lines
    f=next(s for s in reversed(lines) if s.startswith('Final evaluation'))
    m=rx.match(f); assert m, f
    cpw=round(float(m.group(1))*100)
    return cpw if board.turn==chess.WHITE else -cpw

b=chess.Board()
rank=policy(b)
print('POLICY_TOP5',[(m.uci(),round(float(q),6)) for m,q in rank[:5]])
scores=[]
for m,q in rank[:8]:
    b.push(m)
    if b.is_check():
        val=None
    else:
        val=-hce(b)
    b.pop()
    scores.append((m.uci(),val,float(q)))
print('DEPTH1_TOP8_HCE',scores)
best=max((x for x in scores if x[1] is not None),key=lambda x:x[1])
print('BEST',best)
send('quit'); p.wait(timeout=5)
print('SMOKE_OK')
