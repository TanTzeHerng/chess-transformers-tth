#!/usr/bin/env python3
"""ChessGPT-MCTS UCI.
Policy: Adam Karvonen's original ChessGPT 16L transformer, SAN likelihoods only.
Value: Stockfish 16 static CLASSICAL eval (Use NNUE=false), zero SF move search.
Tree: PUCT, terminal chess-rule values. No capture ordering, material heuristics,
handcrafted move scores, or NNUE. MCTS is bounded by UCI time control or stop.
"""
import heapq
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

import chess
import torch
import torch.nn.functional as F
from cloud_match import load_gpt, neural_step, make_prompt, STOI

ROOT=Path(__file__).resolve().parent
WEIGHTS=ROOT/'stockfish_16layers_ckpt_with_optimizer.pt'
STOCKFISH=ROOT/'stockfish16'/'stockfish-windows-x64-avx2.exe'
_MODEL=None
_ENGINE=None
PRINT_LOCK=threading.Lock()

def quantize_chessgpt_linears(model):
    """Quantize only pretrained transformer projection matrices on CPU.
    This changes numerical precision, not policy source or chess features;
    the tokenizer, embeddings, final policy head and positional encodings
    remain untouched. Requires empirical move-quality comparison.
    """
    import torch.nn as nn
    from torch.ao.quantization import quantize_dynamic
    for block in model.transformer.h:
        for parent,attribute in ((block.attn,'c_attn'),(block.attn,'c_proj'),
                                 (block.mlp,'c_fc'),(block.mlp,'c_proj')):
            linear=getattr(parent,attribute)
            wrapped=nn.Sequential(linear)
            converted=quantize_dynamic(wrapped,{nn.Linear},dtype=torch.qint8,inplace=True)
            setattr(parent,attribute,converted[0])
    model.eval()
    return model

def reply(line):
    with PRINT_LOCK:
        print(line,flush=True)

