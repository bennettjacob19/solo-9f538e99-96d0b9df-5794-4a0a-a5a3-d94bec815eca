import importlib.util
import tempfile
import unittest
from pathlib import Path

if importlib.util.find_spec("flask"):
    import app as flask_app
else:  # Allows the storage/state tests to run with only the standard library.
    flask_app = None


@unittest.skipUnless(flask_app is not None, "Flask is not installed")
class FlaskApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "api.db")
        flask_app.app.config.update(TESTING=True, DB_PATH=self.db_path)
        self.client = flask_app.app.test_client()

    def tearDown(self):
        # Close the per-app-context database if a test left one registered.
        with flask_app.app.app_context():
            db = flask_app.g.pop("db", None)
            if db is not None:
                db.close()
        self.tmp.cleanup()

    def test_page_and_state(self):
        response = self.client.get("/")
        self.assertEqual(200, response.status_code)
        self.assertIn("大页".encode(), response.data)

        state = self.client.get("/api/state")
        self.assertEqual(200, state.status_code)
        self.assertEqual(16, len(state.json["slots"]))

    def test_http_pause_edit_failure_rollback_and_retry(self):
        response = self.client.post(
            "/api/batches",
            json={
                "target_start": 0,
                "move_quota": 1,
                "writeback_quota": 2,
                "fail_on_writeback_index": 2,
            },
        )
        self.assertEqual(201, response.status_code)
        batch_id = response.json["batch"]["id"]

        write = self.client.post(
            "/api/guest/write",
            json={"page_id": 1, "content": "HTTP-PAUSE-EDIT"},
        )
        self.assertEqual(200, write.status_code)
        self.assertEqual("hit_dirty", write.json["mode"])

        failed = self.client.post(f"/api/batches/{batch_id}/resume", json={})
        self.assertEqual(409, failed.status_code)
        self.assertEqual("rolled_back", failed.json["status"])
        self.assertTrue(failed.json["state"]["slots"][1]["dirty"])
        self.assertIn("HTTP-PAUSE-EDIT", failed.json["state"]["slots"][1]["content"])

        retried = self.client.post(
            f"/api/batches/{batch_id}/resume", json={"clear_fault": True}
        )
        self.assertEqual(200, retried.status_code)
        self.assertEqual("allocated", retried.json["batch"]["status"])
        for slot_no in range(4):
            self.assertEqual("new-vm", retried.json["state"]["slots"][slot_no]["owner"])

    def test_invalid_alignment_returns_json_error(self):
        response = self.client.post("/api/batches", json={"target_start": 7})
        self.assertEqual(400, response.status_code)
        self.assertIn("4 的倍数", response.json["error"])


if __name__ == "__main__":
    unittest.main()
