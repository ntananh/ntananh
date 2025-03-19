import datetime
from dateutil import relativedelta
import requests
import os
import time
import hashlib

class GitHubStatsGenerator:
    """
    A class to generate GitHub stats for a user, including repos, stars, commits, and LOC.
    """

    def __init__(self):
        self.access_token = os.environ.get(
            'ACCESS_TOKEN', "")
        self.user_name = os.environ.get('USER_NAME', "ntananh")
        self.headers = {'authorization': 'token ' + self.access_token}
        self.owner_id = None  # Will be populated in initialize()
        self.query_count = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0,
                            'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}

        # Create cache directory if it doesn't exist
        if not os.path.exists('cache'):
            os.makedirs('cache')

    def initialize(self):
        """Initialize by fetching user data and setting owner_id"""
        user_data, _ = self.perf_counter(self.user_getter, self.user_name)
        self.owner_id = user_data
        return user_data

    def daily_readme(self, birthday):
        """
        Returns the length of time since given birthday
        e.g. 'XX years, XX months, XX days'
        """
        diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
        return '{} {}, {} {}, {} {}{}'.format(
            diff.years, 'year' + self.format_plural(diff.years),
            diff.months, 'month' + self.format_plural(diff.months),
            diff.days, 'day' + self.format_plural(diff.days),
            ' 🎂' if (diff.months == 0 and diff.days == 0) else '')

    def format_plural(self, unit):
        """Returns 's' if unit is not 1, otherwise empty string"""
        return 's' if unit != 1 else ''

    def simple_request(self, func_name, query, variables):
        """Make a GraphQL request to GitHub API"""
        request = requests.post('https://api.github.com/graphql',
                                json={'query': query, 'variables': variables},
                                headers=self.headers)
        if request.status_code == 200:
            return request
        raise Exception(func_name, ' has failed with a', request.status_code,
                        request.text, self.query_count)

    def query_count_increment(self, func_id):
        """Track number of API calls by function"""
        self.query_count[func_id] += 1

    def graph_commits(self, start_date, end_date):
        """Get total commit count for user within date range"""
        self.query_count_increment('graph_commits')
        query = '''
        query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
            user(login: $login) {
                contributionsCollection(from: $start_date, to: $end_date) {
                    contributionCalendar {
                        totalContributions
                    }
                }
            }
        }'''
        variables = {'start_date': start_date,
                     'end_date': end_date, 'login': self.user_name}
        request = self.simple_request('graph_commits', query, variables)
        return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])

    def graph_repos_stars(self, count_type, owner_affiliation, cursor=None):
        """Get repository or star count for user"""
        self.query_count_increment('graph_repos_stars')
        query = '''
        query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
            user(login: $login) {
                repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                    totalCount
                    edges {
                        node {
                            ... on Repository {
                                nameWithOwner
                                stargazers {
                                    totalCount
                                }
                            }
                        }
                    }
                    pageInfo {
                        endCursor
                        hasNextPage
                    }
                }
            }
        }'''
        variables = {'owner_affiliation': owner_affiliation,
                     'login': self.user_name, 'cursor': cursor}
        request = self.simple_request('graph_repos_stars', query, variables)
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return self.stars_counter(request.json()['data']['user']['repositories']['edges'])

    def stars_counter(self, data):
        """Count total stars in repositories"""
        total_stars = 0
        for node in data:
            total_stars += node['node']['stargazers']['totalCount']
        return total_stars

    def recursive_loc(self, owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
        """Fetch commit history recursively, calculating lines of code"""
        self.query_count_increment('recursive_loc')
        query = '''
        query ($repo_name: String!, $owner: String!, $cursor: String) {
            repository(name: $repo_name, owner: $owner) {
                defaultBranchRef {
                    target {
                        ... on Commit {
                            history(first: 100, after: $cursor) {
                                totalCount
                                edges {
                                    node {
                                        ... on Commit {
                                            committedDate
                                        }
                                        author {
                                            user {
                                                id
                                            }
                                        }
                                        deletions
                                        additions
                                    }
                                }
                                pageInfo {
                                    endCursor
                                    hasNextPage
                                }
                            }
                        }
                    }
                }
            }
        }'''
        variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
        request = requests.post('https://api.github.com/graphql',
                                json={'query': query, 'variables': variables},
                                headers=self.headers)

        if request.status_code == 200:
            if request.json()['data']['repository']['defaultBranchRef'] is not None:
                return self.loc_counter_one_repo(
                    owner, repo_name, data, cache_comment,
                    request.json()[
                        'data']['repository']['defaultBranchRef']['target']['history'],
                    addition_total, deletion_total, my_commits
                )
            else:
                return 0, 0, 0

        self.force_close_file(data, cache_comment)
        if request.status_code == 403:
            raise Exception(
                'Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
        raise Exception('recursive_loc() has failed with a',
                        request.status_code, request.text, self.query_count)

    def loc_counter_one_repo(self, owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
        """
        Calculate lines of code statistics for a single repository
        """
        for node in history['edges']:
            # Check if user exists before checking ID
            if (node['node']['author']['user'] is not None and
                    node['node']['author']['user']['id'] == self.owner_id['id']):
                my_commits += 1
                addition_total += node['node']['additions']
                deletion_total += node['node']['deletions']

        if not history['edges'] or not history['pageInfo']['hasNextPage']:
            return addition_total, deletion_total, my_commits
        else:
            return self.recursive_loc(
                owner, repo_name, data, cache_comment,
                addition_total, deletion_total, my_commits,
                history['pageInfo']['endCursor']
            )

    def loc_query(self, owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
        """Query all repositories and calculate total lines of code"""
        if edges is None:
            edges = []

        self.query_count_increment('loc_query')
        query = '''
        query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
            user(login: $login) {
                repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    pageInfo {
                        endCursor
                        hasNextPage
                    }
                }
            }
        }'''
        variables = {'owner_affiliation': owner_affiliation,
                     'login': self.user_name, 'cursor': cursor}
        request = self.simple_request('loc_query', query, variables)

        if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:
            edges += request.json()['data']['user']['repositories']['edges']
            return self.loc_query(
                owner_affiliation, comment_size, force_cache,
                request.json()[
                    'data']['user']['repositories']['pageInfo']['endCursor'],
                edges
            )
        else:
            return self.cache_builder(
                edges +
                request.json()['data']['user']['repositories']['edges'],
                comment_size, force_cache
            )

    def cache_builder(self, edges, comment_size, force_cache, loc_add=0, loc_del=0):
        """Build and manage cache of repository statistics"""
        cached = True
        filename = f'cache/{hashlib.sha256(self.user_name.encode("utf-8")).hexdigest()}.txt'

        try:
            with open(filename, 'r') as f:
                data = f.readlines()
        except FileNotFoundError:
            data = []
            if comment_size > 0:
                data = [
                    'This line is a comment block. Write whatever you want here.\n'] * comment_size
            with open(filename, 'w') as f:
                f.writelines(data)

        if len(data) - comment_size != len(edges) or force_cache:
            cached = False
            self.flush_cache(edges, filename, comment_size)
            with open(filename, 'r') as f:
                data = f.readlines()

        cache_comment = data[:comment_size]
        data = data[comment_size:]

        for index in range(len(edges)):
            repo_hash, commit_count, *__ = data[index].split()
            if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
                try:
                    if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                        owner, repo_name = edges[index]['node']['nameWithOwner'].split(
                            '/')
                        loc = self.recursive_loc(
                            owner, repo_name, data, cache_comment)
                        data[index] = f"{repo_hash} {edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']} {loc[2]} {loc[0]} {loc[1]}\n"
                except TypeError:
                    data[index] = f"{repo_hash} 0 0 0 0\n"

        with open(filename, 'w') as f:
            f.writelines(cache_comment)
            f.writelines(data)

        for line in data:
            loc = line.split()
            if len(loc) >= 5:  # Make sure we have enough elements
                loc_add += int(loc[3])
                loc_del += int(loc[4])

        return [loc_add, loc_del, loc_add - loc_del, cached]

    def flush_cache(self, edges, filename, comment_size):
        """Clear and initialize cache file"""
        try:
            with open(filename, 'r') as f:
                data = []
                if comment_size > 0:
                    data = f.readlines()[:comment_size]
        except FileNotFoundError:
            data = []
            if comment_size > 0:
                data = [
                    'This line is a comment block. Write whatever you want here.\n'] * comment_size

        with open(filename, 'w') as f:
            f.writelines(data)
            for node in edges:
                f.write(
                    f"{hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest()} 0 0 0 0\n")

    def force_close_file(self, data, cache_comment):
        """Ensure data is saved before potential crash"""
        filename = f'cache/{hashlib.sha256(self.user_name.encode("utf-8")).hexdigest()}.txt'
        with open(filename, 'w') as f:
            f.writelines(cache_comment)
            f.writelines(data)
        print(
            f'There was an error while writing to the cache file. The file, {filename} has had the partial data saved and closed.')

    def add_archive(self):
        """Add archived repository data"""
        try:
            with open('cache/repository_archive.txt', 'r') as f:
                data = f.readlines()
            old_data = data
            data = data[7:len(data) - 3]  # remove the comment block
            added_loc, deleted_loc, added_commits = 0, 0, 0
            contributed_repos = len(data)
            for line in data:
                _, _, my_commits, *loc = line.split()
                added_loc += int(loc[0])
                deleted_loc += int(loc[1])
                if my_commits.isdigit():
                    added_commits += int(my_commits)
            added_commits += int(old_data[-1].split()[4][:-1])
            return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]
        except FileNotFoundError:
            print("Repository archive file not found. Creating empty archive data.")
            return [0, 0, 0, 0, 0]

    def commit_counter(self, comment_size):
        """Count total commits from cache file"""
        total_commits = 0
        filename = f'cache/{hashlib.sha256(self.user_name.encode("utf-8")).hexdigest()}.txt'
        try:
            with open(filename, 'r') as f:
                data = f.readlines()
            data = data[comment_size:]  # remove comment lines
            for line in data:
                parts = line.split()
                if len(parts) >= 3:  # Make sure we have enough elements
                    total_commits += int(parts[2])
            return total_commits
        except FileNotFoundError:
            return 0

    def user_getter(self, username):
        """Get user ID and creation time"""
        self.query_count_increment('user_getter')
        query = '''
        query($login: String!){
            user(login: $login) {
                id
                createdAt
                avatarUrl
                name
                bio
            }
        }'''
        variables = {'login': username}
        request = self.simple_request('user_getter', query, variables)
        user_data = request.json()['data']['user']
        return {'id': user_data['id']}, user_data

    def follower_getter(self, username):
        """Get follower count for user"""
        self.query_count_increment('follower_getter')
        query = '''
        query($login: String!){
            user(login: $login) {
                followers {
                    totalCount
                }
                following {
                    totalCount
                }
            }
        }'''
        variables = {'login': username}
        request = self.simple_request('follower_getter', query, variables)
        user_data = request.json()['data']['user']
        return {
            'followers': int(user_data['followers']['totalCount']),
            'following': int(user_data['following']['totalCount'])
        }

    def perf_counter(self, func, *args):
        """Measure function execution time"""
        start = time.perf_counter()
        func_return = func(*args)
        return func_return, time.perf_counter() - start

    def formatter(self, query_type, difference, func_return=False, whitespace=0):
        """Format and print timing information"""
        print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
        if difference > 1:
            print('{:>12}'.format('%.4f' % difference + ' s '))
        else:
            print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
        if whitespace:
            return f"{'{:,}'.format(func_return): <{whitespace}}"
        return func_return

    def run(self):
        """Main method to run the stats generator"""
        print('Calculation times:')

        # Initialize and get user data
        user_data, user_time = self.perf_counter(self.initialize)
        OWNER_ID, _user_info = user_data
        self.formatter('account data', user_time)

        age_data, age_time = self.perf_counter(self.daily_readme, datetime.datetime(2001, 6, 9))
        self.formatter('age calculation', age_time)

        # Get LOC stats
        total_loc, loc_time = self.perf_counter(
            self.loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
        if total_loc[-1]:
            self.formatter('LOC (cached)', loc_time)
        else:
            self.formatter('LOC (no cache)', loc_time)

        commit_data, commit_time = self.perf_counter(self.commit_counter, 7)
        star_data, star_time = self.perf_counter(
            self.graph_repos_stars, 'stars', ['OWNER'])
        repo_data, repo_time = self.perf_counter(
            self.graph_repos_stars, 'repos', ['OWNER'])
        contrib_data, contrib_time = self.perf_counter(self.graph_repos_stars, 'repos', [
            'OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])

        # Get follower stats
        follower_info, follower_time = self.perf_counter(
            self.follower_getter, self.user_name)

        # Add archived repository data for specific user
        if self.owner_id == {'id': OWNER_ID}:  # only calculate for specific user
            archived_data = self.add_archive()
            for index in range(len(total_loc) - 1):
                total_loc[index] += archived_data[index]
            contrib_data += archived_data[-1]
            commit_data += int(archived_data[-2])

        # Create stats dictionary
        stats = {
            'age': age_data,
            'commits': f"{'{:,}'.format(commit_data)}",
            'stars': f"{'{:,}'.format(star_data)}",
            'repos': f"{'{:,}'.format(repo_data)}",
            'contrib': f"{'{:,}'.format(contrib_data)}",
            'followers': f"{'{:,}'.format(follower_info['followers'])}",
            'following': f"{'{:,}'.format(follower_info['following'])}",
            # Net LOC (additions - deletions)
            'loc': f"{'{:,}'.format(total_loc[2])}",
            'loc_add': total_loc[0],                  # Total additions
            'loc_del': total_loc[1]                   # Total deletions
        }

        # Format and print remaining stats
        self.formatter('commit counter', commit_time)
        self.formatter('stars counter', star_time)
        self.formatter('repo counter', repo_time)
        self.formatter('contrib counter', contrib_time)
        self.formatter('follower stats', follower_time)

        print(f"\nTotal API queries: {sum(self.query_count.values())}")
        for key, value in self.query_count.items():
            print(f"{key}: {value}")

        print('\nGitHub Stats Summary:')
        print(f"Username: {self.user_name}")
        print(f"GitHub Age: {stats['age']}")
        print(f"Repositories: {stats['repos']}")
        print(f"Stars: {stats['stars']}")
        print(f"Commits: {stats['commits']}")
        print(f"Followers: {stats['followers']}")
        print(f"Following: {stats['following']}")
        print(f"Lines of Code (net): {stats['loc']}")
        print(f"Lines Added: {'{:,}'.format(stats['loc_add'])}")
        print(f"Lines Deleted: {'{:,}'.format(stats['loc_del'])}")

        return stats


if __name__ == "__main__":
    stats_generator = GitHubStatsGenerator()
    stats = stats_generator.run()
