#!/usr/bin/env python3
"""Consolidate immutable GitHub Actions Cassandre result artifacts, never dropping unscored puzzles."""
import argparse
import concurrent.futures
import hashlib
import io
import json
import math
import datetime
import os
from pathlib import Path
import re
import time
import urllib.request
import urllib.error
import zipfile

LEGACY_RUN_ID=39586706185
DATASET_SHA256='abef248c372e48b7904368f82921d6491415b91c928d35d164d4e3534c90833b'
SHARD_SIZE=135
TOKEN=os.environ['GH_TOKEN']
REPO=os.environ['GITHUB_REPOSITORY']
CURRENT_RUN=os.environ.get('GITHUB_RUN_ID','')
API='https://api.github.com/repos/'+REPO

def api(path):
    request=urllib.request.Request(API+path,headers={
        'Authorization':'Bearer '+TOKEN,'Accept':'application/vnd.github+json',
        'X-GitHub-Api-Version':'2022-11-28','User-Agent':'Cassandre-recovery'})
    for retry in range(7):
        try:
            with urllib.request.urlopen(request,timeout=65) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code not in (403,429,500,502,503,504):raise
            if e.code==403:
                detail=e.read().decode('utf-8','replace').lower()
                if not any(term in detail for term in ('rate limit','abuse detection','secondary limit')):
                    raise
            if retry==6:raise
            delay=min(120,5*(2**retry))
            print('GITHUB_API_BACKOFF',e.code,'seconds',delay,flush=True)
            time.sleep(delay)
    raise RuntimeError('GitHub REST request exhausted retries '+path)

def artifact_zip(artifact_id):
    # Do not forward GitHub bearer tokens to the signed blob-storage redirect.
    class StopRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):return None
    request=urllib.request.Request(API+'/actions/artifacts/'+str(artifact_id)+'/zip',
        headers={'Authorization':'Bearer '+TOKEN,'Accept':'application/vnd.github+json',
                 'User-Agent':'Cassandre-recovery'})
    opener=urllib.request.build_opener(StopRedirect)
    try:
        response=opener.open(request,timeout=60)
        content=response.read()
        response.close()
        return content
    except urllib.error.HTTPError as e:
        if e.code!=302:raise
        redirect=e.headers.get('Location')
        if not redirect or not redirect.startswith('https://'):
            raise RuntimeError('Unsigned or insecure artifact redirect')
        with urllib.request.urlopen(redirect,timeout=90) as response:
            return response.read()

def artifact_list(run_id):
    result=[]
    for page in range(1,20):
        dat=api('/actions/runs/'+str(run_id)+'/artifacts?per_page=100&page='+str(page))
        items=dat.get('artifacts',[])
        result.extend(x for x in items if not x.get('expired'))
        if len(items)<100:break
    return result

