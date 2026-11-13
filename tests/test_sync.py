import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sync import Issues, publish, refs, synchronize


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), '-c', 'commit.gpgsign=false',
        '-c', 'user.name=Mirror test', '-c', 'user.email=test@example.invalid', *args],
        stderr=subprocess.DEVNULL, text=True).strip()


class Reporter:
    def __init__(self):
        self.calls = []

    def report(self, *args):
        self.calls.append(args)
        return len(self.calls)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.destination = self.root / 'destination.git'
        self.source.mkdir()
        self.destination.mkdir()
        git(self.source, 'init', '-b', 'main')
        git(self.destination, 'init', '--bare')
        self.first = self.commit('one')
        git(self.source, 'branch', 'feature/nested')
        self.entry = dict(name='fixture', upstream_url=str(self.source), baseline_revisions=[self.first])
        self.reporter = Reporter()
        self.counter = 0

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, text):
        (self.source / 'source.txt').write_text(text)
        git(self.source, 'add', 'source.txt')
        git(self.source, 'commit', '-m', text)
        return git(self.source, 'rev-parse', 'HEAD')

    def sync(self):
        self.counter += 1
        work = self.root / str(self.counter)
        work.mkdir()
        report_dir = Path(os.path.relpath(self.root / 'reports'))
        return synchronize(self.entry, work, report_dir, self.reporter, str(self.destination))

    def test_all_branches_fast_forward_noop_and_destination_preservation(self):
        result = self.sync()
        self.assertEqual({r['ref'] for r in result}, {'refs/heads/main', 'refs/heads/feature/nested',
                                                     'refs/heads/upstream/baseline-7.0'})
        self.assertTrue(all(r['status'] == 'created' for r in result))
        git(self.destination, 'update-ref', 'refs/heads/OpenHarmony-7.0-Release-qemu', self.first)
        git(self.destination, 'update-ref', 'refs/heads/local-only', self.first)
        second = self.commit('two')
        self.sync()
        observed = refs(self.source, str(self.destination))
        self.assertEqual(observed['refs/heads/main'], second)
        self.assertEqual(observed['refs/heads/OpenHarmony-7.0-Release-qemu'], self.first)
        self.assertTrue(all(r['status'] == 'unchanged' for r in self.sync()))
        git(self.source, 'branch', '-D', 'feature/nested')
        self.sync()
        observed = refs(self.source, str(self.destination))
        self.assertIn('refs/heads/feature/nested', observed)
        self.assertIn('refs/heads/local-only', observed)

    def test_divergence_reports_issue_and_continues_other_branches(self):
        self.sync()
        destination_commit = self.commit('destination-side')
        git(self.source, 'push', str(self.destination), 'HEAD:main')
        git(self.source, 'reset', '--hard', self.first)
        incoming = self.commit('upstream-rewrite')
        git(self.source, 'branch', 'new-branch')
        result = self.sync()
        self.assertEqual(len(self.reporter.calls), 1)
        self.assertEqual(self.reporter.calls[0][1:4], ('refs/heads/main', destination_commit, incoming))
        self.assertEqual(next(r['status'] for r in result if r['ref'] == 'refs/heads/main'), 'conflict')
        observed = refs(self.source, str(self.destination))
        self.assertEqual(observed['refs/heads/main'], destination_commit)
        self.assertEqual(observed['refs/heads/new-branch'], incoming)

    def test_reserved_qemu_branch_never_published(self):
        git(self.source, 'branch', 'OpenHarmony-7.0-Release-qemu')
        self.sync()
        self.assertNotIn('refs/heads/OpenHarmony-7.0-Release-qemu', refs(self.source, str(self.destination)))
        self.assertEqual(len(self.reporter.calls), 1)

    def test_git_lfs_migration_is_repeatable(self):
        # Exercise conversion with real historical oversized content, even though
        # it is deleted at the current tip. Repeated runs must not create conflicts.
        blob = self.source / 'historical.dat'
        with blob.open('wb') as stream:
            stream.truncate(100_000_001)
        git(self.source, 'add', 'historical.dat')
        git(self.source, 'commit', '-m', 'large historical source asset')
        git(self.source, 'rm', 'historical.dat')
        git(self.source, 'commit', '-m', 'remove historical asset')
        self.entry['large_files'] = 'git-lfs'
        self.sync()
        self.assertTrue(all(r['status'] == 'unchanged' for r in self.sync()))
        self.assertEqual(self.reporter.calls, [])


class IssueTests(unittest.TestCase):
    def test_conflicts_are_deduplicated_in_control_repository(self):
        class FakeIssues(Issues):
            def __init__(self):
                super().__init__()
                self.calls = []
                self.rows = []
            def api(self, path, data=None, method=None):
                self.calls.append((path, data, method))
                if method == 'POST':
                    row = dict(data, number=len(self.rows) + 1)
                    self.rows.append(row)
                    return row
                if method == 'PATCH':
                    return data
                return self.rows[:]
        issues = FakeIssues()
        self.assertEqual(issues.repository, 'openharmony-qemu/mirror-sync')
        self.assertEqual(issues.report('openharmony-qemu/test', 'refs/heads/main', 'a', 'b', 'diverged'), 1)
        self.assertEqual(issues.report('openharmony-qemu/test', 'refs/heads/main', 'a', 'c', 'diverged'), 1)
        self.assertEqual(sum(method == 'POST' for _, _, method in issues.calls), 1)
        self.assertEqual(sum(method == 'PATCH' for _, _, method in issues.calls), 1)


if __name__ == '__main__':
    unittest.main()
