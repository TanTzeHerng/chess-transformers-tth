#!/usr/bin/env python3
"""Aggregate genuine SF17.1 depth1 1024-thread NNUE paired-match result artifacts.
Does NOT alter games, rerun benchmarks or infer scores from job success.
"""
import concurrent.futures
import datetime
import io
import json
import math
import os
import re
import statistics
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from scipy.stats import t as student_t

REPO=os.environ.get('GITHUB_REPOSITORY','TanTzeHerng/chess-transformers-tth')
RUNS=(39584430934,39789842315)
TOKEN=os.environ.get('GH_TOKEN','')
API='https://api.github.com/repos/'+REPO
HEADERS={'Accept':'application/vnd.github+json','User-Agent':'SF171-NNUE-1024-Result-Audit','X-GitHub-Api-Version':'2022-11-28'}
if TOKEN:HEADERS['Authorization']='Bearer '+TOKEN
PATTERN=re.compile(r'^sf171-1024-(?:1000|e8-confirm)-(20\d+)-(\d+)
NAMES=('nn-e8bac1c07a5a.nnue','nn-ddcfb9224cdb.nnue')
BASELINE='nn-1c0000000000.nnue'

def api(path):
    for attempt in range(4):
        try:
            req=urllib.request.Request(API+path,headers=HEADERS)
            with urllib.request.urlopen(req,timeout=40) as response:return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code not in (429,500,502,503,504):raise
        time.sleep(3*(attempt+1))
    raise RuntimeError('API request repeatedly failed '+path)

def download(artifact):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,request,fp,code,msg,headers,newurl):return None
    opener=urllib.request.build_opener(NoRedirect)
    req=urllib.request.Request(API+'/actions/artifacts/'+str(artifact['id'])+'/zip',headers=HEADERS)
    try:
        with opener.open(req,timeout=40) as resp:return resp.read()
    except urllib.error.HTTPError as e:
        if e.code not in (302,307):raise
        url=e.headers.get('Location','')
        if not url.startswith('https://'):raise ValueError('Insecure artifact redirect')
        with urllib.request.urlopen(url,timeout=70) as resp:return resp.read()

def artifact_list():
    ans=[]
    for run_id in RUNS:
        for page in range(1,4):
            v=api(f'/actions/runs/{run_id}/artifacts?per_page=100&page={page}')
            ans.extend(x for x in v.get('artifacts',[]) if not x.get('expired') and PATTERN.fullmatch(x.get('name','')))
            if len(v.get('artifacts',[]))<100:break
    return ans

def extract(artifact):
    match=PATTERN.fullmatch(artifact['name'])
    seed=int(match.group(1));pairs=int(match.group(2))
    with zipfile.ZipFile(io.BytesIO(download(artifact))) as archive:
        files={name.rsplit('/',1)[-1]:name for name in archive.namelist()}
        if 'summary.json' not in files or 'individual_games.jsonl' not in files:
            raise ValueError('Missing summary or individual games '+artifact['name'])
        summary=json.loads(archive.read(files['summary.json']).decode('utf8'))
        data=[json.loads(x) for x in archive.read(files['individual_games.jsonl']).decode('utf8').splitlines() if x.strip()]
    if summary.get('engine')!='Stockfish 17.1 unmodified' or summary.get('threads')!=1024 or summary.get('depth')!=1 or summary.get('baseline')!=BASELINE:
        raise ValueError('Settings mismatch '+artifact['name'])
    if len(summary.get('results',{}))!=1:raise ValueError('Expected one candidate per shard '+artifact['name'])
    cand=list(summary['results'])[0]
    want=NAMES[0] if seed<20261200 or 20261301<=seed<=20261348 else NAMES[1]
    if cand!=want:raise ValueError('Candidate and seed mismatch '+artifact['name'])
    result=summary['results'][cand]
    if result.get('completed')!=result.get('wins',0)+result.get('draws',0)+result.get('losses',0):
        raise ValueError('Completed and WDL disagreement '+artifact['name'])
    if result.get('valid_pairs',0)+result.get('technical_pairs',0)>pairs:
        raise ValueError('More pairs than allocated '+artifact['name'])
    if result.get('completed')!=2*result.get('valid_pairs',0):
        raise ValueError('Chess results do not represent paired games '+artifact['name'])
    if len(result.get('paired_scores',[]))!=result.get('valid_pairs'):
        raise ValueError('Paired scores length mismatch '+artifact['name'])
    if len(data)!=result.get('valid_pairs',0)+result.get('technical_pairs',0):
        raise ValueError('Individual paired record count mismatch '+artifact['name'])
    for gamepair in data:
        if gamepair.get('candidate')!=cand:raise ValueError('Candidate mismatched in individual game '+artifact['name'])
        if not isinstance(gamepair.get('games'),list) or len(gamepair['games'])>2:raise ValueError('Malformed pair')
        if any(g.get('technical') for g in gamepair['games']):
            continue
        if len(gamepair['games'])!=2:raise ValueError('Missing paired game '+artifact['name'])
        for g in gamepair['games']:
            if g.get('result') not in ('1-0','0-1','1/2-1/2'):
                raise ValueError('Non-chess result '+artifact['name'])
    return {'artifact':artifact['name'],'seed':seed,'pairs_requested':pairs,
            'candidate':cand,'wins':result['wins'],'draws':result['draws'],
            'losses':result['losses'],'valid_pairs':result['valid_pairs'],
            'technical_pairs':result['technical_pairs'],
            'paired_scores':result['paired_scores'],'completed':result['completed']}

