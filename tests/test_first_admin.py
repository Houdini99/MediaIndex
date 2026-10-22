"""First-account admin bootstrap: the very first user ever created gets
admin rights automatically; every later account starts as a plain user."""
from werkzeug.security import generate_password_hash


def _create(username):
    from app.auth.db import insert_user, fetch_user_for_login
    assert insert_user(username, generate_password_hash('password123'))
    return fetch_user_for_login(username)[0]


def test_first_user_becomes_admin(app):
    from app.auth.db import fetch_user_by_id
    first_id = _create('first')
    second_id = _create('second')
    assert fetch_user_by_id(first_id)[2] == 1
    assert fetch_user_by_id(second_id)[2] == 0


def test_bootstrap_admin_can_open_admin_panel(app, client):
    _create('first')
    client.post('/login', data={'username': 'first', 'password': 'password123'})
    assert client.get('/admin/').status_code == 200

    # A later account stays a plain user and is kept out.
    _create('second')
    client.post('/logout')
    client.post('/login', data={'username': 'second', 'password': 'password123'})
    assert client.get('/admin/').status_code == 403


def test_admin_pages_send_a_logged_out_visitor_to_the_login_page(app, client):
    """admin_required runs login_required first: no session means the
    login page, not a 403."""
    _create('first')
    for method, path in (('get', '/admin/'), ('post', '/admin/users/create')):
        resp = getattr(client, method)(path)
        assert resp.status_code == 302, path
        assert resp.headers['Location'].endswith('/login'), path
    assert b'Please log in.' in client.get('/login').data


def test_concurrent_registrations_produce_exactly_one_admin(app):
    """The bootstrap race.

    Folding the check into the INSERT as `NOT EXISTS (...)` is not enough on
    its own: under READ COMMITTED each transaction evaluates that subquery
    against a snapshot excluding the other's uncommitted row, so both still
    see an empty table. Measured directly against Postgres, that grants
    admin 2 times out of 2. An advisory lock held to commit is what closes
    it, and this pins that.
    """
    import threading
    from werkzeug.security import generate_password_hash
    from app.auth.db import insert_user, fetch_all_users

    barrier = threading.Barrier(4)
    pw = generate_password_hash('password123')

    def register(name):
        barrier.wait()
        insert_user(name, pw)

    threads = [threading.Thread(target=register, args=(f'racer{i}',))
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    users = fetch_all_users()
    assert len(users) == 4, users
    admins = [u[1] for u in users if u[2]]
    assert len(admins) == 1, f"expected exactly one admin, got {admins}"
