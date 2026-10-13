#!/usr/bin/env python3
"""Accelerated ChessGPT-PUCT: genuinely batched leaf selection and GPU policy.

ONLY model-dependent policy: Adam Karvonen ChessGPT 16L, exact autoregressive
SAN likelihoods. ONLY nonterminal value: Stockfish 16 "eval" with Use NNUE=false.
Terminal positions use chess rules. No captures, piece values, move ordering,
lookahead from another engine, or fabricated simulations.

Batching: collect distinct frontier leaves with virtual loss, evaluate each
using Stockfish classical evaluator, and compute all their neural policies in
microbatches on CUDA/XPU. The UCI nodes counter increases ONLY after a real
leaf evaluation is backed up into MCTS, never on a reservation or cache hit.
"""
import math
import os
import time
from collections import OrderedDict

import chess
import torch
import torch.nn.functional as F

from chessgpt_mcts_uci import Search, Policy, Node, reply
from cloud_match import STOI, make_prompt


def pick_accelerator(requested='auto'):
    if requested in ('auto','cuda') and torch.cuda.is_available():
        return torch.device('cuda')
    if requested in ('auto','xpu') and hasattr(torch,'xpu') and torch.xpu.is_available():
        return torch.device('xpu')
    if requested in ('cuda','xpu'):
        raise RuntimeError('Requested GPU accelerator is not available: '+requested)
    return torch.device('cpu')


