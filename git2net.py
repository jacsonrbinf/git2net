#!/usr/bin/python3

import logging
import sqlite3
import os
import argparse
import sys

from multiprocessing import Pool
from multiprocessing import Semaphore

import pandas as pd
from tqdm import tqdm
import numpy as np
from scipy.stats import entropy

import pydriller as pydriller
from pydriller.git_repository import GitCommandError
from Levenshtein import distance as lev_dist
import datetime

import pathpy as pp

logger = logging.getLogger(__name__)

def get_block_length(lines, k):
    """
    Calculates the length (in number of lines) of a edit of added/deleted lines starting in a given
    line k.

    Parameters
    ----------
    @param lines: dictionary of added or deleted lines
    @param k: line number to check for
    """
    if k not in lines or (k > 1 and k - 1 in lines):
        edit = False
        block_size = 0
    else:
        edit = True
        block_size = 1

    while edit:
        if k + block_size in lines:
            block_size += 1
        else:
            edit = False
    return block_size


def identify_edits(deleted_lines, added_lines, use_blocks=False):
    """
    Maps line numbers between the pre- and post-commit version of a modification.
    """

    # either deleted or added lines must contain items otherwise there would not be a modification
    # to process
    if len(deleted_lines.keys()) > 0:
        max_deleted = max(deleted_lines.keys())
        min_deleted = min(deleted_lines.keys())
    else:
        max_deleted = -1
        min_deleted = np.inf

    if len(added_lines.keys()) > 0:
        max_added = max(added_lines.keys())
        min_added = min(added_lines.keys())
    else:
        max_added = -1
        min_added = np.inf

    # create mapping between pre and post edit line numbers
    pre_to_post = {}

    # create DataFrame holding information on edit
    edits = pd.DataFrame()

    # line numbers of lines before the first addition or deletion do not change
    pre = min(min_added, min_deleted)
    post = min(min_added, min_deleted)

    # counters used to match pre and post line number
    no_post_inc = 0
    both_inc = 0
    no_pre_inc = 0

    # line numbers after the last addition or deletion do not matter for edits
    while (pre <= max_deleted) or (post <= max_added):
        if use_blocks:
            # compute size of added and deleted edits
            # size is reported as 0 if the line is not in added or deleted lines, respectively
            length_added_block = get_block_length(added_lines, post)
            length_deleted_block = get_block_length(deleted_lines, pre)

            # check if a edit has been made
            if (length_deleted_block > 0) or (length_added_block > 0):
                edits = edits.append({'pre start': pre,
                                      'number of deleted lines': length_deleted_block,
                                      'post start': post,
                                      'number of added lines': length_added_block},
                                     ignore_index=True, sort=False)

            # deleted edit is larger than added edit
            if length_deleted_block > length_added_block:
                no_post_inc = length_deleted_block - length_added_block
                both_inc = length_added_block

            # added edit is larger than deleted edit
            elif length_added_block > length_deleted_block:
                no_pre_inc = length_added_block - length_deleted_block
                both_inc = length_deleted_block
        else: # no blocks are considered
            pre_in_deleted = pre in deleted_lines
            post_in_added = post in added_lines
            if pre_in_deleted or post_in_added:
                edits = edits.append({'pre start': pre,
                                      'number of deleted lines': int(pre_in_deleted),
                                      'post start': post,
                                      'number of added lines': int(post_in_added)},
                                     ignore_index=True, sort=False)
            if pre_in_deleted and not post_in_added:
                no_post_inc += 1
            if post_in_added and not pre_in_deleted:
                no_pre_inc += 1

        # increment pre and post counter
        if both_inc > 0:
            both_inc -= 1
            pre_to_post[pre] = post
            pre += 1
            post += 1
        elif no_post_inc > 0:
            no_post_inc -= 1
            pre_to_post[pre] = False
            pre += 1
        elif no_pre_inc > 0:
            no_pre_inc -= 1
            post += 1
        else:
            pre_to_post[pre] = post
            pre += 1
            post += 1

    return pre_to_post, edits.astype(int)

def text_entropy(text):
    return entropy([text.count(chr(i)) for i in range(256)], base=2)

