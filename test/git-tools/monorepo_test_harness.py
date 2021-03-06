"""
   Utility python module that is able to create a test monorepo
   repository as if it was generated by `mt generate`. The test harness also
   produces corresponding downstream and upstream split branches as well.
"""

import os
from pathlib import PosixPath
from git_apple_llvm.git_tools import git, git_output
from typing import Optional, List


def checkout_and_clean(commit_hash: str):
    git('checkout', commit_hash)
    git('reset', '--hard')
    git('clean', '-d', '-f')


def start_new_orphan_branch(name: str):
    git('branch', '-D', name, ignore_error=True)
    git('checkout', '--orphan', name)
    git('rm', '--cached', '-r', '.', ignore_error=True)
    git('clean', '-d', '-f')


def commit_file(filename: str, text: str,
                trailers: Optional[str] = None) -> str:
    """ Commits the contents to the given filename and returns the commit hash. """
    update_path = PosixPath(filename)
    if len(update_path.parent.name) > 0:
        (update_path.parent).mkdir(parents=True,
                                   exist_ok=True)
    update_path.resolve().write_text(text)
    git('add', filename)
    msg = f'Updated {filename}'
    if 'internal' in text:
        msg = f'[internal] {msg}'
    if trailers is not None:
        msg = f'{msg}\n{trailers}'
    git('commit', '-m', msg)
    head_commit = git_output('rev-parse', 'HEAD')
    return head_commit


class CommitBlueprint:
    """ A base class for a split repo commit blueprint. """

    def __init__(self, split_dir: str,
                 split_commit_hash: str):
        self.split_dir = split_dir
        self.split_commit_hash = split_commit_hash
        self.monorepo_commit_hash: Optional[str] = None


class BlobCommitBlueprint(CommitBlueprint):
    """ This commit blueprint represents a change to a blob. """

    def __init__(self, split_dir: str, filename: str, text: str,
                 split_commit_hash: str, parent: Optional[CommitBlueprint],
                 is_internal: bool):
        super().__init__(split_dir, split_commit_hash)
        self.filename = filename
        self.text = text
        self.parent = parent
        self.is_internal = is_internal

    @property
    def monorepo_filename(self):
        return self.filename if self.split_dir == '-' else os.path.join(self.split_dir, self.filename)


class MergeCommitBlueprint(CommitBlueprint):
    """ This commit blueprint represents a merge. """

    def __init__(self, split_dir: str,
                 split_commit_hash: str,
                 downstream: CommitBlueprint,
                 upstream: CommitBlueprint):
        super().__init__(split_dir, split_commit_hash)
        self.downstream = downstream
        self.upstream = upstream


class SplitRepoBuilder:
    """ Constructs the split repos. """

    def __init__(self, split_dir: str):
        start_new_orphan_branch('temp')
        self.split_dir = split_dir
        self.prev_parent: Optional[CommitBlueprint] = None

    def commit_file(self, filename: str, text: str,
                    parent: Optional[BlobCommitBlueprint] = None,
                    internal: bool = False) -> BlobCommitBlueprint:
        if parent is not None:
            checkout_and_clean(parent.split_commit_hash)
        commit_hash = commit_file(filename, text)
        result = BlobCommitBlueprint(split_dir=self.split_dir,
                                     filename=filename,
                                     text=text,
                                     split_commit_hash=commit_hash,
                                     parent=parent if parent else self.prev_parent,
                                     is_internal=internal)
        kind = 'internal' if internal else 'upstream'
        git('branch', '-f', f'split/{self.split_dir}/{kind}/master', commit_hash)
        self.prev_parent = result
        return result

    def commit_merge(self, downstream: CommitBlueprint, upstream: CommitBlueprint) -> MergeCommitBlueprint:
        checkout_and_clean(downstream.split_commit_hash)
        git('merge', upstream.split_commit_hash)
        commit_hash = git_output('rev-parse', 'HEAD')
        git('branch', '-f', f'split/{self.split_dir}/internal/master', commit_hash)
        return MergeCommitBlueprint(split_dir=self.split_dir,
                                    split_commit_hash=commit_hash,
                                    downstream=downstream,
                                    upstream=upstream)


