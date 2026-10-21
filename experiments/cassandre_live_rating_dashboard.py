#!/usr/bin/env python3
"""Cassandre verified Glicko-1 live dashboard, using exact indexed scores from GitHub.
The old snapshot-prefix estimate is intentionally not used for recovery runs.
"""
import datetime as dt
import json
from pathlib import Path
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import live_viewer as original

BASE=Path(__file__).resolve().parent
CACHE=BASE/'rating_dashboard_cache.json'
REPO='TanTzeHerng/chess-transformers-tth'
RUN_URL='https://github.com/'+REPO+'/actions/workflows/cassandre-live-rating.yml'
_TOKEN=None

def token():
    global _TOKEN
    if _TOKEN is not None:return _TOKEN
    try:
        p=subprocess.run(['gh','auth','token'],capture_output=True,text=True,
                         timeout=12,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        _TOKEN=p.stdout.strip() if p.returncode==0 else ''
    except Exception:_TOKEN=''
    return _TOKEN

def request_latest_rating():
    name=urllib.parse.quote('Cassandre live rating')
    url=f'https://api.github.com/repos/{REPO}/commits/main/check-runs?check_name={name}&per_page=15'
    headers={'Accept':'application/vnd.github+json',
             'X-GitHub-Api-Version':'2022-11-28','User-Agent':'Cassandre-Rating-Dashboard'}
    auth=token()
    if auth:headers['Authorization']='Bearer '+auth
    req=urllib.request.Request(url,headers=headers)
    with urllib.request.urlopen(req,timeout=18) as response:
        data=json.load(response)
    checks=[x for x in data.get('check_runs',[]) if x.get('name')=='Cassandre live rating'
            and x.get('status')=='completed' and x.get('conclusion')=='success']
    checks.sort(key=lambda x:x.get('completed_at') or '',reverse=True)
    for check in checks:
        summary=(check.get('output') or {}).get('summary','')
        try:r=json.loads(summary)
        except (TypeError,ValueError):continue
        if r.get('dataset_sha256')!='abef248c372e48b7904368f82921d6491415b91c928d35d164d4e3534c90833b':
            continue
        if r.get('rated',-1)+r.get('invalid_source',0)!=r.get('resolved',-2):
            continue
        r['_check_id']=check.get('id')
        return r
    raise RuntimeError('No verified consolidated rating check available yet')

def fetch():
    try:
        rating=request_latest_rating()
        tmp=CACHE.with_suffix('.tmp')
        tmp.write_text(json.dumps(rating,ensure_ascii=False,indent=2),encoding='utf8')
        tmp.replace(CACHE)
        rating['_cache_only']=False
    except Exception as error:
        if not CACHE.exists():raise
        rating=json.loads(CACHE.read_text(encoding='utf8'))
        rating['_cache_only']=True
        rating['_fetch_error']=str(error)[:120]
    return {'rating_summary':rating,'run_status':'scored-artifact rating','updated':dt.datetime.now().strftime('%H:%M:%S')}

class Viewer(original.Viewer):
    def __init__(self):
        super().__init__()
        self.root.title('Cassandre | Verified live Glicko rating | Original + Recovery')
        self.rating_number['text']='Loading...'
        self.rd_number['text']='Waiting for verified GitHub rating'
        self.subtitle['text']='Exact scored puzzles across original and recovery runs, never counts technical failures as losses.'
        self.rating_note['text']='Glicko-1 single period | start 2500, RD 350 | recalculated from individual results every 30 min.'
        self.fields['Remote shards']['text']='Recovery'
    def apply(self,dat):
        r=dat.get('rating_summary')
        if not r:return
        self.rating_number['text']=f"{r['rating']:,.2f}"
        self.rd_number['text']=f"RD {r['rd']:,.2f} | {r['rated']:,} rated"
        self.fields['Progress']['text']=f"{r['resolved']:,} / {r['total']:,}"
        self.fields['Solved']['text']=f"{r['solved']:,}"
        self.fields['Failed']['text']=f"{r['failed']:,}"
        self.fields['Remote shards']['text']=f"{r['pending']:,} unscored"
        self.bar['value']=r['resolved']
        latest=r.get('latest_record')
        if latest:
            self.puzzle['text']=f"Latest scored puzzle #{latest['id']} • rated {latest['puzzle_rating']}"
            moves=latest.get('moves') or []
            if moves:
                try:self.drawboard(moves[-1]['fen'])
                except Exception:pass
            summary=' '.join(f"{m.get('chosen','?')} ({'✓' if m.get('correct') else '×'})" for m in moves)
            self.details['text']=(f"Result: {'SOLVED' if latest.get('solved') else 'FAILED'}\n"
                                  f"Moves: {summary}\n"
                                  f"Puzzle index: {latest.get('index',0)+1:,}\n"
                                  f"Scored at: {latest.get('completed_at','?')}")
        self.recent['state']='normal'
        self.recent.delete('1.0','end')
        self.recent.insert('end',f"Exact Glicko-1 rating: {r['rating']:.2f}\n"
                                   f"Rating deviation: {r['rd']:.2f}\n"
                                   f"Completed, graded puzzles: {r['rated']:,}\n"
                                   f"Not scored, including technical retries: {r['pending']:,}\n"
                                   f"Original + recovery sources: {len(r.get('source_runs',{}))}\n"
                                   f"Last computed (UTC): {r.get('calculated_at','?')}\n"
                                   f"Formula: {r['method']}\n"
                                   f"GitHub workflow: {RUN_URL}")
        self.recent['state']='disabled'
        cached=' | showing saved verified rating' if r.get('_cache_only') else ''
        self.status['text']=(f"Rating last recalculated: {r.get('calculated_at','?')} UTC"
                              f"{cached} | Dashboard checks every 30 seconds")
        if r.get('_fetch_error'):
            self.status['text']+=' | API: '+r['_fetch_error'][:65]

original.fetch=fetch
if __name__=='__main__':
    Viewer().run()