def extract_edits(git_repo, commit, mod, use_blocks=False):

    df = pd.DataFrame()

    path = mod.new_path

    parsed_lines = git_repo.parse_diff(mod.diff)

    deleted_lines = { x[0]:x[1] for x in parsed_lines['deleted'] }
    added_lines = { x[0]:x[1] for x in parsed_lines['added'] }

    pre_to_post, edits = identify_edits(deleted_lines, added_lines, use_blocks=use_blocks)

    try:
        blame = git_repo.git.blame(commit.hash + '^', '--', path).split('\n')
        for _, edit in edits.iterrows():
            c = {}
            c['mod_filename'] = mod.filename
            c['mod_new_path'] = path
            c['mod_old_path'] = mod.old_path
            c['post_commit'] = commit.hash
            c['mod_added'] = mod.added
            c['mod_removed'] = mod.removed
            c['mod_cyclomatic_complexity'] = mod.complexity
            c['mod_loc'] = mod.nloc
            c['mod_token_count'] = mod.token_count

            deleted_block = []
            for i in range(edit['pre start'],
                            edit['pre start'] + edit['number of deleted lines']):
                deleted_block.append(deleted_lines[i])

            added_block = []
            for i in range(edit['post start'],
                            edit['post start'] + edit['number of added lines']):
                added_block.append(added_lines[i])

            deleted_block = ' '.join(deleted_block)
            added_block = ' '.join(added_block)

            c['pre_starting_line_num'] = edit['pre start']
            if edit['number of deleted lines'] == 0:
                c['pre_len_in_lines'] = None
                c['pre_len_in_chars'] = None
                c['pre_entropy'] = None
                c['pre_commit'] = None
            else:
                blame_fields = blame[edit['pre start'] - 1].split(' ')
                original_commit_hash = blame_fields[0].replace('^', '')
                c['pre_commit'] = original_commit_hash
                c['pre_len_in_lines'] = edit['number of deleted lines']
                c['pre_len_in_chars'] = len(deleted_block)
                if len(deleted_block) > 0:
                    c['pre_entropy'] = text_entropy(deleted_block)
                else:
                    c['pre_entropy'] = None

            c['post_starting_line_num'] = edit['post start']
            if edit['number of added lines'] == 0:
                c['post_len_in_lines'] = None
                c['post_len_in_chars'] = None
                c['post_entropy'] = None

            else:
                c['post_len_in_lines'] = edit['number of added lines']
                c['post_len_in_chars'] = len(added_block)
                if len(added_block) > 0:
                    c['post_entropy'] = text_entropy(added_block)
                    c['levenshtein_dist'] = lev_dist(deleted_block, added_block)
                else:
                    c['post_entropy'] = None
                    c['levenshtein_dist'] = None

            df = df.append(c, ignore_index=True, sort=False)
    except GitCommandError:
        logger.debug("Could not found file %s in commit %s. Probably a double rename!",
                     mod.filename, commit.hash)

    return df


#def process_commit(git_repo, commit, exclude_paths = set(), use_blocks = False):
def process_commit(args):
    # git_repo, commit, exclude_paths, use_blocks = args

    git_repo = pydriller.GitRepository(args['repo_string'])
    commit = git_repo.get_commit(args['commit_hash'])

    df_commit = pd.DataFrame()
    df_edits = pd.DataFrame()

    # parse commit
    c = {}
    c['hash'] = commit.hash
    c['author_email'] = commit.author.email
    c['author_name'] = commit.author.name
    c['committer_email'] = commit.committer.email
    c['committer_name'] = commit.committer.name
    c['author_date'] = commit.author_date.strftime('%Y-%m-%d %H:%M:%S')
    c['committer_date'] = commit.committer_date.strftime('%Y-%m-%d %H:%M:%S')
    c['committer_timezone'] = commit.committer_timezone
    c['modifications'] = len(commit.modifications)
    c['msg_len'] = len(commit.msg)
    c['project_name'] = commit.project_name
    c['parents'] = ','.join(commit.parents)
    c['merge'] = commit.merge
    c['in_main_branch'] = commit.in_main_branch
    c['branches'] = ','.join(commit.branches)

    # parse modification
    for modification in commit.modifications:
        exclude_file = False
        excluded_path = ''
        for x in args['exclude_paths']:
            if modification.new_path:
                if modification.new_path.startswith(x + os.sep):
                    exclude_file = True
                    excluded_path = modification.new_path
            if not exclude_file and modification.old_path:
                if modification.old_path.startswith(x + os.sep):
                    exclude_file = True
                    excluded_path = modification.old_path
        if not exclude_file:
            df_edits = df_edits.append(extract_edits(git_repo, commit, modification,
                                                     use_blocks=args['use_blocks']),
                                       ignore_index=True, sort=True)

    df_commit = pd.DataFrame(c, index=[0])

    return {'commit': df_commit, 'edits': df_edits}


