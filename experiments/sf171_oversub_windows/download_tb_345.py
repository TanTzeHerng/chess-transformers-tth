"""Download verified complete five-piece Syzygy (145 WDL + 145 DTZ) on cloud only."""
import concurrent.futures,hashlib,html.parser,json,os,pathlib,re,sys,time,urllib.request
BASE="https://tablebase.sesse.net/syzygy/3-4-5/"
DEST=pathlib.Path("syzygy")
DEST.mkdir(exist_ok=True)
class ListParser(html.parser.HTMLParser):
    def __init__(self):super().__init__();self.hrefs=[]
    def handle_starttag(self,tag,attrs):
        if tag=="a":
            for k,v in attrs:
                if k=="href":self.hrefs.append(v)
def request(url,timeout=90):
    q=urllib.request.Request(url,headers={"User-Agent":"chess-selfplay-research/1.0 (single sequential download)"})
    return urllib.request.urlopen(q,timeout=timeout)
links=None
for index in [BASE,"http://tablebase.sesse.net/syzygy/3-4-5/","https://tablebase.lichess.ovh/tables/standard/3-4-5/"]:
    try:
        with request(index) as res:page=res.read().decode("utf-8","replace")
        p=ListParser();p.feed(page)
        files=sorted(set(x.split("?")[0].split("/")[-1] for x in p.hrefs
                         if re.fullmatch(r"[KQRBNPv]+\.rtb[zw]",x.split("?")[0].split("/")[-1],re.I)))
        print("MANIFEST_CANDIDATE",index,len(files),flush=True)
        if len(files)>=290 and sum(x.endswith(".rtbw") for x in files)>=145 and sum(x.endswith(".rtbz") for x in files)>=145:
            links=(index,files);break
    except Exception as e:print("INDEX_FAILED",index,repr(e),flush=True)
if not links:raise SystemExit("Complete 290-file Syzygy 3/4/5 set not discoverable")
origin,files=links
stats={"origin":origin,"count":len(files),"downloaded":0,"bytes":0,"started":time.time()}
def safeget(name):
    path=DEST/name
    if path.exists() and path.stat().st_size>100:return path.stat().st_size
    from urllib.parse import urljoin
    url=urljoin(origin,name)
    for attempt in range(1,5):
        try:
            with request(url,timeout=120) as r, (DEST/(name+".tmp")).open("wb") as f:
                while True:
                    buf=r.read(1024*512)
                    if not buf:break
                    f.write(buf)
            size=(DEST/(name+".tmp")).stat().st_size
            if size<16:raise IOError("Truncated file")
            (DEST/(name+".tmp")).replace(path)
            return size
        except Exception as ex:
            print("RETRY",name,attempt,repr(ex),flush=True)
            time.sleep(min(30,2**attempt))
    raise RuntimeError("Unable to download "+name)
# Respect host bandwidth: one transfer at a time.
for i,name in enumerate(files,1):
    n=safeget(name)
    stats["downloaded"]=i;stats["bytes"]+=n
    if i%15==0 or i==len(files):print("TABLEBASE_PROGRESS",i,len(files),round(stats["bytes"]/2**20,1),"MiB",flush=True)
stats["completed"]=time.time()
stats["wdl_files"]=len(list(DEST.glob("*.rtbw")))
stats["dtz_files"]=len(list(DEST.glob("*.rtbz")))
if stats["wdl_files"]!=145 or stats["dtz_files"]!=145:
    raise RuntimeError("Incomplete five-men tables: "+str(stats))
import chess,chess.syzygy
with chess.syzygy.open_tablebase(str(DEST)) as tb:
    b=chess.Board("8/pp4k1/8/8/8/8/8/3QK3 w - - 0 1")
    value=tb.probe_wdl(b)
    stats["probe_wdl"]=value
print("TABLEBASE_READY",json.dumps(stats),flush=True)
pathlib.Path("tablebase_manifest.json").write_text(json.dumps(stats,indent=2))
