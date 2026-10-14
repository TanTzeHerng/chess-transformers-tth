#!/usr/bin/env python3
"""Upload each completed game and current telemetry via VM's dedicated GCS service account."""
import argparse
import json
import mimetypes
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

METADATA="http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"

def access_token():
    req=urllib.request.Request(METADATA,headers={"Metadata-Flavor":"Google"})
    with urllib.request.urlopen(req,timeout=15) as r:
        data=json.load(r)
    return data["access_token"]

def upload_file(bucket, base, path, token):
    dest="multipv256_100/" + path.relative_to(base).as_posix()
    params=urllib.parse.urlencode({"uploadType":"media","name":dest})
    url="https://storage.googleapis.com/upload/storage/v1/b/"+urllib.parse.quote(bucket,safe="")+"/o?"+params
    data=path.read_bytes()
    req=urllib.request.Request(
        url,data=data,method="POST",
        headers={"Authorization":"Bearer "+token,
                 "Content-Type":mimetypes.guess_type(path.name)[0] or "application/octet-stream"})
    with urllib.request.urlopen(req,timeout=45) as r:
        return r.status,len(data)

def sync(bucket,base,seen):
    try:
        token=access_token()
    except Exception as exc:
        print("SYNC_AUTH_FAILED",repr(exc),flush=True)
        return
    for f in sorted(base.glob("*")):
        if not f.is_file() or f.name.endswith(".tmp"):
            continue
        st=f.stat()
        # Force-upload files with identical size but recently updated mtime.
        fingerprint=(st.st_size,st.st_mtime_ns)
        if seen.get(f.name)==fingerprint:
            continue
        try:
            status,bytes_count=upload_file(bucket,base,f,token)
            seen[f.name]=fingerprint
            print("GCS_UPLOADED",f.name,"bytes",bytes_count,"status",status,flush=True)
        except Exception as exc:
            print("GCS_UPLOAD_FAILED",f.name,repr(exc),flush=True)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--bucket",required=True)
    parser.add_argument("--folder",required=True)
    parser.add_argument("--interval",type=int,default=60)
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args()
    base=Path(args.folder)
    seen={}
    while True:
        base.mkdir(parents=True,exist_ok=True)
        sync(args.bucket,base,seen)
        if args.once: break
        time.sleep(args.interval)

if __name__=="__main__":
    main()
