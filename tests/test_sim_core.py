import os
import tempfile
import unittest

import sim_core as sim


class MemorySimulationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.conn = sim.connect(self.db_path)
        sim.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def slot(self, no):
        return dict(self.conn.execute("SELECT * FROM slots WHERE slot_no=?", (no,)).fetchone())

    def disk(self, pid):
        return dict(self.conn.execute("SELECT * FROM disk_pages WHERE page_id=?", (pid,)).fetchone())

    def test_sample_layout(self):
        state = sim.get_state(self.conn)
        self.assertEqual(16, len(state["slots"]))
        self.assertEqual([15], state["live_plan"]["outside_free_slots"] if state["live_plan"] else [15])
        self.assertEqual(1, self.slot(0)["dirty"])
        self.assertEqual(0, self.slot(1)["dirty"])
        self.assertEqual(1, self.slot(2)["dirty"])
        self.assertIsNone(self.slot(3)["owner"])
        self.assertIsNone(self.slot(15)["owner"])
        self.assertTrue(self.disk(14)["content"])

    def test_target_start_must_be_multiple_of_four(self):
        with self.assertRaises(sim.ValidationError):
            sim.create_batch(self.conn, 1, 1, 1)
        with self.assertRaises(sim.ValidationError):
            sim.create_batch(self.conn, 5, 1, 1)

    def test_pause_write_changes_move_content_and_allocates(self):
        batch = sim.create_batch(self.conn, 0, 1, 1)
        self.assertEqual("paused", batch["status"])

        written = sim.guest_write(self.conn, 0, "LATEST-PAUSE-CONTENT")
        self.assertEqual("hit_dirty", written["mode"])

        batch = sim.execute_batch(self.conn, batch["id"])
        self.assertEqual("allocated", batch["status"])
        self.assertEqual(1, batch["result"]["actual_moves"])
        self.assertEqual(1, batch["result"]["actual_writebacks"])
        self.assertEqual(1, batch["result"]["actual_clean_releases"])

        for no in range(4):
            self.assertEqual(sim.NEW_VM, self.slot(no)["owner"])
            self.assertEqual("hugepage", self.slot(no)["role"])
            self.assertIsNone(self.slot(no)["page_id"])

        moved = self.slot(15)
        self.assertEqual(0, moved["page_id"])
        self.assertEqual(sim.OLD_VM, moved["owner"])
        self.assertEqual(1, moved["dirty"])
        self.assertIn("LATEST-PAUSE-CONTENT", moved["content"])

        # Slot 2 was writeback; latest slot content equals disk.
        self.assertEqual(self.slot(2)["owner"], sim.NEW_VM)
        self.assertIn("LIVE-P02", self.disk(2)["content"])

        # Old VM cannot use allocated target slots. Page 1 was released and the
        # only outside free slot was consumed by migration, so it reads disk.
        read = sim.guest_read(self.conn, 1)
        self.assertEqual("direct_disk", read["mode"])
        self.assertIsNone(read["slot_no"])

    def test_writeback_failure_rolls_back_batch_but_keeps_pause_writes(self):
        batch = sim.create_batch(self.conn, 0, 1, 2, fail_on_writeback_index=2)

        # Make clean page 1 dirty during pause and update page 0.
        sim.guest_write(self.conn, 1, "PAUSE-DIRTY-P1")
        sim.guest_write(self.conn, 0, "PAUSE-LATEST-P0")

        with self.assertRaises(sim.BatchRolledBack):
            sim.execute_batch(self.conn, batch["id"])

        # Reorganization is fully undone.
        s0, s1, s2, s3, s15 = [self.slot(i) for i in (0, 1, 2, 3, 15)]
        self.assertEqual(0, s0["page_id"])
        self.assertEqual(1, s0["dirty"])
        self.assertIn("PAUSE-LATEST-P0", s0["content"])
        self.assertEqual(1, s1["page_id"])
        self.assertEqual(1, s1["dirty"])
        self.assertIn("PAUSE-DIRTY-P1", s1["content"])
        self.assertEqual(2, s2["page_id"])
        self.assertEqual(1, s2["dirty"])
        self.assertIsNone(s3["owner"])
        self.assertIsNone(s15["owner"])

        # Pause writes are committed and retained; writebacks were rolled back.
        self.assertNotIn("PAUSE-LATEST-P0", self.disk(0)["content"])
        self.assertNotIn("PAUSE-DIRTY-P1", self.disk(1)["content"])

        failed = sim.get_batch(self.conn, batch["id"])
        self.assertEqual("rolled_back", failed["status"])
        self.assertIn("全部撤销", failed["error"])
        self.assertTrue(failed["result"]["rolled_back"])
        self.assertEqual(0, failed["result"]["actual_moves"])
        self.assertEqual(0, failed["result"]["actual_writebacks"])
        self.assertEqual(1, failed["result"]["attempted_before_failure"]["moves"])
        self.assertEqual(
            1,
            failed["result"]["attempted_before_failure"][
                "writebacks_before_failed_ordinal"
            ],
        )

        # Clear fault and continue: replan uses current three dirty pages.
        batch = sim.execute_batch(self.conn, batch["id"], clear_fault=True)
        self.assertEqual("allocated", batch["status"])
        result = batch["result"]
        self.assertEqual(1, result["actual_moves"])
        self.assertEqual(2, result["actual_writebacks"])
        self.assertEqual(0, result["actual_clean_releases"])
        self.assertEqual(sim.NEW_VM, self.slot(0)["owner"])
        self.assertEqual(sim.NEW_VM, self.slot(3)["owner"])
        self.assertIn("PAUSE-DIRTY-P1", self.disk(1)["content"])
        self.assertIn("PAUSE-LATEST-P0", self.slot(15)["content"])

    def test_guest_fault_uses_outside_slot_then_direct_disk(self):
        batch = sim.create_batch(self.conn, 0, 1, 1)
        self.assertEqual([15], batch["preliminary_plan"]["outside_free_slots"])

        read14 = sim.guest_read(self.conn, 14)
        self.assertEqual("miss_recached", read14["mode"])
        self.assertEqual(15, read14["slot_no"])

        read15 = sim.guest_read(self.conn, 15)
        self.assertEqual("direct_disk", read15["mode"])
        self.assertIsNone(read15["slot_no"])

        write15 = sim.guest_write(self.conn, 15, "DIRECT-WRITE")
        self.assertEqual("direct_disk_write", write15["mode"])
        self.assertIn("DIRECT-WRITE", self.disk(15)["content"])
        self.assertIsNone(self.slot(3)["page_id"])  # protected target slot

    def test_pause_fault_consumes_external_slot_changes_plan(self):
        batch = sim.create_batch(self.conn, 0, 2, 0)
        self.assertEqual(1, batch["preliminary_plan"]["moves"])

        sim.guest_read(self.conn, 14)  # consumes slot 15
        live = sim.get_state(self.conn)["live_plan"]
        self.assertEqual([], live["outside_free_slots"])
        self.assertEqual(0, live["moves"])

        batch = sim.execute_batch(self.conn, batch["id"])
        self.assertEqual("applied", batch["status"])
        self.assertEqual(0, batch["result"]["actual_moves"])
        self.assertEqual(1, batch["result"]["actual_clean_releases"])
        self.assertFalse(batch["result"]["target_all_free_before_allocation"])
        self.assertEqual(0, self.slot(0)["page_id"])
        self.assertEqual(2, self.slot(2)["page_id"])
        self.assertEqual(1, self.slot(0)["dirty"])

    def test_writeback_only_keeps_outside_slot_for_old_vm_refault(self):
        batch = sim.create_batch(self.conn, 0, 0, 2)
        batch = sim.execute_batch(self.conn, batch["id"])
        self.assertEqual("allocated", batch["status"])
        self.assertEqual(2, batch["result"]["actual_writebacks"])
        self.assertEqual(15, sim.external_free_slots(self.conn, None)[0]["slot_no"])

        read = sim.guest_read(self.conn, 1)
        self.assertEqual("miss_recached", read["mode"])
        self.assertEqual(15, read["slot_no"])
        self.assertEqual(1, self.slot(15)["page_id"])

    def test_direct_run_api_style(self):
        batch = sim.create_batch(self.conn, 0, 1, 1, run_now=True)
        self.assertEqual("allocated", batch["status"])
        state = sim.get_state(self.conn)
        self.assertEqual(4, state["stats"]["new_vm_slots"])


if __name__ == "__main__":
    unittest.main()