def los(pairs):
    if len(pairs)<2:return None
    avg=statistics.mean(pairs);std=statistics.stdev(pairs)
    if std==0:return 1.0 if avg>0.5 else 0.0 if avg<0.5 else 0.5
    return float(student_t.cdf((avg-0.5)/(std/math.sqrt(len(pairs))),df=len(pairs)-1))

def main():
    out=Path(os.environ.get('OUT','nnue_audit'))
    out.mkdir(parents=True,exist_ok=True)
    artifacts=artifact_list()
    assert len({x['name'] for x in artifacts})==len(artifacts),'duplicate artifact names'
    print('ARTIFACTS_AVAILABLE',len(artifacts),'FROM_RUNS',RUNS,flush=True)
    records=[];problems=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures={executor.submit(extract,a):a for a in artifacts}
        for future in concurrent.futures.as_completed(futures):
            art=futures[future]
            try:records.append(future.result())
            except Exception as error:
                problems.append({'artifact':art['name'],'error':repr(error)})
                print('ARTIFACT_ERROR',art['name'],repr(error),flush=True)
    assert len({r['seed'] for r in records})==len(records), 'Duplicate opening-seed shards cannot be pooled'
    groups={}
    for name in NAMES:
        chunk=[r for r in records if r['candidate']==name]
        pairs=[p for r in chunk for p in r['paired_scores']]
        wins=sum(r['wins'] for r in chunk)
        draws=sum(r['draws'] for r in chunk)
        losses=sum(r['losses'] for r in chunk)
        valid_pairs=sum(r['valid_pairs'] for r in chunk)
        technical_pairs=sum(r['technical_pairs'] for r in chunk)
        if wins+draws+losses!=2*valid_pairs:raise ValueError('Aggregate paired count invalid')
        groups[name]={'artifact_count':len(chunk),'wins':wins,'draws':draws,'losses':losses,
                      'valid_pairs':valid_pairs,'technical_pairs':technical_pairs,
                      'completed_games':wins+draws+losses,'points':wins+.5*draws,
                      'score_pct':100*(wins+.5*draws)/(wins+draws+losses) if wins+draws+losses else None,
                      'los':los(pairs),'seeds':sorted(r['seed'] for r in chunk),
                      'missing_data_not_scored':True}
    result={'source_run_ids':RUNS,'total_shards':168,'reported_artifacts':len(artifacts),'readable_artifacts':len(records),
            'artifact_errors':problems,'candidates':groups,
            'calculated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'paired_los_method':'Student-t one-sided LOS from per-opening two-game averages',
            'status':'partial' if len(records)<168 else 'complete'}
    path=out/'nnue_1024_summary.json'
    path.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf8')
    (out/'shards.json').write_text(json.dumps(sorted(records,key=lambda x:x['seed']),indent=2),encoding='utf8')
    print('AGGREGATE',json.dumps(result,separators=(',',':')),flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf8') as f:
            f.write('# Stockfish 17.1 1024-thread NNUE match audit\n\n')
            f.write(f"Read {len(records)} of 168 shards. Missing/unreadable shards are NOT scored.\n\n")
            for n,r in groups.items():
                f.write(f"**{n}**: {r['wins']} W / {r['draws']} D / {r['losses']} L in {r['completed_games']} games; "
                        f"score {r['score_pct']}%, LOS {r['los']}, technical pairs {r['technical_pairs']}\n\n")
    if problems:raise RuntimeError('Some published results failed validation; see artifact_errors')

if __name__=='__main__':main()
)
NAMES=('nn-e8bac1c07a5a.nnue','nn-ddcfb9224cdb.nnue')
BASELINE='nn-1c0000000000.nnue'

