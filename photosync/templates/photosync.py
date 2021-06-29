#!/usr/bin/python3

import argparse
import checksumdir
import docker
import errno
import fcntl
import glob
import hashlib
import logging
import re
import random
import string
import sys
from codecs import decode
from contextlib import ContextDecorator, contextmanager
from datetime import datetime, timedelta
from os import listdir, remove, umask
from os.path import join, isdir, getmtime, basename, dirname, exists
from pathlib import Path
from shutil import copyfile, copytree, rmtree, move

@contextmanager
def task_context(lock_file, 
                 log_level=logging.INFO, 
                 log_format="%(asctime)s %(levelname)-8s %(message)s"):
    logging.basicConfig(level=log_level, format=log_format)
    lock_dir = dirname(lock_file)
    Path(lock_dir).mkdir(parents=True, exist_ok=True)
    f = open(lock_file, "w")
    try:
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError as e:
        if e.errno == errno.EAGAIN:
            logging.warn("Another instance of the job is running")
            sys.exit(1)
        raise
    old_umask = umask(0o002)
    try:
        yield
    finally:
        umask(old_umask)
        remove(lock_file)

class FileSystem(ContextDecorator):

    def __init__(self):
        pass

    def copy(self, source_path, target_path, target_fs=None):
        if not target_fs:
            target_fs = self
        self._copy(source_path, target_fs, target_path)

    def _copy(self, source_path, target_fs, target_path):
        pass
    
    def listdir(self, path):
        pass

    def isdir(self, path):
        pass

    def rename(self, source_path, target_path):
        pass
    
    def remove(self, path):
        pass
    
    def exists(self, path):
        pass
    
    def getmtime(self, path):
        pass

class UnixFileSystem(FileSystem):

    def __init__(self, temp_dir):
        super().__init__()
        self.temp_dir = temp_dir

    def __enter__(self):
        self.remove(self.temp_dir)
        Path(self.temp_dir).mkdir(parents=True, exist_ok=True)
        return self
    
    def __exit__(self, exc_type, exc_value, tb):
        self.remove(self.temp_dir)
        if exc_type:
            raise exc_value
        return True

    def _copy(self, source_path, target_fs, target_path):
        if not isinstance(target_fs, UnixFileSystem):
            raise NotImplementedError()
        if target_fs.exists(target_path):
            raise FileExistsError()
        temp_path = join(target_fs.temp_dir, "".join(random.choices(string.ascii_letters + string.digits, k=16)))
        hash_func = checksumdir.dirhash if self.isdir(source_path) else UnixFileSystem._hash_file
        copy_func = copytree if self.isdir(source_path) else copyfile
        copy_func(source_path, temp_path)
        try:
            source_hash = hash_func(source_path)
            temp_hash = hash_func(temp_path)
            if source_hash != temp_hash:
                raise Exception(f"{source_path} hash [{source_hash}] != {temp_path} hash [{temp_hash}]")
            self.rename(temp_path, target_path)
        finally:
            self.remove(temp_path)

    @staticmethod
    def _hash_file(path):
        hash = hashlib.new("md5")
        with open(path, "rb") as f:
            while True:
                data = f.read()
                if not data:
                    break
                hash.update(data)
        return hash.hexdigest()

    def rename(self, source_path, target_path):
        target_dir = dirname(target_path)
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        if self.exists(target_path):
            raise FileExistsError()
        move(source_path, target_path)
    
    def remove(self, path):
        if not self.exists(path):
            return
        if self.isdir(path):
            rmtree(path)
        else:
            remove(path)

    def listdir(self, path):
        return listdir(path)

    def isdir(self, path):
        return isdir(path)
    
    def exists(self, path):
        return exists(path)
    
    def getmtime(self, path):
        return datetime.fromtimestamp(getmtime(path))

class ReadOnlyUnixFileSystem(UnixFileSystem):

    def __init__(self):
        super().__init__(temp_dir=None)
    
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if exc_type:
            raise exc_value
        return True

    def _copy(self, source_path, target_fs, target_path):
        logging.info(f"[NOOP] Copy {source_path} to {target_path}")
    
    def rename(self, source_path, target_path):
        logging.info(f"[NOOP] Rename {source_path} to {target_path}")
    
    def remove(self, path):
        logging.info(f"[NOOP] Remove {path}")

def drop_prefix(str, prefix):
    for i in range(0, len(str)):
        if str[i] != prefix[i]:
            return str[i:]