def process_repo_serial(repo_string, sqlite_db_file, use_blocks=False, exclude=None, _p_commits=[]):
    git_repo = pydriller.GitRepository(repo_string)
    exclude_paths = []
    if exclude:
        with open(exclude) as f:
            exclude_paths = [x.strip() for x in f.readlines()]

    df_commits = pd.DataFrame()
    df_edits = pd.DataFrame()

    i = 0
    commits = [commit for commit in git_repo.get_list_commits() if commit.hash not in _p_commits]
    for commit in tqdm(commits, desc='Serial'):
        args = {'repo_string': repo_string, 'commit_hash': commit.hash, 'use_blocks': use_blocks,
                'exclude_paths': exclude_paths}
        result = process_commit(args)
        df_commits = pd.concat([df_commits, result['commit']], sort=True)
        df_edits = pd.concat([df_edits, result['edits']], sort=True)

    con = sqlite3.connect(sqlite_db_file)
    if not df_commits.empty:
        df_commits.to_sql('commits', con, if_exists='append')
    if not df_edits.empty:
        df_edits.to_sql('edits', con, if_exists='append')


def process_repo_parallel(repo_string, sqlite_db_file, use_blocks=False,
                          num_processes=os.cpu_count(), chunksize=1, exclude=None, _p_commits=[]):
    git_repo = pydriller.GitRepository(repo_string)
    exclude_paths = []
    if exclude:
        with open(exclude) as f:
            exclude_paths = [x.strip() for x in f.readlines()]

    args = [{'repo_string': repo_string, 'commit_hash': commit.hash, 'use_blocks': use_blocks,
             'exclude_paths': exclude_paths}
            for commit in git_repo.get_list_commits() if commit.hash not in _p_commits]

    con = sqlite3.connect(sqlite_db_file)
    p = Pool(num_processes)
    with tqdm(total=len(args), desc='Parallel ({0} processes)'.format(num_processes)) as pbar:
        for i, result in enumerate(p.imap_unordered(process_commit, args, chunksize=chunksize)):
            if not result['commit'].empty:
                result['commit'].to_sql('commits', con, if_exists='append')
            if not result['edits'].empty:
                result['edits'].to_sql('edits', con, if_exists='append')
            pbar.update(1)


def get_unified_changes(repo_string, commit_hash, filename):
    """
    Returns dataframe with github-like unified diff representation of the content of a file before
    and after a commit for a given git repository, commit hash and filename.
    """
    git_repo = pydriller.GitRepository(repo_string)
    commit = git_repo.get_commit(commit_hash)
    for modification in commit.modifications:
        if modification.filename == filename:
            parsed_lines = git_repo.parse_diff(modification.diff)

            deleted_lines = { x[0]:x[1] for x in parsed_lines['deleted'] }
            added_lines = { x[0]:x[1] for x in parsed_lines['added'] }

            pre_to_post, edits = identify_edits(deleted_lines, added_lines)

            post_source_code = modification.source_code.split('\n')

            max_line_no = max(max(deleted_lines.keys()),
                              max(added_lines.keys()),
                              len(post_source_code))

            pre = []
            post = []
            action = []
            code = []

            pre = 1
            post = 1
            while max(pre, post) < max_line_no:
                if pre in edits.keys():
                    cur = pre
                    for i in range(edits[cur][0]):
                        pre.append(pre)
                        post.append(None)
                        action.append('-')
                        code.append(deleted_lines[pre])
                        pre += 1
                    for i in range(edits[cur][2]):
                        pre.append(None)
                        post.append(post)
                        action.append('+')
                        code.append(added_lines[post])
                        post += 1
                else:
                    if pre in pre_to_post.keys():
                        # if pre is not in the dictionary nothing has changed
                        if post < pre_to_post[pre]:
                            # a edit has been added
                            for i in range(pre_to_post[pre] - post):
                                pre.append(None)
                                post.append(post)
                                action.append('+')
                                code.append(added_lines[post])
                                post += 1

                    pre.append(pre)
                    post.append(post)
                    action.append(None)
                    code.append(post_source_code[post - 1]) # minus one as list starts from 0
                    pre += 1
                    post += 1

    return pd.DataFrame({'pre': pre, 'post': post, 'action': action, 'code': code})


