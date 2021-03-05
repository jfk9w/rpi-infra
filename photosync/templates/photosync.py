#!/usr/bin/python3

import errno
import fcntl
import hashlib
import logging
import re
import sys
from datetime import datetime, timedelta
from os import listdir, remove, umask
from os.path import join, isdir, getmtime
from pathlib import Path
from shutil import copyfile, copytree, rmtree


def get_source_dirs():
    source_dirs = {}
    if len(sys.argv) > 1:
        source_dirs[sys.argv[1]] = sys.argv[2]
    else:
        source_dir = "{{ photosync.source.path }}".split("/*/")
        for entry in listdir(source_dir[0]):
            dirpath = join(source_dir[0], entry, source_dir[1])
            if not isdir(dirpath):
                continue
            source_dirs[dirpath] = entry
            logging.info("Adding %s with suffix %s to the list of sources", dirpath, entry)
    return source_dirs


def get_source_ttl_days():
    return int("{{ photosync.source.ttl_days }}")


def get_hash_check_days():
    return int("{{ photosync.hash.check_days | default(30) }}")


class FileTooYoung(BaseException):

    def __init__(self, path):
        self.path = path

    def __str__(self):
        return "File %s is too young" % self.path


def checksum(path):
    if isdir(path):
        return None

    hash = hashlib.new("{{ photosync.hash.mode | default('md5') }}")
    with open(path, "rb") as f:
        while True:
            data = f.read()
            if not data:
                break
            hash.update(data)
    return hash.hexdigest()


def sync_photos():
    camera_filename_re = re.compile(r"^.*?(?P<datetime>\d\d\d\d-?\d\d-?\d\d[-_]?\d\d-?\d\d-?\d\d).*?(?P<extension>\.[a-zA-Z0-9]+)?$")
    synced_files = {}
    expiration_datetime = datetime.now() - timedelta(days=get_source_ttl_days())
    earliest_hash_check = datetime.now() - timedelta(days=get_hash_check_days())
    for source_dir, suffix in get_source_dirs().items():
        for entry in listdir(source_dir):
            source_file = join(source_dir, entry)
            try:
                if entry.startswith("."):
                    continue
                entry_mod_datetime = datetime.fromtimestamp(getmtime(source_file))
                if datetime.now() - timedelta(hours=1) < entry_mod_datetime:
                    raise FileTooYoung(source_file)
                match = camera_filename_re.match(entry)
                entry_datetime = datetime.strptime(re.sub(r"[^\d]", "", match.groupdict()["datetime"]), "%Y%m%d%H%M%S")
                entry_extension = match.groupdict().get("extension")
                if not entry_extension:
                    entry_extension = ""
                    entry_name = entry
                else:
                    entry_name = entry[:-len(entry_extension)]
                entry_date = entry_datetime.strftime("%Y-%m-%d")
                target_date_dir = join("{{ photosync.target.path }}", str(entry_datetime.year), entry_date)
                target_entry = entry_name + "_" + suffix + entry_extension
                target_file = join(target_date_dir, target_entry)
                Path(target_date_dir).mkdir(parents=True, exist_ok=True)
                if not synced_files.get(entry_date):
                    synced_files[entry_date] = set(listdir(target_date_dir))
                while True:
                    if target_entry not in synced_files[entry_date]:
                        try:
                            copytree(source_file, target_file)
                        except OSError as e:
                            if e.errno == errno.ENOTDIR:
                                copyfile(source_file, target_file)
                            else:
                                raise
                        logging.info("Copied %s to %s", source_file, target_file)
                    if entry_datetime < earliest_hash_check:
                        break
                    source_checksum = checksum(source_file)
                    target_checksum = checksum(target_file)
                    if source_checksum == target_checksum:
                        logging.debug("Skipping %s since it's already present at %s",
                                      source_file, target_file)
                        break
                    else:
                        logging.warning("%s checksum [%s] does not match %s [%s]",
                                        source_file, source_checksum,
                                        target_file, target_checksum)
                        remove(target_file)
                if expiration_datetime > entry_datetime:
                    try:
                        remove(source_file)
                    except PermissionError as e:
                        rmtree(source_file)
                    logging.info("Removed %s since it expired (expiration date %s)" % ((source_file, expiration_datetime)))
            except Exception as e:
                logging.error("An exception occurred while processing %s: %s", source_file, e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    f = open("{{ svc_dir }}/.LOCKFILE", "w")
    try:
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError as e:
        if e.errno == errno.EAGAIN:
            logging.warn("Another instance of the job is running")
            sys.exit(1)
        raise
    old_umask = umask(0o002)
    try:
        sync_photos()
    finally:
        umask(old_umask)
