# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright (C) 2016 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from contextlib import contextmanager, suppress
import hashlib
import logging
import os
import shutil
import subprocess
import sys
from typing import Pattern, Callable, Generator

from snapcraft.internal import common
from snapcraft.internal.errors import (
    RequiredCommandFailure,
    RequiredCommandNotFound,
    RequiredPathDoesNotExist,
)

if sys.version_info < (3, 6):
    import sha3  # noqa


logger = logging.getLogger(__name__)


def replace_in_file(directory: str, file_pattern: Pattern,
                    search_pattern: Pattern,
                    replacement: str) -> None:
    """Searches and replaces patterns that match a file pattern.

    :param str directory: The directory to look for files.
    :param str file_pattern: The file pattern to match inside directory.
    :param search_pattern: A re.compile'd pattern to search for within
                           matching files.
    :param str replacement: The string to replace the matching search_pattern
                            with.
    """

    for root, directories, files in os.walk(directory):
        for file_name in files:
            if file_pattern.match(file_name):
                file_path = os.path.join(root, file_name)
                # Don't bother trying to rewrite a symlink. It's either invalid
                # or the linked file will be rewritten on its own.
                if not os.path.islink(file_path):
                    search_and_replace_contents(
                        file_path, search_pattern, replacement)


def search_and_replace_contents(file_path: str,
                                search_pattern: Pattern,
                                replacement: str) -> None:
    """Search file and replace any occurrence of pattern with replacement.

    :param str file_path: Path of file to be searched.
    :param re.RegexObject search_pattern: Pattern for which to search.
    :param str replacement: The string to replace pattern.
    """
    try:
        with open(file_path, 'r+') as f:
            try:
                original = f.read()
            except UnicodeDecodeError:
                # This was probably a binary file. Skip it.
                return

            replaced = search_pattern.sub(replacement, original)
            if replaced != original:
                f.seek(0)
                f.truncate()
                f.write(replaced)
    except PermissionError as e:
        logger.warning('Unable to open {path} for writing: {error}'.format(
            path=file_path, error=e))


def link_or_copy(source: str, destination: str,
                 follow_symlinks: bool=False) -> None:
    """Hard-link source and destination files. Copy if it fails to link.

    Hard-linking may fail (e.g. a cross-device link, or permission denied), so
    as a backup plan we just copy it.

    :param str source: The source to which destination will be linked.
    :param str destination: The destination to be linked to source.
    :param bool follow_symlinks: Whether or not symlinks should be followed.
    """

    try:
        # Note that follow_symlinks doesn't seem to work for os.link, so we'll
        # implement this logic ourselves using realpath.
        source_path = source
        if follow_symlinks:
            source_path = os.path.realpath(source)

        if not os.path.exists(os.path.dirname(destination)):
            create_similar_directory(
                os.path.dirname(source_path),
                os.path.dirname(destination))
        # Setting follow_symlinks=False in case this bug is ever fixed
        # upstream-- we want this function to continue supporting NOT following
        # symlinks.
        os.link(source_path, destination, follow_symlinks=False)
    except OSError:
        # If os.link raised an I/O error, it may have left a file behind.
        # Skip on OSError in case it doesn't exist or is a directory.
        with suppress(OSError):
            os.unlink(destination)

        shutil.copy2(source, destination, follow_symlinks=follow_symlinks)
        uid = os.stat(source, follow_symlinks=follow_symlinks).st_uid
        gid = os.stat(source, follow_symlinks=follow_symlinks).st_gid
        try:
            os.chown(destination, uid, gid, follow_symlinks=follow_symlinks)
        except PermissionError as e:
            logger.debug('Unable to chown {destination}: {error}'.format(
                destination=destination, error=e))