def drop_suffix(str, suffix):
    str = drop_prefix(str[::-1], suffix[::-1])
    if str:
        return str[::-1]

def get_feature(str, mask):
    str = drop_prefix(str, mask)
    if str:
        str = drop_suffix(str, mask)
    return str

def get_source_paths(source_path):
    paths = glob.glob(source_path)
    source_paths = {}
    for path in paths:
        feature = get_feature(path, source_path)
        if feature:
            feature = "_" + feature.replace("/", "_")
        else:
            feature = ""
        source_paths[feature] = path
    return source_paths

def photoprism_index(container_name="photoprism"):
    photoprism = docker.from_env().containers.get(container_name)
    if not photoprism:
        logging.warning(f"{container_name} container not found")
        return
    logging.info("RUN photoprism index")
    exit_code, (stdout, stderr) = photoprism.exec_run(cmd=["photoprism", "index"], demux=True)
    if stdout:
        logging.info(f"\n{decode(str(stdout), 'unicode-escape')}")
    if stderr:
        logging.error(f"\n{decode(str(stderr), 'unicode-escape')}")
    if exit_code != 0:
        logging.error(f"ERROR photoprism index {exit_code}")

class PhotoSync(object):

    re = re.compile(r"^.*?(?P<datetime>20\d\d-?[0-1]\d-?[0-3]\d[-_]?[0-2]\d-?[0-5]\d-?[0-5]\d).*?(?P<extension>\.[a-zA-Z0-9]+)?$")

    def __init__(self, 
                 fs: FileSystem, 
                 source_path, target_dir, 
                 for_last=timedelta(days=3),
                 till_last=timedelta(minutes=10),
                 ttl=timedelta(days=90)):
        self.now = datetime.now()
        self.since = self.now - for_last
        self.till = self.now - till_last
        self.expire = self.now - ttl
        self.fs = fs
        self.source_paths = get_source_paths(source_path)
        self.target_dir = target_dir

    def run(self):
        synced = 0
        for suffix, source_path in self.source_paths.items():
            logging.info(f"SYNC {source_path} {suffix}")
            synced += self.sync(source_path, suffix)
        if synced:
            photoprism_index()

    def sync(self, source_path, suffix):
        mtime = self.fs.getmtime(source_path)
        if not self.fs.isdir(source_path):
            if mtime < self.expire:
                self.fs.remove(source_path)
                logging.info(f"EXPIRE {source_path}")
                return 0
            if mtime < self.since:
                logging.debug(f"TOO OLD {source_path}")
                return 0
            if mtime >= self.till:
                logging.debug(f"TOO YOUNG {source_path}")
                return 0

        source_name = basename(source_path)
        if source_name.startswith("."):
            return 0

        try:
            match = PhotoSync.re.match(source_name)
            if not match:
                raise Exception(f"{source_name} does not match pattern")
            dt_str = re.sub(r"[^\d]", "", match.groupdict()["datetime"])
            dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S")
            ext = match.groupdict().get("extension")
            name = source_name[:-len(ext)] if ext else source_name
            target_path = join(
                self.target_dir,
                str(dt.year),
                dt.strftime("%Y-%m-%d"),
                name + suffix + (ext if ext else ""))
            if self.fs.exists(target_path):
                logging.debug(f"PRESENT {target_path} <- {source_path}")
                return 0
            self.fs.copy(source_path, target_path)
            logging.info(f"COPY {target_path} <- {source_path}")
            return 1
        except BaseException as e:
            if not self.fs.isdir(source_path):
                logging.warning(f"ERROR {source_path} {e}")
                return 0

        if source_path not in self.source_paths.values():
            suffix = f"{suffix}_{source_name}"

        logging.info(f"SCAN {source_path}")
        synced = 0
        for entry_name in self.fs.listdir(source_path):
            entry_path = join(source_path, entry_name)
            synced += self.sync(entry_path, suffix)
        
        return synced

if __name__ == "__main__":
    argparser = argparse.ArgumentParser()
    argparser.add_argument("--source")
    argparser.add_argument("--target")
    argparser.add_argument("--temp")
    args = argparser.parse_args()
    lock_file = args.temp + ".lock"
    with task_context(lock_file, log_format="%(message)s"):
        with UnixFileSystem(args.temp) as fs:
            PhotoSync(fs, args.source, args.target).run()