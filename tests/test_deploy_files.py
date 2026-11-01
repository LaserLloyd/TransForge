"""Static checks on the container files (no Docker needed).

The PUID override (deploy/compose/compose.puid.yml) used to fail for every
PUID other than 1000: `setpriv --init-groups` needs a passwd entry the
read-only root filesystem cannot get, and /data was 0750 owned by 1000, so
another UID could not reach the mounts below it. The real check is the PUID
smoke step in .github/workflows/containers.yml; these keep the two causes out.

Run from the repo root:  python -m unittest discover -s tests
"""
import os
import re
import unittest

import _netguard

_netguard.install(strict_exit=True)  # hermetic: no real sockets

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


class TestPuidPath(unittest.TestCase):
    def test_privilege_drop_does_not_need_a_passwd_entry(self):
        calls = [l for l in read("deploy/docker-entrypoint.sh").splitlines()
                 if l.strip().startswith("exec setpriv")]
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--init-groups", calls[0])
        self.assertIn("--clear-groups", calls[0])

    def test_mount_point_parents_are_traversable_by_any_uid(self):
        text = read("Dockerfile").replace("\\\n", " ")
        modes = {}
        for m in re.finditer(r"install -d -o app -g app -m (\d+) ([^&\n]+)", text):
            for path in m.group(2).split():
                modes[path] = m.group(1)
        for parent in ("/data", "/data/.config", "/data/.local", "/data/.local/state"):
            self.assertEqual(modes.get(parent), "0755", parent)

    def test_manifest_scan_cannot_kill_the_entrypoint(self):
        """`files="$(state_files)"` under set -e: a failing find (cwd /sites not
        enterable by this UID) used to end the entrypoint silently, exit 1."""
        body = read("deploy/docker-entrypoint.sh").split("state_files() {", 1)[1]
        body = body.split("\n}", 1)[0]
        finds = [l for l in body.splitlines() if l.strip().startswith("find ")]
        self.assertTrue(finds)
        for line in finds:
            self.assertTrue(line.rstrip().endswith("|| true"), line)
        self.assertIn("/sites", re.search(
            r"-m 0755 ([^&]+)&&", read("Dockerfile").replace("\\\n", " ")).group(1))

    def test_config_seed_is_escaped_atomic_and_fails_without_template(self):
        """Issue #12: `sed "s|@ENDPOINT@|${ENDPOINT}|"` broke on | & \\ in the
        URL, and a missing template left an empty config that was never
        re-seeded (the shell truncates the target first)."""
        text = read("deploy/docker-entrypoint.sh")
        self.assertNotIn("s|@ENDPOINT@|${ENDPOINT}|", text)
        seed = text.split("# ---- 4. first-start config seed", 1)[1]
        seed = seed.split("# ---- 6.", 1)[0]
        missing = re.search(r'\[ -f "\$TEMPLATE" \] \|\| (\w+) ', seed)
        self.assertIsNotNone(missing, "template-missing branch not found")
        self.assertEqual(missing.group(1), "die")
        self.assertRegex(text, r"die\(\) \{[^}]*exit 1")
        # temp file + mv, never a direct redirect into $CFG
        self.assertIn('mv "$TMP_CFG" "$CFG"', seed)
        self.assertNotRegex(seed, r'>\s*"\$CFG"')

    def test_no_pin_credential_in_deploy_files(self):
        for rel in ("deploy/compose/compose.yml", "deploy/compose/.env.example",
                    "docs/deploy/docker.md", "deploy/docker-entrypoint.sh"):
            text = read(rel)
            self.assertNotRegex(text, r"(?i)mcp.pin|PIN_ENV_FILE", rel)

    def test_ci_smoke_runs_the_puid_path(self):
        wf = read(".github/workflows/containers.yml")
        self.assertIn("-e PUID=1001", wf)
        self.assertIn("--read-only", wf)


if __name__ == "__main__":
    unittest.main()