def link_or_copy_tree(source_tree: str, destination_tree: str,
                      copy_function: Callable[..., None]
                      =link_or_copy) -> None:
    """Copy a source tree into a destination, hard-linking if possible.

    :param str source_tree: Source directory to be copied.
    :param str destination_tree: Destination directory. If this directory
                                 already exists, the files in `source_tree`
                                 will take precedence.
    :param callable copy_function: Callable that actually copies.
    """

    if not os.path.isdir(source_tree):
        raise NotADirectoryError('{!r} is not a directory'.format(source_tree))

    if (not os.path.isdir(destination_tree) and
            os.path.exists(destination_tree)):
        raise NotADirectoryError(
            'Cannot overwrite non-directory {!r} with directory '
            '{!r}'.format(destination_tree, source_tree))

    create_similar_directory(source_tree, destination_tree)

    for root, directories, files in os.walk(source_tree):
        for directory in directories:
            source = os.path.join(root, directory)
            # os.walk doesn't by default follow symlinks (which is good), but
            # it includes symlinks that are pointing to directories in the
            # directories list. We want to treat it as a file, here.
            if os.path.islink(source):
                files.append(directory)
                continue

            destination = os.path.join(
                destination_tree, os.path.relpath(source, source_tree))

            create_similar_directory(source, destination)

        for file_name in files:
            source = os.path.join(root, file_name)
            destination = os.path.join(
                destination_tree, os.path.relpath(source, source_tree))

            copy_function(source, destination)


def create_similar_directory(source: str, destination: str,
                             follow_symlinks: bool=False) -> None:
    """Create a directory with the same permission bits and owner information.

    :param str source: Directory from which to copy name, permission bits, and
                       owner information.
    :param str destintion: Directory to create and to which the `source`
                           information will be copied.
    :param bool follow_symlinks: Whether or not symlinks should be followed.
    """

    stat = os.stat(source, follow_symlinks=follow_symlinks)
    uid = stat.st_uid
    gid = stat.st_gid
    os.makedirs(destination, exist_ok=True)
    try:
        os.chown(destination, uid, gid, follow_symlinks=follow_symlinks)
    except PermissionError as exception:
        logger.debug('Unable to chown {}: {}'.format(destination, exception))

    shutil.copystat(source, destination, follow_symlinks=follow_symlinks)


def executable_exists(path: str) -> bool:
    """Return True if 'path' exists and is readable and executable."""
    return os.path.exists(path) and os.access(path, os.R_OK | os.X_OK)


@contextmanager
def requires_command_success(command: str, not_found_fmt: str=None,
                             failure_fmt: str=None) -> Generator:
    if isinstance(command, str):
        cmd_list = command.split()
    else:
        raise TypeError('command must be a string.')
    kwargs = dict(command=command, cmd_list=cmd_list)
    try:
        subprocess.check_call(
            cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        if not_found_fmt is not None:
            kwargs['fmt'] = not_found_fmt
        raise RequiredCommandNotFound(**kwargs)
    except subprocess.CalledProcessError:
        if failure_fmt is not None:
            kwargs['fmt'] = failure_fmt
        raise RequiredCommandFailure(**kwargs)
    yield


@contextmanager
def requires_path_exists(path: str,
                         error_fmt: str=None) -> Generator:
    if not os.path.exists(path):
        kwargs = dict(path=path)
        if error_fmt is not None:
            kwargs['fmt'] = error_fmt
        raise RequiredPathDoesNotExist(**kwargs)
    yield


def calculate_sha3_384(path: str) -> str:
    """Calculate sha3 384 hash, reading the file in 1MB chunks."""
    return calculate_hash(path, algorithm='sha3_384')


def calculate_hash(path: str, *, algorithm: str) -> str:
    """Calculate the hash for path with algorithm."""
    # This will raise an AttributeError if algorithm is unsupported
    hasher = getattr(hashlib, algorithm)()

    blocksize = 2**20
    with open(path, 'rb') as f:
        while True:
            buf = f.read(blocksize)
            if not buf:
                break
            hasher.update(buf)
    return hasher.hexdigest()


def get_tool_path(command_name):
    """Return the path to the given command

    By default this utilizes the PATH, but if Snapcraft is running out of the
    snap or out of Docker, it ensures it's using the one in the snap, not the
    host.

    :return: Path to command
    :rtype: str
    """
    path = command_name

    if common.is_snap():
        path = _command_path_in_root(os.getenv('SNAP'), command_name)
    elif common.is_docker_instance():
        path = _command_path_in_root(os.path.join(
            os.sep, 'snap', 'snapcraft', 'current'), command_name)

    if path:
        return path
    else:
        return command_name


def _command_path_in_root(root, command_name):
    for bin_directory in (os.path.join('usr', 'local', 'sbin'),
                          os.path.join('usr', 'local', 'bin'),
                          os.path.join('usr', 'sbin'),
                          os.path.join('usr', 'bin'),
                          os.path.join('sbin'),
                          os.path.join('bin')):
        path = os.path.join(root, bin_directory, command_name)
        if os.path.exists(path):
            return path