def previous_recovery_runs():
    result=[]
    for page in range(1,4):
        dat=api('/actions/workflows/cassandre-recovery.yml/runs?per_page=100&page='+str(page))
        items=dat.get('workflow_runs',[])
        for run in items:
            if str(run['id'])!=CURRENT_RUN and run.get('status') in ('completed','in_progress','queued','waiting','pending'):
                result.append(run)
        if len(items)<100:break
    # Never truncate historical recovery runs: older artifacts contain unique rated puzzles.
    # Deduplication by puzzle index occurs during ingestion, so preserving all runs is safe.
    return sorted(result,key=lambda r:r['created_at'])

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dataset',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--audit-partial',action='store_true',help='Read finished shard artifacts during live legacy run; never launch new solvers')
    parser.add_argument('--publish-rating',action='store_true',help='Publish exact Glicko calculation as a GitHub check run')
    args=parser.parse_args()
    if hashlib.sha256(args.dataset.read_bytes()).hexdigest()!=DATASET_SHA256:
        raise RuntimeError('Wrong dataset bytes; cannot reconcile checkpoint indices')
    dataset=[json.loads(line) for line in args.dataset.read_text(encoding='utf8').splitlines() if line.strip()]
    count=len(dataset)
    assert count==33460, 'Puzzle count changed'
    args.out.mkdir(parents=True,exist_ok=True)
    legacy=api('/actions/runs/'+str(LEGACY_RUN_ID))
    if legacy.get('status')!='completed' and not args.audit_partial:
        report={'state':'waiting_for_original_run','total':count,'resolved':0,
                'note':'Do not race against still-running legacy shards'}
        (args.out/'coverage.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        output(os.environ.get('GITHUB_OUTPUT'),[],False)
        print('WAITING_FOR_LEGACY',legacy.get('status'),flush=True)
        return
    resolved={}
    source_stats={}
    sources=[('legacy',LEGACY_RUN_ID,'cassandre-2300-shard-')]
    sources.extend(('recovery',run['id'],'cassandre-recovery-shard-')
                   for run in previous_recovery_runs())
    for name,runid,prefix in sources:
        arts=artifact_list(runid)
        relevant=[]
        for art in arts:
            found=re.fullmatch(re.escape(prefix)+r'(\d+)',art['name'])
            if found and not art.get('expired'):
                relevant.append((int(found.group(1)),art))
            elif name=='recovery' and art['name']=='cassandre-recovery-smoke' and not art.get('expired'):
                # The earlier smoke run successfully rescued puzzle index 2028.
                relevant.append((2028//SHARD_SIZE,art))
        print('SOURCE',name,runid,'ARTIFACTS',len(relevant),flush=True)
        if not relevant:continue
        def ingest(item):
            shard,art=item
            try:
                for attempt in range(4):
                    try:
                        data=artifact_zip(art['id'])
                        break
                    except urllib.error.HTTPError as e:
                        if e.code not in (403,429,500,502,503,504) or attempt==3:raise
                        delay=5*(attempt+1)
                        print('ARTIFACT_RETRY',runid,shard,'HTTP',e.code,'seconds',delay,flush=True)
                        time.sleep(delay)
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    files=[fn for fn in z.namelist() if fn.endswith('results.jsonl')]
                    if not files:return shard,[],None
                    records=[]
                    for line in z.read(files[0]).decode('utf8').splitlines():
                        try:r=json.loads(line)
                        except ValueError:continue
                        idx=r.get('index')
                        if not isinstance(idx,int) or idx<0 or idx>=count:
                            continue
                        if idx//SHARD_SIZE!=shard or r.get('id')!=dataset[idx]['id']:
                            raise RuntimeError('Artifact index or puzzle ID mismatch at '+str(idx))
                        if isinstance(r.get('solved'),bool) or r.get('invalid_source'):
                            records.append(r)
                    return shard,records,None
            except Exception as exc:
                return shard,[],repr(exc)
        good=0
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            for shard,records,err in pool.map(ingest,relevant):
                if err:
                    print('ARTIFACT_WARNING',name,runid,shard,err,flush=True)
                    continue
                for record in records:
                    resolved.setdefault(record['index'],record)
                good+=1
        source_stats[str(runid)]={'artifacts':len(relevant),'readable':good,'resolved_so_far':len(resolved)}
        print('INGESTED',name,runid,'READABLE',good,'TOTAL_UNIQUE',len(resolved),flush=True)
    shards={}
    for idx,record in resolved.items():
        shards.setdefault(idx//SHARD_SIZE,[]).append(record)
    checkpoint_dir=args.out/'checkpoints'
    checkpoint_dir.mkdir(exist_ok=True)
    for shard,records in shards.items():
        path=checkpoint_dir/('shard-%03d.jsonl'%shard)
        with path.open('w',encoding='utf8') as f:
            for record in sorted(records,key=lambda r:r['index']):
                f.write(json.dumps(record,separators=(',',':'))+'\n')
    unscored=[i for i in range(count) if i not in resolved]
    pending_shards=sorted({i//SHARD_SIZE for i in unscored})
    report={'state':('audit_only_original_in_progress' if args.audit_partial and legacy.get('status')!='completed' else ('completed' if not pending_shards else 'retry_required')),
            'total':count,'resolved':len(resolved),'remaining_unscored':len(unscored),
            'pending_shards':pending_shards,'source_runs':source_stats,
            'dataset_sha256':DATASET_SHA256,
            'missing_indices_first_100':unscored[:100],
            'technical_is_not_incorrect':True}
    # Exact Glicko-1 single-period calculation: use real individually indexed,
    # verified results, never assume a shard completed a contiguous prefix.
    q=math.log(10)/400
    starting_rating=2500.0
    starting_rd=350.0
    expected_sum=0.0
    information=0.0
    wins=losses=invalid=0
    most_recent=None
    for idx,record in resolved.items():
        if record.get('invalid_source'):
            invalid+=1
            continue
        if not isinstance(record.get('solved'),bool):
            continue
        opponent_rating=float(dataset[idx]['rating'])
        e=1.0/(1.0+10**((opponent_rating-starting_rating)/400.0))
        expected_sum+=e
        information+=q*q*e*(1.0-e)
        if record['solved']: wins+=1
        else: losses+=1
        if most_recent is None or record.get('completed_at','')>most_recent.get('completed_at',''):
            most_recent=record
    precision=1/(starting_rd**2)+information
    rating={
        'method':'Glicko-1 single period, exact per-puzzle scored records',
        'initial_rating':starting_rating,'initial_rd':starting_rd,
        'rating':starting_rating+q*(wins-expected_sum)/precision,
        'rd':math.sqrt(1/precision),'rated':wins+losses,
        'solved':wins,'failed':losses,'invalid_source':invalid,
        'resolved':len(resolved),'total':count,'pending':len(unscored),
        'expected':expected_sum,'information':information,
        'calculated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'dataset_sha256':DATASET_SHA256,'source_runs':source_stats,
        'latest_record':most_recent, 'no_technical_losses':True,
        'is_complete':len(unscored)==0,
    }
    if rating['rated']+invalid!=rating['resolved']:
        raise RuntimeError('Exact rating records do not reconcile with checkpoint count')
    (args.out/'rating.json').write_text(json.dumps(rating,indent=2,ensure_ascii=False),encoding='utf8')
    if args.publish_rating:
        sha=os.environ['GITHUB_SHA']
        payload={'name':'Cassandre live rating','head_sha':sha,'status':'completed',
                 'conclusion':'success','output':{'title':f"Cassandre {rating['rating']:.2f} ± RD {rating['rd']:.2f}",
                            'summary':json.dumps(rating,separators=(',',':'),ensure_ascii=False)}}
        body=json.dumps(payload).encode('utf8')
        request=urllib.request.Request(API+'/check-runs',data=body,method='POST',headers={
            'Authorization':'Bearer '+TOKEN,'Accept':'application/vnd.github+json',
            'X-GitHub-Api-Version':'2022-11-28','User-Agent':'Cassandre-recovery',
            'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=65) as response:
            entry=json.load(response)
        print('PUBLISHED_GLICKO_CHECK',entry.get('id'),rating['rating'],rating['rd'],rating['rated'],flush=True)
    print('EXACT_GLICKO',rating['rating'],rating['rd'],'RATED',rating['rated'],'PENDING',rating['pending'],flush=True)


    (args.out/'coverage.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    output(os.environ.get('GITHUB_OUTPUT'),pending_shards,not args.audit_partial)
    print('COVERAGE',json.dumps({k:v for k,v in report.items() if k not in ('pending_shards','missing_indices_first_100')},separators=(',',':')),flush=True)
    print('UNSCORED_SHARDS',len(pending_shards),flush=True)

def output(path,shards,ready):
    if path:
        with open(path,'a',encoding='utf8') as file:
            file.write('shards='+json.dumps(shards,separators=(',',':'))+'\n')
            file.write('has_pending='+('true' if shards and ready else 'false')+'\n')

if __name__=='__main__':main()
