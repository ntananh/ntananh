import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import mock_open, patch

import requests

from today import GitHubStatsGenerator


class GenerateReadmeTest(unittest.TestCase):
    def test_counts_keep_thousands_separators(self) -> None:
        template = Path(__file__).with_name('README.template.md').read_text()
        generator = GitHubStatsGenerator.__new__(GitHubStatsGenerator)
        stats = {
            'repos': '47',
            'stars': '0',
            'commits': '983',
            'followers': '20',
            'loc': '727,703',
            'loc_added': '775,219',
            'loc_removed': '47,516',
        }

        for count in ('repos', 'stars', 'commits', 'followers'):
            for total in ('999', '1,061', '1,000,000'):
                with self.subTest(count=count, total=total):
                    file_mock = mock_open(read_data=template)
                    with patch('builtins.open', file_mock):
                        status = generator.generate_readme(
                            'README.template.md', {**stats, count: total})

                    self.assertEqual(status, 0)
                    rendered = file_mock().write.call_args.args[0]
                    self.assertIn(total, rendered)
                    self.assertNotIn('{{', rendered)
                    self.assertIn('727,703', rendered)
                    self.assertIn('775,219++', rendered)
                    self.assertIn('47,516--', rendered)


class RepositoryStatsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        original_directory = Path.cwd()
        os.chdir(temporary.name)
        self.addCleanup(os.chdir, original_directory)
        Path('cache').mkdir()
        with patch.dict(os.environ, {'ACCESS_TOKEN': 'test-token'}):
            self.generator = GitHubStatsGenerator()
        digest = hashlib.sha256(self.generator.user_name.encode()).hexdigest()
        self.cache = Path('cache') / f'{digest}.txt'

    @staticmethod
    def repo(name: str, commits: int = 10) -> dict:
        return {'node': {
            'nameWithOwner': name,
            'defaultBranchRef': {'target': {'history': {'totalCount': commits}}},
        }}

    @staticmethod
    def cache_line(name: str, commits: int = 10) -> str:
        digest = hashlib.sha256(name.encode()).hexdigest()
        return f'{digest} {commits} 5 100 20\n'

    def test_replaced_repository_is_refreshed(self) -> None:
        self.cache.write_text(self.cache_line('owner/old'))
        with patch.object(self.generator, 'recursive_loc', return_value=(50, 10, 2)) as fetch:
            totals = self.generator.cache_builder([self.repo('owner/new')], 0, False)
        fetch.assert_called_once()
        self.assertEqual(totals[:3], [50, 10, 40])
        self.assertIn(hashlib.sha256(b'owner/new').hexdigest(), self.cache.read_text())

    def test_null_repository_does_not_crash_or_keep_old_totals(self) -> None:
        self.cache.write_text(self.cache_line('owner/old'))
        totals = self.generator.cache_builder([{'node': None}], 0, False)
        self.assertEqual(totals[:3], [0, 0, 0])
        self.assertEqual(self.cache.read_text(), '')

    def test_failed_fetch_preserves_cache(self) -> None:
        for force_refresh in (False, True):
            for status in (403, 500):
                with self.subTest(force_refresh=force_refresh, status=status):
                    original = self.cache_line('owner/repo', 9)
                    self.cache.write_text(original)
                    response = requests.Response()
                    response.status_code = status
                    with patch('today.requests.post', return_value=response):
                        with self.assertRaises(requests.HTTPError):
                            self.generator.cache_builder(
                                [self.repo('owner/repo')], 0, force_refresh)
                    self.assertEqual(self.cache.read_text(), original)

    def test_network_failure_preserves_cache(self) -> None:
        original = self.cache_line('owner/repo', 9)
        self.cache.write_text(original)
        with patch('today.requests.post', side_effect=requests.Timeout('timeout')):
            with self.assertRaises(requests.Timeout):
                self.generator.cache_builder([self.repo('owner/repo')], 0, False)
        self.assertEqual(self.cache.read_text(), original)

    def test_partial_history_failure_preserves_cache(self) -> None:
        original = self.cache_line('owner/repo', 9)
        self.cache.write_text(original)
        first_page = requests.Response()
        first_page.status_code = 200
        first_page.json = lambda: {'data': {'repository': {'defaultBranchRef': {
            'target': {'history': {
                'edges': [{'node': {
                    'author': {'user': {'id': 'owner-id'}, 'email': None},
                    'additions': 50,
                    'deletions': 10,
                }}],
                'pageInfo': {'hasNextPage': True, 'endCursor': 'next'},
            }},
        }}}}
        self.generator.owner_id = {'id': 'owner-id'}
        with patch('today.requests.post', side_effect=[first_page, requests.Timeout('timeout')]):
            with self.assertRaises(requests.Timeout):
                self.generator.cache_builder([self.repo('owner/repo')], 0, False)
        self.assertEqual(self.cache.read_text(), original)

    def test_api_errors_preserve_cache(self) -> None:
        for payload in ({'errors': [{'message': 'unavailable'}]},
                        {'data': {'repository': None}}):
            with self.subTest(payload=payload):
                original = self.cache_line('owner/repo', 9)
                self.cache.write_text(original)
                response = requests.Response()
                response.status_code = 200
                with patch.object(response, 'json', return_value=payload):
                    with patch('today.requests.post', return_value=response):
                        with self.assertRaises(RuntimeError):
                            self.generator.cache_builder([self.repo('owner/repo')], 0, False)
                self.assertEqual(self.cache.read_text(), original)

    def test_unchanged_cache_avoids_fetch(self) -> None:
        original = self.cache_line('owner/repo')
        self.cache.write_text(original)
        with patch.object(self.generator, 'recursive_loc') as fetch:
            totals = self.generator.cache_builder([self.repo('owner/repo')], 0, False)
        fetch.assert_not_called()
        self.assertEqual(totals, [100, 20, 80, True])
        self.assertEqual(self.cache.read_text(), original)

    def test_reordered_repositories_use_correct_totals(self) -> None:
        self.cache.write_text(
            self.cache_line('owner/a') + self.cache_line('owner/b'))
        with patch.object(self.generator, 'recursive_loc', return_value=(50, 10, 2)):
            totals = self.generator.cache_builder(
                [self.repo('owner/b'), self.repo('owner/a')], 0, False)
        self.assertEqual(totals[:3], [100, 20, 80])
        hashes = [line.split()[0] for line in self.cache.read_text().splitlines()]
        self.assertEqual(hashes, [hashlib.sha256(name).hexdigest()
                                  for name in (b'owner/b', b'owner/a')])

    def test_empty_repository_has_zero_totals(self) -> None:
        self.cache.write_text(self.cache_line('owner/repo'))
        with patch.object(self.generator, 'recursive_loc') as fetch:
            totals = self.generator.cache_builder(
                [{'node': {'nameWithOwner': 'owner/repo', 'defaultBranchRef': None}}],
                0, False)
        fetch.assert_not_called()
        self.assertEqual(totals[:3], [0, 0, 0])

    def test_star_count_includes_all_pages(self) -> None:
        pages = []
        for stars, has_next, cursor in ((3, True, 'next'), (7, False, None)):
            pages.append({'data': {'user': {
                'repositories': {
                    'edges': [{'node': {'stargazers': {'totalCount': stars}}}],
                    'pageInfo': {'hasNextPage': has_next, 'endCursor': cursor},
                },
            }}})
        with patch.object(self.generator, 'simple_request') as fetch:
            fetch.return_value.json.side_effect = pages
            self.assertEqual(self.generator.graph_repos_stars('stars', ['OWNER']), 10)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(fetch.call_args.args[2]['cursor'], 'next')


if __name__ == '__main__':
    unittest.main()
