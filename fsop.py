# fsop.py - module containing filesystem operation types and common code for them

# NOTE: this version requires "numpy" rpm to be installed
# std python modules
import os
import os.path
import random
import errno
import random_buffer
import numpy  # for gaussian distribution
import subprocess

# my modules
import common
from common import rq, FileAccessDistr, FileSizeDistr
from common import OK, NOTOK, BYTES_PER_KiB, FD_UNDEFINED, FsDriftException
from fsop_counters import FSOPCounters

link_suffix = '.s'
hlink_suffix = '.h'
rename_suffix = '.r'

large_prime = 12373

class FSOPCtx:

    opname_to_opcode = {
         "read":            rq.READ,
         "random_read":     rq.RANDOM_READ,
         "create":          rq.CREATE,
         "random_write":    rq.RANDOM_WRITE,
         "append":          rq.APPEND,
         "softlink":        rq.SOFTLINK,
         "hardlink":        rq.HARDLINK,
         "delete":          rq.DELETE,
         "rename":          rq.RENAME,
         "truncate":        rq.TRUNCATE,
         "remount":         rq.REMOUNT,
         "readdir":         rq.READDIR
    }
    
    opcode_to_opname = {
         rq.READ:           "read",
         rq.RANDOM_READ:    "random_read",
         rq.CREATE:         "create",
         rq.RANDOM_WRITE:   "random_write",
         rq.APPEND:         "append",
         rq.SOFTLINK:       "softlink",
         rq.HARDLINK:       "hardlink",
         rq.DELETE:         "delete",
         rq.RENAME:         "rename",
         rq.TRUNCATE:       "truncate",
         rq.REMOUNT:        "remount",
         rq.READDIR:        "readdir"
    }

    # for gaussian distribution with moving mean, we need to remember simulated time
    # so we can pick up where we left off with moving mean

    SIMULATED_TIME_UNDEFINED = None
    time_save_rate_default = 5  # make this 60 later on

    def __init__(self, params, log, ctrs, onhost, tid):
        self.ctrs = ctrs
        self.params = params
        self.log = log
        self.onhost = onhost
        self.tid = tid
        self.buf = random_buffer.gen_buffer(params.max_record_size_kb*BYTES_PER_KiB)
        self.total_dirs = 1
        self.verbosity = self.params.verbosity 
        for i in range(0, self.params.levels):
            self.total_dirs *= self.params.subdirs_per_dir
        self.max_files_per_dir = self.params.max_files // self.total_dirs
        # most recent center
        self.center = self.params.max_files * random.random() * 0.99
        self.velocity = self.params.mean_index_velocity * 2 * random.random()
        self.simulated_time = FSOPCtx.SIMULATED_TIME_UNDEFINED  # initialized later
        self.time_save_rate = FSOPCtx.time_save_rate_default
        self.simtime_pathname = os.path.join(self.params.network_shared_path, 
                                    'fs-drift-simtime-hst-%s-thrd-%s.tmp' % (self.onhost, self.tid))
        self.fs_fullness = 0.0
        self.fs_stats = None
        self.get_fs_stats()
        self._rqmap = {
            rq.READ:        self.op_read,
            rq.RANDOM_READ: self.op_random_read,
            rq.CREATE:      self.op_create,
            rq.RANDOM_WRITE: self.op_random_write,
            rq.APPEND:      self.op_append,
            rq.SOFTLINK:    self.op_softlink,
            rq.HARDLINK:    self.op_hardlink,
            rq.DELETE:      self.op_delete,
            rq.RENAME:      self.op_rename,
            rq.TRUNCATE:    self.op_truncate,
            rq.REMOUNT:     self.op_remount,
            rq.READDIR:     self.op_readdir,
            }

    # clients invoke functions by workload request type code
    # instead of by function name, using this:

    def invoke_rq(self, rqcode):
        return self._rqmap[rqcode]()

    def scallerr(self, msg, fn, syscall_exception, fd=None):
        self.log.exception(syscall_exception)
        try:
            err = syscall_exception.errno
            if fd == None:
                self.log.error('%s: %s syscall errno %d(%s)' % (
                                msg, fn, err, os.strerror(err)))
            else:
                self.log.error('%s: %s syscall errno %d(%s) fd=%s' % (
                                msg, fn, err, os.strerror(err), str(fd)))
        except Exception:
            self.log.error('non-OSError exception %s: %s' % (msg, fn))
        return NOTOK

    # every so often, update filesystem stats using this call

    def get_fs_stats(self):
        self.fs_stats = os.statvfs(self.params.top_directory)
        self.fs_fullness = (self.fs_stats.f_blocks - self.fs_stats.f_bfree)/float(self.fs_stats.f_blocks)

    def fs_is_full(self):
        if self.fs_fullness * 100.0 > self.params.fullness_limit_pct:
            return True
        return False

    def get_file_size(self, fd):
        stat_info = os.fstat(fd)
        sz = stat_info.st_size
        if sz < 0:
            raise FsDriftException('negative file size %d seen on fd %d' % (sz, fd))
        return sz


    # use the most significant portion of the file_index
    # for the dirname, and the least significant portion for
    # the filename within the directory.

    def gen_random_dirname(self, file_index):
        subdirs_per_dir = self.params.subdirs_per_dir
        d = '.'
        # divide by max_files_per_dir so that we're computing
        # the directory path on a different part of the 
        # random number than the part used to compute the filename
        index = file_index // self.max_files_per_dir
        for j in range(0, self.params.levels):
            subdir_index = 1 + (index % subdirs_per_dir)
            dname = 'd%04d' % subdir_index
            d = os.path.join(d, dname)
            index /= subdirs_per_dir
        return d


    def read_num_from_file(f):
        return f.readline().strip()

    def gen_random_fn(self, is_create=False):
        if self.params.random_distribution == FileAccessDistr.uniform:
            # lower limit 0 means at least 1 file/dir
            index = random.randint(0, self.params.max_files)
        elif self.params.random_distribution == FileAccessDistr.gaussian:
    
            # if simulated time is not defined,
            # attempt to read it in from a file, set to zero if no file
    
            if self.simulated_time == FSOPCtx.SIMULATED_TIME_UNDEFINED:
                try:
                    with open(self.simtime_pathname, 'r') as readtime_fd:
                        version = int(read_num_from_file(readtime_fd))
                        if version != 1: 
                            raise FsDriftException('unrecognized version %d in simtime file' % version)
                        self.simulated_time = int(read_num_from_file(readtime_fd))
                        self.center = float(read_num_from_file(readtime_fd))
                        self.velocity = float(read_num_from_file(readtime_fd))
                except IOError as e:
                    if e.errno != errno.ENOENT:
                        raise e
                    self.simulated_time = 0
                self.center = self.center + (self.simulated_time * self.velocity)
                self.log.info('resuming with simulated time %d' % self.simulated_time)
    
            # for creates, use greater time, so that reads, etc. will "follow" creates most of the time
            # mean and std deviation define gaussian distribution
    
            self.center += self.velocity
            if is_create:
                self.center += (self.params.create_stddevs_ahead * self.params.gaussian_stddev)
            if self.verbosity & 0x20:
                self.log.debug('%f = center' % self.center)
            index_float = numpy.random.normal(
                loc=self.center, scale=self.params.gaussian_stddev)
            self.log.debug('index_float = %f' % index_float)
            file_opstr = 'read'
            if is_create:
                file_opstr = 'create'
            if self.verbosity & 0x20:
                self.log.debug('%s gaussian value is %f' % (file_opstr, index_float))
            index = int(index_float) % self.params.max_files
    
            # since this is a time-varying distribution, record the time every so often
            # so we can pick up where we left off
    
            if self.params.drift_time == -1:
                self.simulated_time += 1
            if self.simulated_time % self.time_save_rate == 0:
                simtime_dir = os.path.dirname(self.simtime_pathname)
                if not os.path.exists(simtime_dir):
                    os.makedirs(simtime_dir)
                with open(self.simtime_pathname, 'w') as time_fd:
                    time_fd.write('1\n') # version
                    time_fd.write('%10d\n' % self.simulated_time)
                    time_fd.write('%f\n' % self.center)
                    time_fd.write('%f\n' % self.velocity)
        else:
            raise FsDriftException('invalid distribution type %d' % self.params.random_distribution)
        if self.verbosity & 0x20:
            self.log.debug('next file index %u out of %u' % (index, self.max_files_per_dir))
        dirpath = self.gen_random_dirname(index)
        fn = os.path.join(dirpath, 'f%09d' % index)
        if self.verbosity & 0x20:
            self.log.debug('next pathname %s' % fn)
        return fn


    def random_file_size(self):
        return random.randint(0, self.params.max_file_size_kb * BYTES_PER_KiB)


    def random_record_size(self):
        return random.randint(1, self.params.max_record_size_kb * BYTES_PER_KiB)


    def random_segment_size(self, filesz):
        segsize = 2 * self.random_record_size()
        if segsize > filesz:
            segsize = filesz//7
        return segsize

    def random_seek_offset(self, filesz):
        return random.randint(0, filesz)


    def try_to_close(self, closefd, filename):
        if closefd != FD_UNDEFINED:
            try:
                os.close(closefd)
            except OSError as e:
                if self.params.tolerate_stale_fh and e.errno == errno.ESTALE:
                    self.ctrs.e_stale_fh += 1
                    return OK
                return self.scallerr('close', filename, e, fd=closefd)
        return OK

    def op_read(self):
        c = self.ctrs
        fd = FD_UNDEFINED
        fn = self.gen_random_fn()
        try:
            if self.verbosity & 0x20000:
                self.log.debug('read file %s' % fn)
            fd = os.open(fn, os.O_RDONLY)
            fsz = self.get_file_size(fd)
            if self.verbosity & 0x4000:
                self.log.debug('read file sz %u' % fsz)
            total_read = 0
            while total_read < fsz:
                rdsz = self.random_record_size()
                bytes = os.read(fd, rdsz)
                count = len(bytes)
                if count < 1:
                    break
                c.read_requests += 1
                c.read_bytes += count
                if self.verbosity & 0x4000:
                    self.log.debug('seq. read off %u sz %u got %u' %\
                        (total_read, rdsz, count))
                total_read += len(bytes)
            c.have_read += 1
        except OSError as e:
            if e.errno == errno.ENOENT:
                c.e_file_not_found += 1
            elif e.errno == errno.ESTALE and self.params.tolerate_stale_fh:
                c.e_stale_fh += 1
                return NOTOK
            else:
                return self.scallerr('op_read', fn, e, fd=fd)
        self.try_to_close(fd, fn)
        return OK

    def op_random_read(self):
        c = self.ctrs
        fd = FD_UNDEFINED
        fn = self.gen_random_fn()
        try:
            total_read_reqs = 0
            target_read_reqs = random.randint(0, self.params.max_random_reads)
            if self.verbosity & 0x20000:
                self.log.debug('randread %s reqs %u' % (fn, target_read_reqs))
            fd = os.open(fn, os.O_RDONLY)
            fsz = self.get_file_size(fd)
            if self.verbosity & 0x2000:
                self.log.debug('randread filesize %u reqs %u' % (
                    fsz, target_read_reqs))
            while total_read_reqs < target_read_reqs:
                off = os.lseek(fd, self.random_seek_offset(fsz), 0)
                rdsz = self.random_segment_size(fsz)
                if self.verbosity & 0x2000:
                    self.log.debug('randread off %u sz %u' % (off, rdsz))
                total_count = 0
                remaining_sz = fsz - off
                while total_count < rdsz:
                    recsz = self.random_record_size()
                    if recsz + total_count > remaining_sz:
                        recsz = remaining_sz - total_count
                    elif recsz + total_count > rdsz:
                        recsz = rdsz - total_count
                    if recsz == 0:
                        break
                    bytebuf = os.read(fd, recsz)
                    count = len(bytebuf)
                    if count < 1:
                        break
                    if self.verbosity & 0x2000:
                        self.log.debug('randread recsz %u count %u' % (recsz, count))
                    total_count += count
                    c.randread_bytes += count
                total_read_reqs += 1
                c.randread_requests += 1
            c.have_randomly_read += 1
        except OSError as e:
            if e.errno == errno.ENOENT:
                c.e_file_not_found += 1
            elif e.errno == errno.ESTALE and self.params.tolerate_stale_fh:
                c.e_stale_fh += 1
                return NOTOK
            else:
                return self.scallerr('random_read', fn, e, fd=fd)
        self.try_to_close(fd, fn)
        return OK


    def maybe_fsync(self, fd):
        c = self.ctrs
        percent = random.randint(0, 100)
        if percent > self.params.fsync_probability_pct + self.params.fdatasync_probability_pct:
            return
        elif percent > self.params.fsync_probability_pct:
            c.fdatasyncs += 1
            os.fdatasync(fd)
        else:
            c.fsyncs += 1
            os.fsync(fd)

    def op_create(self):
        if self.fs_is_full():
            self.log.debug('filesystem full, disabling create')
            return OK
        c = self.ctrs
        fd = FD_UNDEFINED
        fn = self.gen_random_fn(is_create=True)
        target_sz = self.random_file_size()
        if self.verbosity & 0x1000:
            self.log.debug('create %s sz %s' % (fn, target_sz))
        subdir = os.path.dirname(fn)
        if not os.path.isdir(subdir):
            try:
                os.makedirs(subdir)
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    c.e_no_dir_space += 1
                    return OK
                elif e.errno != errno.EEXIST:
                    return self.scallerr('dir create', fn, e)
            c.dirs_created += 1
        try:
            fd = os.open(fn, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            total_sz = 0
            while total_sz < target_sz:
                recsz = self.random_record_size()
                if recsz + total_sz > target_sz:
                    recsz = target_sz - total_sz
                count = os.write(fd, self.buf[0:recsz])
                assert count == recsz 
                if self.verbosity & 0x1000:
                    self.log.debug('create sz %u written %u' % (recsz, count))
                total_sz += count
                c.write_requests += 1
                c.write_bytes += count
            rc = self.maybe_fsync(fd)
            c.have_created += 1
        except OSError as e:
            if e.errno == errno.EEXIST:
                c.e_already_exists += 1
            elif e.errno == errno.ENOSPC:
                if fd == FD_UNDEFINED:
                    c.e_no_inode_space += 1
                else:
                    c.e_no_space += 1
            elif e.errno == errno.ESTALE and self.params.tolerate_stale_fh:
                c.e_stale_fh += 1
                return NOTOK
            else:
                return self.scallerr('create', fn, e, fd=fd)
        self.try_to_close(fd, fn)
        return OK


    def op_append(self):
        fd = FD_UNDEFINED
        if self.fs_is_full():
            self.log.debug('filesystem full, disabling append')
            return OK
        c = self.ctrs
        fn = self.gen_random_fn()
        target_sz = self.random_file_size()
        if self.verbosity & 0x8000:
            self.log.debug('append %s sz %s' % (fn, target_sz))
        try:
            fd = os.open(fn, os.O_WRONLY)
            total_appended = 0
            while total_appended < target_sz:
                recsz = self.random_record_size()
                if recsz + total_appended > target_sz:
                    recsz = target_sz - total_appended
                assert recsz > 0
                if self.verbosity & 0x8000:
                    self.log.debug('append rsz %u' % (recsz))
                count = os.write(fd, self.buf[0:recsz])
                assert count == recsz
                total_appended += count
                c.write_requests += 1
                c.write_bytes += count
            rc = self.maybe_fsync(fd)
            c.have_appended += 1
        except OSError as e:
            if e.errno == errno.ENOENT:
                c.e_file_not_found += 1
            elif e.errno == errno.ENOSPC:
                c.e_no_space += 1
            elif e.errno == errno.ESTALE and self.params.tolerate_stale_fh:
                c.e_stale_fh += 1
                return NOTOK
            else:
                return self.scallerr('append', fn, e, fd=fd)
        self.try_to_close(fd, fn)
        return OK


    def op_random_write(self):
        c = self.ctrs
        fd = FD_UNDEFINED
        fn = self.gen_random_fn()
        try:
            total_write_reqs = 0
            target_write_reqs = random.randint(0, self.params.max_random_writes)
            if self.verbosity & 0x20000:
                self.log.debug('randwrite %s reqs %u' % (fn, target_write_reqs))
            fd = os.open(fn, os.O_WRONLY)
            fsz = self.get_file_size(fd)
            while total_write_reqs < target_write_reqs:
                off = os.lseek(fd, self.random_seek_offset(fsz), 0)
                total_count = 0
                wrsz = self.random_segment_size(fsz)
                if self.verbosity & 0x20000:
                    self.log.debug('randwrite off %u sz %u' % (off, wrsz))
                while total_count < wrsz:
                    recsz = self.random_record_size()
                    if recsz + total_count > wrsz:
                        recsz = wrsz - total_count
                    count = os.write(fd, self.buf[0:recsz])
                    if self.verbosity & 0x20000:
                        self.log.debug('randwrite count=%u recsz=%u' % (count, recsz))
                    assert count == recsz
                    total_count += count
                total_write_reqs += 1
                c.randwrite_requests += 1
                c.randwrite_bytes += total_count
                rc = self.maybe_fsync(fd)
            c.have_randomly_written += 1
        except OSError as e:
            if e.errno == errno.ENOENT:
                c.e_file_not_found += 1
            elif e.errno == errno.ENOSPC:
                c.e_no_space += 1
            elif e.errno == errno.ESTALE and self.params.tolerate_stale_fh:
                c.e_stale_fh += 1
                return NOTOK
            else:
                return self.scallerr('random write', fn, e, fd=fd)
        self.try_to_close(fd, fn)
        return OK


    def op_truncate(self):
        c = self.ctrs
        fd = FD_UNDEFINED
        s = OK
        fn = self.gen_random_fn()
        if self.verbosity & 0x40000:
            self.log.debug('truncate %s' % fn)
        try:
            new_file_size = self.random_file_size()/3
            fd = os.open(fn, os.O_RDWR)
            os.ftruncate(fd, new_file_size)
            c.have_truncated += 1
        except OSError as e:
            if e.errno == errno.ENOENT:
                c.e_file_not_found += 1
            elif e.errno == errno.ESTALE and self.params.tolerate_stale_fh:
                c.e_stale_fh += 1
                return NOTOK
            else:
                return self.scallerr('truncate', fn, e, fd=fd)
        self.try_to_close(fd, fn)
        return OK


    def op_softlink(self):
        if self.fs_is_full():
            self.log.debug('filesystem full, disabling softlink')
            return OK
        c = self.ctrs
        fn = os.getcwd() + os.sep + self.gen_random_fn()
        fn2 = self.gen_random_fn() + link_suffix
        if self.verbosity & 0x10000:
            self.log.debug('link to %s from %s' % (fn, fn2))
        if not os.path.isfile(fn):
            c.e_file_not_found += 1
            return OK
        try:
            rc = os.symlink(fn, fn2)
            c.have_softlinked += 1
        except OSError as e:
            if e.errno == errno.EEXIST:
                c.e_already_exists += 1
            elif e.errno == errno.ENOENT:
                c.e_file_not_found += 1
            elif e.errno == errno.ENOSPC:
                c.e_no_inode_space += 1
            elif e.errno == errno.ESTALE and self.params.tolerate_stale_fh:
                c.e_stale_fh += 1
                return NOTOK
            else:
                return self.scallerr('softlink', fn, e)
        return OK


    def op_hardlink(self):
        if self.fs_is_full():
            self.log.debug('filesystem full, disabling hardlink')
            return OK
        c = self.ctrs
        fn = self.gen_random_fn()
        fn2 = self.gen_random_fn() + hlink_suffix
        if self.verbosity & 0x10000:
            self.log.debug('hard link to %s from %s' % (fn, fn2))
        if not os.path.isfile(fn):
            c.e_file_not_found += 1
            return OK
        try:
            rc = os.link(fn, fn2)
            c.have_hardlinked += 1
        except OSError as e:
            if e.errno == errno.EEXIST:
                c.e_already_exists += 1
            elif e.errno == errno.ENOENT:
                c.e_file_not_found += 1
            elif e.errno == errno.ENOSPC:
                c.e_no_inode_space += 1
            elif e.errno == errno.ESTALE and self.params.tolerate_stale_fh:
                c.e_stale_fh += 1
                return NOTOK
            else:
                return self.scallerr('hardlink', fn, e)
        return OK


    def op_delete(self):
        c = self.ctrs
        fn = self.gen_random_fn()
        if self.verbosity & 0x20000:
            self.log.debug('delete %s' % (fn))
        try:
            linkfn = fn + link_suffix
            if os.path.exists(linkfn):
                if self.verbosity & 0x20000:
                    self.log.debug('delete soft link %s' % (linkfn))
                os.unlink(linkfn)
            else:
                c.e_file_not_found += 1
            hlinkfn = fn + hlink_suffix
            if os.path.exists(hlinkfn):
                if self.verbosity & 0x20000:
                    self.log.debug('delete hard link %s' % (hlinkfn))
                os.unlink(hlinkfn)
            else:
                c.e_file_not_found += 1
            if self.verbosity & 0x20000:
                self.log.debug('delete file %s' % fn)
            os.unlink(fn)
            c.have_deleted += 1
        except OSError as e:
            if e.errno == errno.ENOENT:
                c.e_file_not_found += 1
            elif e.errno == errno.ESTALE and self.params.tolerate_stale_fh:
                c.e_stale_fh += 1
                return NOTOK
            else:
                self.scallerr('delete', fn, e)
        return OK


    def op_rename(self):
        c = self.ctrs
        fn = self.gen_random_fn()
        fn2 = self.gen_random_fn()
        if self.verbosity & 0x20000:
            self.log.debug('rename %s to %s' % (fn, fn2))
        try:
            os.rename(fn, fn2)
            c.have_renamed += 1
        except OSError as e:
            if e.errno == errno.ENOENT:
                c.e_file_not_found += 1
            elif e.errno == errno.EEXIST:
                c.e_already_exists += 1
            elif e.errno == errno.ENOSPC:
                c.e_no_inode_space += 1
            elif e.errno == errno.ESTALE and self.params.tolerate_stale_fh:
                c.e_stale_fh += 1
                return NOTOK
            else:
                return self.scallerr('rename', fn, e)
        return OK


    # unmounting is so risky that we shouldn't try to figure it out
    # make the user tell us the entire mount command
    # we will get mountpoint from last token on the command line
    # assumption: mountpoint comes last on the mount command
    
    def op_remount(self):
        c = self.ctrs
        if self.params.mount_command == None:
            raise FsDriftException('you did not specify mount command for remount option')
        if self.verbosity & 0x40000:
            self.log.debug('remount: %s' % self.params.mount_command)
        mountpoint = self.params.mount_command.split()[-1].strip()
        if not self.params.top_directory.startswith(mountpoint):
            raise common.FsDriftException(
                    'mountpoint %s does not contain topdir %s' % 
                    (mountpoint, topdir))
        with open('/proc/mounts', 'r') as mount_f:
            mounts = [ l.strip().split() for l in mount_f.readlines() ]
        mount_entry = None
        for m in mounts:
            if m[1] == mountpoint:
                mount_entry = m
                break
        if mount_entry == None:
            c.e_not_mounted += 1
        else:
            os.chdir('/tmp')
            rc = os.system('umount %s' % mountpoint)
            if rc != OK:
                c.e_could_not_unmount += 1
                return rc
        rc = os.system(self.params.mount_command)
        if rc != OK:
            c.e_could_not_mount += 1
            return rc
        c.have_remounted += 1
        return OK

    def op_readdir(self):
        c = self.ctrs
        fn = self.gen_random_fn()
        dirpath = os.path.dirname(fn)
        if self.verbosity & 0x20000:
            self.log.debug('readdir %s' % dirpath)
        try:
            dirlist = os.listdir(dirpath)
            c.have_readdir += 1
        except OSError as e:
            if e.errno == errno.ENOENT:
                c.e_dir_not_found += 1
            else:
                return self.scallerr('readdir', dirpath, e)
        return OK

# unit test

if __name__ == "__main__":
    import logging
    import opts
    import fsd_log
    options = opts.parseopts()
    log = fsd_log.start_log('fsop-unittest')
    log.info('hi there')
    if not options.top_directory.__contains__('/tmp/'):
        raise FsDriftException('bad top directory')
    os.system('rm -rf %s' % options.top_directory)
    os.makedirs(options.top_directory)
    os.chdir(options.top_directory)
    log.info('chdir to %s' % options.top_directory)
    ctrs = FSOPCounters()
    ctx = FSOPCtx(options, log, ctrs, 'test-host', 'test-tid')
    ctx.verbosity = -1
    rc = ctx.op_create()
    assert(rc == OK)
    rc = ctx.op_read()
    assert(rc == OK)
    rc = ctx.op_random_read()
    assert(rc == OK)
    rc = ctx.op_append()
    assert(rc == OK)
    rc = ctx.op_random_write()
    assert(rc == OK)
    rc = ctx.op_truncate()
    assert(rc == OK)
    rc = ctx.op_softlink()
    assert(rc == OK)
    rc = ctx.op_hardlink()
    assert(rc == OK)
    rc = ctx.op_delete()
    assert(rc == OK)
    rc = ctx.op_rename()
    assert(rc == OK)
    rc = ctx.op_readdir()
    assert(rc == OK)
    #rc = ctx.op_remount()
    #assert(rc != OK)

    # simulate a mixed-workload run
    for j in range(0, 200):
        for k in FSOPCtx.opcode_to_opname.keys():
            if k != rq.REMOUNT:
                rc = ctx.invoke_rq(k)
            assert(rc == OK)

    # output FSOPCounter object
    print(ctrs)
    ctrs2 = FSOPCounters()
    ctrs.add_to(ctrs2)
    ctrs.add_to(ctrs2)
    assert(ctrs2.have_read > 0 and ctrs2.have_read == 2 * ctrs.have_read)
    print(ctrs.json_dict())

