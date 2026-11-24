"""Tests for aprs_send.py: when an object is sent, and the single APRS-IS connection.

  python3 -m unittest test_send        (from this folder)

Nothing here reaches the real APRS-IS: the connection tests talk to a fake server on 127.0.0.1.
"""
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import aprs_send

CALL = "N0CALL-13"
MIN = 60


def obj(name, comment="Fire Main St", stamp="241500z", live=True, lat="4007.20N", lon="07520.40W"):
    return f";{name:<9}{'*' if live else '_'}{stamp}{lat}/{lon}d{comment}"


class Rules(unittest.TestCase):
    def setUp(self):
        self.book = aprs_send.Book(CALL)

    def offer_and_send(self, packet, now):
        why = self.book.offer(packet, "test", now)
        for key, _p, _why in self.book.due(now):
            self.book.mark_sent(key, now)
        return why

    def test_new_then_unchanged_is_not_sent_again(self):
        self.assertEqual(self.offer_and_send(obj("FIRE1"), 0), "new")
        self.assertEqual(self.offer_and_send(obj("FIRE1"), 2 * MIN), "")
        self.assertEqual(self.offer_and_send(obj("FIRE1"), 25 * MIN + 59), "")

    def test_a_new_time_stamp_alone_is_not_a_change(self):
        self.offer_and_send(obj("FIRE1", stamp="241500z"), 0)
        self.assertEqual(self.offer_and_send(obj("FIRE1", stamp="241502z"), 2 * MIN), "")

    def test_changed_text_position_or_symbol_is_sent(self):
        self.offer_and_send(obj("FIRE1"), 0)
        self.assertEqual(self.offer_and_send(obj("FIRE1", comment="Fire Main St (out)"), 2 * MIN), "changed")
        self.assertEqual(self.offer_and_send(obj("FIRE1", comment="Fire Main St (out)", lat="4007.30N"), 4 * MIN), "changed")
        self.assertEqual(self.offer_and_send(obj("FIRE1", comment="Fire Main St (out)", lat="4007.30N"), 6 * MIN), "")

    def test_resent_at_26_minutes_and_the_clock_starts_again(self):
        self.offer_and_send(obj("FIRE1"), 0)
        self.assertEqual(self.offer_and_send(obj("FIRE1"), 26 * MIN), "refresh")
        self.assertEqual(self.offer_and_send(obj("FIRE1"), 28 * MIN), "")
        self.assertEqual(self.offer_and_send(obj("FIRE1"), 52 * MIN), "refresh")

    def test_a_change_restarts_the_26_minutes(self):
        self.offer_and_send(obj("FIRE1"), 0)
        self.offer_and_send(obj("FIRE1", comment="moved"), 20 * MIN)
        self.assertEqual(self.offer_and_send(obj("FIRE1", comment="moved"), 30 * MIN), "")
        self.assertEqual(self.offer_and_send(obj("FIRE1", comment="moved"), 46 * MIN), "refresh")

    def test_last_changed_is_kept_through_a_refresh(self):
        self.offer_and_send(obj("FIRE1"), 100)
        self.offer_and_send(obj("FIRE1"), 100 + 26 * MIN)
        r = self.book.records["FIRE1    "]
        self.assertEqual((r["changed"], r["sent"]), (100, 100 + 26 * MIN))

    def test_kill_is_sent_once(self):
        self.offer_and_send(obj("FIRE1"), 0)
        self.assertEqual(self.offer_and_send(obj("FIRE1", live=False), 2 * MIN), "kill")
        self.assertEqual(self.offer_and_send(obj("FIRE1", live=False), 4 * MIN), "")

    def test_kill_of_something_never_seen_is_still_sent(self):
        self.assertEqual(self.offer_and_send(obj("OLD1", live=False), 0), "kill")

    def test_object_coming_back_after_a_kill_is_new(self):
        self.offer_and_send(obj("FIRE1"), 0)
        self.offer_and_send(obj("FIRE1", live=False), 2 * MIN)
        self.assertEqual(self.offer_and_send(obj("FIRE1"), 4 * MIN), "new")

    def test_killed_objects_are_forgotten_and_live_ones_kept(self):
        self.offer_and_send(obj("FIRE1"), 0)
        self.offer_and_send(obj("FIRE2"), 0)
        self.offer_and_send(obj("FIRE1", live=False), 2 * MIN)
        self.book.prune(40 * MIN)
        self.assertEqual(list(self.book.records), ["FIRE2    "])

    def test_status_line_is_sent_once(self):
        self.assertEqual(self.offer_and_send(">Traffic events: example", 0), "new")
        self.assertEqual(self.offer_and_send(">Traffic events: example", 5 * MIN), "")

    def test_packets_with_line_breaks_are_refused(self):
        self.assertEqual(self.book.offer(obj("FIRE1") + "\r\nN0CALL>APRS:>injected", "test", 0), "")
        self.assertEqual(self.book.due(0), [])

    def test_nothing_is_sent_unless_a_script_asks(self):
        self.offer_and_send(obj("FIRE1"), 0)
        self.assertEqual(self.book.due(60 * MIN), [])          # the script stopped: no refresh on our own

    def test_waiting_while_offline_keeps_the_newest_and_drops_what_nobody_wants(self):
        self.book.offer(obj("FIRE1", comment="first"), "test", 0)            # offline: offered, not sent
        self.book.offer(obj("FIRE1", comment="second"), "test", 2 * MIN)
        self.book.offer(obj("FIRE2"), "test", 0)                              # its script then stops
        self.book.offer(obj("GONE1", live=False), "test", 0)
        due = {key.strip(): p for key, p, _ in self.book.due(45 * MIN - 1) if key.strip() != "FIRE1"}
        self.assertEqual(set(due), {"GONE1"})                                 # FIRE2 is stale, the kill is not
        self.book.offer(obj("FIRE1", comment="second"), "test", 44 * MIN)
        packets = [p for _k, p, _ in self.book.due(45 * MIN)]
        self.assertIn(obj("FIRE1", comment="second"), packets)
        self.assertNotIn(obj("FIRE1", comment="first"), packets)

    def test_state_survives_a_restart_and_another_callsign_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sender_state.json")
            book = aprs_send.Book(CALL, path)
            book.offer(obj("FIRE1"), "test", 1000)
            book.mark_sent("FIRE1    ", 1000)
            book.offer(obj("GONE1", live=False), "test", 1000)                # still waiting at shutdown
            book.save()
            again = aprs_send.Book(CALL, path)
            self.assertEqual(again.offer(obj("FIRE1"), "test", 1000 + 2 * MIN), "")
            self.assertEqual([k.strip() for k, _p, _w in again.due(1000 + 2 * MIN)], ["GONE1"])
            self.assertEqual(aprs_send.Book("N0CALL-7", path).records, {})


    def test_send_times_for_the_web_page(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sender_state.json")
            book = aprs_send.Book(CALL, path)
            book.offer(obj("FIRE1"), "test", 1000)
            book.mark_sent("FIRE1    ", 1000)
            book.offer(obj("FIRE2"), "test", 1000)                            # waiting, never sent
            book.offer(obj("GONE1", live=False), "test", 1000)
            book.mark_sent("GONE1    ", 1000)
            book.offer(">a status line", "test", 1000)
            book.save()
            self.assertEqual(aprs_send.send_times(path), {
                "FIRE1": {"sent": 1000, "again": 1000 + 26 * MIN, "waiting": False, "changed": 1000, "held": False},
                "FIRE2": {"sent": None, "again": None, "waiting": True, "changed": 1000, "held": False}})
            self.assertEqual(aprs_send.send_times(os.path.join(d, "missing.json")), {})


class FakeAprsIs(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.logins, self.lines, self.drop_after, self.verified = [], [], None, True
        threading.Thread(target=self.serve_forever, daemon=True).start()


class FakeHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server
        self.wfile.write(b"# fake aprsc\r\n")
        srv.logins.append(self.rfile.readline().decode().strip())
        self.wfile.write(b"# logresp N0CALL-13 verified, server FAKE\r\n" if srv.verified
                         else b"# logresp N0CALL-13 unverified, server FAKE\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            srv.lines.append(line.decode().rstrip("\r\n"))
            if srv.drop_after is not None and len(srv.lines) >= srv.drop_after:
                srv.drop_after = None
                return                                        # the server hangs up


class OneConnection(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.outbox = os.path.join(self.dir.name, "outbox")
        self.server = FakeAprsIs()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.link = aprs_send.Link(CALL, aprs_send.passcode(CALL), "127.0.0.1", self.server.server_address[1])
        self.sender = aprs_send.Sender(CALL, True, self.outbox, os.path.join(self.dir.name, "state.json"), self.link, pace=0)

    def wait_lines(self, n):
        end = time.time() + 5
        while len(self.server.lines) < n and time.time() < end:
            time.sleep(0.02)
        return list(self.server.lines)

    def test_three_scripts_share_one_login_and_unchanged_objects_are_not_repeated(self):
        boxes = [aprs_send.Outbox(s, CALL, folder=self.outbox) for s in ("montco", "bucks", "nws")]
        for i, box in enumerate(boxes):
            box.send(obj(f"OBJ{i}"))
            box.flush()
        self.assertEqual(self.sender.step(1000), 3)
        for poll in (1020, 1040):                              # two more polls, nothing changed
            for i, box in enumerate(boxes):
                box.send(obj(f"OBJ{i}", stamp="241504z"))
                box.flush()
            self.assertEqual(self.sender.step(poll), 0)
        boxes[1].send(obj("OBJ1", live=False))                 # one is killed
        boxes[1].flush()
        self.assertEqual(self.sender.step(1060), 1)
        lines = self.wait_lines(4)
        self.assertEqual(len(self.server.logins), 1)
        self.assertIn(f"user {CALL} pass {aprs_send.passcode(CALL)} vers aprs-send", self.server.logins[0])
        self.assertNotIn("filter", self.server.logins[0])
        self.assertEqual(sorted(lines[:3]), [f"{CALL}>APRS,TCPIP*:{obj(f'OBJ{i}')}" for i in range(3)])
        self.assertEqual(lines[3], f"{CALL}>APRS,TCPIP*:{obj('OBJ1', live=False)}")
        self.assertEqual(os.listdir(self.outbox), [])

    def test_status_line_goes_with_the_first_hand_over_only(self):
        box = aprs_send.Outbox("pa511", CALL, status="Traffic events: example", folder=self.outbox)
        box.send(obj("WORK1"))
        box.flush()
        box.send(obj("WORK1"))
        box.flush()
        self.assertEqual(self.sender.step(1000), 2)
        self.assertEqual(sorted(self.wait_lines(2)),
                         sorted([f"{CALL}>APRS,TCPIP*:>Traffic events: example", f"{CALL}>APRS,TCPIP*:{obj('WORK1')}"]))

    def test_a_cancelled_poll_hands_over_nothing(self):
        box = aprs_send.Outbox("montco", CALL, folder=self.outbox)
        box.send(obj("FIRE1"))
        box.close()
        box.flush()
        self.assertFalse(os.path.isdir(self.outbox) and os.listdir(self.outbox))

    def test_packets_for_another_callsign_are_not_sent(self):
        aprs_send.hand_over("web", "N0CALL-7", [obj("OTHER")], folder=self.outbox)
        self.assertEqual(self.sender.step(1000), 0)
        self.assertEqual(os.listdir(self.outbox), [])

    def test_what_is_due_waits_for_the_connection_and_goes_out_after_reconnecting(self):
        aprs_send.hand_over("montco", CALL, [obj("FIRE1")], folder=self.outbox)
        self.server.drop_after = 1
        self.assertEqual(self.sender.step(), 1)
        self.wait_lines(1)
        time.sleep(0.3)                                        # the server has hung up by now
        aprs_send.hand_over("montco", CALL, [obj("FIRE1", comment="worse")], folder=self.outbox)
        self.assertEqual(self.sender.step(), 0)                # no connection: it waits
        self.assertFalse(self.link.up())
        self.link.retry_at = 0                                 # (normally some seconds later)
        self.assertEqual(self.sender.step(), 1)
        self.assertEqual(self.wait_lines(2)[1], f"{CALL}>APRS,TCPIP*:{obj('FIRE1', comment='worse')}")
        self.assertEqual(len(self.server.logins), 2)

    def test_what_was_sent_just_before_the_connection_died_is_sent_again(self):
        aprs_send.hand_over("montco", CALL, [obj("FIRE1"), obj("FIRE2")], folder=self.outbox)
        now = time.time()
        self.assertEqual(self.sender.step(now), 2)
        self.wait_lines(2)
        self.link.drop("test", now)                            # dies before the server is heard from again
        self.link.retry_at = 0
        self.assertEqual(self.sender.step(now + 1), 2)
        self.assertEqual(len(self.wait_lines(4)), 4)
        aprs_send.hand_over("montco", CALL, [obj("FIRE1"), obj("FIRE2")], folder=self.outbox)
        self.assertEqual(self.sender.step(now + 2), 0)         # and then it is quiet again
        self.link.drop("test", now + 2)                        # a connection that keeps dropping ...
        self.link.retry_at = 0
        self.assertEqual(self.sender.step(now + 3), 0)         # ... does not make it send them a third time
        self.assertEqual(len(self.server.lines), 4)

    def test_reconnecting_waits_longer_each_time_a_connection_dies_young(self):
        waits = []
        for i in range(4):
            now = 1000.0 + i
            self.link.retry_at = 0
            self.assertTrue(self.link.connect(now))
            self.link.drop("test", now)
            waits.append(self.link.retry_at - now)
        self.assertEqual(waits, [10, 20, 40, 80])
        self.link.retry_at = 0
        self.link.connect(2000.0)
        self.link.drop("test", 2100.0)                         # this one lasted: back to a short wait
        self.assertEqual(self.link.retry_at - 2100.0, 5)

    def test_unverified_login_sends_nothing(self):
        self.server.verified = False
        aprs_send.hand_over("montco", CALL, [obj("FIRE1")], folder=self.outbox)
        self.assertEqual(self.sender.step(1000), 0)
        time.sleep(0.1)
        self.assertEqual(self.server.lines, [])
        self.assertEqual(len(self.sender.book.due(1001)), 1)   # still waiting

    def test_objects_below_the_minimum_level_are_held_back(self):
        import aprs_priority
        aprs_priority.save_min_level("medium", folder=self.dir.name)
        fire, alarm, mine = obj("FIRE1", comment="BUILDING FIRE MAIN ST"), obj("FIRE2", comment="FIRE ALARM OAK ST"), obj("MYOBJ")
        aprs_send.hand_over("montco", CALL, [fire, alarm, mine, ">status text"], folder=self.outbox)
        self.assertEqual(self.sender.step(1000), 3)            # the alarm is rated low: not sent
        self.assertEqual(sorted(self.wait_lines(3)), sorted(f"{CALL}>APRS,TCPIP*:{p}" for p in (fire, mine, ">status text")))
        self.assertTrue(aprs_send.send_times(os.path.join(self.dir.name, "state.json"))["FIRE2"]["held"])
        aprs_send.hand_over("montco", CALL, [obj("FIRE2", comment="FIRE ALARM OAK ST", live=False)], folder=self.outbox)
        self.assertEqual(self.sender.step(1010), 0)            # its kill is not sent either: it was never on the air
        self.assertNotIn("FIRE2    ", self.sender.book.records)

    def test_raising_the_minimum_takes_objects_off_and_lowering_it_brings_them_back(self):
        import aprs_priority
        alarm = obj("FIRE2", comment="FIRE ALARM OAK ST")
        aprs_send.hand_over("montco", CALL, [alarm], folder=self.outbox)
        self.assertEqual(self.sender.step(1000), 1)            # minimum is low: everything is sent
        aprs_priority.save_min_level("high", folder=self.dir.name)
        aprs_send.hand_over("montco", CALL, [alarm], folder=self.outbox)
        self.assertEqual(self.sender.step(1010), 1)            # on the air and now below the minimum: killed
        self.assertEqual(self.wait_lines(2)[1], f"{CALL}>APRS,TCPIP*:;FIRE2    _241500z4007.20N/07520.40Wd")
        aprs_send.hand_over("montco", CALL, [alarm], folder=self.outbox)
        self.assertEqual(self.sender.step(1020), 0)            # and it stays off, with no second kill
        aprs_priority.save_min_level("low", folder=self.dir.name)
        aprs_send.hand_over("montco", CALL, [alarm], folder=self.outbox)
        self.assertEqual(self.sender.step(1030), 1)
        self.assertEqual(self.wait_lines(3)[2], f"{CALL}>APRS,TCPIP*:{alarm}")

    def test_print_only_mode_never_connects(self):
        sender = aprs_send.Sender(CALL, False, self.outbox, None, None)
        aprs_send.hand_over("montco", CALL, [obj("FIRE1")], folder=self.outbox)
        self.assertEqual(sender.step(1000), 1)
        self.assertEqual(self.server.logins, [])


if __name__ == "__main__":
    unittest.main()
