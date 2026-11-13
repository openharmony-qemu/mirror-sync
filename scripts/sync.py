#!/usr/bin/env python3
"""Synchronize all official branches without force pushes or destination deletion."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request

CONTROL = 'openharmony-qemu/mirror-sync'
BOOTSTRAP_COMMITS = 10000
RECOVERY_COMMITS = 500
TRANSFER_BYTES = 128 * 1024 * 1024
TRANSFER_PREFIX = 'refs/tags/mirror-sync-transfer/'


def git(root, *args, check=True, input_text=None, environment=None):
    result = subprocess.run(['git', '-C', str(root), *args], text=True,
                            input=input_text, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError(result.stderr[-4000:])
    return result


def refs(root, remote):
    return {ref: sha for sha, ref in
            (line.split() for line in git(root, 'ls-remote', '--heads', remote).stdout.splitlines())}


def protected(ref):
    return bool(re.fullmatch(r'refs/heads/OpenHarmony-.+-Release-qemu', ref))


def transient_transport(diagnostic):
    return bool(re.search(r'HTTP (500|502|503|504)\b', diagnostic) or
        any(term in diagnostic.lower() for term in
            ('unexpected disconnect', 'remote end hung up', 'connection reset', 'timed out')))


def transfer_refs(root, destination):
    result = {}
    for line in git(root, 'ls-remote', '--tags', destination, TRANSFER_PREFIX + '*').stdout.splitlines():
        sha, ref = line.split()
        suffix = ref[len(TRANSFER_PREFIX):]
        if not re.fullmatch('[0-9a-f]{64}/[0-9a-f]{40}/[0-9a-f]{40}', suffix) or suffix.split('/')[-1] != sha:
            raise RuntimeError('Unexpected ref in the reserved transfer namespace: ' + ref)
        result[ref] = sha
    return result


def load_transfer_objects(root, destination):
    existing = transfer_refs(root, destination)
    names = list(existing)
    for start in range(0, len(names), 64):
        git(root, 'fetch', '--no-tags', '--filter=blob:none', destination, *names[start:start + 64])
    for sha in set(existing.values()):
        if git(root, 'cat-file', '-t', sha).stdout.strip() != 'tree':
            raise RuntimeError('Transfer ref does not point to a content-addressed tree')
    return existing


def transfer_blobs(root, destination, ref, new, lfs_complete):
    """Seed bounded packs of original blobs; source commits and trees stay intact."""
    staging = load_transfer_objects(root, destination)
    tips = sorted(set(refs(root, destination).values()) | set(staging.values()))
    kinds = git(root, 'cat-file', '--batch-check=%(objectname) %(objecttype)',
                input_text='\n'.join(tips) + '\n').stdout.splitlines() if tips else []
    available = [line.split()[0] for line in kinds if line.split()[-1] in ('commit', 'tree')]
    objects = git(root, 'rev-list', '--objects', '--no-object-names', new, '--not', *available).stdout
    details = git(root, 'cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)',
                  input_text=objects).stdout.splitlines()
    blobs = sorted((parts[0], int(parts[2])) for line in details
                   if (parts := line.split())[1] == 'blob')
    environment = dict(os.environ)
    if lfs_complete:
        environment['GIT_LFS_SKIP_PUSH'] = '1'
    prefix = TRANSFER_PREFIX + hashlib.sha256(ref.encode()).hexdigest() + '/' + new + '/'

    def upload(batch):
        tree = git(root, 'mktree', input_text=''.join(
            f'100644 blob {oid}\t{oid}\n' for oid, _ in batch)).stdout.strip()
        tag = prefix + tree
        print(f'{ref}: staging {len(batch)} original blobs ({sum(size for _, size in batch)} bytes)', flush=True)
        for attempt in range(3):
            pushed = git(root, 'push', '--porcelain', '--progress', destination,
                         tree + ':' + tag, check=False, environment=environment)
            if pushed.returncode == 0 or transfer_refs(root, destination).get(tag) == tree:
                return
            if not transient_transport(pushed.stderr + pushed.stdout) or attempt == 2:
                raise RuntimeError(pushed.stderr[-4000:] + pushed.stdout[-1000:])
            time.sleep(3 * (attempt + 1))

    batch, size = [], 0
    for blob in blobs:
        if blob[1] > TRANSFER_BYTES:
            raise RuntimeError('An individual source blob exceeds the bounded transfer size')
        if batch and size + blob[1] > TRANSFER_BYTES:
            upload(batch)
            batch, size = [], 0
        batch.append(blob)
        size += blob[1]
    if batch:
        upload(batch)
    return len(blobs)


def cleanup_transfer_objects(root, destination, results):
    accepted = {hashlib.sha256(row['ref'].encode()).hexdigest(): row['new'] for row in results
                if row['status'] in ('created', 'updated', 'unchanged')}
    removable = {}
    for ref, tree in transfer_refs(root, destination).items():
        key, target, _ = ref[len(TRANSFER_PREFIX):].split('/')
        if key not in accepted:
            continue
        if target == accepted[key] or git(root, 'merge-base', '--is-ancestor', target,
                                         accepted[key], check=False).returncode == 0:
            removable[ref] = tree
    names = list(removable)
    for start in range(0, len(names), 32):
        batch = names[start:start + 32]
        git(root, 'push', '--atomic', '--porcelain',
            *('--force-with-lease=' + ref + ':' + removable[ref] for ref in batch),
            destination, *(':' + ref for ref in batch))
    return transfer_refs(root, destination)


def transfer_parent(root, destination, ref, new):
    observed = refs(root, destination).get(ref)
    if not observed:
        return observed, True
    if git(root, 'cat-file', '-e', observed + '^{commit}', check=False).returncode:
        git(root, 'fetch', '--no-tags', destination, ref)
    ancestor = git(root, 'merge-base', '--is-ancestor', observed, new, check=False)
    if ancestor.returncode not in (0, 1):
        raise RuntimeError(ancestor.stderr)
    return observed, ancestor.returncode == 0


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


def transfer_checkpoints(root, destination, ref, new, reporter, repository, lfs_complete):
    """Recover a failed large transfer with original first-parent ancestors."""
    current = refs(root, destination)
    history = git(root, 'rev-list', '--first-parent', '--reverse', new).stdout.splitlines()
    positions = {sha: index for index, sha in enumerate(history)}
    old = current.get(ref)
    if old and old not in positions:
        return []
    available = max((positions.get(sha, -1) for sha in current.values()), default=-1)
    checkpoints = [sha for sha in history[RECOVERY_COMMITS - 1::RECOVERY_COMMITS]
                   if positions[sha] > available and sha != new]
    print(f'{repository} {ref}: {len(history)} first-parent commits; '
          f'{len(checkpoints)} recovery checkpoints', flush=True)
    results = []
    for checkpoint in checkpoints:
        observed = refs(root, destination).get(ref)
        if observed in positions and positions[observed] >= positions[checkpoint]:
            continue
        print(f'{repository}: recovering transfer through {checkpoint}', flush=True)
        result = publish(root, destination, {ref: checkpoint}, reporter, repository,
                         lfs_complete=lfs_complete, bootstrap=False)
        results.extend(result)
        if any(row['status'] == 'conflict' for row in result):
            break
    return results


def publish(root, destination, incoming, reporter, repository, lfs_complete=False, bootstrap=True,
            bounded_transfer=False):
    """Test ancestry before each normal push; a racing writer cannot be overwritten."""
    baselines = {ref: sha for ref, sha in incoming.items()
                 if ref.startswith('refs/heads/upstream/baseline-')}
    if baselines and len(baselines) < len(incoming):
        # Complete fixed refs before even attempting ordinary atomic batches.
        # A large unrelated branch must not delay baseline availability.
        results = publish(root, destination, baselines, reporter, repository,
                          lfs_complete=lfs_complete, bootstrap=bootstrap,
                          bounded_transfer=bounded_transfer)
        ordinary = {ref: sha for ref, sha in incoming.items() if ref not in baselines}
        return results + publish(root, destination, ordinary, reporter, repository,
                                 lfs_complete=lfs_complete, bootstrap=bootstrap,
                                 bounded_transfer=bounded_transfer)
    current = refs(root, destination)
    environment = dict(os.environ)
    if lfs_complete:
        environment['GIT_LFS_SKIP_PUSH'] = '1'
    ordered = sorted(incoming, key=lambda ref: (not ref.startswith('refs/heads/upstream/baseline-'), ref))
    # Populate an empty destination through real ancestor commits. Every step is
    # a normal fast-forward; no synthetic history or temporary remote refs are
    # needed. A failed initial transfer can then resume from the accepted prefix.
    if bootstrap and ordered and not bounded_transfer:
        first = next((ref for ref in ordered if not protected(ref)), None)
        old = current.get(first)
        if first and old != incoming[first] and (not current or old):
            history = git(root, 'rev-list', '--first-parent', '--reverse', incoming[first]).stdout.splitlines()
            positions = {sha: index for index, sha in enumerate(history)}
            checkpoints = history[BOOTSTRAP_COMMITS - 1::BOOTSTRAP_COMMITS] if not current or old in positions else []
            for checkpoint in checkpoints:
                if checkpoint == incoming[first]:
                    break
                observed = refs(root, destination).get(first)
                if observed in positions and positions[observed] >= positions[checkpoint]:
                    continue
                print(f'{repository}: publishing ancestor checkpoint for {first}: {checkpoint}', flush=True)
                result = publish(root, destination, {first: checkpoint}, reporter, repository,
                                 lfs_complete=lfs_complete, bootstrap=False)
                if any(row['status'] == 'conflict' for row in result):
                    break
            current = refs(root, destination)
    # Many upstream release/weekly branches point to the same commit. Publishing
    # those new names together uploads exactly the same history as one ref and
    # avoids a separate authenticated round trip for every branch name.
    groups = {}
    for ref in ([] if bounded_transfer else ordered):
        new = incoming[ref]
        if (ref not in current and not protected(ref) and not any(
                existing.startswith(ref + '/') or ref.startswith(existing + '/')
                for existing in current)):
            groups.setdefault(new, []).append(ref)
    created = set()
    for new, names in groups.items():
        for start in range(0, len(names), 128):
            group = names[start:start + 128]
            if len(group) < 2:
                continue
            pushed = git(root, 'push', '--atomic', '--porcelain', '--progress', destination,
                         *(new + ':' + ref for ref in group),
                         check=False, environment=environment)
            after = None if pushed.returncode == 0 else refs(root, destination)
            for ref in group:
                if pushed.returncode == 0 or after.get(ref) == new:
                    created.add(ref)
                    current[ref] = new
            # A rejected atomic batch falls through to the existing per-ref
            # checks: preserve conflicting refs and still publish other branches.
    results = []
    for ref in ordered:
        new = incoming[ref]
        old = None if ref in created else current.get(ref)
        item = dict(ref=ref, old=old, new=new)
        reason = None
        blockers = [existing for existing in current
                    if existing.startswith(ref + '/') or ref.startswith(existing + '/')]
        if ref in created:
            item['status'] = 'created'
        elif protected(ref):
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
        if bounded_transfer and not reason and 'status' not in item:
            transfer_blobs(root, destination, ref, new, lfs_complete)
            old, safe = transfer_parent(root, destination, ref, new)
            item['old'] = old
            if not safe:
                reason = 'Non-fast-forward destination change during object transfer.'
        if not reason and 'status' not in item:
            for attempt in range(3):
                pushed = git(root, 'push', '--porcelain', '--progress', destination, new + ':' + ref,
                             check=False, environment=environment)
                if pushed.returncode == 0:
                    break
                # Only a ref race is a branch conflict. Authentication, quota and
                # other transport errors fail the job with their actual diagnostics.
                after = refs(root, destination)
                observed = after.get(ref)
                blocked = any(existing.startswith(ref + '/') or ref.startswith(existing + '/')
                              for existing in after)
                if observed == new:
                    # The server may commit the ref before its response is lost.
                    break
                if observed != old or blocked:
                    reason = 'Destination changed during synchronization; retry after review.'
                    item['old'] = observed
                    break
                diagnostic = pushed.stderr + pushed.stdout
                transient = transient_transport(diagnostic)
                if not transient or attempt == 2:
                    raise RuntimeError(pushed.stderr[-4000:] + pushed.stdout[-1000:])
                if bootstrap and attempt == 0:
                    try:
                        recovered = transfer_checkpoints(root, destination, ref, new, reporter,
                                                         repository, lfs_complete)
                    except RuntimeError as error:
                        if not transient_transport(str(error)):
                            raise
                        recovered = []
                    if recovered:
                        old, safe = transfer_parent(root, destination, ref, new)
                        item['old'] = old
                        if not safe or any(row['status'] == 'conflict' for row in recovered):
                            reason = 'Destination changed during segmented transfer; review the preserved history.'
                            break
                        current[ref] = old
                        continue
                    transfer_blobs(root, destination, ref, new, lfs_complete)
                    old, safe = transfer_parent(root, destination, ref, new)
                    item['old'] = old
                    if not safe:
                        reason = 'Non-fast-forward destination change during object transfer.'
                        break
                elif bootstrap and attempt == 1:
                    transfer_blobs(root, destination, ref, new, lfs_complete)
                    old, safe = transfer_parent(root, destination, ref, new)
                    item['old'] = old
                    if not safe:
                        reason = 'Non-fast-forward destination change during object transfer.'
                        break
                print(f'{repository} {ref}: transient transport failure; retrying', flush=True)
                time.sleep(3 * (attempt + 1))
            if not reason:
                item['status'] = 'created' if old is None else 'updated'
                current[ref] = new
        if reason:
            item['status'] = 'conflict'
            item['issue'] = reporter.report(repository, ref, item['old'], new, reason)
        results.append(item)
        print(f'{repository} {ref}: {item["status"]}', flush=True)
    return results


def cached_lfs_objects(root):
    settings = git(root, 'lfs', 'env').stdout.splitlines()
    locations = [line.split('=', 1)[1] for line in settings if line.startswith('LocalMediaDir=')]
    if len(locations) != 1:
        raise ValueError('Cannot locate the prepared LFS object store')
    return sorted(path.name for path in Path(locations[0]).glob('*/*/*')
                  if path.is_file() and not path.is_symlink()
                  and re.fullmatch('[0-9a-f]{64}', path.name)
                  and path.parent.name == path.name[2:4]
                  and path.parent.parent.name == path.name[:2])


def has_oversized_blobs(root):
    """Read object sizes before considering an expensive historical rewrite."""
    oversized = False
    with tempfile.TemporaryFile(mode='w+t') as errors:
        with subprocess.Popen(['git', '-C', str(root), 'cat-file', '--batch-all-objects',
                '--unordered', '--batch-check=%(objecttype) %(objectsize)'],
                stdout=subprocess.PIPE, stderr=errors, text=True) as process:
            for line in process.stdout:
                kind, size = line.split()
                if kind == 'blob' and int(size) > 99_000_000:
                    oversized = True
            if process.wait():
                errors.seek(0)
                raise RuntimeError(errors.read()[-4000:])
    return oversized


def prepare(entry, root, report_dir, destination=None):
    report_dir = report_dir.resolve()
    upstream = entry['upstream_url']
    destination = destination or 'https://github.com/openharmony-qemu/' + entry['name'] + '.git'
    git(root, 'init', '--bare')
    git(root, 'config', 'http.version', 'HTTP/1.1')
    git(root, 'remote', 'add', 'upstream', upstream)
    git(root, 'remote', 'add', 'destination', destination)
    print(entry['name'] + ': fetching every upstream branch', flush=True)
    git(root, 'fetch', '--no-tags', 'upstream', '+refs/heads/*:refs/heads/*')
    original = refs(root, 'upstream')
    # Fail rather than publishing a partial snapshot if upstream moved during fetch.
    fetched = dict(line.split(' ', 1)[::-1] for line in
                   git(root, 'for-each-ref', '--format=%(objectname) %(refname)', 'refs/heads').stdout.splitlines())
    if original != fetched:
        raise RuntimeError('Upstream branch inventory changed while fetching; retry the job.')
    print(f'{entry["name"]}: fetched {len(original)} upstream branches', flush=True)
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
        print(entry['name'] + ': inspecting historical Git object sizes', flush=True)
        mapping = report_dir / 'commit-map.csv'
        mapping.unlink(missing_ok=True)
        if has_oversized_blobs(root):
            print(entry['name'] + ': converting oversized historical blobs to LFS', flush=True)
            git(root, 'lfs', 'migrate', 'import', '--everything', '--skip-fetch',
                '--above=99MB', '--yes', '--object-map=' + str(mapping))
        else:
            mapping.write_text('')
            print(entry['name'] + ': no oversized blobs; preserving original history', flush=True)
        print(entry['name'] + ': fetching existing upstream LFS objects', flush=True)
        git(root, 'lfs', 'fetch', '--all', 'upstream')
    hosted = {ref: git(root, 'rev-parse', ref).stdout.strip() for ref in original | baseline_refs}
    report = dict(repository='openharmony-qemu/' + entry['name'], upstream_url=upstream,
                  original_refs=original, baseline_original_refs=baseline_refs, hosted_refs=hosted)
    if entry.get('large_files') == 'git-lfs':
        report['lfs_objects'] = cached_lfs_objects(root)
    (report_dir / 'provenance.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def finish(entry, root, report_dir, reporter):
    report = json.loads((report_dir / 'provenance.json').read_text())
    if entry.get('large_files') == 'git-lfs':
        objects = report['lfs_objects']
        if objects != cached_lfs_objects(root):
            raise RuntimeError('Prepared LFS objects changed; no branches were published')
        print(f'{entry["name"]}: uploading {len(objects)} prepared LFS objects before branch publication', flush=True)
        if objects:
            git(root, 'lfs', 'push', '--object-id', 'destination', '--stdin',
                input_text='\n'.join(objects) + '\n')
        print(entry['name'] + ': LFS upload complete', flush=True)
    load_transfer_objects(root, 'destination')
    results = publish(root, 'destination', report['hosted_refs'], reporter, report['repository'],
                      lfs_complete=entry.get('large_files') == 'git-lfs',
                      bounded_transfer=entry.get('bounded_transfer') is True)
    report['results'] = results
    report['remaining_transfer_refs'] = cleanup_transfer_objects(root, 'destination', results)
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
    if not (os.environ.get('MIRROR_TOKEN') or
            (os.environ.get('MIRROR_APP_ID') and os.environ.get('MIRROR_APP_PRIVATE_KEY'))):
        raise ValueError('Configure MIRROR_APP_ID/MIRROR_APP_PRIVATE_KEY or MIRROR_TOKEN in mirror-sync.')
    git(repo, 'config', 'credential.helper', '')
    git(repo, 'config', 'credential.useHttpPath', 'true')
    git(repo, 'config', 'lfs.cachecredentials', 'false')
    git(repo, 'config', 'credential.https://github.com.helper',
        '!' + shlex.quote(sys.executable) + ' ' +
        shlex.quote(str(Path(__file__).with_name('github_credentials.py').resolve())))
    results = finish(entry, repo, report_dir, Issues())
    print(json.dumps({status: sum(r['status'] == status for r in results)
                      for status in ('created', 'updated', 'unchanged', 'conflict')}))
    if any(r['status'] == 'conflict' for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