def _get_tedges(db_location):
    con = sqlite3.connect(db_location)

    tedges = pd.read_sql("""SELECT x.author_pre as source,
                                   substr(x.pre_commit, 1, 8) as pre_commit,
                                   c_post.author_email AS target,
                                   substr(x.post_commit, 1, 8) AS post_commit,
                                   c_post.committer_date as time,
                                   x.levenshtein_dist as levenshtein_dist
                            FROM (
                                   SELECT c_pre.author_email AS author_pre,
                                          edits.pre_commit,
                                          edits.post_commit,
                                          edits.levenshtein_dist
                                   FROM edits
                                   JOIN commits AS c_pre
                                   ON substr(c_pre.hash, 1, 8) == edits.pre_commit) AS x
                                   JOIN commits AS c_post
                                   ON substr(c_post.hash, 1, 8) == substr(x.post_commit, 1, 8
                                 )
                            WHERE source != target""", con)

    tedges.loc[:,'time'] = pd.to_datetime(tedges.time)

    return tedges


def get_coediting_network(db_location, time_from=None, time_to=None):
    tedges = _get_tedges(db_location)

    if time_from == None:
        time_from = min(tedges.time)
    if time_to == None:
        time_to = max(tedges.time)

    t = pp.TemporalNetwork()
    for idx, edge in tedges.iterrows():
        if (edge.time >= time_from) and (edge.time <= time_to):
            t.add_edge(edge.source,
                       edge.target,
                       edge.time.strftime('%Y-%m-%d %H:%M:%S'),
                       directed=True,
                       timestamp_format='%Y-%m-%d %H:%M:%S')
    return t


def _get_bipartite_edges(db_location):
    con = sqlite3.connect(db_location)

    bipartite_edges = pd.read_sql("""SELECT DISTINCT mod_filename AS target,
                                            commits.author_name AS source,
                                            commits.committer_date AS time
                                     FROM edits
                                     JOIN commits ON edits.post_commit == commits.hash""", con)

    bipartite_edges.loc[:,'time'] = pd.to_datetime(bipartite_edges.time)

    return bipartite_edges


def get_bipartite_network(db_location, time_from=None, time_to=None):
    bipartite_edges = _get_bipartite_edges(db_location)

    if time_from == None:
        time_from = min(bipartite_edges.time)
    if time_to == None:
        time_to = max(bipartite_edges.time)

    n = pp.Network()
    for idx, edge in bipartite_edges.iterrows():
        if (edge.time >= time_from) and (edge.time <= time_to):
            n.add_edge(edge.source, edge.target)
    return n


def _get_dag_edges(db_location):
    con = sqlite3.connect(db_location)

    dag_edges = pd.read_sql("""SELECT DISTINCT x.author_pre||","||substr(x.pre_commit, 1, 8)
                                        AS source,
                                      c_post.author_email||","|| substr(x.post_commit, 1, 8)
                                        AS target,
                                      c_post.committer_date AS time
                               FROM (
                                      SELECT c_pre.author_email AS author_pre,
                                             edits.pre_commit,
                                             edits.post_commit,
                                             edits.levenshtein_dist
                                      FROM edits
                                      JOIN commits AS c_pre
                                      ON substr(c_pre.hash, 1, 8) == edits.pre_commit
                                    ) AS x
                                JOIN (
                                       SELECT *
                                       FROM commits
                                     ) AS c_post
                                ON substr(c_post.hash, 1, 8) == substr(x.post_commit, 1, 8)
                                WHERE x.author_pre != c_post.author_email""", con)

    dag_edges.loc[:,'time'] = pd.to_datetime(dag_edges.time)

    return dag_edges


def get_dag(db_location, time_from=None, time_to=None):
    dag_edges = _get_dag_edges(db_location)

    if time_from == None:
        time_from = min(dag_edges.time)
    if time_to == None:
        time_to = max(dag_edges.time)

    dag = pp.DAG()
    for _, edge in dag_edges.iterrows():
        if (edge.time >= time_from) and (edge.time <= time_to):
            dag.add_edge(edge.source, edge.target)

    dag.topsort()

    return dag


