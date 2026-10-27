"""Tests for scripts/shoot.mjs and scripts/serve-check.sh.

Run: python3 -m unittest tests.test_shoot (from the repo root).

The argument parsing and the missing-Playwright message are always tested.
Real screenshots need Node, Playwright and its Chromium; set
FACTORY_PLAYWRIGHT_DIR to a node_modules that holds `playwright` if it is not
resolvable from here. Without a browser those tests skip with the reason.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHOOT = os.path.join(ROOT, "scripts", "shoot.mjs")
SERVE_CHECK = os.path.join(ROOT, "scripts", "serve-check.sh")
FIXTURE = os.path.join(HERE, "fixtures", "ui")
NODE = shutil.which("node")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass


class FixtureServer:
    def __init__(self):
        handler = functools.partial(QuietHandler, directory=FIXTURE)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def node_eval(code: str) -> str:
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", code],
        capture_output=True, text=True, timeout=30, cwd=ROOT,
    )
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout


def find_playwright():
    """Directory of an installed `playwright` package, or None."""
    candidates = []
    if os.environ.get("FACTORY_PLAYWRIGHT_DIR"):
        candidates.append(os.environ["FACTORY_PLAYWRIGHT_DIR"])
    try:
        out = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=20)
        if out.returncode == 0:
            g = out.stdout.strip()
            candidates += [g, os.path.join(g, "@playwright", "mcp", "node_modules")]
    except (OSError, subprocess.SubprocessError):
        pass
    candidates += ["/opt/npm-tools/node_modules", "/opt/node-tools/node_modules"]
    for c in candidates:
        if os.path.isfile(os.path.join(c, "playwright", "package.json")):
            return os.path.join(c, "playwright")
    return None


@unittest.skipUnless(NODE, "node not installed")
class ShootArgsTest(unittest.TestCase):
    def parse(self, args):
        code = (
            "import { parseArgs } from %s;"
            "console.log(JSON.stringify(parseArgs(%s)));"
        ) % (json.dumps("file://" + SHOOT), json.dumps(args))
        return json.loads(node_eval(code))

    def test_defaults_are_qa_viewports_light(self):
        o = self.parse(["--base", "http://x", "--route", "settings/profile", "--out", "o"])
        self.assertEqual(o["route"], "/settings/profile")
        self.assertEqual(o["label"], "settings-profile")
        self.assertEqual(o["themes"], ["light"])
        self.assertEqual(
            [(v["name"], v["width"], v["height"]) for v in o["viewports"]],
            [("desktop", 1920, 1080), ("tablet", 1024, 768), ("mobile", 375, 667)],
        )

    def test_refine_preset_themes_and_selectors(self):
        o = self.parse(["--base", "http://x", "--route", "/", "--out", "o",
                        "--viewports", "refine", "--themes", "light,dark",
                        "--select", "#a", "--select", ".b c"])
        self.assertEqual([v["width"] for v in o["viewports"]], [1440, 390])
        self.assertEqual(o["themes"], ["light", "dark"])
        self.assertEqual(o["select"], ["#a", ".b c"])
        self.assertEqual(o["label"], "home")

    def test_custom_viewports(self):
        o = self.parse(["--base", "b", "--route", "/", "--out", "o", "--viewports", "800x600,320x480"])
        self.assertEqual([v["name"] for v in o["viewports"]], ["800x600", "320x480"])

    def run_cli(self, args, cwd):
        # NODE_PATH could make a global Playwright resolvable; the project has none.
        env = {k: v for k, v in os.environ.items() if k != "NODE_PATH"}
        return subprocess.run([NODE, SHOOT] + args, capture_output=True, text=True, timeout=60, cwd=cwd, env=env)

    def test_usage_errors_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            for args in (["--route", "/", "--out", "o"],
                         ["--base", "b", "--route", "/", "--out", "o", "--themes", "sepia"],
                         ["--base", "b", "--route", "/", "--out", "o", "--viewports", "big"],
                         ["--base"]):
                out = self.run_cli(args, tmp)
                self.assertEqual(out.returncode, 2, (args, out.stderr))
                self.assertIn("shoot:", out.stderr)

    def test_missing_playwright_says_how_to_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_cli(["--base", "http://127.0.0.1:9", "--route", "/", "--out", "o"], tmp)
            self.assertEqual(out.returncode, 3, out.stderr)
            self.assertIn("npm i -D playwright && npx playwright install chromium", out.stderr)
            self.assertEqual(out.stdout, "")


@unittest.skipUnless(NODE, "node not installed")
class ShootBrowserTest(unittest.TestCase):
    """Real shots against the fixture page, if a browser is available."""

    @classmethod
    def setUpClass(cls):
        pw = find_playwright()
        if not pw:
            raise unittest.SkipTest("Playwright not found; set FACTORY_PLAYWRIGHT_DIR to test real screenshots")
        cls.project = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.project, "node_modules"))
        os.symlink(pw, os.path.join(cls.project, "node_modules", "playwright"))
        probe = subprocess.run(
            [NODE, "-e", "require('playwright').chromium.launch().then(b => b.close())"],
            capture_output=True, text=True, timeout=60, cwd=cls.project,
        )
        if probe.returncode != 0:
            shutil.rmtree(cls.project)
            first = (probe.stderr.strip().splitlines() or ["launch failed"])[0]
            raise unittest.SkipTest(
                "Playwright's Chromium is not installed (npx playwright install chromium): " + first
            )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.project, ignore_errors=True)

    def shoot(self, base, *extra):
        out_dir = os.path.join(self.project, "shots")
        shutil.rmtree(out_dir, ignore_errors=True)
        out = subprocess.run(
            [NODE, SHOOT, "--base", base, "--route", "/index.html", "--out", "shots", "--settle", "0"] + list(extra),
            capture_output=True, text=True, timeout=180, cwd=self.project,
        )
        return out

    def test_three_viewports_and_manifest(self):
        with FixtureServer() as srv:
            out = self.shoot(srv.url, "--label", "fx")
        self.assertEqual(out.returncode, 0, out.stderr)
        m = json.loads(out.stdout)
        self.assertEqual(m["url"], srv.url + "/index.html")
        self.assertEqual([(f["viewport"], f["width"]) for f in m["files"]],
                         [("desktop", 1920), ("tablet", 1024), ("mobile", 375)])
        for f in m["files"]:
            self.assertTrue(os.path.isfile(f["path"]), f["path"])
            with open(f["path"], "rb") as fh:
                self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")
        self.assertEqual(os.path.basename(m["files"][0]["path"]), "fx-desktop-light.png")

    def test_dark_theme_and_crop(self):
        with FixtureServer() as srv:
            out = self.shoot(srv.url, "--viewports", "refine", "--themes", "light,dark",
                             "--select", "#card", "--select", "#missing")
        self.assertEqual(out.returncode, 0, out.stderr)
        m = json.loads(out.stdout)
        pages = [(f["viewport"], f["theme"]) for f in m["files"] if f["kind"] == "page"]
        self.assertEqual(pages, [("desktop", "light"), ("desktop", "dark"), ("mobile", "light"), ("mobile", "dark")])
        crops = [f for f in m["files"] if f["kind"] == "crop"]
        self.assertEqual(len(crops), 4)
        self.assertTrue(crops[0]["path"].endswith("index-html-desktop-light--card.png"))
        self.assertTrue(any("#missing" in w for w in m["warnings"]))
        light = [f["path"] for f in m["files"] if f["kind"] == "page" and f["theme"] == "light"][0]
        dark = [f["path"] for f in m["files"] if f["kind"] == "page" and f["theme"] == "dark"][0]
        with open(light, "rb") as a, open(dark, "rb") as b:
            self.assertNotEqual(a.read(), b.read())

    def test_unreachable_base_exits_4(self):
        out = self.shoot("http://127.0.0.1:%d" % free_port(), "--viewports", "800x600", "--timeout", "5000")
        self.assertEqual(out.returncode, 4, out.stderr)
        self.assertIn("could not load", out.stderr)


@unittest.skipUnless(shutil.which("curl") and shutil.which("jq"), "curl and jq needed")
class ServeCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = os.path.join(self.tmp, "factory.json")
        self.env = dict(os.environ, FACTORY_CONFIG=self.config)

    def tearDown(self):
        pid_file = os.path.join(self.tmp, ".claude", "factory", "serve.pid")  # not a git repo
        if os.path.exists(pid_file):
            with open(pid_file) as fh:
                pid = int(fh.read().strip())
            subprocess.run(["pkill", "-P", str(pid)], capture_output=True)
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, ui):
        with open(self.config, "w") as fh:
            json.dump({"repo": "acme/app", "ui": ui}, fh)

    def run_check(self, *args):
        return subprocess.run(["bash", SERVE_CHECK] + list(args), capture_output=True,
                              text=True, timeout=60, cwd=self.tmp, env=self.env)

    def test_reachable(self):
        with FixtureServer() as srv:
            self.write_config({"base_url": srv.url})
            out = self.run_check()
        self.assertEqual(out.returncode, 0, out.stderr)
        r = json.loads(out.stdout)
        self.assertTrue(r["reachable"])
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["base_url"], srv.url)

    def test_down_with_start_command_hint(self):
        port = free_port()
        self.write_config({"base_url": "http://127.0.0.1:%d/" % port, "start_command": "make serve"})
        out = self.run_check()
        self.assertEqual(out.returncode, 1)
        r = json.loads(out.stdout)
        self.assertFalse(r["reachable"])
        self.assertEqual(r["start_command"], "make serve")
        self.assertIn("PORT=%d make serve" % port, out.stderr)

    def test_down_guesses_package_script(self):
        self.write_config({"base_url": "http://127.0.0.1:%d" % free_port()})
        with open(os.path.join(self.tmp, "package.json"), "w") as fh:
            json.dump({"scripts": {"start": "node s.js", "dev": "vite"}}, fh)
        open(os.path.join(self.tmp, "pnpm-lock.yaml"), "w").close()
        r = json.loads(self.run_check().stdout)
        self.assertEqual(r["start_command"], "pnpm run dev")

    def test_down_and_nothing_to_start(self):
        self.write_config({"base_url": "http://127.0.0.1:%d" % free_port()})
        out = self.run_check()
        self.assertEqual(out.returncode, 1)
        self.assertIsNone(json.loads(out.stdout)["start_command"])
        self.assertIn("ui.start_command", out.stderr)
        self.assertEqual(self.run_check("start").returncode, 3)

    def test_start_waits_until_up(self):
        port = free_port()
        self.write_config({
            "base_url": "http://127.0.0.1:%d" % port,
            "start_command": "exec python3 -m http.server $PORT --bind 127.0.0.1 --directory %s" % FIXTURE,
        })
        out = self.run_check("start", "--wait", "30")
        self.assertEqual(out.returncode, 0, out.stderr)
        r = json.loads(out.stdout)
        self.assertTrue(r["reachable"])
        self.assertIn("pid", r)
        self.assertEqual(self.run_check().returncode, 0)

    def test_bad_argument(self):
        self.assertEqual(self.run_check("--nope").returncode, 2)


if __name__ == "__main__":
    unittest.main()
