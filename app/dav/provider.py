"""Per-user chrooted filesystem provider.

One provider instance rooted at Config.STAGING_DIR serves every user their
own staging/<user_id>/ jail: the authenticated user id (stamped onto the
WSGI environ by MediaIndexDomainController) is prefixed onto every resolved
path, so users can never see or address each other's files.
"""
import os

from wsgidav import util
from wsgidav.dav_error import HTTP_FORBIDDEN, DAVError
from wsgidav.fs_dav_provider import FilesystemProvider

from .dc import ENVIRON_USER_ID


class UserStagingProvider(FilesystemProvider):
    def __init__(self, root_folder):
        super().__init__(root_folder, fs_opts={})

    def _loc_to_file_path(self, path: str, environ: dict = None):
        if not environ or ENVIRON_USER_ID not in environ:
            # Only reachable authenticated; anything else is a config error.
            raise DAVError(HTTP_FORBIDDEN, "Not authenticated")
        user_root = os.path.join(self.root_folder_path, str(environ[ENVIRON_USER_ID]))
        path_parts = path.strip("/").split("/")
        file_path = os.path.abspath(os.path.join(user_root, *path_parts))
        # Containment check against the *user* root (not just STAGING_DIR),
        # so '..' can neither escape staging nor cross into another user.
        if not self._is_equal_or_child_path(
            os.path.realpath(user_root), os.path.realpath(file_path)
        ):
            raise DAVError(
                HTTP_FORBIDDEN,
                f"Access outside your WebDAV folder is not allowed: {path!r}",
            )
        return util.to_unicode_safe(file_path)
