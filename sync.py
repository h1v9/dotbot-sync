import os
import platform
import re
import subprocess
import dotbot
from glob import glob

IS_WINDOWS = (platform.system().lower() == 'windows')
if not IS_WINDOWS:
    import pwd
    import grp

def _fix_windows_path_for_cwrsync(path):
    """
    Convert Windows-style paths like C:\\Users\\Name\\... into
    /cygdrive/c/Users/Name/... so that cwRsync doesn't interpret
    them as remote targets.
    """
    if not IS_WINDOWS:
        return path  # Not on Windows, no rewriting

    # 1) Normalize backslashes to forward slashes
    path = path.replace('\\', '/')

    # 2) Convert "C:/Users/..." => "/cygdrive/c/Users/..."
    match = re.match(r'^([A-Za-z]):/(.*)$', path)
    if match:
        drive_letter = match.group(1).lower()  # e.g. 'c'
        rest = match.group(2)                  # e.g. 'Users/Davide/...'
        path = f'/cygdrive/{drive_letter}/{rest}'

    return path

class Sync(dotbot.Plugin):
    '''
    Sync dotfiles using rsync (works with cwRsync on Windows).
    '''

    _directive = 'sync'

    def can_handle(self, directive):
        return directive == self._directive

    def handle(self, directive, data):
        if directive != self._directive:
            raise ValueError('Sync cannot handle directive %s' % directive)
        return self._process_records(data)

    @staticmethod
    def expand_path(path, globs=False):
        path = os.path.expanduser(path)
        path = os.path.expandvars(path)
        return glob(path) if globs else [path]

    def _chmodown(self, path, chmod, uid, gid):
        """Set file mode and ownership, skipping chown on Windows."""
        try:
            os.chmod(path, chmod)
        except Exception as e:
            self._log.warning(f"Failed to chmod {path} to {oct(chmod)}: {e}")

        if not IS_WINDOWS:
            try:
                os.chown(path, uid, gid)
            except Exception as e:
                self._log.warning(
                    f"Failed to chown {path} to uid={uid}, gid={gid}: {e}"
                )

    def _process_records(self, records):
        success = True
        defaults = self._context.defaults().get('sync', {})

        for destination, source in records.items():
            # Expand the single destination
            (destination,) = self.expand_path(destination, globs=False)

            # Get defaults
            rsync = defaults.get('rsync', 'rsync')
            options = defaults.get('options', ['--delete', '--safe-links'])
            create = defaults.get('create', False)
            fmode = defaults.get('fmode', 644)
            dmode = defaults.get('dmode', 755)

            if not IS_WINDOWS:
                default_owner = pwd.getpwuid(os.getuid()).pw_name
                default_group = grp.getgrgid(os.getgid()).gr_name
            else:
                # On Windows, just pick the current username or None
                default_owner = os.getlogin()
                default_group = None

            owner = defaults.get('owner', default_owner)
            group = defaults.get('group', default_group)

            # By default, show both stdout/stderr
            stdout = None
            stderr = None

            # If `source` is a dict, that means extended config
            if isinstance(source, dict):
                create = source.get('create', create)
                rsync = source.get('rsync', rsync)
                options = source.get('options', options)
                fmode = source.get('fmode', fmode)
                dmode = source.get('dmode', dmode)
                owner = source.get('owner', owner)
                group = source.get('group', group)
                paths_expression = source['path']

                if source.get('stdout', defaults.get('stdout', True)) is False:
                    stdout = open(os.devnull, 'w')
                if source.get('stderr', defaults.get('stderr', True)) is False:
                    stderr = open(os.devnull, 'w')
            else:
                paths_expression = source

            # Convert owner/group to uid/gid if on Unix
            if not IS_WINDOWS:
                uid = pwd.getpwnam(owner).pw_uid if owner else -1
                gid = grp.getgrnam(group).gr_gid if group else -1
            else:
                uid = -1
                gid = -1

            # Create the parent folder if needed
            if create:
                success &= self._create(destination, int('%s' % dmode, 8), uid, gid)

            paths = self.expand_path(paths_expression, globs=True)
            if len(paths) > 1:
                self._log.lowinfo(
                    f'Synchronizing expression {paths_expression} -> {destination}'
                )

            for path_item in paths:
                success &= self._sync(
                    path_item, destination, dmode, fmode, owner, group,
                    rsync, options, stdout, stderr
                )

            # Close any file handles if we opened devnull
            if isinstance(stdout, type(open(os.devnull))):
                stdout.close()
            if isinstance(stderr, type(open(os.devnull))):
                stderr.close()

        if success:
            self._log.info('All synchronizations have been done')
        else:
            self._log.error('Some synchronizations were not successful')
        return success

    def _create(self, path, dmode, uid, gid):
        success = True
        parent = os.path.abspath(os.path.join(path, os.pardir))
        if not os.path.exists(parent):
            try:
                os.mkdir(parent, dmode)
                self._chmodown(parent, dmode, uid, gid)
            except Exception as e:
                self._log.warning(f'Failed to create directory {parent}. {e}')
                success = False
            else:
                self._log.lowinfo(f'Creating directory {parent}')
        return success

    def _sync(self, source, destination, dmode, fmode, owner, group,
              rsync, options, stdout, stderr):
        """
        Synchronizes source to destination with cwRsync.
        Returns True if successful.
        """
        success = False

        # Expand relative paths
        source_abs = os.path.join(self._context.base_directory(), source)
        dest_abs = os.path.expanduser(destination)

        # Convert "C:\Users\..." -> "/cygdrive/c/Users/..." on Windows
        if IS_WINDOWS:
            source_abs = _fix_windows_path_for_cwrsync(source_abs)
            dest_abs = _fix_windows_path_for_cwrsync(dest_abs)

        # Build rsync command
        cmd = [
            rsync,
            '--update',
            '--recursive',
            '--owner',
            '--group',
            f'--chown={owner}:{group}' if (owner and group) else '',
            f'--chmod=D{dmode},F{fmode}'
        ]
        # Remove empty strings if any
        cmd = [c for c in cmd if c]

        # If source is a directory, append a slash (for copying contents)
        original_source_full = os.path.join(self._context.base_directory(), source)
        if os.path.isdir(original_source_full):
            # e.g. /cygdrive/c/Users/Name/Documents/...
            if not source_abs.endswith('/'):
                source_abs += '/'

        full_cmd = cmd + options + [source_abs, dest_abs]

        try:
            #self._log.lowinfo("Running rsync command: " + " ".join(full_cmd))
            ret = subprocess.run(
                full_cmd,
                stdout=stdout,
                stderr=stderr,
                cwd=self._context.base_directory(),
                text=True
            )
            if ret.returncode != 0:
                self._log.warning(
                    f"Failed to sync {source_abs} -> {dest_abs}\n"
                    f"Exit code: {ret.returncode}"
                )
            else:
                success = True
                self._log.lowinfo(f"Synchronized {source_abs} -> {dest_abs}")
        except Exception as e:
            self._log.warning(
                f"Failed to sync {source_abs} -> {dest_abs}. Exception: {e}"
            )

        return success
