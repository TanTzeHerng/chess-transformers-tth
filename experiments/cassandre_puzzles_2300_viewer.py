#!/usr/bin/env python3
"""Local read-only live dashboard for remote GitHub-hosted Cassandre puzzle solvers."""
import datetime as dt
import json
import subprocess
import threading
import tkinter as tk
from tkinter import ttk
import time

REPO='TanTzeHerng/chess-transformers-tth'
WORKFLOW='cassandre-2300-20261021.yml'
PIECES={'p':'♟','r':'♜','n':'♞','b':'♝','q':'♛','k':'♚',
        'P':'♙','R':'♖','N':'♘','B':'♗','Q':'♕','K':'♔'}
TOTAL=33460

def api(path):
    proc=subprocess.run(['gh','api','--method','GET',path],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',
        errors='replace',timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip()[:300])
    return json.loads(proc.stdout)

def fetch():
    runs=api(f'repos/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=1').get('workflow_runs',[])
    if not runs:return {'run_status':'not started','snapshots':[]}
    run=runs[0]
    sha=run['head_sha']
    out=[]
    for page in range(1,8):
        data=api(f'repos/{REPO}/commits/{sha}/check-runs?per_page=100&page={page}')
        items=data.get('check_runs',[])
        for record in items:
            if not record.get('name','').startswith('Cassandre shard '):continue
            summary=(record.get('output') or {}).get('summary')
            try:snapshot=json.loads(summary)
            except (TypeError,ValueError):continue
            if isinstance(snapshot,dict) and 'assigned' in snapshot:
                out.append(snapshot)
        if len(items)<100:break
    return {'run_id':run['id'],'run_url':run['html_url'],
      'run_status':run['status'],'run_conclusion':run.get('conclusion'),
      'snapshots':out,'updated':dt.datetime.now().strftime('%H:%M:%S')}

