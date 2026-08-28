import io
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_script(name):
    with io.open(os.path.join(ROOT, "scripts", name), "r", encoding="utf-8") as handle:
        return handle.read()


class ShareScriptTest(unittest.TestCase):
    def test_tailscale_start_is_persistent_idempotent_and_keeps_loopback(self):
        text = read_script("start_tailscale_share.ps1")
        self.assertIn("funnel --bg --yes $Port", text)
        self.assertIn("http://127.0.0.1", text)
        self.assertIn("Test-LocalShare", text)
        self.assertIn("no duplicate was started", text)
        self.assertIn("share-url.txt", text)
        self.assertIn("status --json 2>$null | Out-String", text)
        self.assertNotIn("$FunnelOutput | ForEach-Object", text)

    def test_task_installer_uses_tailscale_and_keeps_daily_refresh(self):
        text = read_script("install_tasks.ps1")
        self.assertIn("start_tailscale_share.ps1", text)
        self.assertIn("FOF Valuation Share", text)
        self.assertIn("FOF Valuation Daily Refresh", text)
        self.assertIn("-At '18:30'", text)

    def test_cloudflare_quick_tunnel_remains_available(self):
        text = read_script("start_share.ps1")
        self.assertIn("cloudflared", text)
        self.assertIn("tunnel --no-autoupdate", text)


if __name__ == "__main__":
    unittest.main()
