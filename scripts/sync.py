#!/usr/bin/env python3
"""Synchronize all official branches without force pushes or destination deletion."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

CONTROL = 'openharmony-qemu/mirror-sync'


def git(root, *args, check=True):
    result = subprocess.run(['git', '-C', str(root), *args], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError(result.stderr[-4000:])
    return result


def refs(root, remote):
    return {ref: sha for sha, ref in
            (line.split() for line in git(root, 'ls-remote', '--heads', remote).stdout.splitlines())}


def protected(ref):
    return bool(re.fullmatch(r'refs/heads/OpenHarmony-.+-Release-qemu', ref))


class Issues:
    def __init__(self, repository=CONTROL):
        self.repository = repository
        self.existing = None

    def api(self, path, data=None, method=None):
        request = urllib.request.Request('https://api.github.com/repos/' + self.repository + path,
            data=None if data is None else json.dumps(data).encode(), method=method,
            headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                     'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                     'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def report(self, repository, ref, old, new, reason):
        marker = '<!-- mirror-conflict:' + hashlib.sha256((repository + '\n' + ref).encode()).hexdigest() + ' -->'
        if self.existing is None:
            self.existing = []
            page = 1
            while True:
                batch = self.api(f'/issues?state=open&per_page=100&page={page}')
                self.existing.extend(x for x in batch if 'pull_request' not in x)
                if len(batch) < 100:
                    break
                page += 1
        run = os.environ.get('GITHUB_RUN_ID')
        body = (f'{marker}\nRepository: https://github.com/{repository}\n\n'
                f'Ref: `{ref}`\n\nDestination: `{old or "missing"}`\n\n'
                f'Incoming: `{new}`\n\nReason: {reason}\n\n'
                'The destination ref was preserved. Review both histories and resolve manually; '
                'the daily job will retry without force pushing.\n')
        if run:
            body += f'\n[Workflow run](https://github.com/{CONTROL}/actions/runs/{run})\n'
        for issue in self.existing:
            if marker in (issue.get('body') or ''):
                if issue['body'] != body:
                    self.api('/issues/' + str(issue['number']), {'body': body}, 'PATCH')
                return issue['number']
        issue = self.api('/issues', {'title': f'Mirror conflict: {repository.split("/")[-1]} {ref[11:]}',
                                     'body': body}, 'POST')
        self.existing.append(issue)
        return issue['number']


def publish(root, destination, incoming, reporter, repository):
    """Test ancestry before each normal push; a racing writer cannot be overwritten."""
    current = refs(root, destination)
    results = []
    for ref, new in sorted(incoming.items()):
        old = current.get(ref)
        item = dict(ref=ref, old=old, new=new)
        reason = None
        blockers = [existing for existing in current
                    if existing.startswith(ref + '/') or ref.startswith(existing + '/')]
        if protected(ref):
            reason = 'Upstream uses a reserved QEMU development branch name.'
        elif blockers:
            reason = 'Branch namespace conflicts with destination refs: ' + ', '.join(sorted(blockers))
        elif old == new:
            item['status'] = 'unchanged'
        elif old:
            present = git(root, 'cat-file', '-e', old + '^{commit}', check=False)
            if present.returncode:
                git(root, 'fetch', '--no-tags', destination, ref)
            ancestor = git(root, 'merge-base', '--is-ancestor', old, new, check=False)
            if ancestor.returncode not in (0, 1):
                raise RuntimeError(ancestor.stderr)
            if ancestor.returncode == 1:
                reason = 'Non-fast-forward branch update (upstream rewrite or local divergence).'
        if not reason and 'status' not in item:
            pushed = git(root, 'push', '--porcelain', destination, new + ':' + ref, check=False)
            if pushed.returncode:
                # Only a ref race is a branch conflict. Authentication, quota and
                # other transport errors fail the job with their actual diagnostics.
                after = refs(root, destination)
                observed = after.get(ref)
                blocked = any(existing.startswith(ref + '/') or ref.startswith(existing + '/')
                              for existing in after)
                if observed != old or blocked:
                    reason = 'Destination changed during synchronization; retry after review.'
                    item['old'] = observed
                else:
                    raise RuntimeError(pushed.stderr[-4000:] + pushed.stdout[-1000:])
            else:
                item['status'] = 'created' if old is None else 'updated'
                current[ref] = new
        if reason:
            item['status'] = 'conflict'
            item['issue'] = reporter.report(repository, ref, item['old'], new, reason)
        results.append(item)
    return results


def prepare(entry, root, report_dir, destination=None):
    report_dir = report_dir.resolve()
    upstream = entry['upstream_url']
    destination = destination or 'https://github.com/openharmony-qemu/' + entry['name'] + '.git'
    git(root, 'init', '--bare')
    git(root, 'remote', 'add', 'upstream', upstream)
    git(root, 'remote', 'add', 'destination', destination)
    git(root, 'fetch', '--no-tags', 'upstream', '+refs/heads/*:refs/heads/*')
    original = refs(root, 'upstream')
    # Fail rather than publishing a partial snapshot if upstream moved during fetch.
    fetched = dict(line.split(' ', 1)[::-1] for line in
                   git(root, 'for-each-ref', '--format=%(objectname) %(refname)', 'refs/heads').stdout.splitlines())
    if original != fetched:
        raise RuntimeError('Upstream branch inventory changed while fetching; retry the job.')
    baseline_refs = {}
    for index, revision in enumerate(entry.get('baseline_revisions', [])):
        if git(root, 'cat-file', '-e', revision + '^{commit}', check=False).returncode:
            git(root, 'fetch', '--no-tags', 'upstream', revision)
        ref = 'refs/heads/upstream/baseline-7.0' + (f'-{index + 1}' if index else '')
        if ref in original:
            raise ValueError('Official branch collides with the reserved baseline ref: ' + ref)
        git(root, 'update-ref', ref, revision)
        baseline_refs[ref] = revision
    report_dir.mkdir(parents=True, exist_ok=True)
    if entry.get('large_files') == 'git-lfs':
        mapping = report_dir / 'commit-map.csv'
        mapping.unlink(missing_ok=True)
        git(root, 'lfs', 'migrate', 'import', '--everything', '--skip-fetch',
            '--above=99MB', '--yes', '--object-map=' + str(mapping))
        git(root, 'lfs', 'fetch', '--all', 'upstream')
    hosted = {ref: git(root, 'rev-parse', ref).stdout.strip() for ref in original | baseline_refs}
    report = dict(repository='openharmony-qemu/' + entry['name'], upstream_url=upstream,
                  original_refs=original, baseline_original_refs=baseline_refs, hosted_refs=hosted)
    (report_dir / 'provenance.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def finish(entry, root, report_dir, reporter):
    report = json.loads((report_dir / 'provenance.json').read_text())
    if entry.get('large_files') == 'git-lfs':
        git(root, 'lfs', 'push', '--all', 'destination')
    results = publish(root, 'destination', report['hosted_refs'], reporter, report['repository'])
    report['results'] = results
    (report_dir / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    return results


def synchronize(entry, root, report_dir, reporter, destination=None):
    prepare(entry, root, report_dir, destination)
    return finish(entry, root, report_dir, reporter)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--registry', type=Path, default=Path('repositories.json'))
    parser.add_argument('--reports', type=Path, default=Path('reports'))
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--phase', choices=('prepare', 'publish'), required=True)
    args = parser.parse_args()
    registry = json.loads(args.registry.read_text())['repositories']
    entry = next(x for x in registry if x['name'] == args.repository and x['enabled'])
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', entry['name']):
        raise ValueError('Invalid repository name')
    if not entry['upstream_url'].startswith(('https://gitcode.com/', 'https://github.com/', 'https://gitlab.com/')):
        raise ValueError('Unsupported upstream host')
    if any(not re.fullmatch('[0-9a-f]{40}', r) for r in entry.get('baseline_revisions', [])):
        raise ValueError('Baseline must be a full commit SHA')
    os.environ.update(GIT_TERMINAL_PROMPT='0', GIT_LFS_SKIP_SMUDGE='1')
    repo = args.work / (entry['name'] + '.git')
    report_dir = args.reports / entry['name']
    if args.phase == 'prepare':
        repo.mkdir(parents=True, exist_ok=False)
        prepare(entry, repo, report_dir)
        return
    if not os.environ.get('MIRROR_TOKEN'):
        raise ValueError('Configure MIRROR_APP_ID/MIRROR_APP_PRIVATE_KEY or MIRROR_TOKEN in mirror-sync.')
    git(repo, 'config', 'credential.helper', '')
    git(repo, 'config', 'credential.https://github.com.helper',
        '!f() { echo username=x-access-token; echo "password=$MIRROR_TOKEN"; }; f')
    results = finish(entry, repo, report_dir, Issues())
    print(json.dumps({status: sum(r['status'] == status for r in results)
                      for status in ('created', 'updated', 'unchanged', 'conflict')}))
    if any(r['status'] == 'conflict' for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