class ClassicalValue:
    """Read Stockfish 16 'eval', never 'go', with Use NNUE=false."""
    def __init__(self,path):
        self.proc=subprocess.Popen([str(path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL,text=True,bufsize=1,cwd=str(path.parent))
        self.lines=queue.Queue()
        self.thread=threading.Thread(target=self._reader,daemon=True)
        self.thread.start()
        self.send('uci')
        self.identity=''
        self.use_option=False
        while True:
            line=self.read(15)
            if line.startswith('id name'): self.identity=line
            if 'option name Use NNUE' in line: self.use_option=True
            if line=='uciok':break
        if not self.use_option or 'Stockfish 16' not in self.identity:
            raise RuntimeError('Exact Stockfish 16 with a Use NNUE option is mandatory: '+self.identity)
        self.send('setoption name Use NNUE value false')
        self.send('setoption name Threads value 1')
        self.send('isready')
        while self.read(15)!='readyok':pass
        self.cache=OrderedDict()
        self.fresh_evaluations=0
    def _reader(self):
        try:
            for line in self.proc.stdout:self.lines.put(line.strip())
        finally:
            self.lines.put(None)
    def read(self,secs=5):
        try:r=self.lines.get(timeout=secs)
        except queue.Empty:raise RuntimeError('Stockfish 16 evaluation timed out')
        if r is None:raise RuntimeError('Stockfish 16 exited unexpectedly')
        return r
    def send(self,s):
        self.proc.stdin.write(s+'\n')
        self.proc.stdin.flush()
    def static_white_cp(self,board):
        """Return Stockfish classical evaluation in centipawns, White's perspective.
        None if Stockfish's static evaluator cannot handle positions in check.
        """
        key=board.fen()
        if key in self.cache:
            v=self.cache.pop(key)
            self.cache[key]=v
            return v
        self.send('position fen '+key)
        self.send('eval')
        for _ in range(150):
            ln=self.read(5)
            if 'Final evaluation' in ln:
                # SF16 prints in PAWNS; 1.23 => 123 centipawns.
                m=re.search(r'Final evaluation\s*:?\s*([+-]?\d+(?:\.\d+)?)',ln)
                value=100*float(m.group(1)) if m else None
                if value is not None:
                    self.fresh_evaluations+=1
                self.cache[key]=value
                if len(self.cache)>2048:self.cache.popitem(last=False)
                return value
        raise RuntimeError('Stockfish 16 eval output omitted Final evaluation')
    def close(self):
        try:self.send('quit')
        except (BrokenPipeError,OSError):pass
        try:self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:self.proc.kill()

@torch.inference_mode()
def fast_neural_step(model, ids, cache=None):
    """Numerically equivalent GPT decoding for multiple appended tokens.
    Correctly applies a triangular attention mask OFFSET by the cached length:
    unlike unmasked multi-token cached attention, this cannot peek ahead.
    One fused transformer pass replaces one pass PER history character.
    """
    if not ids:
        raise ValueError('fast_neural_step requires new tokens')
    device=model.transformer.wte.weight.device
    xids=torch.tensor([ids],dtype=torch.long,device=device)
    b,t=xids.shape
    prev=cache[0][0].shape[2] if cache is not None else 0
    if prev+t>=model.config.block_size:
        raise ValueError('prompt too long for original absolute-position transformer')
    pos=torch.arange(prev,prev+t,device=device)
    x=model.transformer.wte(xids)+model.transformer.wpe(pos)
    mask=None
    if prev and t>1:
        mask=(torch.arange(prev+t,device=device).unsqueeze(0) <=
              (prev+torch.arange(t,device=device)).unsqueeze(1))
    updated=[]
    for i,h in enumerate(model.transformer.h):
        z=h.ln_1(x)
        channels=z.shape[-1]
        q,k,v=h.attn.c_attn(z).split(channels,dim=-1)
        def heads(y):
            return y.reshape(b,t,h.attn.n_head,channels//h.attn.n_head).transpose(1,2)
        q,k,v=heads(q),heads(k),heads(v)
        if cache is not None:
            k=torch.cat((cache[i][0],k),dim=2)
            v=torch.cat((cache[i][1],v),dim=2)
        att=F.scaled_dot_product_attention(q,k,v,attn_mask=mask,is_causal=cache is None)
        x=x+h.attn.c_proj(att.transpose(1,2).contiguous().reshape(b,t,channels))
        x=x+h.mlp.c_proj(F.gelu(h.mlp.c_fc(h.ln_2(x))))
        updated.append((k,v))
    logits=model.lm_head(model.transformer.ln_f(x[:,-1:,:]))[0,-1]
    return logits,updated

@torch.inference_mode()
def batched_san_costs(model,first_logits,root_cache,sans,batch_size=64):
    """Mathematically identical autoregressive SAN scores in one GPU/CPU
    vectorized operation per microbatch. No per-character GPU synchronization.
    Transformer weights remain the original ChessGPT policy.
    """
    if not sans:
        return []
    device=model.transformer.wte.weight.device
    root_log_probs=F.log_softmax(first_logits.float(),dim=-1)
    prev=root_cache[0][0].shape[2]
    result=[]
    for start in range(0,len(sans),batch_size):
        group=sans[start:start+batch_size]
        batch=len(group)
        lengths=[len(x) for x in group]
        max_len=max(lengths)
        ids=torch.tensor([[STOI[c] for c in san]+[STOI[' ']]*(max_len-len(san))
                          for san in group],dtype=torch.long,device=device)
        if prev+max_len>=model.config.block_size:
            raise ValueError('Model context exhausted: trim PGN history')
        pos=torch.arange(prev,prev+max_len,device=device)
        x=model.transformer.wte(ids)+model.transformer.wpe(pos)
        idx=torch.arange(max_len,device=device)
        mask=(torch.arange(prev+max_len,device=device).unsqueeze(0)
              <=(prev+idx).unsqueeze(1))
        for i,h in enumerate(model.transformer.h):
            z=h.ln_1(x)
            channels=z.shape[-1]
            q,k,v=h.attn.c_attn(z).split(channels,dim=-1)
            def heads(y):
                return y.reshape(batch,max_len,h.attn.n_head,channels//h.attn.n_head).transpose(1,2)
            q,k,v=heads(q),heads(k),heads(v)
            k=torch.cat((root_cache[i][0].expand(batch,-1,-1,-1),k),dim=2)
            v=torch.cat((root_cache[i][1].expand(batch,-1,-1,-1),v),dim=2)
            y=F.scaled_dot_product_attention(q,k,v,attn_mask=mask)
            x=x+h.attn.c_proj(y.transpose(1,2).contiguous().reshape(batch,max_len,channels))
            x=x+h.mlp.c_proj(F.gelu(h.mlp.c_fc(h.ln_2(x))))
        logits=model.lm_head(model.transformer.ln_f(x))
        log_probs=F.log_softmax(logits.float(),dim=-1)
        # Each SAN first character is predicted by the prefill logits.
        first_labels=torch.tensor([STOI[san[0]] for san in group],
                                  dtype=torch.long,device=device)
        first_cost=root_log_probs[first_labels]
        # After writing SAN character j, predict the NEXT SAN character,
        # or the exact PGN delimiter if j is the final character.
        next_labels=torch.tensor([
            [STOI[c] for c in san[1:]]+[STOI[' ']]*(max_len-len(san)+1)
            for san in group
        ],dtype=torch.long,device=device)
        chosen=log_probs.gather(-1,next_labels.unsqueeze(-1)).squeeze(-1)
        valid=(idx.unsqueeze(0)<torch.tensor(lengths,device=device).unsqueeze(1))
        scores=-(first_cost+(chosen*valid).sum(dim=-1))
        # Exactly one GPU->CPU transfer for ALL SAN completions in this batch.
        result.extend(scores.detach().cpu().tolist())
    return result

class Policy:
    """Best-first top-K FULL SAN sequence probabilities from the model.
    Pruning only depends on ChessGPT probability; no chess move scoring.
    """
    def __init__(self,model):
        self.model=model
        self.cache=OrderedDict()
        self.temperature=1.0
        # A transformer KV prefill at 1000 chars takes roughly 64 MiB.
        # Budget the whole prefix cache to avoid unbounded RAM growth.
        self.prefix_cache=OrderedDict()
        self.prefix_budget=240*1024*1024
        self.prefix_bytes=0
        self.san_batch_size=8
        self.full_prefills=0
        self.incremental_prefills=0
        self.cache_hits=0
        self.history_tokens_saved=0

    def reset(self):
        self.cache.clear()
        self.prefix_cache.clear()
        self.prefix_bytes=0

    @staticmethod
    def state_bytes(cache):
        return sum(k.numel()*k.element_size()+v.numel()*v.element_size() for k,v in cache)

    def _remember_prefix(self,prompt,logits,cache):
        bytes_used=self.state_bytes(cache)
        if prompt in self.prefix_cache:
            old=self.prefix_cache.pop(prompt)
            self.prefix_bytes-=old[2]
        self.prefix_cache[prompt]=(logits,cache,bytes_used)
        self.prefix_bytes+=bytes_used
        while self.prefix_bytes>self.prefix_budget and len(self.prefix_cache)>1:
            _,record=self.prefix_cache.popitem(last=False)
            self.prefix_bytes-=record[2]

    def _prefill(self,prompt):
        entry=self.prefix_cache.pop(prompt,None)
        if entry is not None:
            self.prefix_cache[prompt]=entry
            self.cache_hits+=1
            return entry[0],entry[1]
        longest=''
        data=None
        for key,rec in self.prefix_cache.items():
            if len(key)>len(longest) and prompt.startswith(key):
                longest=key
                data=rec
        if data is not None:
            # Cache contains exact untruncated text prefix and complete KV.
            tail=prompt[len(longest):]
            logits,cache=fast_neural_step(self.model,[STOI[c] for c in tail],data[1])
            self.incremental_prefills+=1
            self.history_tokens_saved+=len(longest)
        else:
            logits,cache=fast_neural_step(self.model,[STOI[c] for c in prompt])
            self.full_prefills+=1
        self._remember_prefix(prompt,logits,cache)
        return logits,cache

    def candidates(self,board,k,deadline,stop_event):
        s=make_prompt(board)
        # Keep the last 1000 input characters; model max context is 1024.
        s=s[-(self.model.config.block_size-20):]
        legal=list(board.legal_moves)
        if not legal:return []
        key=(s,tuple(m.uci() for m in legal),k)
        if key in self.cache:
            val=self.cache.pop(key);self.cache[key]=val;return val
        # Exact (not just top-K approximated) ChessGPT SAN probabilities.
        # Vectorized completion scoring replaces dozens of Python/PyTorch
        # forward calls for individual candidate characters.
        first,firstcache=self._prefill(s)
        sans=[board.san(m) for m in legal]
        costs=batched_san_costs(self.model,first,firstcache,sans,batch_size=self.san_batch_size)
        top=sorted(range(len(legal)),key=lambda i:costs[i])[:max(1,min(k,len(legal)))]
        base=costs[top[0]]
        raw=[(legal[i],math.exp(-(costs[i]-base)/self.temperature)) for i in top]
        total=sum(v for _,v in raw)
        result=[(m,v/total) for m,v in raw]
        self.cache[key]=result
        if len(self.cache)>256:self.cache.popitem(last=False)
        return result

class Node:
    __slots__=('prior','n','w','children','expanded')
    def __init__(self,p=1.0):
        self.prior=p
        self.n=0
        self.w=0.0
        self.children={}
        self.expanded=False
    @property
    def q(self):
        return self.w/self.n if self.n else 0.0

class Search:
    def __init__(self,model,stockfish):
        self.model=model
        self.value=stockfish
        self.policy=Policy(model)
        self.c_puct=1.7
        self.root_beam=8
        self.child_beam=4
        self.max_depth=100
        # Delay expensive neural policy expansions. All MCTS simulations
        # still select a path, obtain Stockfish16's classical leaf value, and
        # backpropagate that value. No synthetic/incremented node counters.
        self.expand_after_visits=8
        self.saved_root=None
        self.saved_position_moves=None
    def evaluate(self,board,deadline,stop_event,checks=0):
        terminal=board.outcome(claim_draw=True)
        if terminal:
            if terminal.winner is None:return 0.0
            return 1.0 if terminal.winner==board.turn else -1.0
        cp=self.value.static_white_cp(board)
        if cp is None:
            # SF16's 'eval' specifically returns "none (in check)".
            # Resolve checks with GPT's own move policy, never SF search or
            # a handcrafted king-safety heuristic, then use SF's static eval.
            # This is a one-step policy extension, NOT an alternative value.
            if checks>=8:return None
            moves=self.policy.candidates(board,1,deadline,stop_event)
            if not moves:return None
            board.push(moves[0][0])
            v=self.evaluate(board,deadline,stop_event,checks+1)
            board.pop()
            return None if v is None else -v
        return math.tanh((cp if board.turn else -cp)/320.0)
    def expand(self,node,board,k,deadline,stop_event):
        legal=self.policy.candidates(board,k,deadline,stop_event)
        node.children={m:Node(p) for m,p in legal}
        node.expanded=True
    def principal_variation(self,board,root):
        n=root
        pv=[]
        for _ in range(16):
            if not n.children:break
            m,c=max(n.children.items(),key=lambda it:(it[1].n,it[1].prior))
            if m not in board.legal_moves:break
            pv.append(m.uci());board.push(m);n=c
        for _ in pv:board.pop()
        return pv
    def go(self,board,params,stop_event):
        start=time.monotonic()
        movetime=params.get('movetime')
        if movetime is not None:
            allotted=max(0.02,movetime/1000)
        elif 'infinite' in params or ('nodes' in params and not any(
                key in params for key in ('wtime','btime','winc','binc'))):
            # UCI "go nodes N" has a NODE bound, not an invented default
            # clock. No hidden 2.5-second cutoff.
            allotted=86400
        else:
            color='w' if board.turn else 'b'
            remain=max(0.0,params.get(color+'time',120000)/1000)
            inc=max(0.0,params.get(color+'inc',1000)/1000)
            mtg=max(12,min(45,int(params.get('movestogo',36))))
            # Real clock manager: remaining time and increment, no nodes cap.
            allotted=min(max(0.025,remain-0.12),
                         max(0.08,0.86*(remain/(mtg+4)+inc*0.8)))
        deadline=start+allotted
        # Reuse the exact searched subtree across UCI calls within one game.
        # The game-history prefix is checked to preserve repetition semantics.
        moves=tuple(board.move_stack)
        root=None
        prior=self.saved_position_moves
        if prior is not None and self.saved_root is not None and len(moves)>=len(prior) and moves[:len(prior)]==prior:
            candidate=self.saved_root
            for follow in moves[len(prior):]:
                candidate=candidate.children.get(follow)
                if candidate is None:
                    break
            else:
                root=candidate
        if root is None:
            root=Node()
        policy_expansions=0
        if not root.expanded:
            self.expand(root,board,self.root_beam,deadline,stop_event)
            policy_expansions+=1
        if not root.children:
            raise RuntimeError('No policy move generated in legal position')
        last_info=start
        simulations=0;seldepth=0
        unique_leaves=set()
        fresh_classical_leaves=set()
        cached_evaluation_visits=0
        terminal_visits=0
        leaf_visits=0
        while not stop_event.is_set() and time.monotonic()<deadline:
            if 'nodes' in params and simulations>=params['nodes']:break
            path=[root];move_path=[];cur=root
            while cur.expanded and cur.children and len(move_path)<self.max_depth:
                best=max(cur.children.items(),key=lambda it:(
                    -it[1].q if it[1].n else 0.0)
                    +self.c_puct*it[1].prior*math.sqrt(cur.n+1)/(1+it[1].n))
                mv,nxt=best
                board.push(mv)
                move_path.append(mv)
                cur=nxt;path.append(cur)
                if board.outcome(claim_draw=True):break
            seldepth=max(seldepth,len(move_path))
            leaf_visits+=1
            position_key=' '.join(board.fen().split()[:4])
            unique_leaves.add(position_key)
            fen=board.fen()
            fresh_before=self.value.fresh_evaluations
            cache_hit_before=fen in self.value.cache
            # Original SF16 static evaluation is the only positional value.
            # Terminal nodes and cache returns NEVER count as newly evaluated
            # leaves, and transposed positions count at most once.
            value=self.evaluate(board,deadline,stop_event)
            if not cache_hit_before and self.value.fresh_evaluations>fresh_before and not board.is_check() and not board.is_game_over(claim_draw=True):
                fresh_classical_leaves.add(position_key)
            elif cache_hit_before:
                cached_evaluation_visits+=1
            elif board.is_game_over(claim_draw=True):
                terminal_visits+=1
            if value is None:
                # No classical value exists for this rare unresolved
                # check-extension. Never invent a neutral or handcrafted
                # evaluation in place of the mandated Stockfish 16 value.
                if not cur.expanded and not board.is_game_over(claim_draw=True) and time.monotonic()<deadline:
                    self.expand(cur,board,self.child_beam,deadline,stop_event)
                for _ in move_path:board.pop()
                if time.monotonic()>=deadline:break
                continue
            # Progressive expansion is *not* a free visit: each trial has
            # obtained the mandated Stockfish classical value (or chess
            # terminal outcome) and propagated a true score. The policy is
            # requested only when the frontier attracts enough visits.
            if (not cur.expanded and cur.n+1>=self.expand_after_visits
                and not board.is_game_over(claim_draw=True)
                and time.monotonic()<deadline):
                self.expand(cur,board,self.child_beam,deadline,stop_event)
                policy_expansions+=1
            for n in reversed(path):
                n.n+=1;n.w+=value;value=-value
            for _ in move_path:board.pop()
            simulations+=1
            now=time.monotonic()
            if now-last_info>=1.0:
                self.info(board,root,simulations,seldepth,start)
                last_info=now
        self.info(board,root,simulations,seldepth,start)
        reply('info string policy full_prefills %d incremental_prefills %d exact_cache_hits %d cached_history_chars %d kv_cache_MiB %.1f' %
              (self.policy.full_prefills,self.policy.incremental_prefills,
               self.policy.cache_hits,self.policy.history_tokens_saved,
               self.policy.prefix_bytes/(1024*1024)))
        reply('info string mcts completed_simulations %d unique_leaf_positions %d fresh_distinct_classical_leaf_evals %d cached_eval_visits %d terminal_visits %d leaf_value_visits %d neural_policy_expansions %d expand_threshold %d policy_temperature %.2f'%
              (simulations,len(unique_leaves),len(fresh_classical_leaves),cached_evaluation_visits,terminal_visits,leaf_visits,policy_expansions,self.expand_after_visits,self.policy.temperature))
        self.saved_root=root
        self.saved_position_moves=moves
        if simulations==0:
            move=max(root.children,key=lambda m:root.children[m].prior)
        else:
            move=max(root.children,key=lambda m:(root.children[m].n,root.children[m].prior))
        return move
    def info(self,board,root,sims,depth,start):
        t=max(1,int((time.monotonic()-start)*1000))
        pv=self.principal_variation(board,root)
        v=max(-0.999,min(0.999,root.q))
        cp=round(320*math.atanh(v))
        reply('info depth %d seldepth %d nodes %d time %d nps %d score cp %d pv %s'%
              (max(1,depth),depth,sims,t,sims*1000//t,cp,' '.join(pv)))

def parse_position(s):
    toks=s.split()
    if len(toks)<2:return chess.Board()
    if toks[1]=='startpos':
        b=chess.Board();index=2
    elif toks[1]=='fen':
        try:i=toks.index('moves')
        except ValueError:i=len(toks)
        b=chess.Board(' '.join(toks[2:i]));index=i
    else:raise ValueError('invalid position: '+s)
    if 'moves' in toks[index:]:
        index=toks.index('moves',index)+1
        for move in toks[index:]:b.push_uci(move)
    return b

def parse_go(cmd):
    ts=cmd.split()
    p={}
    for i,z in enumerate(ts):
        if z in ('wtime','btime','winc','binc','movetime','nodes','depth','movestogo') and i+1<len(ts):
            try:p[z]=max(0,int(ts[i+1]))
            except ValueError:pass
    if 'infinite' in ts:p['infinite']=1
    return p

def main():
    global _ENGINE
    board=chess.Board()
    current=None;stop_event=None
    def ensure():
        global _ENGINE
        if _ENGINE is None:
            if not WEIGHTS.is_file() or not STOCKFISH.is_file():
                raise RuntimeError('Missing exact ChessGPT/Stockfish16 checkpoint/executable')
            model=load_gpt()
            requested=os.environ.get('CHESSGPT_DEVICE','auto').lower()
            if requested not in ('auto','cpu','cuda','xpu'):
                raise RuntimeError('CHESSGPT_DEVICE must be auto/cpu/cuda/xpu')
            if requested=='auto':
                requested=('cuda' if torch.cuda.is_available()
                           else 'xpu' if hasattr(torch,'xpu') and torch.xpu.is_available()
                           else 'cpu')
            if requested=='cuda' and not torch.cuda.is_available():
                raise RuntimeError('CUDA GPU requested but not accessible')
            if requested=='xpu' and (not hasattr(torch,'xpu') or not torch.xpu.is_available()):
                raise RuntimeError('Intel XPU requested but not accessible')
            if requested!='cpu':
                if os.environ.get('CHESSGPT_INT8','0')=='1':
                    raise RuntimeError('PyTorch dynamic INT8 quantization is CPU-only; disable CHESSGPT_INT8 for GPU')
                model=model.to(device=requested,dtype=torch.float16).eval()
            reply('info string ChessGPT policy device %s model_dtype %s'%
                  (str(model.transformer.wte.weight.device),str(model.transformer.wte.weight.dtype)))
            if os.environ.get('CHESSGPT_INT8','0')=='1':
                model=quantize_chessgpt_linears(model)
                reply('info string INT8 quantized transformer matrices enabled')
            value=ClassicalValue(STOCKFISH)
            p=value.static_white_cp(chess.Board())
            if p is None:raise RuntimeError('Stockfish16 classical eval preflight failed')
            _ENGINE=Search(model,value)
            reply('info string ChessGPT 16L and Stockfish 16 classical evaluator loaded; startpos eval %.2f cp'%p)
    for raw in sys.stdin:
        cmd=raw.strip()
        try:
            if cmd=='uci':
                reply('id name ChessGPT-MCTS-16L-Stockfish16Classical')
                reply('id author Adam Karvonen (model); ChatGPT (UCI/MCTS)')
                reply('option name CPuct type spin default 170 min 10 max 1000')
                reply('option name TorchThreads type spin default 2 min 1 max 8')
                reply('option name SANBatchSize type spin default 8 min 1 max 128')
                reply('option name QuantizeTransformer type check default false')
                reply('option name RootPolicyBeam type spin default 8 min 1 max 64')
                reply('option name ExpandAfterVisits type spin default 8 min 1 max 64')
                reply('option name PolicyTemperature type spin default 100 min 25 max 2000')
                reply('option name ChildPolicyBeam type spin default 4 min 1 max 64')
                reply('uciok')
            elif cmd=='isready':
                ensure()
                reply('readyok')
            elif cmd=='ucinewgame':
                if _ENGINE:
                    _ENGINE.policy.reset()
                    _ENGINE.saved_root=None
                    _ENGINE.saved_position_moves=None
                board=chess.Board()
            elif cmd.startswith('position'):
                board=parse_position(cmd)
            elif cmd.startswith('setoption'):
                m=re.match(r'setoption name (.*?) value (.*)$',cmd)
                if m:
                    ensure()
                    name,v=m.group(1),int(m.group(2))
                    if name=='CPuct':_ENGINE.c_puct=v/100
                    if name=='TorchThreads':torch.set_num_threads(v)
                    if name=='SANBatchSize':_ENGINE.policy.san_batch_size=v;_ENGINE.policy.cache.clear()
                    if name=='RootPolicyBeam':_ENGINE.root_beam=v
                    if name=='ExpandAfterVisits':_ENGINE.expand_after_visits=v
                    if name=='PolicyTemperature':_ENGINE.policy.temperature=v/100;_ENGINE.policy.cache.clear()
                    if name=='ChildPolicyBeam':_ENGINE.child_beam=v
            elif cmd.startswith('go'):
                ensure()
                if current is not None and current.is_alive():
                    stop_event.set();current.join()
                stop_event=threading.Event()
                position=board.copy(stack=True)
                params=parse_go(cmd)
                def runner(b=position,pp=params,ev=stop_event):
                    try:
                        mv=_ENGINE.go(b,pp,ev)
                        reply('bestmove '+mv.uci())
                    except Exception as ex:
                        reply('info string ENGINE_ERROR '+str(ex).replace('\n',' ')[:250])
                        # Never invent a replacement from another chess engine.
                        # The UCI run fails clearly instead of silently cheating.
                        reply('bestmove 0000')
                current=threading.Thread(target=runner,daemon=True)
                current.start()
            elif cmd=='stop':
                if stop_event:stop_event.set()
                if current:current.join(timeout=12)
            elif cmd=='quit':
                if stop_event:stop_event.set()
                if current:current.join(timeout=12)
                if _ENGINE:_ENGINE.value.close()
                break
        except Exception as ex:
            reply('info string UCI_ERROR '+str(ex).replace('\n',' ')[:260])
            if cmd=='isready':reply('readyok')

if __name__=='__main__':
    try:main()
    except Exception as ex:
        print('ENGINE_FATAL',repr(ex),file=sys.stderr,flush=True)
        raise
