"""Regression checks against an explicitly selected, empty PostgreSQL test database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import bcrypt
import psycopg
import requests
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
TEST_URL = os.getenv("TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Set TEST_DATABASE_URL to an empty, dedicated PostgreSQL test database")
class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(TEST_URL)
        if not url.database or not url.database.endswith("_test"):
            raise RuntimeError("The test database name must end with _test")
        if os.getenv("DATABASE_URL") and make_url(os.environ["DATABASE_URL"]).database == url.database:
            raise RuntimeError("Refusing to test against the configured application database")
        cls.env = dict(os.environ, DATABASE_URL=TEST_URL, SECRET_KEY=secrets.token_hex(32),
                       ENV="test", CLONE_MODE="true", DISABLE_EXTERNAL_INTEGRATIONS="true",
                       PYTHONDONTWRITEBYTECODE="1")
        cls.env.pop("SESSION_COOKIE_NAME", None)
        cls.env["COOKIE_SECURE"] = "false"
        cls.db = psycopg.connect(TEST_URL.replace("postgresql+psycopg://", "postgresql://", 1),
                                autocommit=True)
        # Never truncate an existing installation. Only empty schemas may be reused.
        tables = cls.db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
        for (table,) in tables:
            if table == "alembic_version":
                continue
            from psycopg import sql
            if cls.db.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM public.{})").format(sql.Identifier(table))).fetchone()[0]:
                raise RuntimeError("Refusing to run against a test database containing data")
        cls.migrate("upgrade", "head")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.log = tempfile.TemporaryFile(mode="w+b")
        cls.server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                                       "--host", "127.0.0.1", "--port", str(cls.port)],
                                      cwd=ROOT, env=cls.env, stdout=cls.log, stderr=cls.log)
        for _ in range(100):
            try:
                r = requests.get(cls.base + "/health", timeout=0.5)
                if r.status_code == 200:
                    break
            except requests.RequestException:
                pass
            if cls.server.poll() is not None:
                raise RuntimeError("Test app failed to start")
            time.sleep(0.05)
        else:
            raise RuntimeError("Test app did not become ready")
        cls.password = secrets.token_urlsafe(20) + "aA1!"
        hashed = bcrypt.hashpw(cls.password.encode(), bcrypt.gensalt()).decode()
        cls.org = cls.db.execute("INSERT INTO organization(short_name,inn,status) VALUES ('Test organization','0000000000','ACTIVE') RETURNING id").fetchone()[0]
        cls.dept = cls.db.execute("INSERT INTO department(org_id,name) VALUES (%s,'Test department') RETURNING id", (cls.org,)).fetchone()[0]
        cls.users = {}
        for kind, role, system in [("admin", "ORG_ADMIN", False), ("assignee", "ORG_USER", False),
                                   ("other", "ORG_USER", False), ("system", None, True)]:
            cls.users[kind] = cls.db.execute(
                'INSERT INTO "user"(full_name,email,password_hash,is_system_admin,is_active,org_id,org_role) '
                'VALUES (%s,%s,%s,%s,true,%s,%s) RETURNING id',
                (kind, kind + "@example.invalid", hashed, system, None if system else cls.org, role)
            ).fetchone()[0]
        cls.db.execute("INSERT INTO userdepartment(user_id,department_id) VALUES (%s,%s)",
                       (cls.users["admin"], cls.dept))
        cls.cookies = {}
        for kind in cls.users:
            s = requests.Session(); s.trust_env = False
            response = s.post(cls.base + "/login", data={"email": kind + "@example.invalid", "password": cls.password}, allow_redirects=False)
            if response.status_code != 303:
                raise RuntimeError("Test login failed")
            cls.cookies[kind] = s.cookies.get_dict()

    @classmethod
    def migrate(cls, *args):
        result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=cls.env,
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            # Fixtures have no customer data; avoid echoing complete DSNs or SQL row details.
            raise RuntimeError("Migration failed: " + result.stderr.splitlines()[-1][:250])

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.server.kill(); cls.server.wait()
        cls.log.close()
        # This database was verified empty before the suite created its own fixtures.
        cls.db.execute('TRUNCATE taskhistory,task,companyemail,company,userdepartment,department,"user",organization RESTART IDENTITY CASCADE')
        cls.migrate("downgrade", "base")
        cls.migrate("upgrade", "head")
        cls.db.close()

    def session(self, kind="admin"):
        s = requests.Session(); s.trust_env = False
        s.cookies.update(self.cookies[kind])
        return s

    def new_task(self, status="OPEN", recurrent=False):
        title = "test-" + secrets.token_hex(8)
        tid = self.db.execute(
            "INSERT INTO task(org_id,title,status,priority,priority_mode,assignee_user_id,created_by,due_date,recur_freq,recur_interval) "
            "VALUES (%s,%s,%s,'MEDIUM','AUTO',%s,%s,'2026-10-28T12:00:00+00',%s,1) RETURNING id",
            (self.org, title, status, self.users["assignee"], self.users["admin"], "MONTHLY" if recurrent else "NONE")
        ).fetchone()[0]
        return tid, title

    def test_dashboard_route_is_unique_and_renders(self):
        # Inspect the actual app registered in a subprocess with exactly the test config.
        script = 'from app.main import app; assert sum(r.path=="/org" and "GET" in getattr(r,"methods",set()) for r in app.routes)==1'
        self.assertEqual(subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=self.env, capture_output=True).returncode, 0)
        for path in ["/org", "/org/tasks", "/org/tasks/list", "/org/clients", "/org/users", "/org/departments"]:
            self.assertEqual(self.session().get(self.base + path, timeout=10).status_code, 200, path)
        self.assertEqual(self.session("system").get(self.base + "/admin", timeout=10).status_code, 200)

    def test_invalid_dates_and_empty_titles_do_not_create_tasks(self):
        before = self.db.execute("SELECT count(*) FROM task").fetchone()[0]
        for due in ["31.02.2026", "2026-02-31", "31.12.2026 25:90"]:
            r = self.session().post(self.base + "/org/tasks/new", data={"title": "Invalid date", "due_at": due, "assign_type": "none"}, allow_redirects=False)
            self.assertEqual(r.status_code, 303)
        r = self.session().post(self.base + "/org/tasks/new", data={"title": "   ", "assign_type": "none"}, allow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.db.execute("SELECT count(*) FROM task").fetchone()[0], before)

    def test_unassigned_task_works_on_fresh_schema(self):
        r = self.session().post(self.base + "/org/tasks/new", data={"title": "Unassigned test", "assign_type": "none"}, allow_redirects=False)
        self.assertEqual(r.status_code, 303)
        row = self.db.execute("SELECT department_id,assignee_user_id FROM task WHERE title='Unassigned test'").fetchone()
        self.assertEqual(row, (None, None))

    def test_reschedule_rejects_other_user_and_accepts_assignee(self):
        tid, _ = self.new_task()
        r = self.session("other").post(self.base + f"/org/tasks/{tid}/reschedule", data={"new_due": "2026-10-29"}, allow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.db.execute("SELECT due_date::date FROM task WHERE id=%s", (tid,)).fetchone()[0], date(2026, 10, 28))
        r = self.session("assignee").post(self.base + f"/org/tasks/{tid}/reschedule", data={"new_due": "2026-10-29"}, allow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.db.execute("SELECT due_date::date FROM task WHERE id=%s", (tid,)).fetchone()[0], date(2026, 10, 29))

    def test_login_next_stays_local(self):
        for value, expected in [("https://example.invalid", "/org"), ("//example.invalid", "/org"),
                                ("/\\example.invalid", "/org"), ("/org/tasks", "/org/tasks")]:
            r = requests.post(self.base + "/login", params={"next": value}, data={"email": "admin@example.invalid", "password": self.password}, allow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertEqual(r.headers["Location"], expected)

    def test_cancelled_task_cannot_spawn_successor(self):
        tid, title = self.new_task(status="CANCELED", recurrent=True)
        r = self.session().post(self.base + f"/org/tasks/{tid}/complete", allow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.db.execute("SELECT status FROM task WHERE id=%s", (tid,)).fetchone()[0], "CANCELED")
        self.assertEqual(self.db.execute("SELECT count(*) FROM task WHERE title=%s", (title,)).fetchone()[0], 1)

    def test_simultaneous_completion_creates_one_successor(self):
        tid, title = self.new_task(recurrent=True)
        lock = psycopg.connect(TEST_URL.replace("postgresql+psycopg://", "postgresql://", 1))
        lock.execute("SELECT id FROM task WHERE id=%s FOR UPDATE", (tid,))
        barrier = threading.Barrier(3)
        def complete():
            s = self.session()
            barrier.wait(timeout=5)
            return s.post(self.base + f"/org/tasks/{tid}/complete", allow_redirects=False, timeout=15).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(complete) for _ in range(2)]
            barrier.wait(timeout=5)
            try:
                for _ in range(100):
                    blocked = self.db.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'").fetchone()[0]
                    if blocked >= 2:
                        break
                    time.sleep(0.05)
                self.assertGreaterEqual(blocked, 2, "Both requests must reach the database before releasing the lock")
            finally:
                lock.commit(); lock.close()
            self.assertEqual([f.result(timeout=15) for f in futures], [303, 303])
        rows = self.db.execute("SELECT status FROM task WHERE title=%s ORDER BY id", (title,)).fetchall()
        self.assertEqual(rows, [("DONE",), ("OPEN",)])
        self.assertEqual(self.db.execute("SELECT count(*) FROM taskhistory WHERE task_id=%s AND action='COMPLETE'", (tid,)).fetchone()[0], 1)

    def test_cli_creates_organization_without_external_api(self):
        command = [sys.executable, "-m", "app.cli", "create-organization", "--inn", "0000000001", "--name", "CLI test",
                   "--admin-email", "cli@example.invalid", "--admin-name", "CLI admin"]
        password = secrets.token_urlsafe(20) + "aA1!"
        r = subprocess.run(command, input=password + "\n" + password + "\n", capture_output=True, text=True, env=self.env, cwd=ROOT, timeout=15)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn(password, r.stdout + r.stderr)
        self.assertEqual(self.db.execute('SELECT org_role FROM "user" WHERE email=%s', ("cli@example.invalid",)).fetchone()[0], "ORG_ADMIN")


if __name__ == "__main__":
    unittest.main()