class BatchPolicy:
    """Joint policy head for many different MCTS leaf positions.

    Exact next-token likelihood of each complete legal SAN is computed from
    the same ChessGPT model, without changing the neural priors. Grouping
    different board positions merely moves work into fewer device kernels.
    """
    def __init__(self,model,device,move_batch=48):
        self.model=model
        self.device=device
        self.move_batch=move_batch
        self.baseline=Policy(model) if device.type=='cpu' else None
        self.cached=OrderedDict()
        self.batches=0
        self.leaves_scored=0
        self.moves_scored=0
        self.prefill_characters=0

    def reset(self):
        self.cached.clear()
        if self.baseline:self.baseline.reset()

    @torch.inference_mode()
    def _prefill_many(self,prompts):
        model=self.model
        device=self.device
        lengths=torch.tensor([len(s) for s in prompts],device=device,dtype=torch.long)
        batch=len(prompts)
        maxlen=int(lengths.max().item())
        ids=torch.full((batch,maxlen),STOI[' '],device=device,dtype=torch.long)
        for i,s in enumerate(prompts):
            ids[i,:len(s)]=torch.tensor([STOI[c] for c in s],device=device)
        positions=torch.arange(maxlen,device=device,dtype=torch.long)
        x=model.transformer.wte(ids)+model.transformer.wpe(positions)
        at=torch.arange(maxlen,device=device)
        causal=at[:,None]>=at[None,:]
        valid=at[None,:]<lengths[:,None]
        mask=(causal[None,:,:]&valid[:,None,:])[:,None,:,:]
        caches=[]
        for h in model.transformer.h:
            z=h.ln_1(x)
            channels=z.shape[-1]
            q,k,v=h.attn.c_attn(z).split(channels,dim=-1)
            head_count=h.attn.n_head
            def heads(t):
                return t.reshape(batch,maxlen,head_count,channels//head_count).transpose(1,2)
            q,k,v=heads(q),heads(k),heads(v)
            a=F.scaled_dot_product_attention(q,k,v,attn_mask=mask)
            x=x+h.attn.c_proj(a.transpose(1,2).contiguous().reshape(batch,maxlen,channels))
            x=x+h.mlp.c_proj(F.gelu(h.mlp.c_fc(h.ln_2(x))))
            caches.append((k,v))
        row=torch.arange(batch,device=device)
        final=x[row,lengths-1,:]
        logits=model.lm_head(model.transformer.ln_f(final))
        return logits,caches,lengths

    @torch.inference_mode()
    def _score_moves(self,first,caches,lengths,entries):
        # entries = [(leaf_index, chess.Move, SAN), ...]
        device=self.device
        model=self.model
        source=torch.tensor([q[0] for q in entries],dtype=torch.long,device=device)
        sans=[q[2] for q in entries]
        n=len(entries)
        width=max(map(len,sans))
        max_prompt=caches[0][0].shape[2]
        san_len=torch.tensor([len(s) for s in sans],device=device,dtype=torch.long)
        ids=torch.full((n,width),STOI[' '],device=device,dtype=torch.long)
        for i,s in enumerate(sans):
            ids[i,:len(s)]=torch.tensor([STOI[c] for c in s],device=device)
        valid_len=lengths.index_select(0,source)
        positions=valid_len[:,None]+torch.arange(width,device=device)[None,:]
        x=model.transformer.wte(ids)+model.transformer.wpe(positions)
        past_key_mask=torch.arange(max_prompt,device=device)[None,:]<valid_len[:,None]
        tok_t=torch.arange(width,device=device)
        causal=tok_t[:,None]>=tok_t[None,:]
        mask=torch.cat((past_key_mask[:,None,:].expand(-1,width,-1),
                        causal[None,:,:].expand(n,-1,-1)),dim=-1)[:,None,:,:]
        for j,h in enumerate(model.transformer.h):
            z=h.ln_1(x)
            channels=z.shape[-1]
            q,k,v=h.attn.c_attn(z).split(channels,dim=-1)
            head_count=h.attn.n_head
            def heads(t):
                return t.reshape(n,width,head_count,channels//head_count).transpose(1,2)
            q,k,v=heads(q),heads(k),heads(v)
            old_k,old_v=caches[j]
            k=torch.cat((old_k.index_select(0,source),k),dim=2)
            v=torch.cat((old_v.index_select(0,source),v),dim=2)
            a=F.scaled_dot_product_attention(q,k,v,attn_mask=mask)
            x=x+h.attn.c_proj(a.transpose(1,2).contiguous().reshape(n,width,channels))
            x=x+h.mlp.c_proj(F.gelu(h.mlp.c_fc(h.ln_2(x))))
        logits=model.lm_head(model.transformer.ln_f(x))
        logp=F.log_softmax(logits.float(),dim=-1)
        first_logp=F.log_softmax(first.float(),dim=-1).index_select(0,source)
        first_ids=ids[:,0]
        first_cost=-first_logp.gather(1,first_ids[:,None]).squeeze(1)
        # SAN characters 1..end, followed by an ASCII space. The space
        # matters because that is the delimiter used during GPT training.
        subsequent=torch.full((n,width),STOI[' '],dtype=torch.long,device=device)
        if width>1: subsequent[:,:-1]=ids[:,1:]
        selected=logp.gather(2,subsequent.unsqueeze(2)).squeeze(2)
        active=tok_t[None,:]<san_len[:,None]
        cost=first_cost-(selected*active).sum(dim=1)
        return cost.float().cpu().tolist()

    def candidates_batch(self,boards,k,deadline,stop_event):
        output=[None]*len(boards)
        work=[]
        for i,b in enumerate(boards):
            if not any(b.legal_moves):
                output[i]=[]
                continue
            s=make_prompt(b)[-(self.model.config.block_size-20):]
            key=(s,k)
            if key in self.cached:
                result=self.cached.pop(key)
                self.cached[key]=result
                output[i]=result
                continue
            legal=[(m,b.san(m)) for m in b.legal_moves]
            work.append((i,s,key,legal))
        if self.baseline:
            for i,s,key,legal in work:
                output[i]=self.baseline.candidates(boards[i],k,deadline,stop_event)
                self.cached[key]=output[i]
            return output
        if not work:return output
        self.batches+=1
        # Large GPU arrays are controlled by microbatching positions. The
        # same model and score transformation are used regardless of batch.
        for start in range(0,len(work),max(1,min(12,self.move_batch//12))):
            group=work[start:start+max(1,min(12,self.move_batch//12))]
            prompts=[w[1] for w in group]
            first,caches,lengths=self._prefill_many(prompts)
            entries=[]
            for j,(_,s,key,legal) in enumerate(group):
                entries.extend((j,m,san) for m,san in legal)
            costs=[]
            for a in range(0,len(entries),self.move_batch):
                costs.extend(self._score_moves(first,caches,lengths,entries[a:a+self.move_batch]))
            by_leaf=[[] for _ in group]
            for entry,score in zip(entries,costs):
                by_leaf[entry[0]].append((entry[1],score))
            for j,(outer,s,key,legal) in enumerate(group):
                top=sorted(by_leaf[j],key=lambda x:x[1])[:max(1,min(k,len(legal)))]
                anchor=top[0][1]
                raw=[(m,math.exp(-(cost-anchor))) for m,cost in top]
                total=sum(p for m,p in raw)
                result=[(m,p/total) for m,p in raw]
                output[outer]=result
                self.cached[key]=result
                if len(self.cached)>1000:self.cached.popitem(last=False)
            self.leaves_scored+=len(group)
            self.moves_scored+=len(entries)
            self.prefill_characters+=sum(map(len,prompts))
        return output

    def candidates(self,board,k,deadline,stop_event):
        return self.candidates_batch([board],k,deadline,stop_event)[0]


class BatchedSearch(Search):
    def __init__(self,model,stockfish,device='cpu',leaf_batch=8,san_batch=48):
        super().__init__(model,stockfish)
        self.device=torch.device(device)
        self.policy=BatchPolicy(model,self.device,move_batch=san_batch)
        self.leaf_batch=leaf_batch
        self.max_depth=100

    def go(self,board,params,stop_event):
        begin=time.monotonic()
        if 'movetime' in params:
            allotted=max(0.02,params['movetime']/1000)
        elif 'infinite' in params or ('nodes' in params and not any(
                x in params for x in ('wtime','btime','winc','binc'))):
            allotted=86400
        else:
            c='w' if board.turn else 'b'
            remain=max(0.0,params.get(c+'time',120000)/1000)
            inc=max(0.0,params.get(c+'inc',1000)/1000)
            mtg=max(12,min(45,int(params.get('movestogo',36))))
            allotted=min(max(0.025,remain-0.12),max(0.08,0.86*(remain/(mtg+4)+inc*0.8)))
        deadline=begin+allotted
        moves=tuple(board.move_stack)
        root=None
        old=self.saved_position_moves
        if old is not None and self.saved_root is not None and len(moves)>=len(old) and moves[:len(old)]==old:
            candidate=self.saved_root
            for follow in moves[len(old):]:
                candidate=candidate.children.get(follow)
                if candidate is None:break
            else:root=candidate
        if root is None:root=Node()
        if not root.expanded:
            self.expand(root,board,self.root_beam,deadline,stop_event)
        if not root.children:
            raise RuntimeError('No legal moves or failed ChessGPT policy expansion')
        n_sims=0
        seldepth=0
        start_info=begin
        batch_cap=max(1,self.leaf_batch)
        virtual_penalty=0.9
        while not stop_event.is_set() and time.monotonic()<deadline:
            remaining_nodes=params.get('nodes',10**15)-n_sims
            if remaining_nodes<=0:break
            leaves=[]
            reserved=set()
            virtual={}
            for _ in range(min(batch_cap,remaining_nodes)):
                # Visit a DISTINCT leaf position when possible; duplicate
                # pending positions do not count as extra simulations.
                b=board.copy(stack=True)
                path=[root]
                current=root
                depth=0
                while current.expanded and current.children and depth<self.max_depth:
                    pending_parent=virtual.get(current,0)
                    sq=math.sqrt(current.n+pending_parent+1)
                    def score(item):
                        child=item[1]
                        pending=virtual.get(child,0)
                        return ((-child.q if child.n else 0.0)
                                -virtual_penalty*pending
                                +self.c_puct*child.prior*sq/(1+child.n+pending))
                    move,child=max(current.children.items(),key=score)
                    b.push(move)
                    path.append(child)
                    current=child
                    depth+=1
                    if b.is_game_over(claim_draw=True):break
                position=tuple(b.move_stack)
                if position in reserved:break
                reserved.add(position)
                for ancestor in path:virtual[ancestor]=virtual.get(ancestor,0)+1
                leaves.append((b,current,path))
                seldepth=max(seldepth,depth)
            if not leaves:break
            valued=[]
            # The value comes exclusively from SF16 classical 'eval' or
            # from a genuine terminal outcome. This is not an LLM value head.
            for b,node,path in leaves:
                if stop_event.is_set():break
                score=self.evaluate(b,deadline,stop_event)
                if score is not None:
                    valued.append((b,node,path,score))
            if not valued:break
            expanding=[(b,node) for b,node,path,v in valued
                       if not node.expanded and not b.is_game_over(claim_draw=True)]
            if expanding and time.monotonic()<deadline:
                boards=[b for b,n in expanding]
                priors=self.policy.candidates_batch(boards,self.child_beam,deadline,stop_event)
                for (b,node),candidates in zip(expanding,priors):
                    node.children={m:Node(p) for m,p in candidates}
                    node.expanded=True
            for b,node,path,value in valued:
                for ancestor in reversed(path):
                    ancestor.n+=1
                    ancestor.w+=value
                    value=-value
                n_sims+=1
            now=time.monotonic()
            if now-start_info>=1:
                self.info(board,root,n_sims,seldepth,begin)
                start_info=now
        self.saved_root=root
        self.saved_position_moves=moves
        self.info(board,root,n_sims,seldepth,begin)
        reply('info string accelerator %s leaf_batch %d policy_batches %d leaf_positions %d scored_moves %d'%
              (self.device.type,self.leaf_batch,self.policy.batches,
               self.policy.leaves_scored,self.policy.moves_scored))
        if not n_sims:
            return max(root.children,key=lambda m:root.children[m].prior)
        return max(root.children,key=lambda m:(root.children[m].n,root.children[m].prior))