def mine_git_repo(repo_string, sqlite_db_file, use_blocks=True, num_processes=os.cpu_count(),
                  chunksize=1, exclude=[]):

    # if os.path.exists(sqlite_db_file):
    #     try:
    #         with sqlite3.connect(sqlite_db_file) as con:
    #             prev_method, prev_repository = con.execute(
    #                 "SELECT method, repository FROM _metadata").fetchall()[0]

    #             if (prev_method == 'blocks' if use_blocks else 'lines') and \
    #             (prev_repository == repo_string):
    #                 p_commits = set(x[0]
    #                     for x in con.execute("SELECT hash FROM commits").fetchall())
    #                 c_commits = set(c.hash
    #                     for c in pydriller.GitRepository(repo_string).get_list_commits())
    #                 if not p_commits.issubset(c_commits):
    #                     raise Exception("Found a database that was created with identical " +
    #                                     "settings. However, some commits in the database are not " +
    #                                     "in the provided git repository. Please provide a clean " +
    #                                     "database.")
    #                 else:
    #                     if p_commits == c_commits:
    #                         print("The provided database is already complete!")
    #                         return
    #                     else:
    #                         print("Found a matching database on provided path. " +
    #                                 "Skipping {} ({:.2f}%) of {} commits. {} commits remaining"
    #                                 .format(len(p_commits), len(p_commits) / len(c_commits) * 100,
    #                                         len(c_commits), len(c_commits) - len(p_commits)))
    #             else:
    #                 raise Exception("Found a database on provided path that was created with " +
    #                                 "settings not matching the ones selected for the current " +
    #                                 "run. A path to either no database or a database from a  " +
    #                                 "previously paused run with identical settings is required.")
    #     except sqlite3.OperationalError:
    #         raise Exception("Found a database on provided path that was likely not created with " +
    #                         "git2net. A path to either no database or a database from a " +
    #                         "previously paused run with identical settings is required.")
    # else:
    #     print("Found no database on provided path. Starting from scratch.")
    #     with sqlite3.connect(sqlite_db_file) as con:
    #         con.execute("CREATE TABLE _metadata ('created with', 'repository', 'date', 'method')")
    #         con.execute("""INSERT INTO _metadata ('created with', 'repository', 'date', 'method')
    #                     VALUES (:version, :repository, :date, :method)""",
    #                     {'version': 'git2net alpha',
    #                      'repository': repo_string,
    #                      'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    #                      'method': 'blocks' if use_blocks else 'lines'})
    #         con.commit()
    #         p_commits = []

    p_commits = []

    if num_processes > 1:
        process_repo_parallel(repo_string=repo_string, sqlite_db_file=sqlite_db_file,
                              use_blocks=use_blocks, num_processes=num_processes,
                              chunksize=chunksize, exclude=exclude, _p_commits=p_commits)
    else:
        process_repo_serial(repo_string=repo_string, sqlite_db_file=sqlite_db_file,
                            use_blocks=use_blocks, exclude=exclude, _p_commits=p_commits)



if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Extracts commit and co-editing data from git repositories.')
    parser.add_argument('repo', help='Path to repository to be parsed.', type=str)
    parser.add_argument('outfile', help='Path to SQLite DB file storing results.', type=str)
    parser.add_argument('--exclude', help='Exclude path prefixes in given file.', type=str,
        default=None)
    parser.add_argument('--numprocesses',
        help='Number of CPU cores used for multi-core processing. Defaults to number of CPU cores.',
        default=os.cpu_count(), type=int)
    parser.add_argument('--chunksize', help='Chunk size to be used in multiprocessing.map.',
        default=1, type=int)
    parser.add_argument('--use-blocks',
        help='Compare added and deleted blocks of code rather than lines.', dest='use_blocks',
        action='store_true')
    parser.set_defaults(parallel=True)
    parser.set_defaults(use_blocks=False)

    args = parser.parse_args()

    mine_git_repo(args.repo, args.outfile, exclude=args.exclude, num_processes=args.numprocesses,
                  chunksize=args.chunksize, use_blocks=args.use_blocks)
