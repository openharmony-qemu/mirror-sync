#!/usr/bin/env python3
"""Repository-scoped Git credentials with renewable GitHub App tokens.

Secrets are returned only through Git's credential protocol, never logged or
placed in remote URLs. The private key is used only for a short-lived JWT.
"""
import base64
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def encoded(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def jwt(app_id, private_key):
    if not app_id.isdigit():raise ValueError('Numeric GitHub App ID required')
    now=int(time.time())
    body='.'.join(encoded(json.dumps(part,separators=(',',':')).encode()) for part in
        ({'alg':'RS256','typ':'JWT'},{'iat':now-60,'exp':now+540,'iss':app_id}))
    with tempfile.NamedTemporaryFile(mode='w',prefix='mirror-app-key-') as key:
        os.fchmod(key.fileno(),0o600);key.write(private_key);key.flush()
        signed=subprocess.run(['openssl','dgst','-sha256','-sign',key.name],input=body.encode(),capture_output=True)
        if signed.returncode:raise RuntimeError('Unable to sign GitHub App authentication JWT')
    return body+'.'+encoded(signed.stdout)


def api(path, token, data=None, method=None):
    request=urllib.request.Request('https://api.github.com'+path,
        data=None if data is None else json.dumps(data).encode(),method=method,
        headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
                 'Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'})
    try:
        with urllib.request.urlopen(request,timeout=60) as response:
            raw=response.read();return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        # Do not expose request headers or response bodies from authentication.
        raise RuntimeError(f'GitHub authentication API returned HTTP {error.code}') from None


def cache_path(repository):
    if not re.fullmatch(r'openharmony-qemu/[A-Za-z0-9_.-]+',repository):
        raise ValueError('Expected a QEMU organization repository')
    return Path(os.environ['RUNNER_TEMP'])/'mirror-credentials'/(repository.split('/')[1]+'.json')


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(str(path)+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield


def token(repository):
    app_id=os.environ.get('MIRROR_APP_ID','')
    private_key=os.environ.get('MIRROR_APP_PRIVATE_KEY','')
    if not (app_id and private_key):
        pat=os.environ.get('MIRROR_TOKEN','')
        if not pat:raise ValueError('Mirror App credentials or MIRROR_TOKEN required')
        return pat
    path=cache_path(repository)
    with locked(path):
        if path.exists():
            cached=json.loads(path.read_text())
            if cached.get('repository')==repository and cached['expires_at']>time.time()+300:
                return cached['token']
        signed=jwt(app_id,private_key)
        installation=api('/repos/'+repository+'/installation',signed)
        issued=api('/app/installations/'+str(installation['id'])+'/access_tokens',signed,
            {'repositories':[repository.split('/')[1]],'permissions':{'contents':'write','workflows':'write'}})
        expires=datetime.datetime.fromisoformat(issued['expires_at'].replace('Z','+00:00')).timestamp()
        cached=dict(repository=repository,token=issued['token'],expires_at=expires)
        with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as stream:
            temporary=Path(stream.name)
            try:
                os.fchmod(stream.fileno(),0o600);json.dump(cached,stream);stream.flush()
                os.replace(temporary,path)
            finally:temporary.unlink(missing_ok=True)
        return cached['token']


def allowed(request,repository):
    return (request.get('protocol')=='https' and request.get('host')=='github.com'
            and request.get('path','').rstrip('/') in (repository,repository+'.git',
                                                        repository+'.git/info/lfs'))


def revoke(repository):
    path=cache_path(repository)
    with locked(path):
        if not path.exists():return
        cached=json.loads(path.read_text())
        try:
            if cached['expires_at']>time.time():
                api('/installation/token',cached['token'],method='DELETE')
        finally:path.unlink(missing_ok=True)


def main():
    repository='openharmony-qemu/'+os.environ['MIRROR_REPOSITORY']
    operation=sys.argv[1]
    if operation=='revoke':revoke(repository);return
    request=dict(line.rstrip('\n').split('=',1) for line in sys.stdin if '=' in line)
    if not allowed(request,repository):return
    if operation=='get':
        sys.stdout.write('username=x-access-token\npassword='+token(repository)+'\n\n')
    elif operation=='erase':
        path=cache_path(repository)
        with locked(path):
            if path.exists() and json.loads(path.read_text())['token']==request.get('password'):
                path.unlink()


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Mirror credential helper: '+str(error),file=sys.stderr)
        raise SystemExit(1)