def api(path):
    for attempt in range(4):
        try:
            req=urllib.request.Request(API+path,headers=HEADERS)
            with urllib.request.urlopen(req,timeout=40) as response:return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code not in (429,500,502,503,504):raise
        time.sleep(3*(attempt+1))
    raise RuntimeError('API request repeatedly failed '+path)

def download(artifact):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,request,fp,code,msg,headers,newurl):return None
    opener=urllib.request.build_opener(NoRedirect)
    req=urllib.request.Request(API+'/actions/artifacts/'+str(artifact['id'])+'/zip',headers=HEADERS)
    try:
        with opener.open(req,timeout=40) as resp:return resp.read()
    except urllib.error.HTTPError as e:
        if e.code not in (302,307):raise
        url=e.headers.get('Location','')
        if not url.startswith('https://'):raise ValueError('Insecure artifact redirect')
        with urllib.request.urlopen(url,timeout=70) as resp:return resp.read()

def artifact_list():
    ans=[]
    for page in range(1,4):
        v=api(f'/actions/runs/{RUN}/artifacts?per_page=100&page={page}')
        ans.extend(x for x in v.get('artifacts',[]) if not x.get('expired') and PATTERN.fullmatch(x.get('name','')))
        if len(v.get('artifacts',[]))<100:break
    return ans

def extract(artifact):
    match=PATTERN.fullmatch(artifact['name'])
    seed=int(match.group(1));pairs=int(match.group(2))
    with zipfile.ZipFile(io.BytesIO(download(artifact))) as archive:
        files={name.rsplit('/',1)[-1]:name for name in archive.namelist()}
        if 'summary.json' not in files or 'individual_games.jsonl' not in files:
            raise ValueError('Missing summary or individual games '+artifact['name'])
        summary=json.loads(archive.read(files['summary.json']).decode('utf8'))
        data=[json.loads(x) for x in archive.read(files['individual_games.jsonl']).decode('utf8').splitlines() if x.strip()]
    if summary.get('engine')!='Stockfish 17.1 unmodified' or summary.get('threads')!=1024 or summary.get('depth')!=1 or summary.get('baseline')!=BASELINE:
        raise ValueError('Settings mismatch '+artifact['name'])
    if len(summary.get('results',{}))!=1:raise ValueError('Expected one candidate per shard '+artifact['name'])
    cand=list(summary['results'])[0]
    want=NAMES[0] if seed<20261200 else NAMES[1]
    if cand!=want:raise ValueError('Candidate and seed mismatch '+artifact['name'])
    result=summary['results'][cand]
    if result.get('completed')!=result.get('wins',0)+result.get('draws',0)+result.get('losses',0):
        raise ValueError('Completed and WDL disagreement '+artifact['name'])
    if result.get('valid_pairs',0)+result.get('technical_pairs',0)>pairs:
        raise ValueError('More pairs than allocated '+artifact['name'])
    if result.get('completed')!=2*result.get('valid_pairs',0):
        raise ValueError('Chess results do not represent paired games '+artifact['name'])
    if len(result.get('paired_scores',[]))!=result.get('valid_pairs'):
        raise ValueError('Paired scores length mismatch '+artifact['name'])
    if len(data)!=result.get('valid_pairs',0)+result.get('technical_pairs',0):
        raise ValueError('Individual paired record count mismatch '+artifact['name'])
    for gamepair in data:
        if gamepair.get('candidate')!=cand:raise ValueError('Candidate mismatched in individual game '+artifact['name'])
        if not isinstance(gamepair.get('games'),list) or len(gamepair['games'])>2:raise ValueError('Malformed pair')
        if any(g.get('technical') for g in gamepair['games']):
            continue
        if len(gamepair['games'])!=2:raise ValueError('Missing paired game '+artifact['name'])
        for g in gamepair['games']:
            if g.get('result') not in ('1-0','0-1','1/2-1/2'):
                raise ValueError('Non-chess result '+artifact['name'])
    return {'artifact':artifact['name'],'seed':seed,'pairs_requested':pairs,
            'candidate':cand,'wins':result['wins'],'draws':result['draws'],
            'losses':result['losses'],'valid_pairs':result['valid_pairs'],
            'technical_pairs':result['technical_pairs'],
            'paired_scores':result['paired_scores'],'completed':result['completed']}