def create_monorepo(commits: List[CommitBlueprint]):
    # Create the upstream monorepo.
    start_new_orphan_branch('llvm/master')
    for commit in commits:
        if isinstance(commit, BlobCommitBlueprint) and not commit.is_internal:
            commit.monorepo_commit_hash = commit_file(commit.monorepo_filename, commit.text)

    # Create the downstream monorepo.
    is_first = True
    for commit in commits:
        hs = commit.split_commit_hash
        trailers = f'\n---\napple-llvm-split-commit: {hs}\napple-llvm-split-dir: {commit.split_dir}/'
        if isinstance(commit, BlobCommitBlueprint) and commit.is_internal:
            assert commit.parent and commit.parent.monorepo_commit_hash is not None
            if is_first:
                git('checkout', '-b', 'internal/master', commit.parent.monorepo_commit_hash)
                git('clean', '-d', '-f')
                is_first = False
            commit.monorepo_commit_hash = commit_file(commit.monorepo_filename, commit.text, trailers=trailers)
        elif isinstance(commit, MergeCommitBlueprint):
            assert not is_first
            # Verify the the expected split merge is already there.
            git('merge-base', '--is-ancestor', commit.downstream.monorepo_commit_hash, 'internal/master')
            # Recreate the merge.
            assert commit.upstream.monorepo_commit_hash is not None
            git('merge', '--no-commit', commit.upstream.monorepo_commit_hash)
            msg = f'Merge {commit.upstream.monorepo_commit_hash} into internal/master\n{trailers}'
            git('commit', '-m', msg)
            commit.monorepo_commit_hash = git_output('rev-parse', 'HEAD')

# Create the following monorepo history with the appropriate split branches:
#
#    *     root_merge1 [internal/master]
#    |\
#    | *   root3       [llvm/master]
#    | *   roo2
#    * |   root1_internal
#    * |   clang_merge2
#    |\ \
#    | |/
#    | *   clang4
#    * |   llvm_merge1
#    |\ \
#    | |/
#    | *   llvm2
#    * |   llvm1_internal
#    * |   clang_merge1
#    |\ \
#    | |/
#    | *   clang3
#    | *   root1
#    * |   clang1_internal
#    |/
#    *     clang2
#    *     llvm1
#    *     clang1


def create_simple_test_harness(push_config_json: str = '{}'):
    # These are our commits.
    clang = SplitRepoBuilder('clang')
    clang1 = clang.commit_file('file1', 'test clang')
    clang2 = clang.commit_file('dir/file2', 'test file2')
    clang1_internal = clang.commit_file('file1', 'test clang\ninternal', internal=True)
    clang3 = clang.commit_file('file3', 'test file3', parent=clang2)
    clang_merge1 = clang.commit_merge(clang1_internal, clang3)
    clang4 = clang.commit_file('file4', 'test', parent=clang3)
    clang_merge2 = clang.commit_merge(clang_merge1, clang4)

    llvm = SplitRepoBuilder('llvm')
    llvm1 = llvm.commit_file('file1', 'test llvm')
    llvm1_internal = llvm.commit_file('file2', 'internal test llvm', internal=True)
    llvm2 = llvm.commit_file('dir/subdir/file3', 'test llvm 3', parent=llvm1)
    llvm_merge1 = llvm.commit_merge(llvm1_internal, llvm2)

    root = SplitRepoBuilder('-')
    root1 = root.commit_file('README', 'test -')
    root1_internal = root.commit_file('internal.config', 'internal -', internal=True)
    root2 = root.commit_file('README', 'test monorepo root', parent=root1)
    root3 = root.commit_file('apple-llvm-config/push/internal-master.json', push_config_json, parent=root2)
    root_merge1 = root.commit_merge(root1_internal, root3)

    # The commits here are in 'chronological' order.
    monorepo_commits = [clang1, llvm1, clang2, root1,
                        clang1_internal, clang3, clang_merge1,
                        llvm2, llvm1_internal, llvm_merge1,
                        clang4, clang_merge2,
                        root2, root1_internal, root3, root_merge1]
    create_monorepo(monorepo_commits)
