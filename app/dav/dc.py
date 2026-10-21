"""WsgiDAV domain controller backed by the app's users DB.

Authenticates HTTP Basic credentials against the per-user WebDAV app
password (never the login password — see app.auth.db.set_dav_password_hash)
and enforces the same IP-ban system as the Flask side, which matters here
because Flask's before_app_request hooks never run for the dispatched
/dav branch.
"""
import hmac
import os

from wsgidav.dc.base_dc import BaseDomainController

from ..auth.db import (
    fetch_user_for_dav,
    hash_dav_password,
    is_ip_banned,
)
from ..config import Config
from ..security_log import failed_login

#: environ key carrying the authenticated user's id into the provider
ENVIRON_USER_ID = 'mediaindex.user_id'


class MediaIndexDomainController(BaseDomainController):
    def __init__(self, wsgidav_app, config):
        super().__init__(wsgidav_app, config)

    def get_domain_realm(self, path_info, environ):
        return "MediaIndex WebDAV"

    def require_authentication(self, realm, environ):
        return True

    def supports_http_digest_auth(self):
        # Digest would require storing a recoverable HA1 hash; Basic over
        # HTTPS with a high-entropy app password is the better trade-off.
        return False

    def basic_auth_user(self, realm, user_name, password, environ):
        ip = environ.get('REMOTE_ADDR', '')
        if is_ip_banned(ip):
            return False
        row = fetch_user_for_dav(user_name)
        if (not row or not row[1]
                or not hmac.compare_digest(hash_dav_password(password), row[1])):
            failed_login(user_name, 'webdav', known=row is not None, ip=ip,
                         ua=environ.get('HTTP_USER_AGENT'))
            return False
        uid = row[0]
        environ[ENVIRON_USER_ID] = uid
        # First DAV contact after account creation: make the user's inbox.
        os.makedirs(os.path.join(Config.STAGING_DIR, str(uid)), exist_ok=True)
        return True
