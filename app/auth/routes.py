"""Auth routes, request hooks, and the login_required decorator.

All persistence calls go through app.auth.db — no raw SQL here.
"""
import re
from functools import wraps
from flask import (
    render_template, request, redirect, url_for, flash, session, g, abort
)
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash

from . import auth_bp
from .db import (
    change_own_password,
    count_users,
    fetch_user_by_id,
    fetch_user_for_login,
    insert_user,
    is_ip_banned,
    reset_failed_logins,
)
from ..config import Config
from ..extensions import limiter
from ..security_log import failed_login, log_event


def _client_ip():
    return get_remote_address()


#: Letters, digits, '.', '_' and '-', at most 32 characters. Not cosmetic:
#: the name is embedded in the auto-added `user:<name>` tag, where a comma
#: would split it into two tags, and it is the username half of HTTP Basic
#: auth (WebDAV, /api/upload), where a colon makes the account unable to
#: log in at all.
_USERNAME_RE = re.compile(r'[A-Za-z0-9_.-]{1,32}')
USERNAME_RULE = ('Usernames may contain only letters, digits, ".", "_" and '
                 '"-", up to 32 characters.')


def valid_username(name):
    return bool(_USERNAME_RE.fullmatch(name or ''))


# ── Block permanently banned IPs on every request ─────────────────────────
@auth_bp.before_app_request
def block_banned_ips():
    if is_ip_banned(_client_ip()):
        abort(403)


# ── Load logged-in user onto g for every request ──────────────────────────
@auth_bp.before_app_request
def load_logged_in_user():
    user_id = session.get('user_id')
    g.user = None
    if user_id:
        row = fetch_user_by_id(user_id)
        # A session stamped with an older generation predates a password
        # change and is over, wherever it lives. No stamp at all means a
        # session issued before generations existed: generation 0.
        if row and session.get('gen', 0) == row[3]:
            g.user = {'id': row[0], 'username': row[1], 'is_admin': bool(row[2]),
                      'must_change_password': bool(row[4])}
        else:
            session.clear()


# ── Send an account with an admin-chosen password to choose its own ───────
#: What such a session may still reach.
_NEW_PASSWORD_EXEMPT = {
    'auth.new_password', 'auth.logout', 'static', 'main.favicon', 'main.robots',
}


@auth_bp.before_app_request
def require_new_password():
    if (g.user and g.user['must_change_password']
            and request.endpoint not in _NEW_PASSWORD_EXEMPT):
        return redirect(url_for('auth.new_password'))


# ── login_required decorator (importable by other blueprints) ─────────────
def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            flash('Please log in.', 'warning')
            return redirect(url_for('auth.login'))
        return view(**kwargs)
    return wrapped_view


# ── admin_required decorator (importable by other blueprints) ─────────────
def admin_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            flash('Please log in.', 'warning')
            return redirect(url_for('auth.login'))
        if not g.user.get('is_admin'):
            abort(403)
        return view(**kwargs)
    return wrapped_view


# ── Routes ────────────────────────────────────────────────────────────────
@auth_bp.route('/register', methods=('GET', 'POST'))
@limiter.limit("5 per hour")
def register():
    if not Config.REGISTRATION_OPEN:
        flash('Registration is temporarily closed.', 'warning')
        return redirect(url_for('auth.login'))
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        if not username or not password:
            flash('Username and password are required.', 'danger')
        elif not valid_username(username):
            flash(USERNAME_RULE, 'danger')
        elif len(password) < 8:
            flash('The password must be at least 8 characters long.', 'danger')
        else:
            pw_hash = generate_password_hash(password)
            # Local import: avoids a circular import via app.media at module load
            # time (auth.routes is imported before app.media is fully initialised).
            from ..media.crypto import new_key
            first_account = count_users() == 0
            if insert_user(username, pw_hash, new_key()):
                log_event('user_created', user=username, via='register',
                          admin=first_account)
                if first_account:
                    flash('Registration successful — as the first account you have admin rights. Please log in.', 'success')
                else:
                    flash('Registration successful. Please log in.', 'success')
                return redirect(url_for('auth.login'))
            else:
                flash('Username already exists.', 'warning')
    return render_template('auth/register.html')


@auth_bp.route('/login', methods=('GET', 'POST'))
@limiter.limit("5 per minute")
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']

        row = fetch_user_for_login(username)

        if row and check_password_hash(row[1], password):
            reset_failed_logins(_client_ip())
            session.clear()
            # Must come *after* clear(): `permanent` is stored as the '_permanent'
            # key inside the session itself, so clearing wipes it too. This is what
            # gives the cookie a Max-Age (PERMANENT_SESSION_LIFETIME) instead of it
            # being a browser-session cookie that dies on browser/PC restart.
            session.permanent = True
            session['user_id'] = row[0]
            session['gen'] = row[2]
            log_event('login', user=username, via='login')
            flash(f'Welcome, {username}!', 'success')
            return redirect('/')
        else:
            failed_login(username, 'login', known=row is not None)
            if is_ip_banned(_client_ip()):
                abort(403)
            flash('Invalid login credentials.', 'danger')

    return render_template('auth/login.html')


def _change_password_from_form(via):
    """Apply a password-change form for the logged-in user. Returns the
    reason it was refused, or None once the password has changed. `via`
    names the form in the security log.

    A wrong current password counts as a failed login: otherwise a stolen
    session could guess the password here without ever risking a ban.
    """
    current = request.form.get('current_password', '')
    new = request.form.get('new_password', '')
    row = fetch_user_for_login(g.user['username'])
    if not row or not check_password_hash(row[1], current):
        failed_login(g.user['username'], 'password-change', known=True)
        if is_ip_banned(_client_ip()):
            abort(403)
        return 'Your current password is wrong.'
    if len(new) < 8:
        return 'The new password must be at least 8 characters long.'
    if new != request.form.get('confirm_password', ''):
        return "The new passwords don't match."
    if new == current:
        return 'The new password must be different from the current one.'
    gen = change_own_password(g.user['id'], generate_password_hash(new))
    if gen is None:
        abort(404)
    # The change logged out every session of this user. Keep the browser
    # that made it signed in; every other copy stays revoked.
    session['gen'] = gen
    log_event('password_changed', user=g.user['username'], via=via)
    return None


@auth_bp.route('/login/new-password', methods=('GET', 'POST'))
@limiter.limit("5 per minute", methods=['POST'])
@login_required
def new_password():
    """Choose a password after an admin set one: the first login of an
    account an admin created, or the next login after an admin reset."""
    if not g.user['must_change_password']:
        return redirect('/')
    if request.method == 'POST':
        error = _change_password_from_form('first-login')
        if error is None:
            flash('Password changed. Welcome!', 'success')
            return redirect('/')
        flash(error, 'danger')
    return render_template('auth/new_password.html')


@auth_bp.route('/settings/password', methods=('POST',))
@limiter.limit("5 per minute")
@login_required
def change_password():
    """The password form on the settings page."""
    error = _change_password_from_form('settings')
    if error is None:
        flash('Password changed. Your other devices are logged out.', 'success')
    else:
        flash(error, 'danger')
    return redirect(url_for('media.settings'))


@auth_bp.route('/logout', methods=('POST',))
def logout():
    session.clear()
    response = redirect(url_for('auth.login'))
    response.set_cookie(
        key='session',
        value='',
        expires=0,
        secure=Config.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite='Lax'
    )
    return response