def los(pairs):
    if len(pairs)<2:return None
    avg=statistics.mean(pairs);std=statistics.stdev(pairs)
    if std==0:return 1.0 if avg>0.5 else 0.0 if avg<0.5 else 0.5
    return float(student_t.cdf((avg-0.5)/(std/math.sqrt(len(pairs))),df=len(pairs)-1))

def main():
    out=Path(os.environ.get('OUT','nnue_audit'))
    out.mkdir(parents=True,exist_ok=True)
    artifacts=artifact_list()
    assert len({x['name'] for x in artifacts})==len(artifacts),'duplicate artifact names'
    print('ARTIFACTS_AVAILABLE',len(artifacts),flush=True)
    records=[];problems=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures={executor.submit(extract,a):a for a in artifacts}
        for future in concurrent.futures.as_completed(futures):
            art=futures[future]
            try:records.append(future.result())
            except Exception as error:
                problems.append({'artifact':art['name'],'error':repr(error)})
                print('ARTIFACT_ERROR',art['name'],repr(error),flush=True)
    groups={}
    for name in NAMES:
        chunk=[r for r in records if r['candidate']==name]
        pairs=[p for r in chunk for p in r['paired_scores']]
        wins=sum(r['wins'] for r in chunk)
        draws=sum(r['draws'] for r in chunk)
        losses=sum(r['losses'] for r in chunk)
        valid_pairs=sum(r['valid_pairs'] for r in chunk)
        technical_pairs=sum(r['technical_pairs'] for r in chunk)
        if wins+draws+losses!=2*valid_pairs:raise ValueError('Aggregate paired count invalid')
        groups[name]={'artifact_count':len(chunk),'wins':wins,'draws':draws,'losses':losses,
                      'valid_pairs':valid_pairs,'technical_pairs':technical_pairs,
                      'completed_games':wins+draws+losses,'points':wins+.5*draws,
                      'score_pct':100*(wins+.5*draws)/(wins+draws+losses) if wins+draws+losses else None,
                      'los':los(pairs),'seeds':sorted(r['seed'] for r in chunk),
                      'missing_data_not_scored':True}
    result={'run_id':RUN,'total_shards':120,'reported_artifacts':len(artifacts),'readable_artifacts':len(records),
            'artifact_errors':problems,'candidates':groups,
            'calculated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'paired_los_method':'Student-t one-sided LOS from per-opening two-game averages',
            'status':'partial' if len(records)<120 else 'complete'}
    path=out/'nnue_1024_summary.json'
    path.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf8')
    (out/'shards.json').write_text(json.dumps(sorted(records,key=lambda x:x['seed']),indent=2),encoding='utf8')
    print('AGGREGATE',json.dumps(result,separators=(',',':')),flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf8') as f:
            f.write('# Stockfish 17.1 1024-thread NNUE match audit\n\n')
            f.write(f"Read {len(records)} of 120 shards. Missing/unreadable shards are NOT scored.\n\n")
            for n,r in groups.items():
                f.write(f"**{n}**: {r['wins']} W / {r['draws']} D / {r['losses']} L in {r['completed_games']} games; "
                        f"score {r['score_pct']}%, LOS {r['los']}, technical pairs {r['technical_pairs']}\n\n")
    if problems:raise RuntimeError('Some published results failed validation; see artifact_errors')

if __name__=='__main__':main()
