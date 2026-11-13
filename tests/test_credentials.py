import datetime
import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('credentials',Path(__file__).parents[1]/'scripts/github_credentials.py')
credentials=importlib.util.module_from_spec(spec);spec.loader.exec_module(credentials)


class CredentialsTest(unittest.TestCase):
    def test_jwt_signature_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            key=subprocess.check_output(['openssl','genpkey','-algorithm','RSA',
                '-pkeyopt','rsa_keygen_bits:2048'],stderr=subprocess.DEVNULL)
            public=subprocess.check_output(['openssl','pkey','-pubout'],input=key,stderr=subprocess.DEVNULL)
            with patch.object(credentials.time,'time',return_value=1000):
                signed=credentials.jwt('123',key.decode())
            header,payload,signature=signed.split('.')
            decode=lambda s:base64.urlsafe_b64decode(s+'='*((-len(s))%4))
            self.assertEqual(json.loads(decode(header))['alg'],'RS256')
            self.assertEqual(json.loads(decode(payload)),{'iat':940,'exp':1540,'iss':'123'})
            (root/'public.pem').write_bytes(public);(root/'signature').write_bytes(decode(signature))
            result=subprocess.run(['openssl','dgst','-sha256','-verify',str(root/'public.pem'),
                '-signature',str(root/'signature')],input=(header+'.'+payload).encode(),capture_output=True)
            self.assertEqual(result.returncode,0)

    def test_git_credential_protocol(self):
        helper=Path(__file__).parents[1]/'scripts/github_credentials.py'
        environment=dict(os.environ,MIRROR_REPOSITORY='target',MIRROR_TOKEN='test-only-pat',
                         MIRROR_APP_ID='',MIRROR_APP_PRIVATE_KEY='')
        for host,expected in [('github.com','username=x-access-token\npassword=test-only-pat\n\n'),
                              ('gitcode.com','')]:
            result=subprocess.run([sys.executable,str(helper),'get'],text=True,capture_output=True,
                env=environment,input='protocol=https\nhost='+host+'\npath=openharmony-qemu/target.git\n\n')
            self.assertEqual(result.returncode,0);self.assertEqual(result.stdout,expected)

    def test_expiring_token_is_renewed_with_single_repository_permissions(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{
            'RUNNER_TEMP':root,'MIRROR_APP_ID':'123','MIRROR_APP_PRIVATE_KEY':'test-only-key'},clear=True), \
             patch.object(credentials,'jwt',return_value='test-only-jwt'), \
             patch.object(credentials.time,'time',return_value=1000), \
             patch.object(credentials,'api') as api:
            api.side_effect=[{'id':12},{'token':'test-only-first','expires_at':'1970-01-01T01:16:40Z'},
                             {'id':12},{'token':'test-only-renewed','expires_at':'1970-01-01T02:13:20Z'}]
            self.assertEqual(credentials.token('openharmony-qemu/target'),'test-only-first')
            self.assertEqual(credentials.token('openharmony-qemu/target'),'test-only-first')
            self.assertEqual(api.call_count,2)
            self.assertEqual(api.call_args.args[2],{'repositories':['target'],
                'permissions':{'contents':'write','workflows':'write'}})
            path=credentials.cache_path('openharmony-qemu/target')
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            with patch.object(credentials.time,'time',return_value=4400):
                self.assertEqual(credentials.token('openharmony-qemu/target'),'test-only-renewed')
            self.assertEqual(api.call_count,4)

    def test_credentials_are_not_given_to_other_repositories_or_hosts(self):
        repo='openharmony-qemu/target'
        request=dict(protocol='https',host='github.com',path=repo+'.git')
        self.assertTrue(credentials.allowed(request,repo))
        self.assertTrue(credentials.allowed(dict(request,path=repo+'.git/info/lfs'),repo))
        for change in [dict(host='gitcode.com'),dict(protocol='http'),
                       dict(path='other/target.git'),dict(path=repo+'-different.git')]:
            self.assertFalse(credentials.allowed(dict(request,**change),repo))

    def test_pat_fallback_is_never_saved_or_revoked(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,
                {'RUNNER_TEMP':root,'MIRROR_TOKEN':'test-only-pat'},clear=True), \
                patch.object(credentials,'api') as api:
            self.assertEqual(credentials.token('openharmony-qemu/target'),'test-only-pat')
            credentials.revoke('openharmony-qemu/target')
            api.assert_not_called()

    def test_current_app_token_is_revoked_and_removed(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'RUNNER_TEMP':root}), \
                patch.object(credentials.time,'time',return_value=1000),patch.object(credentials,'api') as api:
            path=credentials.cache_path('openharmony-qemu/target');path.parent.mkdir()
            path.write_text(json.dumps(dict(token='test-only-current',expires_at=2000)))
            credentials.revoke('openharmony-qemu/target')
            api.assert_called_once_with('/installation/token','test-only-current',method='DELETE')
            self.assertFalse(path.exists())


if __name__=='__main__':unittest.main()