class Viewer:
    def __init__(self):
        self.root=tk.Tk()
        self.root.title('Cassandre | 2300+ Puzzle Marathon | REMOTE COMPUTE')
        self.root.geometry('930x640')
        self.root.minsize(800,570)
        self.root.configure(bg='#101b29')
        self.state={'run_status':'connecting','snapshots':[]}
        self.timer=False
        self.title=tk.Label(self.root,text='Cassandre • Chess.com puzzles rated 2300+',font=('Segoe UI Semibold',19),
                            fg='#eef4fa',bg='#101b29')
        self.title.pack(anchor='w',padx=22,pady=(15,4))
        self.subtitle=tk.Label(self.root,text='All chess calculations run on remote Windows workers. This window reads results only.',
                               font=('Segoe UI',10),fg='#a5b8ce',bg='#101b29')
        self.subtitle.pack(anchor='w',padx=24)
        top=tk.Frame(self.root,bg='#101b29');top.pack(fill='x',padx=22,pady=12)
        self.fields={}
        for label in ('Progress','Solved','Failed','Remote shards'):
            box=tk.Frame(top,bg='#1b2e42',padx=15,pady=8);box.pack(side='left',expand=True,fill='x',padx=4)
            tk.Label(box,text=label,font=('Segoe UI',9),fg='#a2b9d0',bg='#1b2e42').pack(anchor='w')
            value=tk.Label(box,text='...',font=('Segoe UI Semibold',19),fg='#f5f8fd',bg='#1b2e42')
            value.pack(anchor='w')
            self.fields[label]=value
        self.bar=ttk.Progressbar(self.root,orient='horizontal',length=850,mode='determinate',
                                 maximum=TOTAL)
        self.bar.pack(fill='x',padx=29,pady=(2,12))
        main=tk.Frame(self.root,bg='#101b29')
        main.pack(fill='both',expand=True,padx=24,pady=(2,10))
        self.board=tk.Canvas(main,width=408,height=408,bg='#101b29',highlightthickness=0)
        self.board.pack(side='left',padx=(3,22),anchor='n')
        right=tk.Frame(main,bg='#101b29');right.pack(side='left',fill='both',expand=True)
        self.puzzle=tk.Label(right,text='Waiting for the first puzzle',font=('Segoe UI Semibold',16),
                             fg='#f1f6fc',bg='#101b29',wraplength=420,justify='left')
        self.puzzle.pack(anchor='w',pady=(7,13))
        self.details=tk.Label(right,text='',font=('Consolas',11),fg='#bad0e6',bg='#101b29',
                              wraplength=420,justify='left')
        self.details.pack(anchor='w')
        self.recent=tk.Text(right,height=11,width=40,font=('Consolas',10),bg='#18293c',fg='#d7e6f3',
                            bd=0,wrap='word',state='disabled',padx=9,pady=8)
        self.recent.pack(fill='both',expand=True,pady=(15,0))
        self.status=tk.Label(self.root,text='Connecting to GitHub Actions...',font=('Segoe UI',10),
                             fg='#a2b9d0',bg='#101b29',anchor='w')
        self.status.pack(fill='x',padx=24,pady=(0,12))
        self.drawboard('8/8/8/8/8/8/8/8')
        self.root.after(1000,self.tick)
    def drawboard(self,fen):
        self.board.delete('all')
        sq=48; ox=10;oy=10
        rows=fen.split()[0].split('/')
        for r in range(8):
            x=0
            for char in rows[r]:
                if char.isdigit():
                    x+=int(char);continue
                x+=1
            for c in range(8):
                left=ox+c*sq;top=oy+r*sq
                self.board.create_rectangle(left,top,left+sq,top+sq,fill=('#e1e4d8' if (r+c)%2==0 else '#7893a2'),outline='')
            col=0
            for piece in rows[r]:
                if piece.isdigit():col+=int(piece);continue
                left=ox+col*sq;top=oy+r*sq
                color='#ffffff' if piece.isupper() else '#151b25'
                self.board.create_text(left+sq/2,top+sq/2,text=PIECES.get(piece,'?'),
                    font=('Segoe UI Symbol',29),fill=color)
                col+=1
        for i,ltr in enumerate('abcdefgh'):
            self.board.create_text(ox+i*sq+sq/2,oy+8*sq+10,text=ltr,fill='#a2b9d0',font=('Segoe UI',9))
        for i in range(8):
            self.board.create_text(ox-5,oy+i*sq+sq/2,text=str(8-i),fill='#a2b9d0',font=('Segoe UI',9),anchor='e')
    def apply(self,dat):
        snaps=dat.get('snapshots',[])
        done=sum(s.get('completed',0) for s in snaps)
        solved=sum(s.get('solved',0) for s in snaps)
        errors=sum(s.get('failed',0) for s in snaps)
        finished=sum(s.get('state')=='completed' for s in snaps)
        self.fields['Progress']['text']=f'{done:,} / {TOTAL:,}'
        self.fields['Solved']['text']=f'{solved:,}'
        self.fields['Failed']['text']=f'{errors:,}'
        self.fields['Remote shards']['text']=f'{finished} / 248'
        self.bar['value']=done
        if snaps:
            last=max((s for s in snaps if s.get('last')),key=lambda s:s.get('updated_at',''),default=None)
            if last:
                rec=last['last']
                self.puzzle['text']=f"Last puzzle #{rec['id']}  •  rated {rec['puzzle_rating']}"
                self.drawboard(rec['moves'][-1]['fen'] if rec['moves'] else last.get('current_fen','8/8/8/8/8/8/8/8'))
                moves=' '.join(e['chosen'] for e in rec['moves'])
                self.details['text']=(f"Result: {'SOLVED' if rec['solved'] else 'FAILED'}\n"
                  f"Engine's chosen moves: {moves}\n"
                  f"Puzzle index: {rec['index']+1:,}\n"
                  f"Last remote update: {last.get('updated_at','?')}")
            snaps=sorted(snaps,key=lambda s:s.get('updated_at',''),reverse=True)
            lines=[]
            for s in snaps[:10]:
                lastrec=s.get('last') or {}
                lines.append(f"{s['start_index']:>5} : {s['completed']:>3}/{s['assigned']:<3} "
                             f"{s['solved']:>3} solved, {s['failed']:>3} failed  "
                             f"last={lastrec.get('id','-')}")
            self.recent['state']='normal'
            self.recent.delete('1.0','end')
            self.recent.insert('end','\n'.join(lines))
            self.recent['state']='disabled'
        self.status['text']=(f"Workflow: {dat.get('run_status')} "
           f"• Remote status panels: {len(snaps)} "
           f"• Last checked {dat.get('updated','?')} "
           f"• Refreshes every 30 seconds")
    def tick(self):
        if not self.timer:
            self.timer=True
            def work():
                try:dat=fetch()
                except Exception as e:dat={'error':str(e)}
                def apply():
                    self.timer=False
                    if 'error' in dat:self.status['text']='GitHub status unavailable: '+dat['error']
                    else:self.apply(dat)
                self.root.after(0,apply)
            threading.Thread(target=work,daemon=True).start()
        self.root.after(30000,self.tick)
    def run(self):self.root.mainloop()

if __name__=='__main__':Viewer().run()
